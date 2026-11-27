"""The judge (lane L3): judge(research, receipts) -> Verdict (app/ledger.py shape), the four guards applied.

The model sees only kept receipts (reposts listed under their original), identity, the site read, claim checks and the
matrix statuses with notes: never raw sources, never rejected receipts. Code, not the model, builds each Flag from its
receipt ids (url, quote, date, author from the first kept id; corroboration from kept non-repost receipts on different
registrable domains; unknown ids dropped), then app/judge/guards.py corrects flags and color.

Views (env JUDGE_VIEWS, default 3; 1 = one call, purpose "judge"): up to three judge calls in parallel, "all" (every kept
receipt, purpose judge_all), "external" (only kept receipts that are independent and not vendor_self, plus
vendor_against_interest; purpose judge_external) and "devil" (a devil's advocate for the other color; judge_devil),
then one reconcile call (purpose "judge") that sees the research and the view answers and writes the final answer.
A failed view is skipped (logged in verdict["guards"]); if every view fails, one plain judge call; if the reconcile
fails, the "all" (else "external") view answer stands. Agreement of the view colors puts "N/N views agree" into
reliability_why and floors reliability at the lowest view reliability; disagreement caps it (two colors: medium, three:
low) and is named in reliability_why. verdict["views"] = [{"view", "overall", "reliability", "key_receipts"}]
([] for one call). The guards run once, on the final answer.
"""
import json, os
from concurrent.futures import ThreadPoolExecutor

from app import ledger, model
from app.judge import guards, prompts

BUCKETS = ("red_flags", "yellow_flags", "green_flags")
QUOTE_MAX = 500
GENERIC_QUESTIONS = ("Which customers of our size and segment can we call as references this month?",
                     "Who owns the company today, and what changed in the product and the terms after the last change "
                     "of ownership or leadership?",
                     "What happens to our data and our contract if the product is discontinued or sold?")


def _kept(receipts):
    return [r for r in receipts if r.get("kept")]


def _domain(r):
    return r.get("domain") or ledger.registrable(r.get("url", ""))


def _ids(raw):
    out = []
    for x in raw if isinstance(raw, list) else [raw] if raw else []:
        s = str(x).strip().strip("[]").strip()
        s = "e" + s if s.isdigit() else s
        if s and s not in out:
            out.append(s)
    return out


def _clip(s, n=QUOTE_MAX):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def judge_input(research, receipts):
    """The user text of the judge call. Only kept receipts; rejected ones and research['sources'] never appear."""
    kept = _kept(receipts)
    kept_ids = {r["id"] for r in kept}
    keep = lambda ids: [i for i in ids or [] if i in kept_ids]  # noqa: E731
    idt = research.get("identity") or {}
    site = research.get("site") or {}
    out = [f"PRODUCT URL: {research.get('url', '')}", "", "IDENTITY:", json.dumps({
        k: idt.get(k) for k in ("product", "vendor", "legal_entity", "parent", "former_names", "domains", "country",
                                "same_name_risks", "notes")} | {"receipts": keep(idt.get("evidence"))},
        ensure_ascii=False)]
    out += ["", "SITE (the vendor's own story):", json.dumps({
        "core_promise": site.get("core_promise", ""), "segment": site.get("segment", ""),
        "claims": [{"id": c.get("id"), "text": c.get("text"), "kind": c.get("kind"), "quote": _clip(c.get("quote"), 300)}
                   for c in site.get("claims", [])],
        "inconsistencies": [{"text": i.get("text"), "quote": _clip(i.get("quote"), 300)}
                            for i in site.get("inconsistencies", [])],
        "signals": site.get("signals", {})}, ensure_ascii=False)]
    claims = {c.get("id"): c.get("text") for c in site.get("claims", [])}
    out += ["", "CLAIM CHECKS:"] + ([json.dumps({"claim_id": c.get("claim_id"), "claim": claims.get(c.get("claim_id"), ""),
                                                "status": c.get("status"), "receipts": keep(c.get("evidence")),
                                                "note": c.get("note", "")}, ensure_ascii=False)
                                    for c in research.get("claim_checks", [])] or ["(none)"])
    out += ["", "MATRIX (category: status, queries run, receipts, note):"]
    m = research.get("matrix", {})
    for k, (name, *_rest) in ledger.CATEGORIES.items():
        row = m.get(str(k), {})
        ids = keep(row.get("evidence"))
        out.append(f"{k} {name}: {row.get('status', 'open')}; {len(row.get('queries', []))} queries; "
                   f"receipts {', '.join(ids) or '-'}; {row.get('note', '')}".rstrip("; "))
    reposts = {}
    for r in kept:
        if r.get("dup_of") in kept_ids:
            reposts.setdefault(r["dup_of"], []).append({"id": r["id"], "url": r.get("url"), "domain": _domain(r),
                                                        "date": r.get("date", "")})
    out += ["", "RECEIPTS (kept only):"]
    for r in kept:
        if r.get("dup_of") in kept_ids:
            continue
        line = {"id": r["id"], "category": r.get("category"), "finding": r.get("text"), "quote": _clip(r.get("quote")),
                "url": r.get("url"), "domain": _domain(r), "date": r.get("date", ""),
                "author_type": r.get("author_type"), "severity": r.get("severity"), "polarity": r.get("polarity"),
                "promise_link": r.get("promise_link", ""), "independent": bool(r.get("independent")),
                "dated": bool((r.get("checks") or {}).get("dated"))}
        if r["id"] in reposts:
            line["reposts"] = reposts[r["id"]]
        out.append(json.dumps(line, ensure_ascii=False))
    if not kept:
        out.append("(none: no receipt survived the checks)")
    out += ["", "DISCONFIRMATION (attempts to disprove the decisive findings):"] + _disconfirmation(research, keep)
    failed = research.get("sources_failed", [])
    out += ["", f"FAILED SOURCES (no signal, never a negative): {len(failed)}"]
    return "\n".join(out)


def _disconfirmation(research, keep):
    d = research.get("disconfirmation")
    if not isinstance(d, dict) or not d:
        return ["(not run)"]
    if d.get("skipped"):
        return [f"(skipped: {d['skipped']})"]
    targets, queries = d.get("targets") or [], d.get("queries") or []
    out = [f"targets: {len(targets)}; queries run: {len(queries)}"]
    out += ["target: " + (t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)) for t in targets]
    for o in d.get("outcomes") or []:
        if isinstance(o, dict):
            out.append(json.dumps({"target": o.get("target"), "outcome": o.get("outcome"), "why": _clip(o.get("why"), 300),
                                   "receipts": keep(_ids(o.get("evidence")))}, ensure_ascii=False))
    return out


def build_flag(f, by_id):
    """A model flag -> a ledger Flag. Unknown ids dropped; url, quote, date, author from the first kept id."""
    ids = [i for i in _ids(f.get("receipts")) if i in by_id]
    kept = [by_id[i] for i in ids if by_id[i].get("kept")]
    first = kept[0] if kept else {}
    domains = {_domain(r) for r in kept if not r.get("dup_of")}
    cat = f.get("category")
    try:
        cat = int(cat)
    except (TypeError, ValueError):
        cat = None
    if cat not in ledger.CATEGORIES:
        cat = first.get("category")
    sev = f.get("severity") if f.get("severity") in ledger.SEVERITIES else first.get("severity", "minor")
    return {"text": str(f.get("text") or first.get("text") or "").strip(), "category": cat, "severity": sev,
            "url": first.get("url", ""), "quote": first.get("quote", ""), "date": first.get("date", ""),
            "author": first.get("author_type", ""),
            "corroboration": "independent" if len(domains) >= 2 else "one",
            "basis": f.get("basis") if f.get("basis") in ledger.BASES else "not_confirmed", "receipts": ids}


_RANK = {a: i for i, a in enumerate(ledger.AUTHOR_TYPES)}
_SEV = {s: i for i, s in enumerate(ledger.SEVERITIES)}


def best_receipt(receipts):
    """The strongest kept receipt: author type, then severity, then independent, then not a repost."""
    kept = _kept(receipts)
    if not kept:
        return None
    return min(kept, key=lambda r: (_RANK.get(r.get("author_type"), 99), _SEV.get(r.get("severity"), 9),
                                    not r.get("independent"), bool(r.get("dup_of"))))


def reasons(verdict, research, receipts):
    """Every flag red, yellow, green as {text, url, quote}; else the best kept receipt; else a site claim."""
    out = [{"text": f["text"], "url": f["url"], "quote": f["quote"]} for b in BUCKETS for f in verdict.get(b, [])]
    if out:
        return out
    r = best_receipt(receipts)
    if r:
        return [{"text": r.get("text", ""), "url": r.get("url", ""), "quote": r.get("quote", "")}]
    for c in (research.get("site") or {}).get("claims", []):
        if c.get("url") and c.get("quote"):
            return [{"text": "no receipt survived the checks; the vendor's own claim: " + str(c.get("text", "")),
                     "url": c["url"], "quote": c["quote"]}]
    return [{"text": "too little evidence: no receipt survived the checks", "url": research.get("url", ""), "quote": ""}]


def build_verdict(research, receipts, ans, error=None):
    """The model answer -> a Verdict before the guards."""
    by_id = {r["id"]: r for r in receipts if r.get("id")}
    idt = research.get("identity") or {}
    log = []
    overall = str(ans.get("overall") or "").strip().capitalize()
    if overall not in ledger.COLORS:
        log.append(f"judge: overall {ans.get('overall')!r} is not a color -> Yellow")
        overall = "Yellow"
    rel = str(ans.get("reliability") or "").strip().lower()
    rel = rel if rel in ledger.RELIABILITY else "low"
    v = {"url": research.get("url", ""), "product": idt.get("product", ""), "vendor": idt.get("vendor", ""),
         "parent": idt.get("parent", ""), "former_names": list(idt.get("former_names") or []),
         "overall": overall, "call": overall, "reliability": rel,
         "reliability_why": str(ans.get("reliability_why") or "")}
    for b in BUCKETS:
        raw = ans.get(b) if isinstance(ans.get(b), list) else []
        v[b] = [build_flag(f, by_id) for f in raw if isinstance(f, dict)]
    qs = [str(q).strip() for q in ans.get("vendor_questions") or [] if str(q).strip()][:3]
    qs += [q for q in GENERIC_QUESTIONS if q not in qs][:3 - len(qs)]
    v |= {"rejected": [{"text": r.get("text", ""), "url": r.get("url", ""), "why": r.get("why", "")}
                       for r in receipts if not r.get("kept")],
          "reasoning": str(ans.get("reasoning") or ""),
          "blind_spots": [str(s) for s in ans.get("blind_spots") or [] if str(s).strip()],
          "buyer_next_step": str(ans.get("buyer_next_step") or ""), "vendor_questions": qs,
          "would_flip": str(ans.get("would_flip") or ""),
          "coverage": {str(k): (research.get("matrix", {}).get(str(k)) or {}).get("status", "open")
                       for k in ledger.CATEGORIES},
          "sources_failed": list(research.get("sources_failed") or []), "guards": log, "reasons": [],
          "model": model.model_id(), "run_id": "", "trace": "", "seconds": 0.0, "error": error, "mode": model.mode()}
    return v


VIEWS = (("all", "judge_all"), ("external", "judge_external"), ("devil", "judge_devil"))
_REL = {r: i for i, r in enumerate(ledger.RELIABILITY)}  # high 0, medium 1, low 2


def external_receipts(receipts):
    """The external view's receipts: kept, and independent and not vendor_self, or vendor_against_interest."""
    return [r for r in receipts if r.get("kept") and ((r.get("independent") and r.get("author_type") != "vendor_self")
                                                       or r.get("author_type") == "vendor_against_interest")]


def _color(a):
    c = str(a.get("overall") or "").strip().capitalize()
    return c if c in ledger.COLORS else None


def _rel(a):
    r = str(a.get("reliability") or "").strip().lower()
    return r if r in ledger.RELIABILITY else "low"


def _ask(system, user, purpose):
    """-> (answer dict, None) or (None, error text)."""
    try:
        a = model.ask_json(system, user, purpose=purpose)
    except model.ModelError as e:
        return None, str(e)
    return (a, None) if isinstance(a, dict) else (None, f"answer is {type(a).__name__}")


def key_receipts(ans, by_id, n=5):
    """The ids a view's color rests on: its own color's bucket first (red under Yellow too), then the rest."""
    order = {"Red": ("red_flags", "yellow_flags", "green_flags"), "Yellow": ("yellow_flags", "red_flags", "green_flags"),
             "Green": ("green_flags", "yellow_flags", "red_flags")}.get(_color(ans), BUCKETS)
    out = []
    for b in order:
        for f in ans.get(b) or []:
            for i in _ids(f.get("receipts")) if isinstance(f, dict) else []:
                if i in by_id and by_id[i].get("kept") and i not in out:
                    out.append(i)
    return out[:n]


def reconcile_input(user, answers):
    """The reconcile call's user text: the research as the 'all' view saw it, then each view's answer."""
    keys = ("overall", "reliability", "reliability_why", "red_flags", "yellow_flags", "green_flags", "reasoning",
            "blind_spots", "would_flip", "argued_against", "argued_for")
    out = [user, "", "VIEWS (independent judge answers on this research):"]
    for name, a in answers.items():
        out.append(f"{name}: " + json.dumps({k: a[k] for k in keys if k in a}, ensure_ascii=False))
    return "\n".join(out)


def _views(research, receipts, user, n, log):
    """-> (final answer or None, views list). None means every view failed: the caller makes one plain call."""
    plan = VIEWS[:n]
    users = {"all": user, "devil": user, "external": judge_input(research, external_receipts(receipts))}
    with ThreadPoolExecutor(len(plan)) as ex:
        futs = {name: ex.submit(_ask, prompts.VIEW_SYSTEM[name], users[name], purpose) for name, purpose in plan}
    answers = {}
    for name, _p in plan:
        a, err = futs[name].result()
        if err is None and _color(a) is None:
            err = f"overall {a.get('overall')!r} is not a color"
        if err:
            log.append(f"views: {name} failed, skipped: {_clip(err, 200)}")
        else:
            answers[name] = a
    if not answers:
        log.append("views: every view failed, one plain judge call instead")
        return None, []
    by_id = {r["id"]: r for r in receipts if r.get("id")}
    views = [{"view": k, "overall": _color(a), "reliability": _rel(a), "key_receipts": key_receipts(a, by_id)}
             for k, a in answers.items()]
    final, err = _ask(prompts.RECONCILE_SYSTEM, reconcile_input(user, answers), "judge")
    if err:
        pick = next((k for k in ("all", "external") if k in answers), None)
        if pick is None:
            log.append(f"views: reconcile failed ({_clip(err, 200)}) and only the devil view answered, one plain "
                       f"judge call instead")
            return None, views
        log.append(f"views: reconcile failed ({_clip(err, 200)}), the {pick} view's answer stands")
        final = answers[pick]
    return final, views


def apply_agreement(v, views, n):
    """Agreement floors reliability at the lowest view's; disagreement caps it; both are named in reliability_why."""
    if not views:
        return v
    colors = [x["overall"] for x in views]
    k = len(views)
    if k < 2:
        note = f"1 of {n} views answered ({views[0]['view']} {colors[0]})"
    elif len(set(colors)) == 1 and colors[0] == v["overall"]:
        note = f"{k}/{k} views agree" + (f" ({n - k} failed)" if k < n else "")
        floor = max((x["reliability"] for x in views), key=_REL.get)
        if _REL[floor] < _REL[v["reliability"]]:
            v["guards"].append(f"views: reliability {v['reliability']} -> {floor}, {note}")
            v["reliability"] = floor
    else:
        cap = "low" if len(set(colors) | {v["overall"]}) >= 3 else "medium"
        note = "views disagree: " + ", ".join(f"{x['view']} {x['overall']}" for x in views)
        if len(set(colors)) == 1:
            note = f"the reconcile chose {v['overall']} against {k}/{k} views ({colors[0]})"
        if _REL[v["reliability"]] < _REL[cap]:
            v["guards"].append(f"views: reliability {v['reliability']} -> {cap}, {note}")
            v["reliability"] = cap
    agreed = note.endswith("views agree") or " views agree (" in note
    if not (agreed and note.split(" (")[0] in v["reliability_why"]):
        v["reliability_why"] = f"{note}; {v['reliability_why']}".rstrip("; ")
    return v


def judge(research, receipts, views=None):
    """-> Verdict. views (default env JUDGE_VIEWS, 3) = number of judge views; 1 = one plain call.
    A failed model call yields a Yellow with low reliability and `error` set (the batch goes on)."""
    try:
        n = int(views if views is not None else os.environ.get("JUDGE_VIEWS") or 3)
    except ValueError:
        n = 3
    n = max(1, min(len(VIEWS), n))
    user = judge_input(research, receipts)
    log, view_list, ans, error = [], [], None, None
    if n > 1:
        ans, view_list = _views(research, receipts, user, n, log)
    if ans is None:
        ans, err = _ask(prompts.SYSTEM, user, "judge")
        if err:
            error = f"judge: {err}"
            ans = {"overall": "Yellow", "reliability": "low",
                   "reliability_why": "the judge model call failed, so the color is the default, not a judgement",
                   "reasoning": "The judge model call failed; no judgement was made on the receipts."}
    v = build_verdict(research, receipts, ans, error)
    v["guards"] = log + v["guards"]
    v["views"] = view_list
    if not error:
        apply_agreement(v, view_list, n)
    v = guards.apply(v, research, receipts)
    v["reasons"] = reasons(v, research, receipts)
    return v
