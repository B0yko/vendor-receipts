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
  git -C "$M" rev-parse -q --verify "$TAG^{commit}" >/dev/null || die "no tag or commit $TAG"
  if [ ! -d "$W" ]; then
    git -C "$M" worktree add -q --detach "$W" "$TAG"
    for f in data .cache llm.env runs; do [ -e "$W/$f" ] || ln -s "$M/$f" "$W/$f"; done
  else
    git -C "$W" checkout -q --detach "$TAG"
  fi
  echo "blind: worktree $W at $(git -C "$W" rev-parse --short HEAD) ($TAG)"
}

batch() {  # $1 cases, $2 out, $3 extra make args
  cd "$W"
  make batch CASES="$1" OUT="$2" SUBMIT="$SUBMIT" RUN_ID="${RUN_ID:-blind}" CONC="${CONC:-10}" \
    MODEL_CONC="${MODEL_GLOBAL_CONC:-40}" MODEL_GLOBAL_CONC="${MODEL_GLOBAL_CONC:-40}" WALL_CAP="${WALL_CAP:-1380}" \
    JUDGE_RESERVE="${JUDGE_RESERVE:-600}" SINGLE_VIEW_BELOW="${SINGLE_VIEW_BELOW:-420}" ${3:-}
}

case ${1:-} in
  run)
    CSV=${2:-data/blind.csv}
    [ -f "$M/$CSV" ] || die "no $M/$CSV"
    worktree
    [ -e "$W/$CSV" ] || die "$W/$CSV not visible (data/ link)"
    mkdir -p "$SUBMIT"
    batch "$CSV" "${OUT:-runs/verdicts.jsonl}"
    echo "blind: done; next: tools/blind.sh ship (failed ids: tools/blind.sh rerun id1,id2)"
    ;;
  rerun)
    [ -n "${2:-}" ] || die "rerun needs ids"
    CSV=${CSV:-data/blind.csv}
    worktree
    RUN_ID=${RUN_ID:-blind-rerun} batch "$CSV" runs/rerun.jsonl "ONLY=$2"
    # the rerun's export holds only the rerun ids: rebuild the full submission (the last non-error line per id wins)
    cat runs/verdicts.jsonl runs/rerun.jsonl > runs/verdicts_all.jsonl
    python3 -m app.run --cases "$CSV" --from-jsonl runs/verdicts_all.jsonl --submit "$SUBMIT"
    echo "blind: submission rebuilt from runs/verdicts_all.jsonl"
    ;;
  rebuild)
    CSV=${2:-data/blind.csv}
    worktree; cd "$W"
    python3 -m app.run --cases "$CSV" --from-jsonl "${OUT:-runs/verdicts.jsonl}" --submit "$SUBMIT"
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
