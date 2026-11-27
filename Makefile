# Product commands (see AGENTS.md). llm.env (not tracked) sets KC, LLM_ORDER and the LLM_<NAME>_URL/_MODEL lines.
# Inputs in data/, outputs in runs/ (both untracked). PY=python3 skips uv (no packages).
-include llm.env
export
PY      ?= python3
RUN     ?= $(PY) -m app.run
LABELS  ?= data/hackathon_40_solutions_commented.csv
DEV     ?= data/cases_dev.jsonl
HOLD    ?= data/cases_holdout.jsonl
PREDS   ?= runs/preds_dev.jsonl
GREEN   ?= runs/eval/green.json
SMOKE   ?= tests/smoke_cases.jsonl
CASES   ?= data/blind_cases.jsonl
OUT     ?= runs/verdicts.jsonl
EVALOPT ?= --label overall
WALL_CAP ?= 1200
CONC    ?= 8
QUICK   ?= data/cases_quick.jsonl
RUN_ID  ?= $(shell date +%H%M)
export WALL_CAP CONC RUN_ID

.PHONY: check test smoke eval holdout cases green batch one usage quick report baseline control
# check = unit tests + smoke from the cache: under 30 s. The eval runs separately (never inside a lane's loop).
check: test smoke
	@echo "CHECK PASSED $$(date +%H:%M) $$(git rev-parse --short HEAD 2>/dev/null)"

test:
	$(PY) -m unittest discover -s tests

smoke:
	@if [ ! -f $(SMOKE) ]; then echo "SKIP smoke: no $(SMOKE) yet"; exit 0; fi; mkdir -p .cache; \
	RECEIPTS_FAKE=1 RUN_ID=smoke $(RUN) --cases $(SMOKE) --out .cache/smoke.jsonl && \
	python3 tools/eval.py tests/smoke_labels.csv .cache/smoke.jsonl --label overall --strict --min-accuracy 1

cases:
	python3 tools/eval.py $(LABELS) --split dev --cases-out $(DEV) $(EVALOPT)
	python3 tools/eval.py $(LABELS) --split holdout --cases-out $(HOLD) $(EVALOPT)

eval:
	@if [ ! -f $(LABELS) ]; then echo "SKIP eval: no labels at $(LABELS)"; exit 0; fi; \
	[ -f $(DEV) ] || $(MAKE) -s cases; \
	$(KC) $(RUN) --cases $(DEV) --out $(PREDS) && \
	python3 tools/eval.py $(LABELS) $(PREDS) --split dev --save runs/eval/last.json $(EVALOPT) \
	  $$([ -f $(GREEN) ] && echo --baseline $(GREEN))

holdout:
	[ -f $(HOLD) ] || $(MAKE) -s cases
	$(KC) $(RUN) --cases $(HOLD) --out runs/preds_holdout.jsonl
	python3 tools/eval.py $(LABELS) runs/preds_holdout.jsonl --split holdout --save runs/eval/holdout-$$(date +%H%M).json $(EVALOPT)

green:
	@git diff --quiet HEAD -- . || { echo "commit first: green tags a committed tree"; exit 1; }
	$(MAKE) -s check
	$(MAKE) -s eval
	@mkdir -p $(dir $(GREEN)); [ ! -f runs/eval/last.json ] || cp runs/eval/last.json $(GREEN)
	@n=$$(( $$(git tag -l 'green-*' | wc -l) + 1 )); git tag green-$$n && echo "tagged green-$$n at $$(git rev-parse --short HEAD)"

batch:
	@mkdir -p $(dir $(OUT))
	$(KC) $(RUN) --cases $(CASES) --out $(OUT) $(if $(ONLY),--only $(ONLY)) $(if $(SUBMIT),--submit $(SUBMIT))

# One live product end to end (D1): make one URL=https://...
one:
	@mkdir -p runs
	echo '{"url": "$(URL)"}' > runs/one_case.jsonl
	$(KC) $(RUN) --cases runs/one_case.jsonl --out runs/one.jsonl --concurrency 1

usage:
	@$(KC) $(PY) -c 'from app.research import tools; import json; print(json.dumps(tools.usage()))'

# Quick set: 2 Green, 2 Yellow, 2 Red from dev (data/cases_quick.jsonl, made by the Lead from the dev split).
quick:
	$(KC) $(RUN) --cases $(QUICK) --out runs/preds_quick_$(RUN_ID).jsonl
	python3 tools/eval.py $(LABELS) runs/preds_quick_$(RUN_ID).jsonl --split dev $(EVALOPT) | tail -15

# Report of a run (evalkit, lane L4): make report PREDS=runs/preds_dev.jsonl RUN_ID=<id> [SPLIT=dev]
SPLIT ?= dev
report:
	$(KC) $(PY) -m evalkit.report --preds $(PREDS) --split $(SPLIT) --run-dir runs/$(RUN_ID) --out runs/$(RUN_ID)/report.md \
	  --save runs/eval/report_$(RUN_ID).json $$([ -f runs/eval/report_prev.json ] && echo --prev runs/eval/report_prev.json)

# Plain baseline on the same retrieved data: make baseline RUN_ID=<id>
baseline:
	$(KC) $(PY) -m evalkit.baseline --run-dir runs/$(RUN_ID) --out runs/baseline_$(RUN_ID).jsonl

control:
	$(KC) $(RUN) --cases tests/control_cases.jsonl --out runs/control_$(RUN_ID).jsonl
