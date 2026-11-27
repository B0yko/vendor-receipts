"""Self-audit (lane L6, plan step 9): code only, no model. The pipeline runs it after the judge.

    from app.check.audit import audit
    verdict = audit(verdict, receipts)      # a copy: reasoning audited, verdict["audit"] set; never raises

Reasoning: split into sentences at . ! ? and the ellipsis char. A citation right after the terminator ("fined. [e3]")
stays with its sentence; a citation that opens a lowercase continuation ("[e3] shows ...") goes to the next one.
Abbreviations (e.g., Inc.), initials and decimals do not split; blank lines and bullet lines do. Citations:
[e3], [e3, e7], [e3; e7], [e3-e5] and an id-only parenthesis (e3). Each sentence must cite at least one KEPT
receipt. A sentence that cites none, or only rejected or unknown ids, is removed, with one exception: the final
sentence may stay uncited when it is short advice (at most ADVICE_WORDS words: "a buyer should ...", "ask ...",
"next step ...") and cites no id at all; it is logged as advice. If no cited sentence would remain, the original
reasoning is kept, nothing is removed, and the note says "audit would empty the reasoning".
Flags: every flag in red/yellow/green_flags needs a kept receipt among its receipts whose url equals the flag url
(modulo scheme, www., trailing slash, fragment: receipts.url_key). Otherwise it is logged in uncited_flags. Flags
are never dropped here (the guards did that).
verdict["audit"] = {
  "kept_sentences": n, "total_sentences": n, "removed": [{"sentence", "why"}], "advice": [sentence],
  "cited_ids": [kept ids cited by the audited reasoning, first appearance order],
  "rejected_ids": [cited ids of rejected receipts], "unknown_ids": [cited ids with no receipt],
  "uncited_flags": [{"bucket", "text", "url", "receipts", "why"}],
  "would_remove": [{"sentence", "why"}] (only when the fallback kept the original), "note": "", "summary": "..."}
On an internal error: the reasoning is unchanged and audit = {"error": "...", ...the same keys, empty}.
"""
import re

from app.check import receipts as rc

BUCKETS = ("red_flags", "yellow_flags", "green_flags")
ADVICE_WORDS = 45
ADVICE_CHARS = 350
EMPTY_NOTE = "audit would empty the reasoning"
ABBREV = {"e.g", "i.e", "eg", "ie", "cf", "vs", "approx", "ca", "inc", "ltd", "co", "corp", "llc", "plc", "gmbh",
          "mr", "mrs", "ms", "dr", "prof", "st", "no", "nr", "fig", "vol", "dept", "est", "u.s", "u.k", "e.u", "jan",
          "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}

_ID_LIST = r"e\d+(?:\s*(?:[,;/&]|and|to|-|\u2013|\u2014)\s*e?\d+)*"
_BRACKET = re.compile(r"\[([^\[\]\n]{1,200})\]")
_PAREN = re.compile(r"\(\s*(" + _ID_LIST + r")\s*\)", re.I)
_ID = re.compile(r"(?<![a-z0-9])e0*(\d+)(?![a-z0-9])", re.I)
_RANGE = re.compile(r"(?<![a-z0-9])e0*(\d+)\s*(?:-|\u2013|\u2014|to)\s*e?0*(\d+)(?![a-z0-9])", re.I)
_HARD = re.compile(r"\n[ \t]*\n|\n(?=[ \t]*(?:[-*\u2022\u2013]|\d{1,2}[.)])[ \t])")
_END = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019\u00bb)]*")
_CITES = re.compile(r"(?:[ \t]*(?:\[[^\[\]\n]{1,200}\]|\(\s*" + _ID_LIST + r"\s*\)))+", re.I)
_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_ADVICE = re.compile(
    r"\b(?:buyers?|procurement|purchasers?|you|we)\b[^.!?]{0,60}?\b(?:should|must|needs? to|ought to|"
    r"is advised to|would do well to)\b"
    r"|\b(?:next step|recommend(?:ed|s|ation)?|before (?:signing|shortlisting|buying|purchasing|committing|renewing|"
    r"a shortlist|a decision|a contract))\b"
    r"|^\W*(?:ask|request|verify|check|confirm|obtain|demand|require|insist|get|shortlist|do not shortlist|"
    r"don't shortlist|treat|plan|negotiate)\b", re.I)


def _norm(x):
    s = str(x).strip().lower()
    m = re.fullmatch(r"e0*(\d+)", s)
    return f"e{int(m.group(1))}" if m else s


def _ids(inner):
    out = []
    for m in _RANGE.finditer(inner):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b <= a + 20:
            out += [f"e{i}" for i in range(a, b + 1)]
    out += [f"e{int(m.group(1))}" for m in _ID.finditer(inner)]
    return list(dict.fromkeys(out))


def cited(sentence):
    """-> the normalised receipt ids a sentence cites ([e3], [e3, e7], [e3-e5], (e3)), first appearance order."""
    out = []
    for m in _BRACKET.finditer(sentence):
        out += _ids(m.group(1))
    for m in _PAREN.finditer(sentence):
        out += _ids(m.group(1))
    return list(dict.fromkeys(out))


def _abbrev(text, pos):
    m = _WORD_BEFORE.search(text[max(0, pos - 30):pos])
    if not m:
        return False
    w = m.group(1).lower().rstrip(".")
    return len(w) == 1 or w in ABBREV


def sentences(text):
    """-> [(start, end)]: the sentence spans of text (whitespace stripped, empty pieces dropped)."""
    cuts = {0, len(text)}
    cuts.update(m.start() for m in _HARD.finditer(text))
    for m in _END.finditer(text):
        t_start, t_end = m.start(), m.end()
        c = _CITES.match(text, t_end)
        c_end = c.end() if c else t_end
        rest = text[c_end:]
        if rest and not rest[0].isspace():
            continue                                   # 2.5, acme.example, e.g.x: not a sentence end
        nxt = rest.lstrip()
        if not nxt:
            cuts.add(c_end)
            continue
        if nxt[0].islower():
            if c_end > t_end:
                cuts.add(t_end)                        # "[e3] shows ...": the citation opens the next sentence
            continue
        if m.group().rstrip("\"'\u201d\u2019\u00bb)") == "." and _abbrev(text, t_start):
            continue
        cuts.add(c_end)
    spans, cs = [], sorted(cuts)
    for a, b in zip(cs, cs[1:]):
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        s = seg.strip()
        if s and any(ch.isalnum() for ch in s):
            spans.append((a + lead, a + lead + len(s)))
    return spans


def is_advice(sentence):
    s = sentence.strip()
    return (len(s) <= ADVICE_CHARS and len(s.split()) <= ADVICE_WORDS and _ADVICE.search(s) is not None)


def _status(receipts):
    """-> {normalised id: {"id", "kept", "url"}}; a kept receipt wins over a rejected one with the same id."""
    out = {}
    for r in receipts if isinstance(receipts, list) else []:
        if not isinstance(r, dict) or r.get("id") in (None, ""):
            continue
        k = _norm(r["id"])
        rec = {"id": str(r["id"]), "kept": r.get("kept") is True, "url": r.get("url") if isinstance(r.get("url"), str)
               else ""}
        if k not in out or (rec["kept"] and not out[k]["kept"]):
            out[k] = rec
    return out


def _why(rej, unk, status):
    if not rej and not unk:
        return "cites no receipt"
    r = ", ".join(status[x]["id"] for x in rej)
    u = ", ".join(unk)
    if rej and not unk:
        return f"cites only rejected receipts: {r}"
    if unk and not rej:
        return f"cites only unknown ids: {u}"
    return f"cites no kept receipt (rejected: {r}; unknown: {u})"


def _sep(text, end):
    ws = re.match(r"\s*", text[end:]).group()
    n = ws.count("\n")
    return "\n" * min(n, 2) if n else " "


def audit_reasoning(text, status):
    """-> (reasoning, report) for a reasoning text and the receipt status map."""
    if isinstance(text, list):
        text = " ".join(str(x) for x in text if x)
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    rep = {"kept_sentences": 0, "total_sentences": 0, "removed": [], "advice": [], "cited_ids": [],
           "rejected_ids": [], "unknown_ids": [], "note": ""}
    spans = sentences(text)
    rep["total_sentences"] = len(spans)
    if not spans:
        rep["note"] = "no reasoning"
        return text, rep
    keep, removed, rejected, unknown = [], [], [], []
    for i, (a, b) in enumerate(spans):
        s = text[a:b]
        ids = cited(s)
        good = [x for x in ids if x in status and status[x]["kept"]]
        rej = [x for x in ids if x in status and not status[x]["kept"]]
        unk = [x for x in ids if x not in status]
        rejected += rej
        unknown += unk
        if good:
            keep.append((a, b, good))
        elif not ids and i == len(spans) - 1 and is_advice(s):
            keep.append((a, b, []))
            rep["advice"].append(s)
        else:
            removed.append({"sentence": s, "why": _why(rej, unk, status)})
    rep["rejected_ids"] = [status[x]["id"] for x in dict.fromkeys(rejected)]
    rep["unknown_ids"] = list(dict.fromkeys(unknown))
    if removed and not any(g for *_, g in keep):
        rep.update(kept_sentences=len(spans), would_remove=removed, note=EMPTY_NOTE)
        return text, rep
    rep["kept_sentences"] = len(keep)
    rep["removed"] = removed
    rep["cited_ids"] = [status[x]["id"] for x in dict.fromkeys(x for *_, g in keep for x in g)]
    if not removed:
        return text, rep
    out, prev = [], None
    for a, b, _ in keep:
        if prev is not None:
            out.append(_sep(text, prev))
        out.append(text[a:b])
        prev = b
    return "".join(out), rep


def _flag_ids(x):
    if isinstance(x, str):
        return [f"e{int(m.group(1))}" for m in _ID.finditer(x)]
    if isinstance(x, list):
        return list(dict.fromkeys(_norm(i) for i in x if isinstance(i, (str, int)) and str(i).strip()))
    return []


def audit_flags(verdict, status):
    """-> [{"bucket", "text", "url", "receipts", "why"}] for flags without a kept receipt on their url."""
    out = []
    for b in BUCKETS:
        flags = verdict.get(b)
        for f in flags if isinstance(flags, list) else []:
            if not isinstance(f, dict):
                out.append({"bucket": b, "text": "", "url": "", "receipts": [], "why": "flag is not an object"})
                continue
            ids = _flag_ids(f.get("receipts"))
            url = f.get("url") if isinstance(f.get("url"), str) else ""
            key = rc.url_key(url)
            kept = [status[x] for x in ids if x in status and status[x]["kept"]]
            if key and any(rc.url_key(k["url"]) == key for k in kept):
                continue
            if not ids:
                why = "no receipt ids"
            elif not kept:
                rej = [status[x]["id"] for x in ids if x in status]
                unk = [x for x in ids if x not in status]
                parts = (["rejected: " + ", ".join(rej)] if rej else []) + (["unknown: " + ", ".join(unk)] if unk else [])
                why = f"no kept receipt among its receipts ({'; '.join(parts)})"
            elif not key:
                why = "flag has no url"
            else:
                why = "no kept receipt on the flag url (kept receipts on: " + ", ".join(
                    dict.fromkeys(k["url"] or "(no url)" for k in kept))[:300] + ")"
            out.append({"bucket": b, "text": str(f.get("text") or "")[:300], "url": url,
                        "receipts": [str(i) for i in f.get("receipts")] if isinstance(f.get("receipts"), list)
                        else ids, "why": why})
    return out


def _summary(rep):
    s = (f"audit: {rep['kept_sentences']} of {rep['total_sentences']} reasoning sentences kept, "
         f"{len(rep['removed'])} removed")
    if rep["advice"]:
        s += ", uncited advice kept"
    s += f"; {len(rep['uncited_flags'])} flag(s) without a kept receipt on their url"
    return s + (f"; {rep['note']}" if rep["note"] else "")


def audit(verdict, receipts):
    """-> a copy of verdict with the audited reasoning and verdict["audit"]. Never raises; a non-dict is returned
    as it is."""
    if not isinstance(verdict, dict):
        return verdict
    v = dict(verdict)
    try:
        status = _status(receipts)
        v["reasoning"], rep = audit_reasoning(verdict.get("reasoning"), status)
        rep["uncited_flags"] = audit_flags(verdict, status)
        rep["summary"] = _summary(rep)
        v["audit"] = rep
    except Exception as e:  # noqa: BLE001 - the audit never breaks a verdict
        v["reasoning"] = verdict.get("reasoning")
        v["audit"] = {"error": f"{type(e).__name__}: {str(e)[:160]}", "kept_sentences": None, "removed": [],
                      "advice": [], "cited_ids": [], "uncited_flags": [], "note": "audit failed; reasoning unchanged",
                      "summary": f"audit failed ({type(e).__name__}); reasoning unchanged"}
    return v
