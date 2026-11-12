#!/bin/bash
# WALL CALIBRATION for a newly added model — measure its iteration rate on a
# FRESH host instead of guessing a timeout for it.
#
# v8's whole wall-sizing doctrine is "size the wall from fresh-host iteration
# rate; the wall must never be the binding constraint; max_iters is the budget."
# A model added to the roster with a hand-picked timeout quietly reintroduces
# exactly the guesswork that doctrine exists to abolish -- and the cost of
# getting it wrong is a `timed_out=true` row that looks like a model result and
# is not.
#
# So: one short unscored run on a clean host, purely to observe seconds per
# iteration. The result is NOT a data row. It is written to its own log and the
# tag makes that unmissable.
#
# Usage: bakeoff-v8-calibrate.sh <model>
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
HOSTTEL="$BASE/bakeoff/harness/bakeoff-v8-hosttel.py"
HOST="http://localhost:11434"
WORKER="/Users/user/bin/ollama-worker-v7.py"
MODEL="${1:?usage: bakeoff-v8-calibrate.sh <model>}"
OUT="$BASE/model-buildoff-2026-08-22/calibration-$(echo "$MODEL" | tr '/:' '--').log"

source "$BASE/bakeoff/harness/bakeoff-v8-lib.sh"

CTX=$(ctx_for "$MODEL")
if [ "$CTX" -eq 0 ]; then
  echo "ABORT: no native context recorded for '$MODEL' -- add it to native_ctx_for()"
  exit 1
fi

echo "--- calibration: $MODEL (ctx $CTX) ---"
restart_inference_server "$HOST" "$MODEL" "$CTX" || echo "  (gate not met -- rate will read pessimistic, noted)"
echo "host: $(python3 "$HOSTTEL")"

# A deliberately small budget: 6 iterations is plenty to see a per-iteration
# rate, and short enough that a slow model cannot eat the calibration slot.
WT="$BASE/bakeoff-build-2026-08-22/clamshell/$(echo "$MODEL" | tr '/:' '--')"
[ -d "$WT" ] || WT="$BASE/bakeoff-build-2026-08-22/clamshell/qwen3-coder-30b"
if [ ! -d "$WT" ]; then
  echo "ABORT: no worktree to calibrate in ($WT)"
  exit 1
fi
git -C "$WT" reset --hard 8803d67 >/dev/null 2>&1
git -C "$WT" clean -fd >/dev/null 2>&1

START=$(date +%s)
python3 "$WORKER" --model "$MODEL" --host "$HOST" --cwd "$WT" \
  --task "$TASK_CLAMSHELL" --max-iters 6 --num-ctx "$CTX" \
  --temperature 0.2 --top-p 0.95 --top-k 20 > "$OUT" 2>&1
DUR=$(( $(date +%s) - START ))

ITERS=$(grep -ac -- '--- iteration ' "$OUT" 2>/dev/null); ITERS=${ITERS:-0}
WARM=$(grep -a -o 'warmup_s=[0-9.]*' "$OUT" 2>/dev/null | tail -1 | cut -d= -f2); WARM=${WARM%.*}; WARM=${WARM:-0}
LOOP=$(( DUR - WARM )); [ "$LOOP" -lt 0 ] && LOOP=0

echo
echo "MODEL:        $MODEL"
echo "duration:     ${DUR}s  (warmup ${WARM}s, loop ${LOOP}s)"
echo "iterations:   $ITERS"
if [ "$ITERS" -gt 0 ] && [ "$LOOP" -gt 0 ]; then
  RATE=$(( LOOP / ITERS ))
  echo "RATE:         ${RATE} s/iteration on a fresh host"
  # Same formula the drivers document: 900s warmup budget + rate x 30 x 1.8
  SUGGEST=$(( 900 + RATE * 30 * 18 / 10 ))
  echo "SUGGESTED WALL: ${SUGGEST}s   (900 warmup + ${RATE} x 30 iters x 1.8)"
  echo
  # Measured-then-ignored is theatre. Compare against the roster and FAIL if the
  # roster wall is too small, so the pipeline stops instead of dispatching a
  # model we already know will hit its wall -- a timed_out row is an instrument
  # defect, not a result.
  ROSTER_WALL=$(grep -o "\"${MODEL}|[^|]*|[0-9]*" "$BASE/bakeoff/harness/bakeoff-v8-macstudio.sh" 2>/dev/null | awk -F'|' '{print $3}' | head -1)
  if [ -n "$ROSTER_WALL" ]; then
    echo "ROSTER WALL:    ${ROSTER_WALL}s"
    if [ "$SUGGEST" -gt "$ROSTER_WALL" ]; then
      echo "FAIL: measured rate implies ${SUGGEST}s but the roster allows ${ROSTER_WALL}s."
      echo "      Raise the wall in bakeoff-v8-macstudio.sh before dispatching."
      exit 1
    fi
    echo "OK: roster wall is adequate for the measured rate."
  else
    echo "NOTE: could not read a roster wall for $MODEL -- compare by hand."
  fi
else
  echo "RATE:         indeterminate -- read $OUT before trusting any wall for this model"
fi
grep -a -o 'tokens prompt=[0-9]* output=[0-9]*.*' "$OUT" 2>/dev/null | tail -1 | sed 's/^/tokens: /'
echo "--- calibration complete (THIS IS NOT A DATA ROW) ---"
