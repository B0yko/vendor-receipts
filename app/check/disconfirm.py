"""Disconfirmation (lane L6, plan step 7): one pass that tries to break the tentative verdict, run before the judge.

    from app.check.disconfirm import disconfirm
    research, receipts, record = disconfirm(research, receipts, deadline=None)

Targets: the kept negative receipts of severity critical or moderate (reposts excluded; at most 5; critical first,
then direct promise link, then research order). With none, four Green probes for the product and vendor: a hidden
incident, an ownership change, a frozen product, a lawsuit.
Steps:
  1. One model call (purpose "disconfirm_plan") proposes 2 queries per target, tool "search" or "news". A target
     the model leaves without queries gets code templates; extra or unknown-target queries are dropped.
  2. The queries run in parallel (ThreadPoolExecutor(6)) through app.research.tools.run_tool("search", ...) (news =
     topic "news"; imported lazily). Every result lands in research["sources"] (kind "search"); a snippet that is
     not already inside a known page's text is appended to that text. A failed query goes to sources_failed.
  3. One model call (purpose "disconfirm") over the targets and the new results returns new Evidence (verbatim
     quotes, hypothesis "disconfirmation of e<n>: ...") and one outcome per target.
  4. New evidence gets ids continuing the research's numbering (e<max+1>, ...), runs through receipts.check
     (resolves, same company, says it), gets dup_of against the earlier kept receipts, and is appended to
     research["evidence"] and to the receipts.
Outcomes: for a negative target, survived = the finding stands; weakened = it holds but is resolved, partly fixed,
old, disputed or smaller than stated; refuted = overturned, retracted, fixed for good, or about another company. For
a Green probe the hypothesis is "no such problem": survived = nothing found, weakened = an unconfirmed or minor sign,
refuted = a receipted problem of that kind. Code enforces receipts: "weakened" or "refuted" without a kept new
receipt is recorded as "survived" (the model's word stays in model_outcome); a target without an answer is
"survived" with why "no verdict".
Record (also research["disconfirmation"]):
  {"mode": "negatives" | "green_probes", "targets": [{"target", "kind", "category", "severity", "text", "question"}],
   "queries": [{"target", "tool", "query", "results", "error"}],
   "outcomes": [{"target", "outcome", "why", "evidence": [new ids], "kept": [kept new ids], "model_outcome"}],
   "new_evidence": n, "kept": n, "seconds"}
Skip: {"skipped": "<why>", "seconds"} when the deadline leaves under 240 s (deadline is a time.time() epoch; a value
under 1e9 is read as time.monotonic()), when under 120 s are left after the searches, when no search returned
anything, or on any error. Then the research comes back with only "disconfirmation" set and the receipts unchanged.
Never raises. The input research and receipts are never mutated (the research returned is a copy).
"""
import concurrent.futures
import datetime
import json
import re
import time

from app import ledger, model
from app.check import receipts as rc

MIN_LEFT = 240
LEFT_AFTER_SEARCH = 120
SEARCH_WAIT = 150
MAX_TARGETS = 5
QUERIES_PER_TARGET = 2
WORKERS = 6
MAX_RESULTS = 6
SNIPPET = 1500
MAX_NEW_PER_TARGET = 4
OUTCOMES = ("survived", "weakened", "refuted")
_SEV = {"critical": 0, "moderate": 1}
_LINK = {"direct": 0, "adjacent": 1}
_EID = re.compile(r"^e(\d+)$")
_HYP = re.compile(r"^\s*disconfirmation of [^:]*:\s*", re.I)

# id, category, tool, what the probe looks for, two fallback query templates ({p} product, {v} vendor)
PROBES = (
    ("probe:incident", 11, "news", "a hidden security incident, data breach or major outage",
     ('"{p}" data breach OR security incident', '"{v}" outage OR breach OR vulnerability')),
    ("probe:ownership", 2, "news", "an ownership change: acquisition, merger, sale, insolvency, or a new owner's "
     "layoffs or price rises", ('"{v}" acquired OR acquisition OR merger', '"{v}" layoffs OR insolvency OR new owner')),
    ("probe:frozen", 4, "search", "a frozen or ending product: end of life, discontinued, maintenance mode, no releases",
     ('"{p}" discontinued OR "end of life" OR sunset', '"{p}" "no longer maintained" OR "last release"')),
    ("probe:lawsuit", 10, "news", "a lawsuit, class action, regulator fine or settlement",
     ('"{v}" lawsuit OR "class action"', '"{v}" fined OR regulator OR settlement')),
)
NEGATIVE_QUESTION = "Was it overturned or appealed, resolved or fixed, is it outdated, or about another company?"
NEGATIVE_TEMPLATES = ('"{v}" {topic} overturned OR appeal OR resolved', '"{p}" {topic} fixed OR update OR statement')

PLAN_SYSTEM = """You plan a disconfirmation pass for a vendor due-diligence report. The research reached the targets
below; your job is to try to break each one with web searches, not to confirm it.
- A finding (kind "negative"): was the ruling overturned or appealed, the issue resolved or fixed, the report
  outdated or superseded, or is it about another company with the same name?
- A Green probe (kind "green_probe"): search for the named problem that a polished vendor site would not show.
Write exactly 2 queries per target. Each query names the product or the vendor (or the legal entity, parent or a
former name) so results are about this company; use the vendor's country language for one query when it helps.
tool "news" for events (rulings, incidents, deals, lawsuits, layoffs), "search" otherwise. No site: operators.
Answer with one JSON object and nothing else:
{"queries": [{"target": "<target id>", "tool": "search", "query": "<at most 12 words>"}]}"""

CHECK_SYSTEM = """You finish a disconfirmation pass for a vendor due-diligence report. You get the targets (findings
of the research, or Green probes) and the search results retrieved to test them. Use only the given results, nothing
from memory.
1. evidence: for each result that bears on a target and is about this product or vendor (not a same-name company),
   one item whose quote is copied character for character from that result's content (one or two sentences, at
   least 40 characters, no paraphrase; mark a skipped middle with "...") and whose url is the result's url exactly:
   {"id": "n1", "target": "<target id>", "category": <1-24>, "text": "<the finding in one plain sentence>",
    "url": "...", "quote": "...", "date": "<YYYY-MM-DD | YYYY-MM | YYYY | empty: the event date in the text, never
    today>", "author_type": "<AUTHOR_TYPES>", "severity": "critical|moderate|minor",
    "polarity": "negative|positive|neutral" (the buyer's view: a fix or an overturned ruling is positive, an incident
    is negative), "promise_link": "direct|adjacent|none", "hypothesis": "disconfirmation of <target id>: <what was
    tested>"}
2. outcomes: exactly one per target:
   {"target": "<target id>", "outcome": "survived|weakened|refuted", "why": "<at most 30 words>", "evidence": ["n1"]}
   A negative finding: survived = it stands (nothing in the results undoes it); weakened = it holds but is resolved,
   partly fixed, old, disputed or smaller than stated; refuted = overturned, retracted, fixed for good, or about
   another company.
   A Green probe (the hypothesis is "no such problem"): survived = nothing of that kind found; weakened = an
   unconfirmed or minor sign; refuted = a receipted problem of that kind.
   weakened and refuted cite the evidence ids that show it; survived may cite none.
Answer with one JSON object and nothing else: {"evidence": [...], "outcomes": [...]}""".replace(
    "<AUTHOR_TYPES>", "|".join(ledger.AUTHOR_TYPES))


def _run_tool(name, args):
    from app.research import tools  # lane L1, imported lazily so this module loads without it
    return tools.run_tool(name, args)


def _left(deadline):
    if deadline is None:
        return float("inf")
    try:
        d = float(deadline)
    except (TypeError, ValueError):
        return float("inf")
    return d - (time.monotonic() if d < 1e9 else time.time())


def _s(x, n=None):
    s = x if isinstance(x, str) else ("" if x is None else str(x))
    return s[:n] if n else s


def _ident(research):
    i = research.get("identity") if isinstance(research.get("identity"), dict) else {}
    keys = ("product", "vendor", "legal_entity", "parent", "former_names", "domains", "country", "languages",
            "same_name_risks")
    return {k: i.get(k) for k in keys if i.get(k)}


def _names(research):
    i = _ident(research)
    host = rc.host_of(_s(research.get("url"))).removeprefix("www.")
    p = _s(i.get("product")) or _s(i.get("vendor")) or host
    v = _s(i.get("vendor")) or _s(i.get("legal_entity")) or p
    return p, v


def targets_of(research, receipts):
    """-> (mode, targets): the kept negative critical/moderate receipts, else the four Green probes."""
    cands = []
    for n, r in enumerate(x for x in receipts or [] if isinstance(x, dict)):
        if (r.get("kept") and not r.get("dup_of") and r.get("polarity") == "negative"
                and r.get("severity") in _SEV and r.get("id")):
            cands.append((_SEV[r["severity"]], _LINK.get(r.get("promise_link"), 2), n, r))
    cands.sort(key=lambda c: c[:3])
    if cands:
        return "negatives", [
            {"target": _s(r["id"]), "kind": "negative", "category": r.get("category"), "severity": r["severity"],
             "text": _s(r.get("text"), 400), "quote": _s(r.get("quote"), 600), "url": _s(r.get("url")),
             "date": _s(r.get("date")), "author_type": _s(r.get("author_type")), "question": NEGATIVE_QUESTION}
            for *_, r in cands[:MAX_TARGETS]]
    p, v = _names(research)
    return "green_probes", [{"target": pid, "kind": "green_probe", "category": cat, "severity": "", "tool": tool,
                             "text": f"No {what} for {p} ({v}).", "question": f"Is there {what}?"}
                            for pid, cat, tool, what, _ in PROBES]


def _fallback(t, research):
    p, v = _names(research)
    if t["kind"] == "green_probe":
        tmpl, tool = next(x[4] for x in PROBES if x[0] == t["target"]), t.get("tool", "search")
        return [(tool, q.format(p=p, v=v)) for q in tmpl]
    cat = t.get("category")
    topic = ledger.CATEGORIES[cat][0].split(",")[0].lower() if cat in ledger.CATEGORIES else ""
    return [("news" if cat in (2, 9, 10, 11, 21) else "search", " ".join(q.format(p=p, v=v, topic=topic).split()))
            for q in NEGATIVE_TEMPLATES]


def plan(research, targets):
    """-> [{"target", "tool", "query"}]: 2 per target from the model, code templates where it gave none."""
    user = "\n".join([f"PRODUCT URL: {_s(research.get('url'))}", f"TODAY: {datetime.date.today().isoformat()}",
                      "IDENTITY: " + json.dumps(_ident(research), ensure_ascii=False), "TARGETS:"]
                     + [json.dumps({k: v for k, v in t.items() if k != "tool"}, ensure_ascii=False) for t in targets])
    ans = model.ask_json(PLAN_SYSTEM, user, purpose="disconfirm_plan")
    raw = ans.get("queries") if isinstance(ans, dict) else None
    by = {t["target"]: [] for t in targets}
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        tgt, text = _s(q.get("target")), " ".join(_s(q.get("query")).split())[:200]
        tool = "news" if _s(q.get("tool")).lower() == "news" else "search"
        if tgt in by and text and len(by[tgt]) < QUERIES_PER_TARGET and text.casefold() not in (
                x[1].casefold() for x in by[tgt]):
            by[tgt].append((tool, text))
    out, seen = [], set()
    for t in targets:
        for tool, text in by[t["target"]] or _fallback(t, research):
            if (tool, text.casefold()) not in seen:
                seen.add((tool, text.casefold()))
                out.append({"target": t["target"], "tool": tool, "query": text})
    return out


def _search(q):
    args = {"query": q["query"], "max_results": MAX_RESULTS}
    if q["tool"] == "news":
        args["topic"] = "news"
    try:
        res = _run_tool("search", args)
    except Exception as e:  # noqa: BLE001 - tools never raise by contract; guard anyway
        return {"results": [], "error": f"{type(e).__name__}: {str(e)[:160]}"}
    return res if isinstance(res, dict) else {"results": [], "error": "tool returned no dict"}


def run_queries(queries, deadline):
    """-> [(query, result dict)] in query order; a query still running at the wait cap gets error 'timeout'."""
    wait = min(SEARCH_WAIT, max(1.0, _left(deadline) - LEFT_AFTER_SEARCH))
    pool = concurrent.futures.ThreadPoolExecutor(WORKERS)
    try:
        futs = [pool.submit(_search, q) for q in queries]
        concurrent.futures.wait(futs, timeout=wait)
        return [(q, f.result() if f.done() else {"results": [], "error": f"timeout after {wait:.0f} s"})
                for q, f in zip(queries, futs)]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _merge_sources(r, ran):
    """Adds every search result to r["sources"] (r's own dicts); -> results for the prompt, deduped by url."""
    sources = r["sources"]
    keys = {rc.url_key(u): u for u in sources}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    shown, results = {}, []
    for q, res in ran:
        q["results"], q["error"] = 0, res.get("error") or None
        items = res.get("results") if isinstance(res.get("results"), list) else []
        if q["error"] and not items:
            r["sources_failed"].append({"what": q["query"], "error": _s(q["error"], 300)})
        for it in items:
            if not isinstance(it, dict) or not _s(it.get("url")).strip():
                continue
            url, content = _s(it.get("url")).strip(), _s(it.get("content"))
            k = rc.url_key(url)
            if not k or not content.strip():
                continue
            q["results"] += 1
            have = keys.get(k)
            if have is None:
                sources[url] = {"text": content, "kind": "search", "title": _s(it.get("title")),
                                "published_date": _s(it.get("published_date")), "fetched_at": now}
                keys[k] = url
            else:
                s = dict(sources[have]) if isinstance(sources[have], dict) else {}
                if rc.normalise(content) not in rc.normalise(s.get("text")):
                    s["text"] = (_s(s.get("text")) + "\n\n" + content).strip()
                s.setdefault("kind", "search")
                if not s.get("published_date") and it.get("published_date"):
                    s["published_date"] = _s(it.get("published_date"))
                sources[have] = s
            if k in shown:
                if q["target"] not in shown[k]["targets"]:
                    shown[k]["targets"].append(q["target"])
                continue
            shown[k] = {"targets": [q["target"]], "query": q["query"], "url": url, "title": _s(it.get("title"), 200),
                        "published_date": _s(it.get("published_date")), "content": content[:SNIPPET]}
            results.append(shown[k])
    return results


def _next_ids(research, receipts):
    n, rnd = 0, len(research.get("rounds") or [])
    for e in list(research.get("evidence") or []) + list(receipts or []):
        if isinstance(e, dict):
            m = _EID.match(_s(e.get("id")))
            n = max(n, int(m.group(1))) if m else n
            rnd = max(rnd, e["round"]) if isinstance(e.get("round"), int) else rnd
    return n + 1, rnd + 1


def _pick(v, allowed, default):
    v = _s(v).strip().lower()
    return v if v in allowed else default


def _new_evidence(ans, tmap, research, receipts, titles):
    """-> (evidence with final ids, {model id: final id}, raw outcomes)."""
    raw = ans.get("evidence") if isinstance(ans.get("evidence"), list) else []
    outs = ans.get("outcomes") if isinstance(ans.get("outcomes"), list) else []
    cited = {}
    for o in outs:
        if isinstance(o, dict) and isinstance(o.get("evidence"), list):
            for i in o["evidence"]:
                cited.setdefault(_s(i), _s(o.get("target")))
    n, rnd = _next_ids(research, receipts)
    out, idmap, per = [], {}, {}
    for x in raw:
        if not isinstance(x, dict) or not _s(x.get("url")).strip() or not _s(x.get("quote")).strip():
            continue
        mid = _s(x.get("id"))
        tgt = _s(x.get("target")) or cited.get(mid, "")
        tgt = tgt if tgt in tmap else cited.get(mid, "") if cited.get(mid, "") in tmap else ""
        if per.get(tgt, 0) >= MAX_NEW_PER_TARGET:
            continue
        per[tgt] = per.get(tgt, 0) + 1
        t = tmap.get(tgt, {})
        try:
            cat = int(x.get("category"))
        except (TypeError, ValueError):
            cat = None
        cat = cat if cat in ledger.CATEGORIES else (t.get("category") if t.get("category") in ledger.CATEGORIES else 8)
        hyp = _HYP.sub("", _s(x.get("hypothesis"))).strip() or t.get("question", "")
        nid = f"e{n}"
        n += 1
        if mid and mid not in idmap:
            idmap[mid] = nid
        url = _s(x.get("url")).strip()
        out.append({"id": nid, "category": cat, "text": _s(x.get("text"), 600).strip() or _s(x.get("quote"), 300),
                    "url": url, "quote": _s(x.get("quote")), "date": _s(x.get("date")).strip(),
                    "author_type": _pick(x.get("author_type"), ledger.AUTHOR_TYPES, "aggregator"),
                    "severity": _pick(x.get("severity"), ledger.SEVERITIES, "minor"),
                    "polarity": _pick(x.get("polarity"), ledger.POLARITIES, "neutral"),
                    "promise_link": _pick(x.get("promise_link"), ledger.PROMISE_LINKS, "none"),
                    "hypothesis": f"disconfirmation of {tgt or 'the verdict'}: {hyp}".rstrip(": "),
                    "source_title": titles.get(rc.url_key(url)) or _s(x.get("source_title"), 200), "round": rnd,
                    "_target": tgt})
    return out, idmap, outs


def _outcomes(targets, outs, idmap, new_receipts):
    kept = {r["id"] for r in new_receipts if r.get("kept")}
    by_target = {}
    for r in new_receipts:
        by_target.setdefault(r.get("_target", ""), []).append(r["id"])
    given = {}
    for o in outs:
        if isinstance(o, dict) and _s(o.get("target")) and _s(o.get("target")) not in given:
            given[_s(o.get("target"))] = o
    res = []
    for t in targets:
        o = given.get(t["target"])
        if o is None:
            res.append({"target": t["target"], "outcome": "survived", "why": "no verdict", "evidence": [],
                        "kept": [], "model_outcome": None})
            continue
        word = _s(o.get("outcome")).strip().lower()
        ev = [idmap[_s(i)] for i in (o.get("evidence") if isinstance(o.get("evidence"), list) else [])
              if _s(i) in idmap]
        ev += [i for i in by_target.get(t["target"], []) if i not in ev]
        k = [i for i in ev if i in kept]
        why = _s(o.get("why"), 400).strip()
        outcome = word if word in OUTCOMES else "survived"
        if outcome != "survived" and not k:
            why = (why + " " if why else "") + f"(model said {word}; no kept receipt shows it, recorded as survived)"
            outcome = "survived"
        elif word not in OUTCOMES:
            why = (why + " " if why else "") + f"(unknown outcome {word!r}, recorded as survived)"
        res.append({"target": t["target"], "outcome": outcome, "why": why, "evidence": ev, "kept": k,
                    "model_outcome": word or None})
    return res


def _copy(research):
    r = dict(research) if isinstance(research, dict) else {}
    r["sources"] = dict(r["sources"]) if isinstance(r.get("sources"), dict) else {}
    r["evidence"] = list(r["evidence"]) if isinstance(r.get("evidence"), list) else []
    r["sources_failed"] = list(r["sources_failed"]) if isinstance(r.get("sources_failed"), list) else []
    r["stats"] = dict(r["stats"]) if isinstance(r.get("stats"), dict) else {}
    return r


def _stat(r, k, n):
    r["stats"][k] = (r["stats"].get(k) if isinstance(r["stats"].get(k), int) else 0) + n


def _skip(research, receipts, why, t0, **extra):
    r = dict(research) if isinstance(research, dict) else {}
    rec = {"skipped": why, **extra, "seconds": round(time.monotonic() - t0, 1)}
    r["disconfirmation"] = rec
    return r, list(receipts) if isinstance(receipts, list) else [], rec


def disconfirm(research, receipts, deadline=None):
    """-> (research copy with the new evidence, sources and "disconfirmation", receipts + new receipts, record)."""
    t0 = time.monotonic()
    try:
        left = _left(deadline)
        if left < MIN_LEFT:
            return _skip(research, receipts, f"deadline: {max(0, left):.0f} s left, needs {MIN_LEFT}", t0)
        if not isinstance(research, dict):
            return _skip(research, receipts, "no research", t0)
        receipts = [x for x in receipts if isinstance(x, dict)] if isinstance(receipts, list) else []
        mode, targets = targets_of(research, receipts)
        tmap = {t["target"]: t for t in targets}
        try:
            queries = plan(research, targets)
        except Exception as e:  # noqa: BLE001
            return _skip(research, receipts, f"plan model error: {type(e).__name__}: {_s(e, 160)}", t0)
        r = _copy(research)
        ran = run_queries(queries, deadline)
        _stat(r, "tool_calls", len(queries))
        _stat(r, "llm_calls", 1)
        results = _merge_sources(r, ran)
        qrec = [{k: q[k] for k in ("target", "tool", "query", "results", "error")} for q in queries]
        if not results:
            return _skip(research, receipts, "no search results", t0, queries=qrec)
        if _left(deadline) < LEFT_AFTER_SEARCH:
            return _skip(research, receipts, f"deadline: under {LEFT_AFTER_SEARCH} s left after the searches", t0,
                         queries=qrec)
        user = "\n".join([f"PRODUCT URL: {_s(research.get('url'))}", f"TODAY: {datetime.date.today().isoformat()}",
                          "IDENTITY: " + json.dumps(_ident(research), ensure_ascii=False), "TARGETS:"]
                         + [json.dumps({k: v for k, v in t.items() if k != "tool"}, ensure_ascii=False)
                            for t in targets]
                         + ["RESULTS:"] + [json.dumps(x, ensure_ascii=False) for x in results])
        try:
            ans = model.ask_json(CHECK_SYSTEM, user, purpose="disconfirm")
        except Exception as e:  # noqa: BLE001
            return _skip(research, receipts, f"disconfirm model error: {type(e).__name__}: {_s(e, 160)}", t0,
                         queries=qrec)
        _stat(r, "llm_calls", 1)
        ans = ans if isinstance(ans, dict) else {}
        titles = {rc.url_key(x["url"]): x["title"] for x in results}
        new_ev, idmap, outs = _new_evidence(ans, tmap, research, receipts, titles)
        targets_by_id = {e["id"]: e.pop("_target") for e in new_ev}
        new_receipts = []
        if new_ev:
            mini = dict(r)
            mini["evidence"] = new_ev
            new_receipts = rc.check(mini)
            old = {}
            for x in receipts:
                if x.get("kept"):
                    old.setdefault(rc.dup_key(x.get("quote")), (x.get("id"), rc.url_key(_s(x.get("url")))))
            for x in new_receipts:
                first = old.get(rc.dup_key(x.get("quote")))
                if x.get("dup_of") is None and first and first[1] != rc.url_key(_s(x.get("url"))):
                    x["dup_of"] = first[0]
                x["_target"] = targets_by_id.get(x["id"], "")
        outcomes = _outcomes(targets, outs, idmap, new_receipts)
        for x in new_receipts:
            x.pop("_target", None)
        r["evidence"] += new_ev
        rec = {"mode": mode, "targets": [{k: t.get(k) for k in ("target", "kind", "category", "severity", "text",
                                                                 "question")} for t in targets],
               "queries": qrec, "outcomes": outcomes, "new_evidence": len(new_ev),
               "kept": sum(1 for x in new_receipts if x.get("kept")), "seconds": round(time.monotonic() - t0, 1)}
        r["disconfirmation"] = rec
        return r, receipts + new_receipts, rec
    except Exception as e:  # noqa: BLE001 - never raises: the judge runs on what research and receipts hold
        return _skip(research, receipts, f"error: {type(e).__name__}: {_s(e, 160)}", t0)
