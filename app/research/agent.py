"""Research brain (lane L2): research(url, deadline=None, hints=None) -> Research (app/ledger.py shape, every key present).

identity -> site read -> plan over the 24 categories -> research loop -> claim check:
1. Site read: tools.fetch(url) (a deep URL stays the product), up to 14 same-site pages picked by keyword plus
   /.well-known/security.txt; pages with < 400 chars of text go to one tools.extract batch. Every text lands in
   research["sources"]; site.pages lists every page.
2. Intake (one model call, purpose "intake", in parallel with one call per category group, purpose "plan", for the
   hypotheses and round-1 actions of that group; merged in fixed group order; the buyer's product and vendor names from `hints` are anchors to
   verify, stored in identity.notes): identity, promise, claims, evidence from the vendor's own pages,
   hypotheses and round-1 actions for all 24 categories (ordered round-robin by category, so a cap trims depth first).
   Answer size is the time cost: prompts.CAPS (intake <= 20k chars, round <= 15k, per-field caps) is said in the
   prompts and enforced here: lists and summary texts are cut; an over-long quote drops its evidence, never cut.
3. Rounds (<= 4): the actions run on 8 threads, results kept in action order; every retrieved text lands in sources,
   failures in sources_failed, queries in matrix[cat].queries. One model call per category group (purpose "round",
   in parallel; GROUPS) turns that group's results into evidence, matrix updates of its own rows, claim checks (merged
   by claim_id; contradicted is never lowered) and next actions; a group with nothing new and no open row sits out, a
   failed group call keeps its rows as they are. Code assigns ids e1.., keeps only evidence that
   passes ledger.validate_evidence and whose url is in sources, links ids into matrix rows and identity.evidence.
4. Exit on done, no actions, all rows closed, or a cap (4 rounds, 120 tool calls counting the site read, a deadline
   closer than JUDGE_RESERVE seconds, default 300): stats.capped = True. Open rows close as found (evidence),
   searched_not_found (queries ran) or keep a "not reached" note; every claim gets a claim check (default not_found).
Prompts carry no clocks and keep a fixed order, so a rerun replays from llm.py's cache. research() never raises: a
failure lands in sources_failed and the partial Research is returned.
Tests patch `agent.tools` with fakes and answer the model through app.model.set_fake("intake" / "round").
"""
import concurrent.futures, datetime, importlib, json, os, sys, threading, time, traceback, urllib.parse

from app import ledger, model
from app.research import prompts

try:
    tools = importlib.import_module("app.research.tools")
except ModuleNotFoundError as _e:  # lane L1 writes app/research/tools.py; until it lands, tests patch agent.tools
    if _e.name != "app.research.tools":
        raise
    tools = None

MAX_ROUNDS = 4
MAX_TOOL_CALLS = 120
MAX_SITE_PAGES = 14
CAPS = prompts.CAPS          # answer-size caps, said in the prompts and enforced here after parsing
GROUPS = prompts.GROUPS      # A 1-4, B 5-8, C 9-13, D 14-15, E1 16-19, E2 20-24: one model call per group, parallel
GROUP_OF = prompts.GROUP_OF
MAX_NEXT_ACTIONS = CAPS["group_round_actions"] * len(GROUPS)
_CLAIM_RANK = {"not_found": 0, "confirmed": 1, "contradicted": 2}   # a later answer never lowers a claim's status
SHORT_PAGE = 400
WORKERS = 8
SITE_KEYWORDS = ("about", "company", "team", "imprint", "impressum", "legal", "customers", "case-stud", "pricing",
                 "security", "trust", "compliance", "privacy", "terms", "changelog", "release", "docs", "careers",
                 "jobs", "status", "blog", "news")
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".zip", ".mp4", ".woff",
             ".woff2", ".xml")
_STRIP = ("seconds", "tool", "args", "fn")  # run_tool bookkeeping: never in a prompt (it would break the cache)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _reserve():
    try:
        return float(os.environ.get("JUDGE_RESERVE", 300))
    except ValueError:
        return 300.0


def _defrag(url):
    return (url or "").split("#", 1)[0].strip()


def _str(v):
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def _cat(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    s = _str(v)
    return int(s) if s.isdigit() else v


def _words(v, n):
    """At most n words of a summary text (never used on quotes: a quote is verbatim or dropped)."""
    w = _str(v).split()
    return " ".join(w) if len(w) <= n else " ".join(w[:n]) + " ..."


def _wayback_date(ts):
    ts = _str(ts)
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return ts[:4] if ts[:4].isdigit() else ""


def pick_links(links, base, skip=()):
    """Up to MAX_SITE_PAGES same-site URLs from a page's links: per keyword (in SITE_KEYWORDS order) the matching link
    with the shortest path, so /blog wins over /blog/some-post."""
    skip = {_defrag(s).rstrip("/") for s in skip}
    cands, seen = [], set()
    for i, link in enumerate(links or []):
        u = _defrag((link or {}).get("url"))
        if not u.startswith(("http://", "https://")) or u.rstrip("/") in skip or u in seen:
            continue
        parts = urllib.parse.urlsplit(u)
        if ledger.registrable(u) != base or parts.path.lower().endswith(_SKIP_EXT):
            continue
        seen.add(u)
        hay = f"{parts.hostname or ''}{parts.path} {_str((link or {}).get('text'))}".lower()
        cands.append((len(parts.path) + len(parts.query), i, u, hay))
    chosen = []
    for kw in SITE_KEYWORDS:
        hits = sorted(c for c in cands if kw in c[3] and c[2] not in chosen)
        if hits:
            chosen.append(hits[0][2])
        if len(chosen) >= MAX_SITE_PAGES:
            break
    return chosen


def _security_txt(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme or 'https'}://{p.netloc}/.well-known/security.txt"


def _tool_url(tool, args, res):
    """An URL a human can open for a structured tool result ('' when there is none)."""
    q = urllib.parse.quote
    if tool in ("registry", "github"):
        u = _str(res.get("url"))
        if not u and tool == "github" and args.get("owner_repo"):
            u = f"https://github.com/{args['owner_repo']}"
        return u
    if tool == "crtsh":
        return f"https://crt.sh/?q={q(_str(res.get('domain') or args.get('domain')))}"
    if tool == "dns":
        return f"https://dns.google/resolve?name={q(_str(res.get('host') or args.get('host')))}"
    if tool == "wayback":
        return f"https://web.archive.org/web/*/{_str(res.get('url') or args.get('url'))}"
    u = _str(res.get("url"))
    return u if u.startswith(("http://", "https://")) else ""


def _tool_text(tool, res):
    d = {k: v for k, v in res.items() if k not in _STRIP and k != "error"}
    if tool == "wayback" and isinstance(d.get("snapshots"), list) and len(d["snapshots"]) > 24:
        s = d["snapshots"]
        d["snapshots"] = [s[round(i * (len(s) - 1) / 23)] for i in range(24)]
        d["snapshots_note"] = f"24 of {len(s)} snapshots, evenly spaced"
    return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


def _hints(h):
    """Batch-input identity hints {"id", "product", "vendor"} -> clean strings (anchors to verify, never facts)."""
    h = h if isinstance(h, dict) else {}
    return {k: _str(h.get(k))[:200] for k in ("id", "product", "vendor") if _str(h.get(k))}


class _Run:
    def __init__(self, url, deadline, hints=None):
        self.r = ledger.new_research(url)
        self.hints = _hints(hints)
        if self.hints.get("product") or self.hints.get("vendor"):
            self.r["identity"]["notes"] = "Buyer's identity hints (to verify): " + "; ".join(
                f'{k} "{v}"' for k, v in self.hints.items())
        self.r["started"] = _now_iso()
        self.deadline = deadline
        self.t0 = time.time()
        self.hyp = {str(k): [] for k in ledger.CATEGORIES}
        self.seen = set()            # executed actions (tool + args)
        self.checks = {}             # claim_id -> latest claim check
        self.quotes = set()          # (url, quote) of kept evidence
        self.lock = threading.Lock()
        self.table = {n: {"doc": (t or {}).get("doc", ""), "args": (t or {}).get("args", "")}
                      for n, t in (getattr(tools, "TOOLS", None) or {}).items()}

    # ---- plumbing
    def fail(self, what, error):
        self.r["sources_failed"].append({"what": _str(what)[:300], "error": _str(error)[:500]})

    def call(self, fn, *a):
        """One tool call (never raises; tools.* never raise either, this is the belt)."""
        try:
            res = fn(*a)
            return res if isinstance(res, dict) else {"error": f"tool returned {type(res).__name__}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def parallel(self, fn, items):
        if not items:
            return []
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            return list(ex.map(fn, items))   # map keeps the input order: prompts stay deterministic

    def ask(self, system, user, purpose):
        with self.lock:
            self.r["stats"]["llm_calls"] += 1
        try:
            ans = model.ask_json(system, user, purpose)
        except Exception as e:  # noqa: BLE001
            self.fail(purpose, f"model: {e}")
            return None
        if not isinstance(ans, dict):
            self.fail(purpose, f"model answer is {type(ans).__name__}")
            return None
        return ans

    def add_source(self, url, text, kind, title="", date=""):
        """-> the text that is new for this URL ('' when already known). Same URL again: new text is appended."""
        url, text = _defrag(url), _str(text)
        if not url or not text:
            return ""
        src = self.r["sources"].get(url)
        if src is None:
            self.r["sources"][url] = {"text": text, "kind": kind, "title": _str(title),
                                      "published_date": _str(date), "fetched_at": _now_iso()}
            return text
        if text in src["text"]:
            return ""
        src["text"] += "\n\n" + text
        src["title"] = src["title"] or _str(title)
        src["published_date"] = src["published_date"] or _str(date)
        return text

    def source_key(self, url):
        u = _defrag(url)
        for c in (u, u.rstrip("/"), u + "/", u.replace("http://", "https://", 1), u.replace("https://", "http://", 1)):
            if c and c in self.r["sources"]:
                return c
        return None

    def add_unique(self, lst, v):
        if v and v not in lst:
            lst.append(v)

    def closed(self):
        return all(row["status"] != "open" for row in self.r["matrix"].values())

    # ---- 1. site read
    def site_read(self):
        url = self.r["url"]
        home = self.call(tools.fetch, url)
        self.r["stats"]["tool_calls"] += 1
        final = _defrag(home.get("final_url")) or url
        base = ledger.registrable(final)
        targets = pick_links(home.get("links"), base, skip=(url, final)) + [_security_txt(final)]
        rest = self.parallel(lambda u: self.call(tools.fetch, u), targets)
        self.r["stats"]["tool_calls"] += len(targets)
        pages, short = [], []
        for u, res in [(url, home)] + list(zip(targets, rest)):
            key = _defrag(res.get("final_url")) or u
            status, text = res.get("status"), _str(res.get("text"))
            ok = not res.get("error") and (status is None or (isinstance(status, int) and status < 400))
            page = {"url": key, "status": status, "chars": len(text) if ok else 0, "kind": "fetch"}
            self.r["site"]["pages"].append(page)
            if res.get("error"):
                self.fail(u, res["error"])
            if ok:
                self.add_source(key, text, "fetch", res.get("title"))
            is_sec = u.endswith("/.well-known/security.txt")
            if len(text) < SHORT_PAGE and not is_sec and status not in (404, 410):
                short.append((u, page))
            pages.append((key, page))
        if short:
            ex = self.call(tools.extract, [u for u, _ in short])
            self.r["stats"]["tool_calls"] += 1
            if ex.get("error") and not ex.get("results"):
                self.fail("extract " + ", ".join(u for u, _ in short)[:250], ex["error"])
            byurl = {_defrag(x.get("url")): x for x in ex.get("results") or [] if isinstance(x, dict)}
            for u, page in short:
                x = byurl.get(_defrag(u)) or byurl.get(page["url"])
                if x and _str(x.get("text")):
                    self.add_source(page["url"], x["text"], "extract")
                    page["kind"], page["chars"] = "extract", len(self.r["sources"][page["url"]]["text"])
            for f in ex.get("failed") or []:
                if isinstance(f, dict):
                    self.fail(f.get("url"), f.get("error"))
        meta = home.get("meta") or {}
        years = [y for y in meta.get("copyright_years") or [] if _str(y).isdigit()]
        self.r["site"]["signals"] = {"copyright_year": str(max(int(y) for y in years)) if years else "",
                                     "generator": _str(meta.get("generator")), "notes": ""}
        for d in (ledger.registrable(url), base):
            self.add_unique(self.r["identity"]["domains"], d)
        seen, out = set(), []
        for key, page in pages:
            src = self.r["sources"].get(key)
            if src and key not in seen:
                seen.add(key)
                out.append({"url": key, "title": src["title"], "text": src["text"]})
        return out

    # ---- 2. intake
    def intake(self, pages):
        """One intake call (identity, site, vendor-site evidence) and one plan call per group, all in parallel from
        the same site text; merged in fixed group order -> round-1 actions."""
        calls = [("intake", None, prompts.intake_system(self.table),
                  prompts.intake_user(self.r["url"], pages, hints=self.hints))]
        calls += [("plan", g, prompts.plan_system(self.table),
                   prompts.intake_user(self.r["url"], pages, hints=self.hints, group=g)) for g in GROUPS]
        with concurrent.futures.ThreadPoolExecutor(len(calls)) as ex:
            answers = list(ex.map(lambda c: self.ask(c[2], c[3], c[0]), calls))
        ans = answers[0]
        if ans is None:
            self.hint_fallback()
        else:
            self.take_site(ans)
        acts = []
        if ans is not None and (ans.get("plan") or ans.get("actions")):    # one-call answer (older fixtures)
            acts += self.take_plan(ans, None)
        for (_, g, _s, _u), a in zip(calls[1:], answers[1:]):
            if a is not None:
                acts += self.take_plan(a, g)
        by = {}
        for a in acts:
            by.setdefault(a["category"], []).append(a)
        out, depth, keys = [], 0, set()
        while len(out) < len(acts) and depth <= max((len(v) for v in by.values()), default=0):
            for c in by:                     # round-robin by category (group order, then the model's priority):
                if depth < len(by[c]):       # the cap trims depth, never whole categories
                    a = by[c][depth]
                    k = json.dumps([a["tool"], a["args"]], sort_keys=True, ensure_ascii=False, default=str)
                    if k not in keys:
                        keys.add(k)
                        out.append(a)
            depth += 1
        return out[:CAPS["intake_actions"]]

    def take_site(self, ans):
        self.merge_identity(ans.get("identity"), overwrite=True)
        self.hint_fallback()
        site = ans.get("site") if isinstance(ans.get("site"), dict) else {}
        self.r["site"]["core_promise"] = _str(site.get("core_promise"))
        self.r["site"]["segment"] = _str(site.get("segment"))
        claims = [c for c in site.get("claims") or [] if isinstance(c, dict) and _str(c.get("text"))]
        for c in claims[:CAPS["claims"]]:
            u, q = _str(c.get("url")), _str(c.get("quote"))
            self.r["site"]["claims"].append({"id": f"c{len(self.r['site']['claims']) + 1}",
                                             "text": _words(c["text"], CAPS["claim_text_words"]),
                                             "kind": _str(c.get("kind")) or "other",
                                             "quote": q if len(q) <= CAPS["claim_quote_chars"] else "",
                                             "url": self.source_key(u) or u})
        incs = [c for c in site.get("inconsistencies") or [] if isinstance(c, dict) and _str(c.get("text"))]
        for c in incs[:CAPS["inconsistencies"]]:
            q = _str(c.get("quote"))
            self.r["site"]["inconsistencies"].append({"text": _words(c["text"], CAPS["evidence_text_words"]),
                                                      "quote": q if len(q) <= CAPS["claim_quote_chars"] else "",
                                                      "url": _str(c.get("url"))})
        sig = site.get("signals") if isinstance(site.get("signals"), dict) else {}
        for k in ("copyright_year", "generator", "notes"):
            if _str(sig.get(k)) and not self.r["site"]["signals"].get(k):
                self.r["site"]["signals"][k] = _words(sig.get(k), CAPS["note_words"])
        self.take_evidence(ans.get("evidence"), 0, CAPS["evidence"])

    def take_plan(self, ans, group):
        """A plan answer -> hypotheses and not_applicable rows (the group's categories only) and its actions."""
        cats = {str(c) for c in GROUPS[group]} if group else {str(c) for c in ledger.CATEGORIES}
        plan = ans.get("plan") if isinstance(ans.get("plan"), dict) else {}
        for k, p in plan.items():
            k = str(_cat(k))
            if k not in self.r["matrix"] or k not in cats or not isinstance(p, dict):
                continue
            self.hyp[k] = [_words(h, CAPS["hypothesis_words"]) for h in p.get("hypotheses") or []
                           if _str(h)][:CAPS["hypotheses"]]
            na = _words(p.get("not_applicable"), CAPS["note_words"])
            row = self.r["matrix"][k]
            if na:
                row["status"], row["note"] = "not_applicable", na
            elif self.hyp[k]:
                row["note"] = "hypotheses: " + "; ".join(self.hyp[k])
        acts = self.norm_actions(ans.get("actions"), limit=None)
        if group is None:
            return acts
        per, out = {}, []                   # at most 2 per category: one busy category never crowds out another
        for x in acts:
            per[x["category"]] = per.get(x["category"], 0) + 1
            if per[x["category"]] <= CAPS["plan_actions_per_category"]:
                out.append(x)
        return out

    def hint_fallback(self):
        """Intake gave no identity: the buyer's names stand in (still marked unverified in identity.notes)."""
        for k in ("product", "vendor"):
            if not self.r["identity"][k] and self.hints.get(k):
                self.r["identity"][k] = self.hints[k]

    def merge_identity(self, d, overwrite):
        if not isinstance(d, dict):
            return
        ident = self.r["identity"]
        for k in ("product", "vendor", "legal_entity", "parent", "country"):
            v = _str(d.get(k))
            if v and (overwrite or not ident[k] or k in ("parent", "legal_entity")):
                ident[k] = v
        if _str(d.get("notes")):
            ident["notes"] = (ident["notes"] + " " if ident["notes"] else "") + _words(d["notes"],
                                                                                    CAPS["identity_notes_words"])
        for k in ("former_names", "domains", "languages", "same_name_risks"):
            vals = d.get(k) if isinstance(d.get(k), list) else []
            for v in vals[:CAPS["same_name_risks"]] if k == "same_name_risks" else vals:
                self.add_unique(ident[k], _str(v))
        names = {p["name"] for p in ident["key_people"]}
        for p in (d.get("key_people") if isinstance(d.get("key_people"), list) else [])[:CAPS["key_people"]]:
            if isinstance(p, dict) and _str(p.get("name")) and _str(p["name"]) not in names:
                names.add(_str(p["name"]))
                ident["key_people"].append({"name": _str(p["name"]), "role": _str(p.get("role"))})

    def norm_actions(self, raw, limit):
        out, keys = [], set()
        for a in raw or []:
            if not isinstance(a, dict):
                continue
            tool, args, cat = _str(a.get("tool")), a.get("args"), _cat(a.get("category"))
            if tool not in self.table or not isinstance(args, dict) or cat not in ledger.CATEGORIES:
                self.fail(f"action {tool or '?'} cat {cat}", "dropped: unknown tool, args not an object or bad category")
                continue
            key = json.dumps([tool, args], sort_keys=True, ensure_ascii=False, default=str)
            if key in self.seen or key in keys:
                continue
            keys.add(key)
            out.append({"tool": tool, "args": args, "category": cat,
                        "hypothesis": _words(a.get("hypothesis"), CAPS["action_hypothesis_words"])})
            if limit and len(out) >= limit:
                break
        return out

    # ---- 3. rounds
    def run_action(self, a):
        return self.call(tools.run_tool, a["tool"], a["args"])

    def ingest(self, actions, results):
        """Results into sources and matrix rows -> (action summaries, research hints, per action its retrieved
        [(url, text)])."""
        summary, hints, gots = [], [], []
        for i, (a, res) in enumerate(zip(actions, results), 1):
            tool, args, row = a["tool"], a["args"], self.r["matrix"][str(a["category"])]
            self.seen.add(json.dumps([tool, args], sort_keys=True, ensure_ascii=False, default=str))
            q = _str(args.get("query"))
            self.add_unique(row["queries"], (q if tool == "search" else f"news: {q}") if tool in ("search", "news") and q
                            else f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}")
            err, got = res.get("error"), []
            if tool in ("search", "news"):
                got = [(x.get("url"), x.get("title"), x.get("published_date"), x.get("content"), "search")
                       for x in res.get("results") or [] if isinstance(x, dict)]
            elif tool == "extract":
                got = [(x.get("url"), "", x.get("published_date"), x.get("text"), "extract") for x in res.get("results") or []
                       if isinstance(x, dict)]
                for f in res.get("failed") or []:
                    if isinstance(f, dict):
                        self.fail(f.get("url"), f.get("error"))
            elif tool == "fetch":
                st = res.get("status")
                if not err and (st is None or (isinstance(st, int) and st < 400)):
                    got = [(res.get("final_url") or res.get("url") or args.get("url"), res.get("title"), "",
                            res.get("text"), "fetch")]
            elif tool == "wayback_get":
                ts = _str(res.get("timestamp"))
                got = [(res.get("url"), f"Archived snapshot {ts} of {_str(args.get('url'))}", _wayback_date(ts),
                        res.get("text"), "wayback")]
            elif tool == "research":
                if _str(res.get("report")):
                    hints.append({"n": i, "category": a["category"], "text": _str(res["report"]) + "\nSources named: " + ", ".join(
                        _str(s.get("url")) for s in res.get("sources") or [] if isinstance(s, dict))})
            elif not err:
                u = _tool_url(tool, args, res)
                got = [(u, f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}", "",
                        _tool_text(tool, res), "tool")]
            got = [g for g in got if _defrag(g[0]) and _str(g[3])]
            if err:
                self.fail(f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}", err)
            mine = []
            for u, title, date, text, kind in got:
                u, text = _defrag(u), _str(text)
                self.add_source(u, text, kind, title, date)
                self.add_unique(row["sources"], u)
                mine.append((u, text))
            gots.append(mine)
            summary.append({"n": i, "tool": tool, "args": args, "category": a["category"],
                            "hypothesis": a["hypothesis"], "results": len(got), "error": err})
        return summary, hints, gots

    def group_sources(self, cats, actions, gots, before):
        """The texts this round retrieved for the group's actions that were not known before the round."""
        items = {}
        for i, (a, got) in enumerate(zip(actions, gots), 1):
            if a["category"] not in cats:
                continue
            for u, text in got:
                if text in before.get(u, ""):
                    continue
                src = self.r["sources"][u]
                it = items.setdefault(u, {"url": u, "title": src["title"], "date": src["published_date"],
                                          "kind": src["kind"], "actions": [], "text": ""})
                if text not in it["text"]:
                    it["text"] += ("\n" if it["text"] else "") + text
                if i not in it["actions"]:
                    it["actions"].append(i)
        return [it for it in items.values() if it["text"]]

    def digest(self, cats, n=60):
        """At most n evidence lines (id, category, text): the group's own first, then the latest others; id order."""
        own = [e for e in self.r["evidence"] if e["category"] in cats]
        other = [e for e in self.r["evidence"] if e["category"] not in cats]
        keep = own[-n:]
        keep += other[-(n - len(keep)):] if n > len(keep) else []
        keep.sort(key=lambda e: int(e["id"][1:]))
        return [{"id": e["id"], "category": e["category"], "text": e["text"]} for e in keep]

    def round(self, n, actions):
        """Run the actions, then one round call per group in parallel; answers merge in group order (ids too)."""
        t = time.time()
        results = self.parallel(self.run_action, actions)
        self.r["stats"]["tool_calls"] += len(actions)
        before = {u: s["text"] for u, s in self.r["sources"].items()}
        summary, hints, gots = self.ingest(actions, results)
        system, calls = prompts.round_system(self.table), []
        for g, cats in GROUPS.items():
            srcs = self.group_sources(cats, actions, gots, before)
            if not srcs and all(self.r["matrix"][str(c)]["status"] != "open" for c in cats):
                continue                       # nothing new and nothing open: the group sits this round out
            view = {"url": self.r["url"], "group": g, "identity": self.r["identity"],
                    "core_promise": self.r["site"]["core_promise"], "segment": self.r["site"]["segment"],
                    "claims": self.r["site"]["claims"], "matrix": self.r["matrix"], "hypotheses": self.hyp,
                    "evidence": self.digest(cats), "round": n, "rounds_left": MAX_ROUNDS - n,
                    "tool_calls_left": max(0, MAX_TOOL_CALLS - self.r["stats"]["tool_calls"]),
                    "actions": [x for x in summary if x["category"] in cats],
                    "hints": [h for h in hints if h["category"] in cats], "sources": srcs}
            calls.append((g, prompts.round_user(view)))
        with concurrent.futures.ThreadPoolExecutor(max(1, len(calls))) as ex:
            answers = list(ex.map(lambda c: self.ask(system, c[1], "round"), calls))
        new, per_group, done = 0, [], bool(calls)
        for (g, _u), ans in zip(calls, answers):
            if ans is None:                    # logged by ask(); the group's rows keep their state
                done = False
                continue
            new += self.take_evidence(ans.get("evidence"), n, CAPS["group_evidence"])
            self.matrix_updates(ans.get("matrix_updates"), GROUPS[g])
            self.claim_checks(ans.get("claim_checks"))
            self.merge_identity(ans.get("identity_updates"), overwrite=False)
            per_group.append(self.norm_actions(ans.get("next_actions"), limit=CAPS["group_round_actions"]))
            done = done and ans.get("done") is True
        nxt, keys = [], set()
        for depth in range(CAPS["group_round_actions"]):   # interleave groups: a tool-call cap trims all evenly
            for acts in per_group:
                if depth < len(acts):
                    k = json.dumps([acts[depth]["tool"], acts[depth]["args"]], sort_keys=True, ensure_ascii=False,
                                   default=str)
                    if k not in keys:
                        keys.add(k)
                        nxt.append(acts[depth])
        self.r["rounds"].append({"n": n, "actions": actions, "new_evidence": new,
                                 "seconds": round(time.time() - t, 1)})
        return nxt, done

    def take_evidence(self, raw, rnd, cap):
        kept = 0
        for x in raw or []:
            if not isinstance(x, dict):
                self.fail("evidence", "dropped: not an object")
                continue
            e = {"id": "", "category": _cat(x.get("category")),
                 "text": _words(x.get("text"), CAPS["evidence_text_words"]),
                 "url": _str(x.get("url")), "quote": _str(x.get("quote")), "date": _str(x.get("date")),
                 "author_type": _str(x.get("author_type")), "severity": _str(x.get("severity")).lower(),
                 "polarity": _str(x.get("polarity")).lower(), "promise_link": _str(x.get("promise_link")).lower(),
                 "hypothesis": _words(x.get("hypothesis"), CAPS["evidence_hypothesis_words"]),
                 "source_title": _str(x.get("source_title"))[:200], "round": rnd}
            if e["promise_link"] not in ledger.PROMISE_LINKS:
                e["promise_link"] = "none"
            problems = ledger.validate_evidence({**e, "id": "new"})
            key = self.source_key(e["url"])
            if key is None:
                problems.append("url not in sources")
            if len(e["quote"]) > CAPS["evidence_quote_chars"]:      # never cut a quote: `resolves` stays honest
                problems.append(f"quote over {CAPS['evidence_quote_chars']} chars")
            if kept >= cap and not problems:
                problems.append(f"over the cap of {cap} evidence per answer")
            if problems:
                self.fail(e["url"] or "evidence", "dropped: " + "; ".join(problems))
                continue
            e["url"] = key
            if (key, e["quote"]) in self.quotes:
                continue
            self.quotes.add((key, e["quote"]))
            e["id"] = f"e{len(self.r['evidence']) + 1}"
            e["source_title"] = self.r["sources"][key]["title"] or e["source_title"]
            self.r["evidence"].append(e)
            row = self.r["matrix"][str(e["category"])]
            row["evidence"].append(e["id"])
            self.add_unique(row["sources"], key)
            if e["category"] == 1:
                self.r["identity"]["evidence"].append(e["id"])
            kept += 1
        return kept

    def matrix_updates(self, upd, cats):
        """Status and note updates for the group's own rows only (another group's rows are not this call's)."""
        own = {str(c) for c in cats}
        for k, u in (upd if isinstance(upd, dict) else {}).items():
            k = str(_cat(k))
            if k not in own or not isinstance(u, dict):
                continue
            row = self.r["matrix"][k]
            if _str(u.get("status")) in ledger.STATUSES:
                row["status"] = _str(u["status"])
            if _str(u.get("note")):
                row["note"] = _words(u["note"], CAPS["note_words"])

    def claim_checks(self, raw):
        ids = {c["id"] for c in self.r["site"]["claims"]}
        known = {e["id"] for e in self.r["evidence"]}
        for c in raw or []:
            if not isinstance(c, dict) or _str(c.get("claim_id")) not in ids:
                continue
            status = _str(c.get("status"))
            if status not in ledger.CLAIM_STATUSES:
                continue
            refs = []
            for ref in c.get("evidence") or []:
                ref = _str(ref)
                if ref in known:
                    self.add_unique(refs, ref)
                else:
                    key = self.source_key(ref)
                    for e in self.r["evidence"]:
                        if key and e["url"] == key:
                            self.add_unique(refs, e["id"])
            cid, old = _str(c["claim_id"]), self.checks.get(_str(c["claim_id"]))
            note = _words(c.get("note"), CAPS["note_words"])
            if old and _CLAIM_RANK[status] < _CLAIM_RANK[old["status"]]:
                continue                       # never contradicted -> confirmed, never a found status -> not_found
            if old and status == old["status"]:
                refs = old["evidence"] + [x for x in refs if x not in old["evidence"]]
                note = note or old["note"]
            self.checks[cid] = {"claim_id": cid, "status": status, "evidence": refs, "note": note}

    # ---- 4. exit
    def finish(self):
        capped = self.r["stats"]["capped"]
        for row in self.r["matrix"].values():
            if row["status"] != "open":
                continue
            if row["evidence"]:
                row["status"] = "found"
            elif row["queries"]:
                row["status"] = "searched_not_found"
                row["note"] = row["note"] or "searched; nothing relevant found"
            else:
                tag = "not reached (cap)" if capped else "not reached (no query was run)"
                row["note"] = f"{row['note']}; {tag}" if row["note"] else tag
        self.r["claim_checks"] = [self.checks.get(c["id"]) or {"claim_id": c["id"], "status": "not_found",
                                                               "evidence": [], "note": "no outside check found"}
                                  for c in self.r["site"]["claims"]]
        self.r["seconds"] = round(time.time() - self.t0, 1)
        return self.r

    def run(self):
        try:
            if tools is None:
                raise RuntimeError("app.research.tools is missing (lane L1)")
            actions = self.intake(self.site_read())
            reserve = _reserve()
            for n in range(1, MAX_ROUNDS + 1):
                if not actions or self.closed():
                    break
                if self.deadline is not None and self.deadline - time.time() < reserve:
                    self.r["stats"]["capped"] = True
                    break
                budget = MAX_TOOL_CALLS - self.r["stats"]["tool_calls"]
                if budget <= 0:
                    self.r["stats"]["capped"] = True
                    break
                if len(actions) > budget:
                    actions, self.r["stats"]["capped"] = actions[:budget], True
                actions, done = self.round(n, actions)
                if done:
                    break
            else:
                if actions and not self.closed():
                    self.r["stats"]["capped"] = True
        except Exception as e:  # noqa: BLE001  a partial Research beats a lost product in a batch
            traceback.print_exc(file=sys.stderr)
            self.fail("agent", f"{type(e).__name__}: {e}")
        return self.finish()


def research(url, deadline=None, hints=None):
    """-> Research for the product at `url` (app/ledger.py shape). `deadline`: epoch seconds; no new round starts
    when fewer than JUDGE_RESERVE seconds remain (the judge needs them), and stats.capped is set. `hints`: the batch
    input's {"id", "product", "vendor"}: identity anchors for the intake prompt and identity.notes, never facts (the
    site read may correct them; a mismatch is a same-name risk)."""
    return _Run(url, deadline, hints).run()
