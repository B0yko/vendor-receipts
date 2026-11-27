"""trace.md: the research trace of one product, for an LLM-as-judge and a human auditor (lane L5).

    md = trace.render(research, receipts, verdict)   # Markdown string; never raises on missing keys

Sections: header (url, color, reliability and why, model, run id, seconds, capped); 1 identity and anchors with
receipt ids; 2 the matrix, all 24 rows; 3 claims ledger; 4 decision path (disconfirmation: targets, queries, outcome
per target with why and new receipts, or the skip reason; judge views with their agreement; flags per bucket with
their receipts and any disconfirmation outcome, guard log, reasoning, would flip, blind spots, vendor questions, buyer next step; every flag shows its url and full quote,
whitespace collapsed, never cut, so a checker can find it on the page; the self-audit after the reasoning: removed
sentences with why, flags without a kept receipt; a step that did not run says "Not run."); 5 rejected receipts, failed sources and
limits; research rounds; appendix with every kept receipt, full quote and checks.
"""
import json

from app.ledger import CATEGORIES

CHECKS = ("resolves", "same_company", "says_it", "dated")
BUCKETS = (("red_flags", "Red flags"), ("yellow_flags", "Yellow flags"), ("green_flags", "Green flags"))


def _cell(x, limit=None):
    if isinstance(x, (list, tuple)):
        x = ", ".join(str(i) for i in x if i not in (None, ""))
    s = " ".join(str(x if x is not None else "").split()).replace("|", "\\|")
    if limit and len(s) > limit:
        s = s[:limit - 1] + "…"
    return s or "–"


def _inline(x):
    """One line of prose (not a table cell): whitespace collapsed, nothing escaped or cut, so quotes stay verbatim."""
    return " ".join(str(x if x is not None else "").split())


def _table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def _checks(rc):
    """'resolves yes same_company yes says_it yes (partial: <missing detail>) dated no': L6's says_it_note sits next
    to the says_it check."""
    c = rc.get("checks") or {}
    if not c:
        return "not checked"
    note = _inline(rc.get("says_it_note"))
    return " ".join(f"{k} {'yes' if c.get(k) else 'no'}" + (f" ({note})" if k == "says_it" and note else "")
                    for k in CHECKS if k in c)


def _model_calls(stats, v):
    """stats.llm_calls counts the research loop and disconfirmation only (each adds to research stats), so it is
    labelled that way; a pipeline-wide count (verdict stats model_calls) is shown when a later step records one."""
    out = f"research model calls: {stats.get('llm_calls', 0)}"
    total = ((v or {}).get("stats") or {}).get("model_calls")
    return out + (f" · all model calls: {total}" if total not in (None, "") else "")


def _quote_block(q):
    lines = str(q or "").strip().splitlines() or [""]
    return ["> " + ln for ln in lines]


def _yn(b):
    return "yes" if b else "no"


def _args(a):
    if isinstance(a, dict):
        return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in a.items())
    return a


def _tid(t):
    """The receipt id (or probe name) a disconfirmation target or outcome refers to."""
    if isinstance(t, dict):
        t = t.get("id") or t.get("target") or t.get("receipt") or t.get("evidence") or t.get("probe") or ""
    if isinstance(t, (list, tuple)):
        t = ", ".join(str(x) for x in t)
    return _inline(t)


def _target(t, by_id):
    tid, text, extra = _tid(t), "", ""
    if isinstance(t, dict):
        text = t.get("text") or t.get("probe") or t.get("claim") or t.get("hypothesis") or t.get("question") or ""
        extra = ", ".join(_inline(t[k]) for k in ("severity", "category", "kind") if t.get(k) not in (None, ""))
    rc = by_id.get(tid) or {}
    text = _inline(text or rc.get("text"))
    if rc and not extra:
        extra = ", ".join(x for x in (rc.get("severity"), rc.get("author_type")) if x)
    out = tid if not text or text == tid else f"{tid}: {text}"
    return out + (f" ({extra})" if extra else "")


def _receipt_state(rid, by_id):
    rc = by_id.get(rid)
    if rc is None:
        return f"{rid} (not in the receipts list)"
    if "kept" not in rc:
        return rid
    return f"{rid} kept" if rc.get("kept") else f"{rid} rejected ({_inline(rc.get('why')) or 'no reason given'})"


def _query(q):
    if not isinstance(q, dict):
        return _inline(q)
    args = q.get("args") if isinstance(q.get("args"), dict) else {}
    text = q.get("query") or args.get("query") or q.get("q") or ""
    tool = q.get("tool") or q.get("topic") or args.get("topic") or ""
    res = q.get("results")
    n = len(res) if isinstance(res, list) else (q.get("n_results") if q.get("n_results") is not None else res)
    out = (f"[{_tid(q.get('target'))}] " if q.get("target") else "") + (f"{tool}: " if tool else "")
    out += f"`{_inline(text).replace('`', "'")}`"
    if n is not None:
        out += f" · {n} results"
    if q.get("error"):
        out += f" · error: {_inline(q['error'])}"
    return out


def disconfirmation_summary(research):
    d = (research or {}).get("disconfirmation")
    if not d:
        return "not run"
    if d.get("skipped"):
        return f"skipped ({_inline(d['skipped'])})"
    outs = [_inline(o.get("outcome") or "?") for o in d.get("outcomes") or []]
    tail = ", ".join(f"{outs.count(x)} {x}" for x in dict.fromkeys(outs))
    n = len(d.get("targets") or [])
    return f"{n} target{'' if n == 1 else 's'}" + (f": {tail}" if tail else "")


def _disconfirmation(r, by_id):
    d = r.get("disconfirmation")
    L = ["", "### Disconfirmation (before the judge)"]
    if not d:
        return L + ["Not run."]
    if d.get("skipped"):
        return L + [f"Not run: skipped ({_inline(d['skipped'])})."]
    targets, queries, outcomes = d.get("targets") or [], d.get("queries") or [], d.get("outcomes") or []
    L.append(f"Ran: {disconfirmation_summary(r)}; {len(queries)} queries"
             + (f"; {d['seconds']} s" if d.get("seconds") is not None else "") + ".")
    L += ["", "Targets:"] + ([f"- {_target(t, by_id)}" for t in targets] or ["- none"])
    L += ["", "Queries run:"] + ([f"- {_query(q)}" for q in queries] or ["- none"])
    L += [""]
    if outcomes:
        L += _table(("Target", "Outcome", "Why", "New receipts"),
                    [(_tid(o.get("target")), o.get("outcome", "?"), o.get("why"),
                      [_receipt_state(i, by_id) for i in o.get("evidence") or []]) for o in outcomes])
    else:
        L.append("No outcome recorded.")
    return L


def views_summary(verdict):
    views = [x for x in (verdict or {}).get("views") or [] if isinstance(x, dict)]
    if not views:
        return "not run"
    colors = [x.get("overall") for x in views if x.get("overall")]
    if not colors:
        return f"0/{len(views)} views answered"
    top = max(dict.fromkeys(colors), key=colors.count)
    n = colors.count(top)
    final = (verdict or {}).get("overall", "?")
    if n * 2 <= len(views) and len(set(colors)) > 1:
        return ("no majority: " + ", ".join(f"{c} {colors.count(c)}" for c in dict.fromkeys(colors))
                + f" of {len(views)} views; final {final}")
    return f"{n}/{len(views)} views say {top}; final {final}"


def _views(v):
    views = [x for x in v.get("views") or [] if isinstance(x, dict)]
    L = ["", "### Judge views"]
    if not views:
        return L + ["Not run (one judge call)."]
    L += [f"Agreement: {views_summary(v)}.", ""]
    return L + _table(("View", "Overall", "Reliability", "Key receipts"),
                      [(x.get("view", "?"), x.get("overall") or "no answer", x.get("reliability"),
                        x.get("key_receipts") or []) for x in views])


def audit_summary(verdict):
    a = (verdict or {}).get("audit")
    if not a:
        return "not run"
    if a.get("skipped"):
        return f"skipped ({_inline(a['skipped'])})"
    kept = a.get("kept_sentences")
    kept = len(kept) if isinstance(kept, (list, tuple)) else kept
    return (f"{'?' if kept is None else kept} sentences kept, {len(a.get('removed') or [])} removed, "
            f"{len(a.get('uncited_flags') or [])} uncited flags")


def _flag_ref(f):
    if not isinstance(f, dict):
        return _inline(f)
    head = _inline(f.get("text") or f.get("flag") or f.get("id") or "?")
    where = " · ".join(_inline(f[k]) for k in ("bucket", "color", "url") if f.get(k))
    why = _inline(f.get("why") or f.get("reason") or "")
    return head + (f" ({where})" if where else "") + (f": {why}" if why else "")


def _audit(v):
    a = v.get("audit")
    L = ["", "### Audit (after the judge)"]
    if not a:
        return L + ["Not run."]
    if a.get("skipped"):
        return L + [f"Not run: skipped ({_inline(a['skipped'])})."]
    cited = a.get("cited_ids") or []
    L.append(f"Ran: {audit_summary(v)}" + (f"; cited receipts {', '.join(map(str, cited))}" if cited else "") + ".")
    removed = a.get("removed") or []
    L += ["", "Removed sentences:"] + ([f"- \"{_inline(x.get('sentence'))}\": {_inline(x.get('why')) or 'no reason given'}"
                                        if isinstance(x, dict) else f"- \"{_inline(x)}\"" for x in removed] or ["- none"])
    L += ["", "Flags without a kept receipt on their url:"] + (
        [f"- {_flag_ref(f)}" for f in a.get("uncited_flags") or []] or ["- none"])
    known = {"kept_sentences", "removed", "cited_ids", "uncited_flags", "skipped"}
    for k, x in a.items():
        if k not in known and x not in (None, "", [], {}):
            items = x if isinstance(x, list) else [x]
            L += ["", f"{k.replace('_', ' ').capitalize()}:"] + [
                f"- {_inline(json.dumps(i, ensure_ascii=False) if isinstance(i, dict) else i)}" for i in items]
    return L


def _steps(v):
    st = ((v or {}).get("stats") or {}).get("steps") or {}
    if not st:
        return []
    return ["- Steps (wall s): " + " · ".join(f"{k} {'not reached' if x is None else x}" for k, x in st.items())]


def render(research, receipts, verdict):
    r, v = research or {}, verdict or {}
    receipts = receipts or []
    by_id = {x.get("id"): x for x in receipts}
    for e in r.get("evidence") or []:
        by_id.setdefault(e.get("id"), e)
    ident, site, stats = r.get("identity") or {}, r.get("site") or {}, r.get("stats") or {}
    title = v.get("product") or ident.get("product") or r.get("slug") or v.get("url") or r.get("url", "")
    L = [f"# Trace: {title}", "",
         f"- URL: {v.get('url') or r.get('url', '')}",
         f"- Color: **{v.get('overall', '?')}**",
         f"- Reliability: {v.get('reliability', '?')} ({_cell(v.get('reliability_why'))})",
         f"- Model: {v.get('model', '?')} · mode: {v.get('mode', '?')} · run id: {v.get('run_id', '?')}",
         f"- Seconds: {v.get('seconds', r.get('seconds', '?'))} (research {r.get('seconds', '?')}) · capped: "
         f"{_yn(stats.get('capped'))} · tool calls: {stats.get('tool_calls', 0)} · {_model_calls(stats, v)}"]
    L += _steps(v)
    L.append(f"- Disconfirmation: {disconfirmation_summary(r)} · judge views: {views_summary(v)} · audit: "
             f"{audit_summary(v)}")
    if v.get("error"):
        L.append(f"- Error: {v['error']} (missing evidence is no signal)")
    L += ["", "## 1 Identity and anchors", ""]
    people = [f"{p.get('name', '')} ({p.get('role', '')})" for p in ident.get("key_people") or []]
    L += _table(("Field", "Value"), [
        ("Product", ident.get("product") or v.get("product")), ("Vendor", ident.get("vendor") or v.get("vendor")),
        ("Legal entity", ident.get("legal_entity")), ("Parent", ident.get("parent") or v.get("parent")),
        ("Former names", ident.get("former_names") or v.get("former_names")), ("Domains", ident.get("domains")),
        ("Key people", people), ("Country", ident.get("country")), ("Languages", ident.get("languages")),
        ("Same-name risks", ident.get("same_name_risks")), ("Notes", ident.get("notes")),
        ("Receipts", ident.get("evidence")), ("Core promise", site.get("core_promise")),
        ("Segment", site.get("segment"))])
    sig = {k: x for k, x in (site.get("signals") or {}).items() if x not in (None, "", [], {})}
    if sig or site.get("pages"):
        L += ["", f"Site: {len(site.get('pages') or [])} pages read; signals: "
                  + (_cell("; ".join(f"{k} {sig[k]}" for k in sig)) if sig else "none")]
    for inc in site.get("inconsistencies") or []:
        L.append(f"- Inconsistency: {_cell(inc.get('text'))}: \"{_cell(inc.get('quote'))}\" ({inc.get('url', '')})")

    L += ["", "## 2 Matrix (24 categories)", ""]
    m = r.get("matrix") or {}
    rows = []
    for k, (name, *_rest) in CATEGORIES.items():
        row = m.get(str(k)) or {}
        rows.append((k, name, row.get("status", "missing"), "; ".join(row.get("queries") or []),
                     row.get("sources") or [], row.get("evidence") or [], row.get("note")))
    L += _table(("#", "Category", "Status", "Queries run", "Sources read", "Evidence", "Note"), rows)

    L += ["", "## 3 Claims ledger", ""]
    claims = {c.get("id"): c for c in site.get("claims") or []}
    checks = {c.get("claim_id"): c for c in r.get("claim_checks") or []}
    crow = []
    for cid in list(claims) + [c for c in checks if c not in claims]:
        c, ck = claims.get(cid, {}), checks.get(cid, {})
        crow.append((cid, c.get("text") or ck.get("note"), ck.get("status", "unchecked"), ck.get("evidence") or [],
                     ck.get("note")))
    L += _table(("Id", "Claim", "Status", "Receipts", "Note"), crow) if crow else ["No site claims recorded."]

    L += ["", "## 4 Decision path"]
    L += _disconfirmation(r, by_id)
    L += _views(v)
    dis = {}
    for o in (r.get("disconfirmation") or {}).get("outcomes") or []:
        dis.setdefault(_tid(o.get("target")), o)
    for key, label in BUCKETS:
        flags = v.get(key) or []
        L += ["", f"### {label} ({len(flags)})"]
        if not flags:
            L.append("None.")
        for f in flags:
            ids = f.get("receipts") or []
            source = next((by_id[i] for i in ids if by_id.get(i, {}).get("kept")), f)
            quote = _inline(source.get("quote"))
            L += [f"- **{_inline(f.get('text'))}** (category {f.get('category', '?')}, {f.get('severity', '?')}, "
                  f"basis {f.get('basis', '?')}, corroboration {f.get('corroboration', '?')})",
                  f"  - URL: {source.get('url', '')}",
                  f"  - Quote: \"{quote}\"",
                  f"  - Date: {source.get('date') or 'undated'} · author: "
                  f"{source.get('author_type') or source.get('author', '?')}"]
            for rid in ids:
                rc = by_id.get(rid)
                if rc is None:
                    L.append(f"  - Receipt {rid}: not in the receipts list")
                    continue
                L.append(f"  - Receipt {rid}: {rc.get('url', '')} · {rc.get('date') or 'undated'} · "
                         f"{rc.get('author_type', '?')} · checks: {_checks(rc)}")
                if _inline(rc.get("quote")) != quote:
                    L.append(f"    - Quote: \"{_inline(rc.get('quote'))}\"")
                if rid in dis:
                    o = dis[rid]
                    L.append(f"    - Disconfirmation: {o.get('outcome', '?')} ({_inline(o.get('why')) or 'no reason given'})"
                             + (f"; new receipts {', '.join(o.get('evidence'))}" if o.get("evidence") else ""))
    L += ["", "### Guard log"] + ([f"- {g}" for g in v.get("guards") or []] or ["No guard fired."])
    L += ["", "### Reasoning", "", str(v.get("reasoning") or "–")]
    L += _audit(v)
    L += ["", "### Would flip", "", str(v.get("would_flip") or "–")]
    L += ["", "### Blind spots"] + ([f"- {b}" for b in v.get("blind_spots") or []] or ["None listed."])
    L += ["", "### Vendor questions"] + ([f"{i}. {q}" for i, q in enumerate(v.get("vendor_questions") or [], 1)]
                                         or ["None listed."])
    L += ["", "### Buyer next step", "", str(v.get("buyer_next_step") or "–")]

    L += ["", "## 5 Rejected receipts, failed sources, limits", ""]
    rej = [x for x in receipts if not x.get("kept")]
    if rej:
        L += _table(("Id", "Finding", "URL", "Why rejected", "Checks"),
                    [(x.get("id"), x.get("text"), x.get("url"), x.get("why"), _checks(x)) for x in rej])
    else:
        L.append("No receipt was rejected.")
    extra = [x for x in v.get("rejected") or [] if x.get("url") not in {y.get("url") for y in rej}]
    for x in extra:
        L.append(f"- Rejected by the judge: {_cell(x.get('text'))} ({x.get('url', '')}): {_cell(x.get('why'))}")
    L += ["", "### Sources failed (no signal, never a negative)"]
    failed = r.get("sources_failed") or v.get("sources_failed") or []
    L += [f"- {_cell(s.get('what'))}: {_cell(s.get('error'))}" for s in failed] or ["None."]
    openrows = [k for k in map(str, CATEGORIES) if (m.get(k) or {}).get("status", "open") == "open"]
    L += ["", "### Limits",
          f"- Capped at the wall cap or a call cap: {_yn(stats.get('capped'))}",
          f"- Rounds {len(r.get('rounds') or [])}, tool calls {stats.get('tool_calls', 0)}, research model calls "
          f"{stats.get('llm_calls', 0)}, sources read {len(r.get('sources') or {})}, failed {len(failed)}",
          f"- Categories still open: {', '.join(openrows) if openrows else 'none'}",
          f"- Receipts kept {sum(1 for x in receipts if x.get('kept'))} of {len(receipts)}"]

    L += ["", "## Research rounds", ""]
    rr = [(x.get("n"), "; ".join(f"{a.get('tool', '?')}({_cell(_args(a.get('args')), 80)})"
                                 + (f" [cat {a['category']}]" if a.get("category") else "")
                                 for a in x.get("actions") or []),
           x.get("new_evidence", 0), x.get("seconds", "")) for x in r.get("rounds") or []]
    L += _table(("Round", "Actions", "New evidence", "Seconds"), rr) if rr else ["No rounds recorded."]

    L += ["", "## Appendix: kept receipts"]
    kept = [x for x in receipts if x.get("kept")]
    if not kept:
        L += ["", "None."]
    for x in kept:
        L += ["", f"### {x.get('id')}: {_cell(x.get('text'))}",
              f"- URL: {x.get('url', '')}",
              f"- Date: {x.get('date') or 'undated'} · author type: {x.get('author_type', '?')} · category "
              f"{x.get('category', '?')} · severity {x.get('severity', '?')} · polarity {x.get('polarity', '?')}",
              f"- Domain: {x.get('domain', '?')} · independent: {_yn(x.get('independent'))}"
              + (f" · repost of {x['dup_of']}" if x.get("dup_of") else ""),
              f"- Checks: {_checks(x)}", f"- Source: {_cell(x.get('source_title'))}"]
        if x.get("hypothesis"):
            L.append(f"- Hypothesis: {_inline(x['hypothesis'])}")
        L.append("")
        L += _quote_block(x.get("quote"))
    return "\n".join(L) + "\n"
