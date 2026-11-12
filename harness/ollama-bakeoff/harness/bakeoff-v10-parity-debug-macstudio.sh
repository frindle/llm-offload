#!/bin/bash
# v10 PARITY — DEBUG TOP-UP. Phase 3, chained to run after the photo/clamshell
# parity round (pid 71116) exits.
#
# WHY THIS EXISTS: the first coverage audit counted CSV ROWS and concluded the
# debug cell was already at parity, n=6 for all nine models. It is not. Three of
# each newcomer's six debug rows are `ABORT_NO_WORKTREE` -- written before the
# plex-automation worktrees existed. Real reps:
#
#   every incumbent      6 real / 0 abort
#   gpt-oss:20b          3 real / 3 ABORT
#   ornith-1.5:9b        3 real / 3 ABORT
#   ornith-1.5:35b       3 real / 3 ABORT
#
# An abort row is not a measurement. Counting rows made the matrix look uniform
# when the newcomers had half the evidence -- and their "3/3 on the debug cell"
# headline rests on 3 reps where the incumbents were scored over 6.
#
# The worktrees exist now (created after those aborts, which is exactly why the
# other 3 reps succeeded), so this is a straight top-up: reps 4-6.
#
# Numbering continues at 4 so the rep column stays a real identifier and these
# rows pool with the catch-up round's 1-3 without collision.
set -uo pipefail
export RUN_TAG=v10parity
export ONLY_TASK=debug
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

case "$RESULTS_CSV" in
  */results-v10parity.csv) ;;
  *) echo "ABORT: RESULTS_CSV is '$RESULTS_CSV', expected results-v10parity.csv" >&2; exit 1 ;;
esac

ROSTER=(
  "ornith-1.5:9b|ornith-1.5-9b|2400|no|no"
  "ornith-1.5:35b|ornith-1.5-35b|3600|no|no"
  "gpt-oss:20b|gpt-oss-20b|3600|no|no"
)

echo "=== $RUN_TAG DEBUG TOP-UP START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

for REP in 4 5 6; do
  echo "=== $RUN_TAG DEBUG REP $REP/6 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG <<< "$M"
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" \
                 "$BACKEND" "$HOST" "$REP" "base" </dev/null
  done
done

echo "=== $RUN_TAG DEBUG TOP-UP COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
