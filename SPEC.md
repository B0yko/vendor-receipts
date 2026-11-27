# SPEC: vendor-receipts
Challenge: stackgini · Brief: the partner PDF (one challenge; Norma on top: no)

Goal (one sentence): <what a user can do after this that they could not before>
User and moment: <who decides what, when, and what changes for them>
Input -> output: <one case in, one verdict out, fields per the output contract in AGENTS.md>

## Demo path (2-3 minutes, the beats; details in the project's out/demo.md)
1. <first 30 s: the problem and the surprise>
2. <the agent on a case, live or replay, the key reason and its source>
3. <the number: eval on held-out cases, and the catch that a naive agent misses>

## Done checks (each maps to a brief criterion and a command; Lead flips features.json only after `make check`)
- D1 An unseen case returns a verdict with reasons (brief: done 1). Check: `make smoke`.
- D2 Every reason links to a page a human can open, and its quote is on that page (brief: done 2).
  Check: `python3 tools/eval.py <labels> <preds> --links`.
- D3 Both sides right on the dev split: accuracy >= <x>, no class below <y> recall (brief: done 3). Check: `make eval`.
- D4 The batch finishes <N> cases in <= 25 min, p90 <= 4 min per case, each verdict written as it completes.
  Check: `make holdout` timing lines.
- D5 The demo path works offline. Check: `REPLAY=1 make smoke`.

## Out of scope (say no fast)
- <UI beyond one page or a CLI>, <accounts>, <deploys>, <a second challenge>

## Interfaces (frozen with the schema commit; a change needs a STATE.md decision line)
- `app/ledger.py`: the evidence ledger schema, owned by Andrii; the one contract both lanes code against.
- `app/run.py --cases FILE --out FILE [--only id,...]`, output contract in AGENTS.md.

## Lanes (one writer until the schema commit; then two, on disjoint paths; Lead or Andrii merges one diff at a time)
- Shared (Lead or Andrii only): `app/ledger.py`, `SPEC.md`, `Makefile`, `pyproject.toml`, `AGENTS.md`.
- Lane A, collectors (main checkout `build/vendor-receipts`): `app/collect/`, `tests/test_collect_*.py`.
- Lane B, checks, judge, outputs, eval (worktree `build/vendor-receipts-b`, branch `lane-b`): `app/check/`, `app/judge/`,
  `app/out/`, `app/eval/`, `app/run.py`, `tests/test_b_*.py`.
