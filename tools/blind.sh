#!/bin/sh
# The 19:00 blind run and the public submission, one subcommand per step (Lead-owned).
#   tools/blind.sh run  [CSV]   batch from tag blind-run in its own worktree (../vendor-receipts-blind), all products in
#                               parallel; writes runs/verdicts.jsonl and, into the main checkout, stackgini-verdicts.csv
#                               and traces/<id>.md. Default CSV data/blind.csv (columns id,url,product,vendor).
#   tools/blind.sh rerun IDS    re-run failed ids (comma list) into runs/rerun.jsonl and merge them into the submission.
#   tools/blind.sh rebuild [CSV] write the submission from runs/verdicts.jsonl without running (after a stop or crash).
#   tools/blind.sh ship         redact labelled-set sources unless ALLOW_LABELLED=1, commit stackgini-verdicts.csv and
#                               traces/, push main through the pre-push guard (never --no-verify).
#   tools/blind.sh flip         full-history publish check, then the visibility flip only on PASS.
# The run splits the products by row parity into two processes: half a on Tavily key KEY_A (tavily-api-key), half b
# on KEY_B (tavily-api-key-2), each with its own limiter bucket at TAVILY_RPM (95); then ONE submission is rebuilt.
# Env: TAG (blind-run), SUBMIT (the main checkout), WALL_CAP (1380), CONC (10), MODEL_GLOBAL_CONC (40), ALLOW_LABELLED
# (default 1, Andrii 17:54: labelled-set names and domains allowed only in stackgini-verdicts.csv and traces/ as our own
# research citations; labelled row texts stay blocked everywhere; ALLOW_LABELLED=0 redacts instead).
# Rehearsal: TAG=HEAD SUBMIT=/tmp/x RECEIPTS_FAKE=1 tools/blind.sh run tests/smoke_cases.jsonl
set -eu
M=$(cd "$(dirname "$0")/.." && pwd)
W=${BLIND_WORKTREE:-$M/../vendor-receipts-blind}
TAG=${TAG:-blind-run}
SUBMIT=${SUBMIT:-$M}
die() { echo "blind: $*" >&2; exit 1; }

worktree() {
  REV=$(git -C "$M" rev-parse -q --verify "$TAG^{commit}") || die "no tag or commit $TAG"
  if [ ! -d "$W" ]; then
    git -C "$M" worktree add -q --detach "$W" "$REV"
    for f in data .cache llm.env runs; do [ -e "$W/$f" ] || ln -s "$M/$f" "$W/$f"; done
  else
    git -C "$W" checkout -q --detach "$REV"
  fi
  echo "blind: worktree $W at $(git -C "$W" rev-parse --short HEAD) ($TAG)"
}

batch() {  # $1 cases, $2 out, $3 tavily key item, $4 limiter dir, $5 extra make args; no --submit (rebuilt after)
  cd "$W"
  TAVILY_RPM="${TAVILY_RPM:-95}" TAVILY_RL_DIR="$4" \
  make batch CASES="$1" OUT="$2" RUN_ID="${RUN_ID:-blind}" CONC="${CONC:-10}" \
    MODEL_CONC="${MODEL_GLOBAL_CONC:-40}" MODEL_GLOBAL_CONC="${MODEL_GLOBAL_CONC:-40}" WALL_CAP="${WALL_CAP:-1380}" \
    JUDGE_RESERVE="${JUDGE_RESERVE:-600}" SINGLE_VIEW_BELOW="${SINGLE_VIEW_BELOW:-420}" \
    KC="kc env TAVILY_API_KEY=$3 LLM_OR_KEY=openrouter-api-key --" SUBMIT= ${5:-}
}

halves() {  # $1 csv -> runs/blind_half_a.csv, runs/blind_half_b.csv (rows by parity, header kept); any input format
  python3 - "$1" <<'PY'
import sys, os
src = sys.argv[1]
lines = open(src, encoding="utf-8-sig").read().splitlines(keepends=True)
csv_like = src.endswith(".csv")
head, rows = (lines[:1], lines[1:]) if csv_like else ([], lines)
if csv_like:  # quoted multi-line fields: split by parsed records, not by lines
    import csv, io
    recs = list(csv.reader(io.StringIO("".join(rows))))
    out = []
    for r in recs:
        b = io.StringIO(); csv.writer(b, lineterminator="\n").writerow(r); out.append(b.getvalue())
    rows = out
rows = [r for r in rows if r.strip()]
ext = ".csv" if csv_like else ".jsonl"
for name, part in (("a", rows[0::2]), ("b", rows[1::2])):
    with open(f"runs/blind_half_{name}{ext}", "w") as f:
        f.writelines(head + part)
print(f"halves: {len(rows[0::2])} + {len(rows[1::2])} of {len(rows)}")
PY
}

rebuild() {  # $1 csv: one submission from every verdict line so far (the last non-error line per id wins)
  cd "$W"
  : > runs/verdicts.jsonl
  for f in runs/verdicts_a.jsonl runs/verdicts_b.jsonl runs/rerun.jsonl; do [ -f "$f" ] && cat "$f" >> runs/verdicts.jsonl; done
  echo "blind: rebuild from $(wc -l < runs/verdicts.jsonl | tr -d ' ') verdict lines"
  python3 -m app.run --cases "$1" --from-jsonl runs/verdicts.jsonl --submit "$SUBMIT"
}

case ${1:-} in
  run)
    CSV=${2:-data/blind.csv}
    [ -f "$M/$CSV" ] || die "no $M/$CSV"
    worktree
    [ -e "$W/$CSV" ] || die "$W/$CSV not visible (data/ link)"
    mkdir -p "$SUBMIT"
    rm -f "$W"/runs/verdicts_a.jsonl "$W"/runs/verdicts_b.jsonl "$W"/runs/rerun.jsonl "$W"/runs/verdicts.jsonl
    (cd "$W" && halves "$CSV")
    ext=${CSV##*.}; [ "$ext" = csv ] || ext=jsonl
    # two processes, two Tavily accounts, two limiter buckets (Andrii 18:30: ~2 x 95 RPM)
    (batch "runs/blind_half_a.$ext" runs/verdicts_a.jsonl "${KEY_A:-tavily-api-key}" /tmp/vr-rl-key1 > "$W/runs/blind_a.log" 2>&1) &
    (batch "runs/blind_half_b.$ext" runs/verdicts_b.jsonl "${KEY_B:-tavily-api-key-2}" /tmp/vr-rl-key2 > "$W/runs/blind_b.log" 2>&1) &
    wait
    tail -2 "$W/runs/blind_a.log" "$W/runs/blind_b.log"
    rebuild "$CSV"
    echo "blind: done; next: tools/blind.sh ship (failed ids: tools/blind.sh rerun id1,id2)"
    ;;
  rerun)
    [ -n "${2:-}" ] || die "rerun needs ids"
    CSV=${CSV:-data/blind.csv}
    worktree
    RUN_ID=${RUN_ID:-blind-rerun} batch "$CSV" runs/rerun.jsonl "${KEY_B:-tavily-api-key-2}" /tmp/vr-rl-key2 "ONLY=$2"
    rebuild "$CSV"
    echo "blind: submission rebuilt from runs/verdicts.jsonl"
    ;;
  rebuild)
    worktree
    rebuild "${2:-data/blind.csv}"
    ;;
  ship)
    cd "$M"
    [ -f stackgini-verdicts.csv ] && [ -d traces ] || die "no stackgini-verdicts.csv or traces/ in $M"
    if [ "${ALLOW_LABELLED:-1}" = 1 ]; then
      export PREPUSH_ALLOW_LABELLED_SOURCES=1
      python3 tools/redact_labelled.py --row-text --keep-sources   # labelled row text stays blocked (Andrii 17:54)
    else
      python3 tools/redact_labelled.py --row-text
      python3 tools/redact_labelled.py
    fi
    git add stackgini-verdicts.csv traces
    n=$(($(wc -l < stackgini-verdicts.csv) - 1))
    git commit -qm "blind run: verdicts and traces ($n products, from tag $TAG $(git rev-parse --short "$TAG^{commit}"))"
    git push origin HEAD
    git log --oneline -1
    ;;
  flip)
    cd "$M"
    if [ "${ALLOW_LABELLED:-1}" = 1 ]; then python3 tools/publish_check.py --allow-labelled-sources --flip
    else python3 tools/publish_check.py --flip; fi
    ;;
  *) sed -n 2,13p "$0"; exit 2 ;;
esac
