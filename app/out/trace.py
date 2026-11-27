"""trace.md: the research trace of one product, for an LLM-as-judge and a human auditor (lane L5).

    md = trace.render(research, receipts, verdict)   # Markdown string; never raises on missing keys

Sections: header (url, color, reliability and why, model, run id, seconds, capped); 1 identity and anchors with
receipt ids; 2 the matrix, all 24 rows; 3 claims ledger; 4 decision path (flags per bucket with their receipts, guard
log, reasoning, would flip, blind spots, vendor questions, buyer next step; every flag shows its url and full quote,
whitespace collapsed, never cut, so a checker can find it on the page); 5 rejected receipts, failed sources and
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
    c = rc.get("checks") or {}
    if not c:
        return "not checked"
    return " ".join(f"{k} {'yes' if c.get(k) else 'no'}" for k in CHECKS if k in c)


def _quote_block(q):
    lines = str(q or "").strip().splitlines() or [""]
    return ["> " + ln for ln in lines]


def _yn(b):
    return "yes" if b else "no"


def _args(a):
    if isinstance(a, dict):
        return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in a.items())
    return a


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
         f"{_yn(stats.get('capped'))} · tool calls: {stats.get('tool_calls', 0)} · model calls: {stats.get('llm_calls', 0)}"]
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
    for key, label in BUCKETS:
        flags = v.get(key) or []
        L += ["", f"### {label} ({len(flags)})"]
        if not flags:
            L.append("None.")
        for f in flags:
            ids = f.get("receipts") or []
            first = by_id.get(ids[0], {}) if ids else {}
            quote = _inline(first.get("quote") or f.get("quote"))
            L += [f"- **{_inline(f.get('text'))}** (category {f.get('category', '?')}, {f.get('severity', '?')}, "
                  f"basis {f.get('basis', '?')}, corroboration {f.get('corroboration', '?')})",
                  f"  - URL: {f.get('url') or first.get('url', '')}",
                  f"  - Quote: \"{quote}\"",
                  f"  - Date: {f.get('date') or first.get('date') or 'undated'} · author: "
                  f"{f.get('author') or first.get('author_type', '?')}"]
            for rid in ids:
                rc = by_id.get(rid)
                if rc is None:
                    L.append(f"  - Receipt {rid}: not in the receipts list")
                    continue
                L.append(f"  - Receipt {rid}: {rc.get('url', '')} · {rc.get('date') or 'undated'} · "
                         f"{rc.get('author_type', '?')} · checks: {_checks(rc)}")
                if _inline(rc.get("quote")) != quote:
                    L.append(f"    - Quote: \"{_inline(rc.get('quote'))}\"")
    L += ["", "### Guard log"] + ([f"- {g}" for g in v.get("guards") or []] or ["No guard fired."])
    L += ["", "### Reasoning", "", str(v.get("reasoning") or "–")]
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
          f"- Rounds {len(r.get('rounds') or [])}, tool calls {stats.get('tool_calls', 0)}, model calls "
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
              f"- Checks: {_checks(x)}", f"- Source: {_cell(x.get('source_title'))}", ""]
        L += _quote_block(x.get("quote"))
    return "\n".join(L) + "\n"
