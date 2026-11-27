"""The four guards in code (lane L3; the rules: app/ledger.py GUARDS), because a model can break them.

apply(verdict, research, receipts) -> verdict, with one line per change in verdict["guards"]. Order:
  G4 a flag with no kept receipt is dropped (a failed source is no signal);
  G2 a flag whose kept receipts are all user_review is dropped, its text goes to blind_spots as "users report ...";
  G3 a red flag of category 14 (site quality) moves to yellow;
  D  (color rule: a Red survived disconfirmation) a red flag whose kept receipts are all targets refuted in
     research["disconfirmation"] moves to yellow; a surviving red flag on a weakened target caps reliability at
     medium. Targets name receipt ids ("e3", {"id": "e3"} or text that cites [e3]); a target without ids is a no-op;
  G1 a red flag whose kept receipts are all vendor_self (a claim, never proof) moves to yellow, unless one of them
     backs a contradicted claim check (a receipted contradiction on the vendor's own site);
     Red needs a red flag with an external receipt (independent and not vendor_self) or a vendor statement against
     its own interest (vendor_against_interest, any domain), or a contradicted claim check backed by a kept receipt,
     else Yellow; Red with no red flag left -> Yellow; Green with any surviving red flag -> Yellow;
  G3 a Yellow resting only on category-14 flags becomes Green when a kept green flag shows the product alive
     (categories 4-7), else stays Yellow, logged "too little evidence".
`call` always equals the final `overall`.
"""
import json, re

from app import ledger

BUCKETS = ("red_flags", "yellow_flags", "green_flags")
SITE_QUALITY = 14
ALIVE = (4, 5, 6, 7)


def _short(text, n=80):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _color(bucket):
    return bucket.split("_")[0]


def _users_report(text, kept):
    t = str(text or "").strip().rstrip(".")
    if t.lower().startswith("users report"):
        t = t[len("users report"):].strip()
    elif t[:1].isupper() and not t[1:2].isupper():
        t = t[0].lower() + t[1:]
    dates = sorted(d for d in (r.get("date", "") for r in kept) if d)
    period = f", {dates[0]}" + (f" to {dates[-1]}" if dates[-1] != dates[0] else "") if dates else ""
    return f"users report {t} ({len(kept)} source{'s' if len(kept) != 1 else ''}{period}; not confirmed elsewhere)"


def _target_ids(t):
    return set(re.findall(r"\be\d+\b", t if isinstance(t, str) else json.dumps(t)))


def disconfirmed(research, outcome):
    """Receipt ids named by the disconfirmation targets with this outcome (survived, weakened, refuted)."""
    d = research.get("disconfirmation")
    outs = d.get("outcomes") if isinstance(d, dict) else None
    return set().union(*[_target_ids(o.get("target")) for o in outs or []
                         if isinstance(o, dict) and o.get("outcome") == outcome])


def apply(verdict, research, receipts):
    by_id = {r["id"]: r for r in receipts if r.get("id")}
    log = verdict.setdefault("guards", [])
    blind = verdict.setdefault("blind_spots", [])

    def kept(f):
        return [by_id[i] for i in f.get("receipts", []) if i in by_id and by_id[i].get("kept")]

    def red_basis(f):
        """An external receipt or a vendor statement against its own interest (decision L3-2)."""
        return any((r.get("independent") and r.get("author_type") != "vendor_self")
                   or r.get("author_type") == "vendor_against_interest" for r in kept(f))

    for b in BUCKETS:
        survivors = []
        for f in verdict.get(b, []):
            k = kept(f)
            if not k:
                log.append(f"G4 dropped {_color(b)} flag '{_short(f.get('text'))}': no kept receipt")
            elif all(r.get("author_type") == "user_review" for r in k):
                log.append(f"G2 dropped {_color(b)} flag '{_short(f.get('text'))}': user reviews only, moved to "
                           f"blind spots")
                blind.append(_users_report(f.get("text"), k))
            else:
                survivors.append(f)
        verdict[b] = survivors

    moved = [f for f in verdict["red_flags"] if f.get("category") == SITE_QUALITY]
    for f in moved:
        log.append(f"G3 moved red flag '{_short(f.get('text'))}' to yellow: site quality never makes a Red")
    verdict["red_flags"] = [f for f in verdict["red_flags"] if f.get("category") != SITE_QUALITY]
    verdict["yellow_flags"] = moved + verdict["yellow_flags"]

    refuted, weakened = disconfirmed(research, "refuted"), disconfirmed(research, "weakened")
    gone = [f for f in verdict["red_flags"] if refuted and {r["id"] for r in kept(f)} <= refuted]
    for f in gone:
        log.append(f"D moved red flag '{_short(f.get('text'))}' to yellow: its finding was refuted by disconfirmation")
    verdict["red_flags"] = [f for f in verdict["red_flags"] if not any(f is g for g in gone)]
    verdict["yellow_flags"] = gone + verdict["yellow_flags"]
    shaky = [f for f in verdict["red_flags"] if weakened & {r["id"] for r in kept(f)}]
    if shaky and verdict.get("reliability") == "high":
        verdict["reliability"] = "medium"
        log.append(f"D reliability high -> medium: red flag '{_short(shaky[0].get('text'))}' was weakened by "
                   f"disconfirmation")

    contradicted = {i for c in research.get("claim_checks", []) if c.get("status") == "contradicted"
                    for i in c.get("evidence", []) if by_id.get(i, {}).get("kept")}
    claims_only = [f for f in verdict["red_flags"] if all(r.get("author_type") == "vendor_self" for r in kept(f))
                   and not contradicted & set(f.get("receipts", []))]
    for f in claims_only:
        log.append(f"G1 moved red flag '{_short(f.get('text'))}' to yellow: the vendor's own claims only, never proof")
    verdict["red_flags"] = [f for f in verdict["red_flags"] if not any(f is m for m in claims_only)]
    verdict["yellow_flags"] = claims_only + verdict["yellow_flags"]

    red = verdict["red_flags"]
    if verdict["overall"] == "Red":
        if not red:
            verdict["overall"] = "Yellow"
            log.append("G1 Red -> Yellow: no red flag left")
        elif not any(red_basis(f) for f in red) and not contradicted:
            verdict["overall"] = "Yellow"
            log.append("G1 Red -> Yellow: no red flag has an external receipt or a vendor statement against its own "
                       "interest, and no claim check is contradicted by a kept receipt")
    elif verdict["overall"] == "Green" and red:
        verdict["overall"] = "Yellow"
        log.append(f"G1 Green -> Yellow: red flag '{_short(red[0].get('text'))}' survived the guards")

    negatives = verdict["red_flags"] + verdict["yellow_flags"]
    if verdict["overall"] == "Yellow" and negatives and all(f.get("category") == SITE_QUALITY for f in negatives):
        alive = [f for f in verdict["green_flags"] if f.get("category") in ALIVE]
        if alive:
            verdict["overall"] = "Green"
            log.append(f"G3 Yellow -> Green: only site-quality flags against it, alive: '{_short(alive[0].get('text'))}'")
        else:
            log.append("G3 Yellow kept: only site-quality flags against it and no green flag shows the product alive, "
                       "too little evidence")
    if verdict["overall"] not in ledger.COLORS:
        verdict["overall"] = "Yellow"
    verdict["call"] = verdict["overall"]
    return verdict
