"""The shared contract of Vendor Receipts (Lead-owned; a change needs a STATE.md decision line).

Plain JSON-able dicts flow through the pipeline:
  app.research.agent.research(url, deadline) -> Research      (lanes L1 tools, L2 research brain: app/research/)
  app.check.receipts.check(research)          -> [Receipt]     (lane L6: app/check/)
  app.judge.judge.judge(research, receipts)   -> Verdict       (lane L3: app/judge/, the four guards in code)
  app.out.*                                   -> JSONL + CSV + trace.md (lane L3: app/out/)
Nothing in app/ reads data/ (labels live there; only evalkit/ reads them). The agent's knowledge is the brief, the
matrix below, the color rule and general buyer practice.

Evidence (written by the research loop; one finding = one verbatim quote from one source):
  {"id": "e12", "category": 9, "text": "the finding in one plain sentence", "url": "https://...",
   "quote": "verbatim text copied from the retrieved source text", "date": "YYYY-MM-DD | YYYY-MM | YYYY | ''"
   (the event date, never the fetch date), "author_type": AUTHOR_TYPES, "severity": SEVERITIES,
   "polarity": POLARITIES, "promise_link": PROMISE_LINKS, "hypothesis": "what was being tested",
   "source_title": "...", "round": 2}

Receipt = Evidence + {"checks": {"resolves": bool, "same_company": bool, "says_it": bool, "dated": bool},
   "kept": bool, "why": "why rejected ('' when kept)", "domain": "registrable domain of url",
   "independent": bool (not on the vendor's own domains), "dup_of": "e3" | None (a repost of the same text)}
   kept = resolves and same_company and says_it. `dated` is recorded and feeds reliability; it never rejects alone.
   A rejected receipt stays in the trace with its reason and is never used by the judge.

Research (what app.research.agent.research returns; every key present, see new_research):
  {"url", "slug", "started" (ISO), "seconds",
   "identity": {"product", "vendor", "legal_entity", "parent", "former_names": [], "domains": [], "key_people":
                [{"name", "role"}], "country", "languages": [], "same_name_risks": [], "notes", "evidence": ["e1"]},
   "site": {"core_promise", "segment", "claims": [{"id": "c1", "text", "kind", "quote", "url"}],
            "inconsistencies": [{"text", "quote", "url"}], "pages": [{"url", "status", "chars", "kind"}],
            "signals": {"copyright_year", "generator", "notes"}},
   "matrix": {"1": {"status": STATUSES, "queries": ["..."], "sources": ["url"], "evidence": ["e3"],
                    "note": "what was found, or why not applicable"}, ..., "24": {...}},
   "claim_checks": [{"claim_id": "c1", "status": CLAIM_STATUSES, "evidence": ["e5"], "note"}],
   "evidence": [Evidence],
   "sources": {"https://...": {"text", "kind": "search|extract|fetch|wayback|tool", "title", "published_date",
               "fetched_at"}},       # every retrieved text, by URL: the resolves check reads quotes from here
   "sources_failed": [{"what": "url or query", "error": "..."}],
   "rounds": [{"n": 1, "actions": [{"tool", "args", "category", "hypothesis"}], "new_evidence": 7, "seconds": 41.2}],
   "stats": {"tool_calls": 0, "llm_calls": 0, "capped": False},
   "disconfirmation": {"targets", "queries", "outcomes": [{"target", "outcome": "survived|weakened|refuted", "why",
                       "evidence"}], "seconds"} | {"skipped": "..."}   (added by app/check/disconfirm.py, optional)}
  deadline arguments everywhere are time.time() values.

Flag (in a Verdict): {"text", "category", "severity", "url", "quote", "date", "author", "corroboration": "one |
   independent", "basis": BASES, "receipts": ["e3", "e7"]}  (url, quote, date, author come from the first receipt)

Verdict (one JSON line per product; the CSV mirrors Stackgini's columns):
  {"url", "product", "vendor", "parent", "former_names": [], "overall": COLORS, "call": same as overall,
   "reliability": RELIABILITY, "reliability_why", "green_flags": [Flag], "yellow_flags": [Flag], "red_flags": [Flag],
   "rejected": [{"text", "url", "why"}], "reasoning", "blind_spots": [], "buyer_next_step", "vendor_questions": [3],
   "would_flip", "coverage": {"1": status, ...}, "sources_failed": [], "guards": ["guard log lines"],
   "reasons": [{"text", "url", "quote"}] (every kept flag, red then yellow then green; for tools/eval.py),
   "model", "run_id", "trace": "runs/<run_id>/<slug>/trace.md", "seconds", "error": None | "...",
   "mode": "live" | "replay" | "fake"}
"""
import re
import urllib.parse

# id -> (name, group, the buyer's question, where to look). The research plan walks all 24; the trace shows all 24.
CATEGORIES = {
    1: ("Identity", "A Who is it", "Which product, which vendor, any same-name confusion?",
        "product page, imprint, company record, registers (through search results)"),
    2: ("Ownership, deals, finances", "A Who is it",
        "Who owns it now, how often was it sold, what does the owner do after a deal (layoffs, price rises, holding "
        "pattern), funding, insolvency?", "press, deal releases, the owner's pages"),
    3: ("Names, licences, portfolio", "A Who is it",
        "What exactly is bought under this name (renames, bundles, editions, SKUs)?", "vendor docs, licensing pages, press"),
    4: ("Lifecycle", "A Who is it",
        "Is it developed, superseded or ending (legacy page, end of life, last release, frozen after a deal)?",
        "vendor docs, lifecycle and end-of-life pages, news"),
    5: ("Pulse", "B Substance", "Is anyone still building it?",
        "dated releases, changelog, commits, package registries, app stores"),
    6: ("Scale and team", "B Substance", "Does the size fit the claims; bus factor (one maintainer, side project)?",
        "company record, contributors, team page, search"),
    7: ("Customers and social proof", "B Substance",
        "Are the customers real (customers naming the vendor on their own sites, case studies, job ads) or partner "
        "badges and unconfirmed logos?", "search outside the vendor domain"),
    8: ("Claim verification", "B Substance",
        "Do the features exist in the docs, integrations in partner marketplaces, certifications in the certifier's "
        "register; are numbers consistent across pages and over time; are analyst claims worded exactly?",
        "docs, partner marketplaces, certifier registers, archived pages"),
    9: ("Regulators", "C Risk", "Any rulings, fines or orders (consumer protection, data protection, securities, "
        "competition)?", "regulator sites, press, news search"),
    10: ("Litigation", "C Risk", "Class actions, settlements, claims about deceptive marketing?", "court reports, press, news"),
    11: ("Security incidents", "C Risk",
         "Breaches, supply-chain compromise, critical advisories; and how were they disclosed (status page only, or a "
         "full public write-up)?", "status page, security press, advisories (CVE, vendor advisories)"),
    12: ("Ethics, sanctions, geopolitics", "C Risk",
         "Conduct against the product's promise or an EU buyer's interest (operating in sanctioned markets without "
         "disclosure, selling user data), jurisdiction moves?", "research lists, press, sanctions lists"),
    13: ("Governance and legal entity", "C Risk",
         "Imprint, entity in a register, address type (virtual office), privacy policy and terms as real documents, a "
         "data processing agreement, the certifications the segment expects (ISO 27001, SOC 2, BSI C5, TISAX, HIPAA, "
         "FedRAMP), certificate numbers verifiable?", "imprint, legal pages, trust centre, certifier registers"),
    14: ("Site quality", "D Taste",
         "Design age, copyright year, dead links (soft-404 aware), empty help centre, site-builder or generated-copy "
         "markers, AI imagery, screenshots that are not this product, conflicting dates on one page? A prior only: it "
         "never moves the color by itself; it tells the loop where to dig (5-8).", "the vendor site itself"),
    15: ("Founders' history", "D Taste", "Earlier companies, failures, controversies; a founder quoting himself as a "
         "testimonial?", "press, company records through search, founder pages"),
    16: ("Terms, privacy and their changes", "E Extensions",
         "What may the vendor do with customer data (AI training, selling, sharing, subprocessors, unilateral changes) "
         "in the current text, and how did it change (Wayback snapshots about 6, 12 and 24 months ago against today, "
         "compared by meaning)? Any user or press backlash? An open announcement with an opt-out lowers severity; "
         "rewording alone does not count.", "terms and privacy pages, Wayback snapshots, news"),
    17: ("Hosting and data residency", "E Extensions",
         "Where does the app really run (DNS of the app domain) against 'EU hosting' or sovereignty claims; the "
         "subprocessor list?", "DNS, subprocessor page, DPA"),
    18: ("Technical footprint", "E Extensions",
         "Does the product exist technically (app, login, api and status subdomains, certificate history, API docs, "
         "SDKs on registries, security.txt)? The strongest hollow-product detector.",
         "crt.sh, DNS, docs, package registries, GitHub"),
    19: ("Voice of users", "E Extensions", "What do users repeatedly report? Hypothesis generator only (rules below).",
         "review and forum pages reached through search results only"),
    20: ("Licence and open-source governance", "E Extensions",
         "Relicensing (to BSL, SSPL and similar), CLAs that allow closing the code, forks after a licence change, "
         "foundation or not?", "repository, licence files, press, fork announcements"),
    21: ("Leadership and workforce", "E Extensions",
         "Abrupt CEO or CTO changes, layoffs, hiring freeze or growth, employer reviews?", "press, careers page, news"),
    22: ("Content provenance", "E Extensions",
         "Is the copy original (exact-phrase search of 2-3 distinctive sentences: a clone or a template), AI-written "
         "copy, stock or AI photos of 'team' and 'customers'?", "exact-phrase search"),
    23: ("Commercial terms and exit", "E Extensions",
         "Pricing transparency, auto-renewal, data export, lock-in; a pricing change over time when category 2 found a "
         "deal?", "pricing page, terms, Wayback"),
    24: ("Platform dependency", "E Extensions",
         "Is it a wrapper around another vendor's API, an app that lives inside one ecosystem?", "docs, site, search"),
}
CROSS_CUTTING = ("Timeline consistency in every category: a claim such as 'since 2015' against first traces in "
                 "Wayback, certificates or press.")

REVIEW_RULES = """Rules for category 19 (voice of users), so it never creates false positives:
1. A review generates a hypothesis; the flag, if any, lives in an authoritative category (9-12) once a regulator,
   court, status page or press confirms it. Reviews stay context.
2. Themes, not sentiment: only concrete failure themes count (billing traps, data loss, security, abandoned support,
   frozen product, price rises). Usability and feature wishes are ignored.
3. A pattern, not a quote: specific, repeated by independent authors on 2 or more platforms, recent (12-24 months),
   relevant to the buyer's segment, and judged against the size of the user base.
4. Balance: positives and negatives are both collected; the trace shows the distribution, not picked quotes.
5. A vendor that acknowledges and fixes counts as a positive.
6. Wording: "users report X (n sources, period)", never "the vendor does X".
7. Author type user_review can never be the only basis of a yellow or red flag; no Red and no Green from reviews."""

MATRIX_RULES = """Matrix rules:
1. At least 2 queries per category and at least one source that is not the vendor, unless not applicable (say why).
2. Every category ends with a status: found, searched_not_found (with the queries run) or not_applicable (with the
   reason). The whole matrix goes into the trace, empty categories included.
3. Search under every anchor: product, vendor, legal entity, parent, former names; English plus the vendor's
   country language.
4. A failed source (timeout, 403, paywall, JS-only page) is no signal, never a negative."""

AUTHOR_TYPES = {
    "authority": "regulator, court, government body",
    "vendor_against_interest": "the vendor stating something against its own interest (end of life, incident, price rise)",
    "major_press_research": "major press or a research organisation",
    "registry_archive": "a register, certifier register, package registry, Wayback, DNS or certificate log",
    "trade_press": "trade or industry press, analyst blog",
    "aggregator": "aggregator, directory, database site, wiki",
    "user_review": "user review, forum post, social post",
    "vendor_self": "the vendor about itself (a claim, never proof)",
}
SEVERITIES = ("critical", "moderate", "minor")
POLARITIES = ("negative", "positive", "neutral")  # negative = a caveat or risk for the buyer; positive = a strength
PROMISE_LINKS = ("direct", "adjacent", "none")    # link of the finding to the product's core promise
STATUSES = ("found", "searched_not_found", "not_applicable", "open")
CLAIM_STATUSES = ("confirmed", "contradicted", "not_found")
COLORS = ("Green", "Yellow", "Red")
RELIABILITY = ("high", "medium", "low")
BASES = ("confirmed", "contradicted", "not_confirmed")

COLOR_RULE = """How the color is chosen (no numeric weights). Check in this order and stop at the first that applies:
1. Red: a critical finding with high reliability that survived disconfirmation: an authoritative ruling against the
   core promise, fabricated proof on the vendor's own site, a contradicted key claim, a cloned site, or the product
   ending with no successor.
2. Yellow: a real, alive vendor with a caveat the buyer must act on (ownership churn, one maintainer, a hidden recent
   incident, legacy with a successor, terms weakened against the promise, missing certifications for an enterprise
   pitch), a critical finding that is not proven, or too little evidence (not_confirmed).
3. Green: alive (recent activity or receipted scale) with no such caveat. Minor findings stay yellow flags on a Green
   and never change the color. Every Green lists its strongest caveat.
Fields that carry the weight: author type, severity, link to the core promise, corroboration (one source or two or
more independent sources), reliability in words (high / medium / low, with the reason)."""

GUARDS = """Four guards in code (app/judge/guards.py), because a model can break them:
1. No Red without an external receipt (independent: not on the vendor's domains, author type other than
   vendor_self), a vendor statement against its own interest (author type vendor_against_interest, e.g. an end-of-life
   notice, on any domain), or a receipted contradiction on the vendor's own site (a claim check with status
   contradicted and a kept receipt).
2. User reviews are never the only basis of a flag (a flag whose kept receipts are all user_review is dropped).
3. Looks never change the color (category 14 findings never make a Red, and never make a Yellow on their own).
4. A failed source is no signal, never a negative (a yellow or red flag without a kept receipt is dropped)."""


def new_research(url):
    """The empty Research skeleton with all 24 matrix rows open."""
    return {"url": url, "slug": slug(url), "started": "", "seconds": 0.0,
            "identity": {"product": "", "vendor": "", "legal_entity": "", "parent": "", "former_names": [],
                         "domains": [], "key_people": [], "country": "", "languages": [], "same_name_risks": [],
                         "notes": "", "evidence": []},
            "site": {"core_promise": "", "segment": "", "claims": [], "inconsistencies": [], "pages": [],
                     "signals": {}},
            "matrix": {str(k): {"status": "open", "queries": [], "sources": [], "evidence": [], "note": ""}
                       for k in CATEGORIES},
            "claim_checks": [], "evidence": [], "sources": {}, "sources_failed": [], "rounds": [],
            "stats": {"tool_calls": 0, "llm_calls": 0, "capped": False}}


def slug(url):
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    s = (u.hostname or "").removeprefix("www.") + u.path.rstrip("/")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "case"


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"}


def registrable(host_or_url):
    """Registrable domain without the Public Suffix List: last two labels, three for co.uk-style suffixes."""
    h = host_or_url
    if "://" in h:
        h = urllib.parse.urlsplit(h).hostname or ""
    parts = h.lower().strip(".").removeprefix("www.").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def validate_evidence(e):
    p = []
    for k in ("id", "category", "text", "url", "quote", "author_type", "severity", "polarity"):
        if k not in e or e[k] in (None, ""):
            p.append(f"evidence {e.get('id', '?')}: missing {k}")
    if e.get("category") not in CATEGORIES:
        p.append(f"evidence {e.get('id', '?')}: category {e.get('category')!r} not in 1..24")
    if e.get("author_type") not in AUTHOR_TYPES:
        p.append(f"evidence {e.get('id', '?')}: author_type {e.get('author_type')!r}")
    if e.get("severity") not in SEVERITIES:
        p.append(f"evidence {e.get('id', '?')}: severity {e.get('severity')!r}")
    if e.get("polarity") not in POLARITIES:
        p.append(f"evidence {e.get('id', '?')}: polarity {e.get('polarity')!r}")
    return p


def validate_research(r):
    p = [f"research: missing {k}" for k in new_research("x") if k not in r]
    m = r.get("matrix", {})
    p += [f"matrix: row {k} missing" for k in map(str, CATEGORIES) if k not in m]
    p += [f"matrix {k}: status {v.get('status')!r}" for k, v in m.items() if v.get("status") not in STATUSES]
    for e in r.get("evidence", []):
        p += validate_evidence(e)
    return p


def validate_verdict(v):
    p = [f"verdict: missing {k}" for k in ("url", "overall", "call", "green_flags", "yellow_flags", "red_flags",
                                           "reasoning", "reasons", "coverage", "trace") if k not in v]
    if v.get("overall") not in COLORS:
        p.append(f"verdict: overall {v.get('overall')!r}")
    for b in ("green_flags", "yellow_flags", "red_flags"):
        for f in v.get(b, []):
            if not f.get("url") or not f.get("text"):
                p.append(f"verdict {b}: flag without url or text")
    if not v.get("reasons") and not v.get("error"):
        p.append("verdict: no reasons")
    return p
