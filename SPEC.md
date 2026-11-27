# SPEC: vendor-receipts
Challenge: Stackgini "Agents with Taste" (AI Agents Berlin, 27.11.2026). One challenge.

Goal: a buyer (or another agent) gives a vendor URL and gets what the website does not show: one color
(Green / Yellow / Red) in Stackgini's shape (green, yellow and red flags plus reasoning), every flag with a receipt
(URL, verbatim quote, date, author type, checks passed), and a research trace an auditor can follow.
User and moment: a procurement lead before a shortlist decision; the call says shortlist / shortlist after a named
check / do not shortlist.
Input -> output: `receipts run <url>` or `make batch CASES=<file>` -> one Verdict JSON line per product
(`app/ledger.py`), a Stackgini-shaped CSV next to it, and `runs/<run_id>/<slug>/trace.md`.

## Pipeline (one product; the plan: project `out/idea-review.md` §4-7)
URL -> 1 identity + 2 site read (one intake call over the fetched vendor pages) -> 3 plan over the 24-category matrix
-> 4 research loop (rounds of parallel tool calls; new hypotheses from evidence; exit when every category has a
status, or at the caps) -> 5 claim check -> 6 receipt checks (resolves, same company, says it, dated) -> 8 judge
(color rule in `app/ledger.py`, then the four guards in code) -> verdict + trace + CSV row.
Caps: research rounds <= 5 including the plan, tool calls <= 120, per-product wall cap `WALL_CAP` seconds (judge
reserve included; a product at the cap is judged on what it has, `stats.capped` true).

## Demo path (2:30)
1. Two masked cards with opposite colors: a polished site that ends Red on an authoritative receipt, a dated site
   that ends Green on its release pulse.
2. A receipt that opens at its quote; one rejected receipt (same name, different company).
3. The 24-row matrix in the trace: what was searched and not found.
4. Holdout count against all-Yellow and the plain agent; their flags found; verified extra flags.
5. A live run on the jury's URL from the frozen commit.

## Done checks
- D1 An unseen URL returns a verdict with flags, receipts and a trace. Check: `make one URL=<public url>`.
- D2 Every flag links to a page a human can open and its quote is on that page. Check: `python3 tools/eval.py
  <labels> <preds> --label overall --links`.
- D3 Beats all-Yellow (11/28) and the plain agent on dev; false Red on Green = 0 target. Check: `make eval` +
  `make report`.
- D4 The blind batch (6-10 products, parallel) finishes inside its wall cap. Check: the `seconds` field per line.
- D5 `make check` passes offline in under 30 s (unit tests, leak test, fake smoke).

## Out of scope
UI beyond the CLI and Markdown trace; accounts; deploys; a second challenge; logins on vendor sites; scraping of review
sites, registers or social networks (reached only through search results).

## Dataset policy (out/idea-review.md §8)
The agent never sees the labelled CSV: no examples, flag texts, reasoning or product names in prompts or configs.
Only `evalkit/` reads `data/`. `tests/test_leak.py` fails the build on any CSV product, vendor or domain string in
the agent's files. Holdout rows are touched only by `make holdout`, once, at the 18:15 freeze.

## Interfaces (frozen 17:05; a change needs a STATE.md decision line)
- `app/ledger.py` (Lead): the shapes Evidence, Receipt, Research, Flag, Verdict; the 24 CATEGORIES, REVIEW_RULES,
  MATRIX_RULES, AUTHOR_TYPES, COLOR_RULE, GUARDS; `new_research`, `slug`, `registrable`, `validate_*`.
- `app/model.py` (Lead): `ask_json(system, user, purpose, max_tokens=None) -> dict`; `set_fake`, `mode`, `model_id`.
- `app/research/tools.py` (L1): every function returns a dict and never raises; `error` is None or a string; all
  cached through `cache.cached` (Tavily, Wayback, crt.sh, DNS, registries) or `cache.http_get` (vendor pages).
  - `search(query, topic="general", time_range=None, depth="advanced", max_results=8, include_domains=None,
    exclude_domains=None) -> {"query", "results": [{"url", "title", "content", "published_date", "score"}], "error"}`
  - `extract(urls, depth="advanced") -> {"results": [{"url", "text"}], "failed": [{"url", "error"}], "error"}`
  - `fetch(url) -> {"url", "final_url", "status", "title", "text", "links": [{"url", "text"}], "meta":
    {"generator", "copyright_years": []}, "error"}` (polite: 1 request/s per host, no logins)
  - `wayback(url, limit=200) -> {"url", "first", "last", "count", "snapshots": [{"timestamp", "url", "status"}], "error"}`
  - `wayback_get(url, timestamp) -> {"url" (the archive URL), "timestamp", "text", "error"}` (nearest snapshot)
  - `dns(host) -> {"host", "a": [], "aaaa": [], "cname": [], "ns": [], "error"}`
  - `crtsh(domain) -> {"domain", "names": [], "first_seen", "count", "error"}`
  - `registry(name, ecosystem) -> {"name", "ecosystem", "latest", "latest_date", "first_date", "url", "error"}`
    (pypi, npm, crates, packagist, rubygems; GitHub repos via `github(owner_repo)`)
  - `github(owner_repo) -> {"repo", "pushed_at", "stars", "archived", "license", "open_issues", "url", "error"}`
  - `research(query) -> {"report", "sources": [{"url", "title"}], "error"}` (Tavily /research; slow; used sparingly)
  - `TOOLS`: name -> {"fn", "doc", "args"} for the loop prompt; `run_tool(name, args) -> dict` (validates names and
    args, records `tool`, `args`, `seconds`); `usage() -> dict` (Tavily /usage, not cached).
  - A Tavily plan or usage-limit error appends one line to `runs/alerts.log` (and stderr).
- `app/research/agent.py` (L2): `research(url, deadline=None) -> Research` (the `app/ledger.py` shape, every key
  present, partial when the deadline hits).
- `app/check/receipts.py` (L6): `check(research) -> [Receipt]`.
- `app/judge/judge.py` (L3): `judge(research, receipts) -> Verdict` (guards applied: `app/judge/guards.py`).
- `app/pipeline.py` (L3): `check_vendor(url, run_dir, deadline=None, research_fn=None) -> Verdict`; writes
  `identity.json`, `research.json`, `receipts.json`, `verdict.json`, `trace.md` into `run_dir`.
- `app/run.py` (L3): `python3 -m app.run --cases FILE --out FILE [--only id,...] [--concurrency N]
  [--wall-cap S]`; cases: JSONL `{"url", "id"?}`, CSV with a url column, or one URL per line; writes each Verdict
  line as it completes, then `<out>.csv` (Stackgini columns) and `runs/<run_id>/manifest.json`.
- Model answer shapes that cross lanes (L3, L6 and L5's fake fixtures code against them):
  - purpose `says_it` (L6): user text lists receipts `{id, claim, quote, source_title, url}`; answer
    `{"results": [{"id": "e3", "supports": true, "why": "..."}]}`.
  - purpose `judge` (L3): answer `{"overall": "Green|Yellow|Red", "reliability": "high|medium|low", "reliability_why",
    "green_flags": [{"text", "category": 1-24, "severity", "basis": "confirmed|contradicted|not_confirmed",
    "receipts": ["e3"]}], "yellow_flags": [...], "red_flags": [...], "reasoning" (every sentence cites [e3]),
    "blind_spots": [], "buyer_next_step", "vendor_questions": [3 strings], "would_flip"}`.
- `evalkit/` (L4): reads labels from `data/` and our run artifacts; `python3 -m evalkit.report`; the plain baseline
  `python3 -m evalkit.baseline` (homepage + the same search results, one call, no rule, no checks).

## Lanes (disjoint paths; lanes commit on their branch; Lead merges one diff at a time, then `make check`)
- Shared (Lead only): `app/ledger.py`, `app/model.py`, `app/__init__.py`, `llm.py`, `cache.py`, `SPEC.md`,
  `Makefile`, `pyproject.toml`, `AGENTS.md`, `README.md`, `tests/test_lib.py`.
- L1 tool layer (worktree `build/vendor-receipts-l1`, branch `l1`): `app/research/tools.py`, `tests/test_l1_*.py`.
- L2 research brain: identity, site read, plan, loop, claim check (worktree `build/vendor-receipts-l2`, branch `l2`):
  `app/research/agent.py`, `app/research/prompts.py`, `tests/test_l2_*.py`.
- L3 judge and the four guards (worktree `build/vendor-receipts-b`, branch `lane-b`): `app/judge/` (init excepted),
  `tests/test_l3_*.py`.
- L6 receipt checks (worktree `build/vendor-receipts-l6`, branch `l6`): `app/check/` (init excepted), `tests/test_l6_*.py`.
- L5 outputs, pipeline, batch (worktree `build/vendor-receipts-l5`, branch `l5`): `app/out/` (init excepted),
  `app/pipeline.py`, `app/run.py`, `app/cli.py`, `tests/test_l5_*.py`, `tests/fixtures/`, `tests/smoke_cases.jsonl`,
  `tests/smoke_labels.csv`.
- L4 evaluation (worktree `build/vendor-receipts-l4`, branch `l4`): `evalkit/`, `tests/test_leak.py`, `tests/leak_allow.txt`,
  `tests/test_l4_*.py`, `tests/control_cases.jsonl`.
- Package inits `app/research/__init__.py`, `app/check/__init__.py`, `app/judge/__init__.py`, `app/out/__init__.py`: Lead.
