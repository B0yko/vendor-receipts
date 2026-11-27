# vendor-receipts
<one-line pitch: who it helps decide what>

## What it does
<two or three sentences: input, what the agent checks, what it returns, why a buyer can act on it>

## Run
Python 3.12+, no packages to install. Set a model endpoint (any OpenAI-compatible API):
`export LLM_ORDER=main LLM_MAIN_URL=https://.../v1 LLM_MAIN_MODEL=<model> LLM_MAIN_KEY=<key>`
- `make smoke`: one end-to-end run on the public examples in `tests/`.
- `make batch CASES=cases.jsonl OUT=verdicts.jsonl`: one JSON line per case (`{"url": "https://..."}` in).
- `REPLAY=1 make smoke`: the same run from the cache, offline.

## Demo video
<link>

## Results
<aggregates from `tools/eval.py --md`: cases, accuracy, kappa, reasons with a link, links that open; no per-company calls>

## How it works
<the pipeline in 4-6 bullets; which partner tools it uses and where>

## Built during the event
Built at AI Agents Berlin, "From Research to Commerce", 27.11.2026. Everything in this repository was written
between 15:00 and 19:45 CET (see the commit history), except the generic utilities `cache.py`, `llm.py` and
`tools/eval.py` and the file templates, which were prepared before the event and contain no product logic.
Open-source tools used: <list>.
