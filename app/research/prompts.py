"""Prompts of the research brain (lane L2): the intake call and the round call of app/research/agent.py.

Generic buyer due diligence only: no real product, vendor or domain names (examples use Acme and example.com).
Everything is built from app/ledger.py (CATEGORIES, MATRIX_RULES, REVIEW_RULES, CROSS_CUTTING, AUTHOR_TYPES) and the
tool docs, with no clocks and a fixed order, so the same inputs give the same prompt and llm.py's cache replays a rerun.

    system = intake_system(table); user = intake_user(url, pages)
    system = round_system(table);  user = round_user(view)
Answer size is the time cost: CAPS holds the targets (intake <= 20k chars, round <= 15k) and the per-field caps that
size_block() states and app/research/agent.py enforces.
`table` is {tool name: {"doc", "args"}} (app.research.tools.TOOLS without the functions).
"""
import json

from app import ledger

QUESTION = "What will a buyer not see on this website?"
SOURCE_CHARS = 4000      # text shown per source in a round
PAGE_CHARS = 6000        # text shown per vendor page at intake (the page at the URL gets twice that)
PROMPT_CHARS = 300_000   # whole user prompt, about

# Answer size is the time cost (~110 output chars/s at xhigh): targets and hard caps, said in the prompts and enforced
# by app/research/agent.py after parsing (lists cut, words cut; an over-long quote drops its evidence, never cut).
CAPS = {"intake_chars": 20_000, "round_chars": 15_000,
        "identity_notes_words": 40, "same_name_risks": 3, "key_people": 5,
        "claims": 15, "claim_text_words": 20, "claim_quote_chars": 200, "inconsistencies": 5,
        "evidence": 12, "evidence_text_words": 25, "evidence_quote_chars": 250, "evidence_hypothesis_words": 12,
        "hypotheses": 2, "hypothesis_words": 15, "intake_actions": 40, "round_actions": 24,
        "action_hypothesis_words": 12, "note_words": 20}

ROLE = f"""You are the research brain of a buyer's due-diligence agent. A procurement lead is about to decide whether
to shortlist a software product and gives its URL. The question behind every step: "{QUESTION}"
Your job is to find what the vendor's own pages do not show (ownership, deals, rulings, incidents, a frozen product,
hollow claims, changed terms), and the strengths an outside source confirms, each with a receipt: a source URL and a
quote copied from it. You never decide the color; the judge does that later from your evidence."""


def categories_block():
    lines = ["The 24 categories of the research matrix (id. name [group]: the buyer's question | where to look):"]
    for k, (name, group, question, where) in ledger.CATEGORIES.items():
        lines.append(f"{k}. {name} [{group}]: {question} | {where}")
    lines.append(ledger.CROSS_CUTTING)
    return "\n".join(lines)


def author_types_block():
    return "author_type (who is speaking in the quote):\n" + "\n".join(
        f"- {k}: {v}" for k, v in ledger.AUTHOR_TYPES.items())


def tools_block(table):
    lines = ["Tools for actions ({\"tool\": name, \"args\": {...}}):"]
    for name, t in (table or {}).items():
        t = t or {}
        args = t.get("args", "")
        if not isinstance(args, str):
            args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        lines.append(f"- {name}: {(t.get('doc') or '').strip()}" + (f" Args: {args}" if args else ""))
    return "\n".join(lines)


SEARCH_RULES = """How to search:
- Search under every anchor: product name, vendor, legal entity, parent, former names; in English and in the language of
  the vendor's country (a German vendor gets German queries too, e.g. "Acme GmbH Insolvenz").
- Precise queries beat generic ones: name plus the topic ("Acme Cloud" acquisition, "Acme Ltd" fine regulator).
  For an outside view exclude the vendor's own domains (exclude_domains).
- Review platforms, forums, social networks, registers and employer-review sites are reached only through search
  results (their snippets are sources); never fetch or extract them.
- Read a page in full (extract or fetch) only when a search snippet is not enough to quote the fact.
- A failed source (timeout, 403, paywall, JS-only page) is no signal, never a negative."""

SNOWBALL = """Snowball (each finding opens the next search):
- An acquisition or merger: search what the new owner did after its other deals (layoffs, price rises, products frozen
  or ended) and whether this product changed afterwards (pricing, terms, release pace).
- A former name or another legal entity: search under it as well.
- A founder or executive: search their earlier companies and their history.
- A claim on the site (certification, customer, integration, number, age such as "since 2015"): verify it outside the
  vendor's domain (the certifier's register, the customer's own site, the partner's marketplace, archived pages).
- Terms and privacy: list snapshots with wayback, then read the snapshots about 6, 12 and 24 months ago with
  wayback_get and compare them with today's text by meaning (data use, AI training, selling or sharing data,
  subprocessors, unilateral changes), not by wording.
- A weak signal (category 14, how the site looks) is only a pointer: it says where to dig (categories 5-8), never a
  finding that matters by itself."""

EVIDENCE_RULES = """Evidence rules:
- One finding = one quote from one source given to you in this conversation (a numbered page or source below).
- Copy the quote character for character from that source's text: one passage of 10-300 characters, no ellipsis, no
  edits, no translation. Never quote from memory.
- A fact you know but cannot quote from a given text is a hypothesis: turn it into an action that would retrieve it,
  never into evidence.
- url is exactly the url of the source the quote comes from, as listed.
- date is the date of the event the quote describes (YYYY-MM-DD, YYYY-MM or YYYY), never the date the page was
  fetched; '' when the text gives none.
- severity: critical = would change a shortlist decision (a ruling against the core promise, the product ending,
  fabricated proof, a contradicted key claim, a cloned site); moderate = a caveat the buyer must act on; minor =
  context.
- polarity: negative = a caveat or risk for the buyer; positive = a strength (a dated recent release, a certificate in
  the certifier's register, a customer naming the vendor on its own site); neutral = a fact without a direction.
- promise_link: direct when the finding touches the core promise, adjacent when it touches the segment's needs,
  none otherwise.
- Collect positives as well as negatives: the trace shows the balance, not picked quotes."""

EVIDENCE_SHAPE = ('{"category": 1-24, "text": "the finding in one plain sentence", "url": "source url", '
                  '"quote": "verbatim passage", "date": "YYYY-MM-DD | YYYY-MM | YYYY | \'\'", '
                  f'"author_type": "{"|".join(ledger.AUTHOR_TYPES)}", "severity": "{"|".join(ledger.SEVERITIES)}", '
                  f'"polarity": "{"|".join(ledger.POLARITIES)}", "promise_link": "{"|".join(ledger.PROMISE_LINKS)}", '
                  '"hypothesis": "what was being tested"}')
ACTION_SHAPE = '{"tool": "search", "args": {"query": "..."}, "category": 1-24, "hypothesis": "what this tests"}'
JSON_ONLY = "Answer with one JSON object only, compact (no indentation, no line breaks): no prose, no code fence."


def size_block(kind):
    """The answer-size target and hard caps for the intake or the round call."""
    c = CAPS
    ev = (f"evidence <= {c['evidence']} per answer (the strongest first), text <= {c['evidence_text_words']} words, "
          f"quote <= {c['evidence_quote_chars']} chars and still verbatim (pick a shorter passage; a longer quote "
          f"drops the evidence), hypothesis <= {c['evidence_hypothesis_words']} words, no source_title (code fills "
          f"it), date and promise_link omitted when unknown")
    act = f"each {{tool, args, category, hypothesis <= {c['action_hypothesis_words']} words}}"
    if kind == "intake":
        caps = [f"identity notes <= {c['identity_notes_words']} words; same_name_risks <= {c['same_name_risks']}; "
                f"key_people <= {c['key_people']}",
                f"claims <= {c['claims']} (the ones a buyer relies on most), text <= {c['claim_text_words']} words, "
                f"quote <= {c['claim_quote_chars']} chars; inconsistencies <= {c['inconsistencies']}; signals notes "
                f"<= {c['note_words']} words",
                ev,
                f"plan: per category at most {c['hypotheses']} hypotheses of <= {c['hypothesis_words']} words, or "
                f"not_applicable with a reason of <= {c['note_words']} words",
                f"actions <= {c['intake_actions']}, {act}"]
        target = c["intake_chars"]
    else:
        caps = [ev, f"matrix_updates notes and claim_checks notes <= {c['note_words']} words",
                f"next_actions <= {c['round_actions']}, {act}",
                f"identity_updates: only new facts; notes <= {c['identity_notes_words']} words"]
        target = c["round_chars"]
    return "\n".join([f"Answer size (every output character costs time: the same substance in fewer characters):",
                      f"- The whole answer stays under {target:,} characters of compact JSON (no pretty printing). "
                      "Omit empty strings, empty lists and unknown optional fields.",
                      "- Never restate the input: no copied page text beyond the verbatim quotes, no repeating of "
                      "claims, identity, matrix state or earlier evidence.",
                      "- Hard caps (code cuts anything above them):"] + [f"  - {x}" for x in caps])


def _common(table):
    return "\n\n".join([categories_block(), ledger.MATRIX_RULES, ledger.REVIEW_RULES, author_types_block(),
                        tools_block(table), SEARCH_RULES, SNOWBALL, EVIDENCE_RULES])


def intake_system(table):
    task = f"""This call is the intake. You get the vendor's own pages (numbered, fetched from the site at the URL).
The product is the one at the given URL, even when the URL is a deep page of a larger vendor site.
The buyer may name the product and vendor (identity hints): use them as anchors to verify, never as facts. The
pages decide the identity; a mismatch between the buyer's names and the pages is a same-name risk.
Do five things:
1. identity: the product, the vendor, its legal entity (from the imprint or legal pages), the parent if any, former
   names, the vendor's domains, key people with roles, the country, the site's languages, same-name risks (other
   companies or products with a similar name a search could confuse), notes.
2. site: the core promise in one sentence (what the buyer buys it for), the segment (who it is for), the claims a
   buyer would rely on (features, integrations, certifications, customers, numbers, hosting, analyst mentions, age),
   each with a verbatim quote and its page url; inconsistencies within or between pages (conflicting numbers, dates,
   names); signals (copyright year, site generator, notes on design age, empty sections, template copy).
3. evidence: findings from these pages only (author_type vendor_self, or vendor_against_interest when the vendor
   states something against its own interest, such as an end-of-life notice or a price rise).
4. plan: for each of the 24 categories, at most 2 short hypotheses for this product (what could be hidden there), or
   not_applicable with the reason (for example category 20 for a closed-source product with no public code).
5. actions: the first research round, at most 40: one per applicable category first, a second for the categories
   that matter most for this buyer (later rounds add the rest), ordered by what matters most. Most actions are searches; also use the technical tools where they test a claim (dns of the
   app domain, crtsh of the vendor domain, wayback of the terms and privacy pages, registry or github for SDKs).

Answer shape:
{{"identity": {{"product": "", "vendor": "", "legal_entity": "", "parent": "", "former_names": [], "domains": [],
  "key_people": [{{"name": "", "role": ""}}], "country": "", "languages": [], "same_name_risks": [], "notes": ""}},
 "site": {{"core_promise": "", "segment": "", "claims": [{{"text": "", "kind": "feature|integration|certification|customer|number|hosting|analyst|age|other", "quote": "", "url": ""}}],
  "inconsistencies": [{{"text": "", "quote": "", "url": ""}}], "signals": {{"copyright_year": "", "generator": "", "notes": ""}}}},
 "evidence": [{EVIDENCE_SHAPE}],
 "plan": {{"1": {{"hypotheses": ["..."], "not_applicable": ""}}, "...": {{}}, "24": {{"hypotheses": [], "not_applicable": "the reason"}}}},
 "actions": [{ACTION_SHAPE}]}}
{JSON_ONLY}"""
    return "\n\n".join([ROLE, task, _common(table), size_block("intake")])


def _cap(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n] + " [...cut]"


def hints_line(hints):
    """The buyer's names for the product as anchors to verify ('' without hints)."""
    hints = hints or {}
    p, v = hints.get("product"), hints.get("vendor")
    if not (p or v):
        return ""
    name = f'this product "{p}"' if p else "this product"
    return (f"Identity hints (anchors to verify, not facts): the buyer names {name}"
            + (f' by vendor "{v}"' if v else "") + ". The site read may correct them; a mismatch with the pages is a "
            "same-name risk: note it in same_name_risks and identity.notes, and search under both names.")


def intake_user(url, pages, hints=None):
    """pages: [{"url", "title", "text"}] in fetch order; the first is the page at the URL. hints: {"product",
    "vendor"} as the buyer names them (optional)."""
    hl = hints_line(hints)
    head = [f"Product URL: {url}"] + ([hl] if hl else []) + [f"Vendor pages fetched: {len(pages)}", ""]
    parts, size = [], sum(len(h) for h in head)
    for i, p in enumerate(pages, 1):
        block = (f"[P{i}] url: {p['url']}\ntitle: {p.get('title') or ''}\n"
                 f"{_cap(p.get('text'), PAGE_CHARS * 2 if i == 1 else PAGE_CHARS)}\n")
        if size + len(block) > PROMPT_CHARS:
            parts.append(f"({len(pages) - i + 1} more pages omitted for length)")
            break
        parts.append(block)
        size += len(block)
    return "\n".join(head + parts + ["", f"Question: {QUESTION}", JSON_ONLY])


def round_system(table):
    task = f"""This call is one round of the research loop. You get the identity, the core promise, the claims, the
matrix state (status, queries run, evidence ids, notes per category), the evidence so far, this round's actions and
their results (numbered sources with url, title, date and text). Do five things:
1. evidence: new findings from this round's sources only, each with a verbatim quote (rules below). Do not repeat
   evidence already listed.
2. matrix_updates: for every category this round touched: status found (evidence exists), searched_not_found (the
   queries ran and nothing relevant came back; say what was searched), not_applicable (the reason) or open (more to
   do), and a short note of what was found.
3. claim_checks: for the site's claims these results confirm or contradict: status confirmed, contradicted or
   not_found, evidence as the urls of the quotes (or the ids of earlier evidence), a note.
4. next_actions: at most 24, the snowball from what you found first, then open categories and anchors not yet
   searched. Never repeat a query or action already run. An empty list ends the research.
5. done: true when every category has a final status or nothing worthwhile is left to search.
Optional identity_updates (parent, legal_entity, former_names, key_people, same_name_risks, notes) when a source
reveals them; they become new anchors.

Answer shape:
{{"evidence": [{EVIDENCE_SHAPE}],
 "matrix_updates": {{"9": {{"status": "{'|'.join(ledger.STATUSES)}", "note": ""}}}},
 "claim_checks": [{{"claim_id": "c1", "status": "{'|'.join(ledger.CLAIM_STATUSES)}", "evidence": ["url or e3"], "note": ""}}],
 "next_actions": [{ACTION_SHAPE}],
 "identity_updates": {{}},
 "done": false}}
{JSON_ONLY}"""
    return "\n\n".join([ROLE, task, _common(table), size_block("round")])


def _matrix_lines(matrix, hypotheses):
    lines = []
    for k, (name, *_rest) in ledger.CATEGORIES.items():
        row = matrix[str(k)]
        q = row.get("queries") or []
        shown = q[-12:]
        more = f" (+{len(q) - len(shown)} earlier)" if len(q) > len(shown) else ""
        lines.append(f"{k}. {name} [{row.get('status')}] evidence: {', '.join(row.get('evidence') or []) or '-'}"
                     f" | queries{more}: {' || '.join(shown) or '-'}"
                     f" | hypotheses: {'; '.join(hypotheses.get(str(k)) or []) or '-'}"
                     f" | note: {row.get('note') or '-'}")
    return lines


def round_user(view):
    """view: {"url", "identity", "core_promise", "segment", "claims", "matrix", "hypotheses", "evidence", "round",
    "rounds_left", "tool_calls_left", "actions": [{"n", "tool", "args", "category", "hypothesis", "results",
    "error"}], "hints": [{"n", "text"}], "sources": [{"url", "title", "date", "kind", "actions", "text"}]}"""
    ident = {k: v for k, v in view["identity"].items() if k != "evidence"}
    head = [f"Product URL: {view['url']}",
            f"Round {view['round']}; rounds left after this one: {view['rounds_left']}; "
            f"tool calls left: {view['tool_calls_left']}",
            "Identity: " + json.dumps(ident, ensure_ascii=False, sort_keys=True),
            f"Core promise: {view.get('core_promise') or '-'}",
            f"Segment: {view.get('segment') or '-'}",
            "Claims:"]
    head += [f"- {c['id']} [{c.get('kind') or 'other'}] {c['text']} (quote: \"{_cap(c.get('quote'), 300)}\", "
             f"{c.get('url') or '-'})" for c in view["claims"]] or ["- none"]
    head += ["", "Matrix state:"] + _matrix_lines(view["matrix"], view["hypotheses"])
    head += ["", "Evidence so far:"]
    head += [f"- {e['id']} cat {e['category']} {e['polarity']}/{e['severity']}: {_cap(e['text'], 200)} ({e['url']})"
             for e in view["evidence"]] or ["- none"]
    head += ["", f"This round's actions ({len(view['actions'])}):"]
    for a in view["actions"]:
        head.append(f"A{a['n']} cat {a['category']} {a['tool']} "
                    f"{json.dumps(a['args'], ensure_ascii=False, sort_keys=True)} -> {a['results']} results"
                    + (f", error: {a['error']}" if a.get("error") else "")
                    + (f" | hypothesis: {a['hypothesis']}" if a.get("hypothesis") else ""))
    for h in view.get("hints") or []:
        head.append(f"A{h['n']} research report (a hint for next actions, not a quotable source): "
                    f"{_cap(h['text'], SOURCE_CHARS)}")
    text = "\n".join(head)
    tail = "\n".join(["", f"Question: {QUESTION}", JSON_ONLY])
    parts, size = [f"\nThis round's sources ({len(view['sources'])}, new text only):"], len(text) + len(tail)
    for i, s in enumerate(view["sources"], 1):
        block = (f"[S{i}] url: {s['url']}\ntitle: {s.get('title') or ''}\ndate: {s.get('date') or ''}\n"
                 f"kind: {s.get('kind')}; from actions: {', '.join('A' + str(n) for n in s.get('actions') or [])}\n"
                 f"{_cap(s.get('text'), SOURCE_CHARS)}\n")
        if size + len(block) > PROMPT_CHARS:
            parts.append(f"({len(view['sources']) - i + 1} more sources omitted for length)")
            break
        parts.append(block)
        size += len(block)
    return text + "\n".join(parts) + tail
