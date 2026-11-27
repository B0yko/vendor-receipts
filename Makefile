# Product commands (see AGENTS.md). llm.env (not tracked) sets KC, LLM_ORDER and the LLM_<NAME>_URL/_MODEL lines.
# Inputs in data/, outputs in runs/ (both untracked). PY=python3 skips uv (no packages).
-include llm.env
export
PY      ?= uv run python
RUN     ?= $(PY) -m app.run
LABELS  ?= data/labels.csv
DEV     ?= data/cases_dev.jsonl
HOLD    ?= data/cases_holdout.jsonl
PREDS   ?= runs/preds_dev.jsonl
GREEN   ?= runs/eval/green.json
SMOKE   ?= tests/smoke_cases.jsonl
CASES   ?= data/blind_cases.jsonl
OUT     ?= runs/verdicts.jsonl
EVALOPT ?=

.PHONY: check test smoke eval holdout cases green batch
# check = unit tests + smoke from the cache: under 30 s. The eval runs separately (never inside a lane's loop).
check: test smoke
	@echo "CHECK PASSED $$(date +%H:%M) $$(git rev-parse --short HEAD 2>/dev/null)"

test:
	$(PY) -m unittest discover -s tests

smoke:
	@if [ ! -f $(SMOKE) ]; then echo "SKIP smoke: no $(SMOKE) yet"; exit 0; fi; mkdir -p .cache; \
	$(KC) $(RUN) --cases $(SMOKE) --out .cache/smoke.jsonl && \
	python3 tools/eval.py tests/smoke_labels.csv .cache/smoke.jsonl --strict --min-accuracy 1

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
	$(KC) $(RUN) --cases $(CASES) --out $(OUT) $(if $(ONLY),--only $(ONLY))
