"""Flags and errors of one run against the labels (lane L4); every model call goes through app.model.ask_json (cached).

    res = flags.analyze(rows, preds, run_dir)        # {"products": {id: {...}}, "totals": {...}}
    md = flags.render(res, details=True)             # the "Flags and errors" section of report.md

Per product:
- matcher (purpose "match"): their flag items (each bucket's text split into items) against our flags (the verdict
  line) -> per item found / partly / missed with our matching flag text; extra = our flags no item matched.
- extra check (purpose "extra"): each extra flag -> verified (backed by its receipt quote, material for a buyer, not
  contradicted by their reasoning) or noise.
- error classifier (purpose "classify"): each missed item and a wrong color, given a capped digest of our
  research.json (identity, evidence texts, source titles and snippets) -> judge_error | research_gap |
  identity_error with why.
- grounding: share of receipts kept (receipts.json); coverage: matrix rows with a status (not "open") out of 24.
A model failure marks that step's items "error" (never counted as missed) and is listed per product.
Their item texts go to the model only; outputs name items by id ("t3") with our text and the model's why.
"""
import concurrent.futures as cf, json, os, re

from app import ledger, model
from evalkit.labels import pred_color

BUCKETS = (("red", "red_flags"), ("yellow", "yellow_flags"), ("green", "green_flags"))
MATCHES = ("found", "partly", "missed")
CLASSES = ("judge_error", "research_gap", "identity_error")
DIGEST_CAP = 24000
CLIP = 300

MATCH_SYSTEM = """You compare two reviews of the same software vendor. A human analyst wrote flag items (theirs, t1,
t2, ...); an agent wrote flags (ours, o1, o2, ...). For each of their items decide by meaning, not wording, whether
our flags say the same thing; the flag color may differ:
- found: one or more of our flags state the same fact, risk or strength;
- partly: our flags touch the same topic but miss its key point, or state it much weaker or stronger;
- missed: none of our flags covers it.
Use only the texts given, no outside knowledge.
Answer {"items": [{"id": "t1", "match": "found|partly|missed", "ours": ["o2"], "why": "one short sentence"}]} with
every item id exactly once; "ours" lists the flags that cover it ([] when missed)."""

EXTRA_SYSTEM = """An agent raised flags about a software vendor that a human analyst did not raise. For each flag decide:
- backed: its receipt quote really supports the flag's statement;
- material: a procurement buyer would act on it (change the decision, or ask the vendor a question); trivia,
  marketing claims and generic statements are not material;
- contradicted: the analyst's reasoning states the opposite.
Answer {"results": [{"id": "o3", "backed": true, "material": true, "contradicted": false, "why": "one short
sentence"}]} with every flag id exactly once."""

CLASSIFY_SYSTEM = """You diagnose why a vendor-research agent got something wrong. You see the human analyst's color and
reasoning, the errors to explain (a wrong color and their items the agent missed), the agent's color and reasoning,
and a digest of everything the agent retrieved (identity, evidence, source titles and snippets). Classify each error:
- identity_error: the agent researched a different company or product (same-name confusion, wrong entity) or mixed two;
- research_gap: the facts needed are not in the retrieved material;
- judge_error: the retrieved material holds what was needed, but the conclusion ignored or mis-weighed it (name the
  evidence id or source title).
Answer {"errors": [{"id": "color" or "t3", "class": "judge_error|research_gap|identity_error", "why": "one or two
sentences"}]} with every error id exactly once."""

_BULLET = re.compile(r"^\s*(?:[-*\u2022\u2013\u2014>]+|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+", re.I)


def split_items(text):
    """A bucket's free text -> items: one per line (bullets and numbering stripped); a single line splits on ';'."""
    t = str(text or "").replace("\r", "\n").strip()
    if not t or t.lower() in ("-", "none", "n/a", "na", "[]"):
        return []
    lines = [_BULLET.sub("", ln).strip() for ln in t.split("\n")]
    lines = [ln for ln in lines if ln and ln.lower() not in ("-", "none", "n/a")]
    if len(lines) == 1:
        lines = [p.strip() for p in lines[0].split(";") if p.strip()]
    return lines


def their_items(row):
    items = []
    for color, col in BUCKETS:
        items += [{"bucket": color, "text": x} for x in split_items(row.get(col))]
    return [dict(it, id=f"t{i}") for i, it in enumerate(items, 1)]


def our_flags(pred):
    out = []
    for color, col in BUCKETS:
        for f in (pred or {}).get(col) or []:
            if isinstance(f, dict) and f.get("text"):
                out.append({"bucket": color, "text": f["text"], "url": f.get("url", ""), "quote": f.get("quote", ""),
                            "category": f.get("category"), "basis": f.get("basis", "")})
    return [dict(f, id=f"o{i}") for i, f in enumerate(out, 1)]


def _clip(s, n=CLIP):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _by_id(answer, key):
    out = {}
    for x in (answer or {}).get(key) or []:
        if isinstance(x, dict) and x.get("id") is not None:
            out.setdefault(str(x["id"]), x)
    return out


def match(row, pred):
    """-> (items with match/ours/our_text/why, extra flags, error)."""
    items, ours = their_items(row), our_flags(pred)
    if not items:
        return [], ours, None
    if not ours:
        return [dict(it, match="missed", ours=[], our_text="", why="no flags of ours") for it in items], [], None
    user = ("Their items:\n" + "\n".join(f"[{it['id']}] ({it['bucket']}) {it['text']}" for it in items)
            + "\n\nOur flags:\n" + "\n".join(f"[{f['id']}] ({f['bucket']}) {f['text']}" for f in ours))
    try:
        ans = _by_id(model.ask_json(MATCH_SYSTEM, user, purpose="match"), "items")
    except model.ModelError as e:
        return [dict(it, match="error", ours=[], our_text="", why="") for it in items], [], f"match: {e}"
    text = {f["id"]: f["text"] for f in ours}
    out, used = [], set()
    for it in items:
        a = ans.get(it["id"], {})
        m = a.get("match") if a.get("match") in MATCHES else "missed"
        ids = [str(i) for i in (a.get("ours") or []) if str(i) in text] if m != "missed" else []
        if m != "missed" and not ids:
            m = "missed"
        used.update(ids)
        out.append(dict(it, match=m, ours=ids, our_text=" | ".join(text[i] for i in ids),
                        why=str(a.get("why") or ("not answered" if not a else ""))))
    return out, [f for f in ours if f["id"] not in used], None


def check_extra(row, extras):
    """-> (extras with status verified|noise and why, error)."""
    if not extras:
        return [], None
    user = (f"Analyst's reasoning:\n{_clip(row.get('reasoning'), 3000)}\n\nAgent's flags the analyst did not raise:\n"
            + "\n".join(f"[{f['id']}] ({f['bucket']}) {f['text']}\n  receipt: {f['url']}\n  quote: {_clip(f['quote'], 600)}"
                        for f in extras))
    try:
        ans = _by_id(model.ask_json(EXTRA_SYSTEM, user, purpose="extra"), "results")
    except model.ModelError as e:
        return [dict(f, status="error", why="") for f in extras], f"extra: {e}"
    out = []
    for f in extras:
        a = ans.get(f["id"], {})
        ok = a.get("backed") is True and a.get("material") is True and a.get("contradicted") is False
        out.append(dict(f, status="verified" if ok else "noise", backed=a.get("backed"), material=a.get("material"),
                        contradicted=a.get("contradicted"), why=str(a.get("why") or ("not answered" if not a else ""))))
    return out, None


def digest(research, cap=DIGEST_CAP):
    """A capped text digest of research.json: identity, evidence, matrix notes, source titles and snippets."""
    if not research:
        return "(no research.json)"
    idt = research.get("identity") or {}
    parts = ["Identity: " + json.dumps({k: idt.get(k) for k in ("product", "vendor", "legal_entity", "parent",
                                                               "former_names", "domains", "country") if idt.get(k)})]
    parts.append("Evidence:")
    parts += [f"[{e.get('id')}] cat {e.get('category')} {e.get('polarity', '')}/{e.get('severity', '')}: "
              f"{_clip(e.get('text'), 200)} | quote: {_clip(e.get('quote'), 200)} | {e.get('source_title', '')} "
              f"{e.get('url', '')}" for e in research.get("evidence") or []]
    parts.append("Matrix:")
    parts += [f"{k} {v.get('status')}: {_clip(v.get('note'), 150)}" for k, v in (research.get("matrix") or {}).items()
              if isinstance(v, dict)]
    parts.append("Sources:")
    parts += [f"- {s.get('title') or ''} ({s.get('kind', '')}) {u}: {_clip(s.get('text'), 200)}"
              for u, s in (research.get("sources") or {}).items() if isinstance(s, dict)]
    out = "\n".join(parts)
    return out if len(out) <= cap else out[:cap] + "\n[digest capped]"


def classify(row, pred, missed, research):
    """-> (errors [{id, class, why}], error). One call per product for a wrong color and every missed item."""
    theirs, ours = row.get("color", ""), pred_color(pred) or "-"
    todo = ([{"id": "color", "text": f"color: theirs {theirs}, ours {ours}"}] if pred and theirs != ours else [])
    todo += [{"id": it["id"], "text": f"missed ({it['bucket']}): {it['text']}"} for it in missed]
    if not todo:
        return [], None
    if research is None:
        return [{"id": t["id"], "class": "", "why": "no research.json"} for t in todo], None
    user = (f"Analyst: color {theirs}. Reasoning: {_clip(row.get('reasoning'), 3000)}\n\n"
            f"Agent: color {ours}. Reasoning: {_clip((pred or {}).get('reasoning'), 3000)}\n\n"
            "Errors to classify:\n" + "\n".join(f"[{t['id']}] {t['text']}" for t in todo)
            + "\n\nDigest of what the agent retrieved:\n" + digest(research))
    try:
        ans = _by_id(model.ask_json(CLASSIFY_SYSTEM, user, purpose="classify"), "errors")
    except model.ModelError as e:
        return [{"id": t["id"], "class": "error", "why": ""} for t in todo], f"classify: {e}"
    out = []
    for t in todo:
        a = ans.get(t["id"], {})
        out.append({"id": t["id"], "class": a.get("class") if a.get("class") in CLASSES else "",
                    "why": str(a.get("why") or ("not answered" if not a else ""))})
    return out, None


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def product_dir(pred, run_dir):
    """The product's artifact folder: the verdict's trace folder, else <run_dir>/<slug(url)>."""
    tr = (pred or {}).get("trace") or ""
    cands = []
    if tr:
        d = os.path.dirname(tr)
        cands += [d] + ([os.path.join(run_dir, os.path.basename(d))] if run_dir else [])
    if run_dir:
        cands.append(os.path.join(run_dir, ledger.slug((pred or {}).get("url", ""))))
    return next((c for c in cands if c and os.path.isdir(c)), None)


def grounding(receipts):
    if not isinstance(receipts, list):
        return None
    kept = sum(1 for r in receipts if isinstance(r, dict) and r.get("kept"))
    return {"kept": kept, "total": len(receipts), "share": kept / len(receipts) if receipts else None}


def coverage(pred, research):
    cov = (pred or {}).get("coverage") or {str(k): (v or {}).get("status") for k, v in
                                           ((research or {}).get("matrix") or {}).items()}
    n = sum(1 for k in map(str, ledger.CATEGORIES) if cov.get(k) in ledger.STATUSES and cov.get(k) != "open")
    return {"with_status": n, "of": len(ledger.CATEGORIES)} if cov else None


def analyze_product(row, pred, run_dir=None, use_model=True):
    d = product_dir(pred, run_dir)
    research = _load(os.path.join(d, "research.json")) if d else None
    receipts = _load(os.path.join(d, "receipts.json")) if d else None
    res = {"id": row["id"], "theirs": row.get("color"), "ours": pred_color(pred) or "-",
           "grounding": grounding(receipts), "coverage": coverage(pred, research), "errors_model": [],
           "items": [], "extra": [], "errors": []}
    if not use_model or not pred:
        return res
    items, extras, err = match(row, pred)
    res["items"] = items
    res["extra"], err2 = check_extra(row, extras)
    missed = [it for it in items if it["match"] == "missed"]
    res["errors"], err3 = classify(row, pred, missed, research)
    res["errors_model"] = [e for e in (err, err2, err3) if e]
    return res


def analyze(rows, preds, run_dir=None, use_model=True, concurrency=8):
    """rows: label rows of one split; preds: {key: verdict line}. Products run in parallel (model.py caps calls)."""
    todo = [(r, preds.get(r["key"])) for r in rows]
    with cf.ThreadPoolExecutor(max(1, concurrency)) as ex:
        results = list(ex.map(lambda rp: analyze_product(rp[0], rp[1], run_dir, use_model), todo))
    return {"products": {r["id"]: r for r in results}, "totals": totals(results), "model": use_model}


def totals(results):
    items = [it for r in results for it in r["items"]]
    t = {"items": len(items), **{m: sum(it["match"] == m for it in items) for m in MATCHES + ("error",)}}
    t["by_bucket"] = {c: {m: sum(it["match"] == m for it in items if it["bucket"] == c) for m in MATCHES + ("error",)}
                      | {"n": sum(it["bucket"] == c for it in items)} for c, _ in BUCKETS}
    ex = [f for r in results for f in r["extra"]]
    t["extra"] = {"n": len(ex), **{s: sum(f["status"] == s for f in ex) for s in ("verified", "noise", "error")}}
    errs = [e for r in results for e in r["errors"]]
    t["errors"] = {c or "unclassified": sum(e["class"] == c for e in errs) for c in CLASSES + ("", "error")}
    g = [r["grounding"] for r in results if r["grounding"]]
    kept, tot = sum(x["kept"] for x in g), sum(x["total"] for x in g)
    t["grounding"] = {"kept": kept, "total": tot, "share": kept / tot if tot else None, "products": len(g)}
    cv = [r["coverage"]["with_status"] for r in results if r["coverage"]]
    t["coverage"] = {"mean": sum(cv) / len(cv) if cv else None, "full": sum(c == 24 for c in cv), "products": len(cv),
                     "of": len(ledger.CATEGORIES)}
    t["model_errors"] = sum(len(r["errors_model"]) for r in results)
    return t


def _pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else f"{a}/{b}"


def render(res, details=True):
    """Markdown section. details=False (holdout): aggregates and per-product counts only, no item lines."""
    t, md = res["totals"], ["## Flags and errors", ""]
    if res.get("model"):
        md.append(f"- Their flag items: {t['items']}; found {_pct(t['found'], t['items'])}, partly {t['partly']}, "
                  f"missed {t['missed']}" + (f", not judged (model error) {t['error']}" if t["error"] else ""))
        md += [f"  - {c}: found {b['found']}, partly {b['partly']}, missed {b['missed']} of {b['n']}"
               for c, b in t["by_bucket"].items() if b["n"]]
        e = t["extra"]
        md.append(f"- Our extra flags: {e['n']}; verified {e['verified']}, noise {e['noise']}"
                  + (f", error {e['error']}" if e["error"] else ""))
        md.append("- Errors (missed items and wrong colors): "
                  + ", ".join(f"{k} {v}" for k, v in t["errors"].items() if v or k in CLASSES))
    else:
        md.append("- Flags not compared (--no-model).")
    g, c = t["grounding"], t["coverage"]
    md.append(f"- Grounding (receipts kept): " + (f"{_pct(g['kept'], g['total'])} over {g['products']} products"
                                                   if g["products"] else "n/a (no receipts.json)"))
    md.append("- Coverage (matrix rows with a status): " + (f"mean {c['mean']:.1f}/{c['of']}, all {c['of']} on "
                                                             f"{c['full']}/{c['products']} products" if c["products"]
                                                             else "n/a"))
    if t["model_errors"]:
        md.append(f"- Model errors: {t['model_errors']} (per product below)")
    md += ["", "| id | theirs | ours | found / partly / missed | extra verified / noise | errors | grounding | coverage |",
           "|---|---|---|---|---|---|---|---|"]
    for pid, r in res["products"].items():
        n = {m: sum(it["match"] == m for it in r["items"]) for m in MATCHES}
        ex = {s: sum(f["status"] == s for f in r["extra"]) for s in ("verified", "noise")}
        er = ", ".join(f"{e['id']}:{e['class'] or '?'}" for e in r["errors"])
        g = r["grounding"]
        md.append(f"| {pid} | {r['theirs']} | {r['ours']} | {n['found']} / {n['partly']} / {n['missed']} | "
                  f"{ex['verified']} / {ex['noise']} | {er} | {_pct(g['kept'], g['total']) if g else ''} | "
                  f"{r['coverage']['with_status'] if r['coverage'] else ''} |")
    if details and res.get("model"):
        md += ["", "### Per item (their items by id; our flag text)", ""]
        for pid, r in res["products"].items():
            if not (r["items"] or r["extra"] or r["errors"] or r["errors_model"]):
                continue
            md.append(f"**{pid}** ({r['theirs']} / {r['ours']})")
            cls = {e["id"]: e for e in r["errors"]}
            for it in r["items"]:
                c = cls.get(it["id"])
                md.append(f"- {it['id']} ({it['bucket']}) {it['match']}"
                          + (f": ours \"{_clip(it['our_text'], 200)}\"" if it["our_text"] else "")
                          + (f"; {_clip(it['why'], 200)}" if it["why"] else "")
                          + (f"; error {c['class'] or '?'}: {_clip(c['why'], 240)}" if c else ""))
            if "color" in cls:
                md.append(f"- color: {cls['color']['class'] or '?'}: {_clip(cls['color']['why'], 300)}")
            for f in r["extra"]:
                md.append(f"- extra {f['id']} ({f['bucket']}) {f['status']}: \"{_clip(f['text'], 200)}\""
                          + (f"; {_clip(f['why'], 200)}" if f["why"] else ""))
            md += [f"- model error: {_clip(e, 200)}" for e in r["errors_model"]]
            md.append("")
    return "\n".join(md)


def strip_labels(res, details=True):
    """The JSON-able result without their item texts (and, for holdout, without per-item lines)."""
    out = {"totals": res["totals"], "model": res.get("model"), "products": {}}
    for pid, r in res["products"].items():
        p = {k: v for k, v in r.items() if k not in ("items", "extra", "errors")}
        if details:
            p["items"] = [{k: v for k, v in it.items() if k != "text"} for it in r["items"]]
            p["extra"] = [{k: f.get(k) for k in ("id", "bucket", "text", "url", "status", "why")} for f in r["extra"]]
            p["errors"] = r["errors"]
        else:
            p["counts"] = {m: sum(it["match"] == m for it in r["items"]) for m in MATCHES} | {
                "verified": sum(f["status"] == "verified" for f in r["extra"]),
                "noise": sum(f["status"] == "noise" for f in r["extra"])}
            p["errors"] = [{"id": e["id"], "class": e["class"]} for e in r["errors"]]
        out["products"][pid] = p
    return out
