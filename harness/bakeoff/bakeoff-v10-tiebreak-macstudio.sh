#!/bin/bash
# v10 — PRE-REGISTERED TIEBREAK REP for ornith-1.5:35b. Phase 4.
#
# The two-turn driver pre-registers (bakeoff-v10-catchup-twoturn-macstudio.sh
# lines 44-46): "n=2, with a pre-registered tiebreak: if a model's two reps yield
# DIFFERENT classifications, run exactly one tiebreak rep. Fixed now so it cannot
# be invented after seeing results."
#
# ornith-1.5:35b rep 1 RECOVERED (turn 2 verify yes, 15 iters, 111s) and rep 2
# did not (30 iters, 454s). The reps differ, so the tiebreak is OWED under the
# protocol -- it is not a new decision, it is the pre-registered consequence.
#
# EXACTLY ONE rep, for EXACTLY ONE model. Not a re-run of the round. Running
# more than the pre-registered single rep would be choosing n after seeing the
# data, which is the thing the pre-registration exists to prevent.
#
# Rows land in results-v10parity-tiebreak.csv -- NOT in the completed
# results-v10catchup-twoturn.csv, which is written up and must stay frozen.
set -uo pipefail
export RUN_TAG=v10parity-tiebreak
export ONLY_TASK=clam
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"
# ⚠ MUST NOT equal $RESULTS_CSV. Sourcing the lib writes its 28-column v8 header
# to results-${RUN_TAG}.csv immediately (bakeoff-v8-lib.sh:77). If the two-turn
# CSV shares that name, the `[ -f ... ] || echo <two-turn header>` below sees a
# file that already exists, skips its own header, and the run then appends
# 11-column two-turn rows underneath a 28-column header.
#
# That is not hypothetical -- it happened on the first tiebreak run. The mixed
# file parsed without error, `turn2_outcome` came back None for every row, and
# the routing generator filtered on `turn2_outcome == "RAN"` and SILENTLY DROPPED
# the recovery it had just measured. Aggregate read 2/14 when it was 3/15.
# A schema collision that still parses is worse than one that crashes.
R3_CSV="$OUTDIR/results-${RUN_TAG}-twoturn.csv"
if [ "$R3_CSV" = "$RESULTS_CSV" ]; then
  echo "ABORT: two-turn CSV collides with the lib's results file ($R3_CSV)" >&2
  exit 1
fi
R3_TURN2_VERIFY="swift build && swift run Clamshell confirmation-bridge-selftest"

case "$R3_CSV" in
  */results-v10parity-tiebreak.csv) ;;
  *) echo "ABORT: unexpected results path '$R3_CSV'" >&2; exit 1 ;;
esac
[ -f "$R3_CSV" ] || echo "model,rep,turn1_verify,turn1_files,turn2_outcome,turn2_verify,turn2_files,turn2_iters,turn2_dur_s,classification,transcript" > "$R3_CSV"

MODEL="ornith-1.5:35b"; SLUG="ornith-1.5-35b"; TMO=3600; REP=3
CTX=$(ctx_for "$MODEL")
WT_DIR="$WT_BASE/clamshell/$SLUG"
LOG1="$OUTDIR/$SLUG-clamshell-confirmation-bridge-$RUN_TAG-$BACKEND-base-r"
SAMPLING=(--temperature 0.6 --top-p 0.95)   # non-qwen default, as in the source driver

compose_turn2() {
  local WT="$1" VERIFY_TEXT="$2"
  local DIFF; DIFF=$(git -C "$WT" diff HEAD 2>/dev/null | head -400)
  local UNTRACKED; UNTRACKED=$(git -C "$WT" ls-files --others --exclude-standard 2>/dev/null | head -40)
  cat <<COMPOSED
You previously worked in this repository and produced the changes below. Your work does not pass its verification command.

--- YOUR CHANGES (git diff) ---
${DIFF}

--- FILES YOU CREATED ---
${UNTRACKED}

--- VERIFICATION OUTPUT ---
Command: ${R3_TURN2_VERIFY}

${VERIFY_TEXT}

Fix it. The repository is exactly as you left it.
COMPOSED
}

echo "=== $RUN_TAG TIEBREAK START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

run_model_v8 "$MODEL" "$SLUG" "$TMO" "no" "no" "$BACKEND" "$HOST" "$REP" "base" </dev/null

T1LOG="${LOG1}${REP}.log"
T1VERIFY="no"; grep -aq "VERIFY PASSED" "$T1LOG" 2>/dev/null && T1VERIFY="yes"
T1FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

if [ "$T1VERIFY" = "yes" ]; then
  echo "$MODEL,$REP,yes,$T1FILES,NO_DEFECT_FOUND,na,na,na,na,na,$(basename "$T1LOG")" >> "$R3_CSV"
  unload_model "$MODEL" "$HOST"
elif [ "$T1FILES" -eq 0 ]; then
  echo "$MODEL,$REP,no,0,NO_TURN1_OUTPUT,na,na,na,na,na,$(basename "$T1LOG")" >> "$R3_CSV"
  unload_model "$MODEL" "$HOST"
else
  VERIFY_TEXT=$(sed -n '/running verify command/,$p' "$T1LOG" 2>/dev/null | head -120)
  TASK2=$(compose_turn2 "$WT_DIR" "$VERIFY_TEXT")
  LOG2="$OUTDIR/$SLUG-clamshell-r3turn2-$RUN_TAG-$BACKEND-base-r${REP}.log"
  restart_inference_server "$HOST" "$MODEL" "$CTX" || true
  T2START=$(date +%s)
  python3 "$WORKER" \
    --model "$MODEL" --host "$HOST" --cwd "$WT_DIR" \
    --task "$TASK2" --verify "$R3_TURN2_VERIFY" \
    --max-iters 30 --num-ctx "$CTX" \
    "${SAMPLING[@]}" > "$LOG2" 2>&1 &
  WPID=$!
  ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
  wait "$WPID" 2>/dev/null
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null
  T2DUR=$(( $(date +%s) - T2START ))
  T2VERIFY="no"; grep -aq "VERIFY PASSED" "$LOG2" 2>/dev/null && T2VERIFY="yes"
  T2ITERS=$(grep -ac "iteration " "$LOG2" 2>/dev/null || echo 0)
  T2FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  echo "$MODEL,$REP,no,$T1FILES,RAN,$T2VERIFY,$T2FILES,$T2ITERS,$T2DUR,,$(basename "$LOG2")" >> "$R3_CSV"
  unload_model "$MODEL" "$HOST"
fi

echo "=== $RUN_TAG TIEBREAK COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
