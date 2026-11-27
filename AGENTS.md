# vendor-receipts: instructions for coding agents
What it does: see SPEC.md (goal, done checks D1..Dn, out of scope). Plan state: STATE.md (not tracked).

## Commands
- `make check` (under 30 s): unit tests + smoke (end to end on 2 public examples, from the cache). Done = this
  passes; paste its last 20 lines. "Looks done" is not done.
- `make batch CASES=<file> OUT=<file> [ONLY=id,id]`: the one batch command (evals, the 19:00 blind run, re-runs).
  It writes one JSON line per case as each case completes.
- `make eval` (dev split vs the last green baseline; Lead runs it, not a lane), `make holdout` (only at the freeze;
  never tune on it), `make cases` (dev/holdout inputs from the labels, keys only).
- `REPLAY=1 make smoke` must pass with the network off: every external call goes through `cache.py` or `llm.py`.
- `make green` on a clean, committed tree: check + eval, saves the eval baseline, tags `green-N`.
- Keys: `llm.env` (not tracked) sets `KC = kc env VAR=item ... --` and the model endpoints. Never write a key into a file.

## Layout
- `app/`: the agent; `app/run.py` is the batch CLI (`--cases FILE --out FILE [--only id,...]`). Python via `uv run`
  (`pyproject.toml`); `make PY=python3 ...` runs without uv.
- `cache.py` (`cached`, `http_get`), `llm.py` (`chat`, `chat_json`, endpoint fallback): generic utilities, prepared
  before the event. The `openai` SDK with the same LLM_* env is fine too; keep every call cached.
- `tests/`: unit tests; `tests/smoke_cases.jsonl` and `tests/smoke_labels.csv` hold public, non-event examples only.
- `data/` (inputs: labelled examples, blind cases) and `runs/` (verdicts, manifests, eval outputs): not tracked;
  event data stays in the event. `hooks/pre-push` runs `tools/prepush.py` on every pushed commit: labelled-set rows, domains and names are blocked
  everywhere; blind-product domains only in `stackgini-verdicts.csv` and `traces/`. `tools/publish_check.py` runs the same
  rules over the whole history before the repo goes public (`--flip` only on PASS).
- `.cache/`: cached external calls. Not tracked.
- Lanes: each lane edits only its own paths (SPEC.md § Lanes); shared files change only through Lead or Andrii.

## Output contract: one JSON line per case
`{"url": ..., "call": ..., "reasons": [{"text": ..., "url": ..., "quote": ...}], "seconds": 3.2, "error": null, "mode": "live"|"replay"}`
Every reason has a URL a human can open; `quote` is text that appears on that page.

## Rules
- One writer at a time. Edit only the files your brief names; do not commit unless the brief says so.
- The runner reads only case inputs; labels are read only by `tools/eval.py`.
- Polite to vendor sites: `cache.http_get` spaces requests per host; fetch only what a case needs.
- Keep `make check` under 30 s: no sleeps in tests; a test HTTP server runs `serve_forever(poll_interval=0.05)`.
- A red check is fixed or your edits are reverted. Never delete, skip or weaken a test to get green.
- No new dependency without a reason in your report. No secrets in files; event data only in `data/` and `runs/`; tests use example.com.
