#!/bin/bash
# devstral:24b, v7 integration task, under its PROVEN config — llama-server +
# hand-written jinja tool template. NOT launched automatically; waits for the
# v7 Mac Studio leg to finish.
#
# devstral is excluded from bakeoff-v7-macstudio.sh for the same reason it was
# quarantined in v6: native Ollama produces zero tool calls for this model
# under the worker's system prompt (bug 10). The worker's own
# --system-prompt-file help says so verbatim. Do not "simplify" this back onto
# native Ollama -- that trade has already been made once and cost two rows.
#
# NOT using --system-prompt-file: the OpenHands scaffold prompt is recorded as
# making results WORSE. The jinja template alone is what was proven.
set -uo pipefail
export RUN_TAG=v7
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7-lib.sh"

LLAMA_HOST="http://localhost:8091"
BACKEND="macstudio-llamaserver"
TAG="${RUN_TAG}-${BACKEND}"

echo "[$TAG] waiting for the v7 macstudio leg to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "v7 MACSTUDIO COMPLETE" "$DRIVER_LOG"; do sleep 30; done

if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] starting llama-server for devstral ..." >> "$DRIVER_LOG"
  nohup /Users/user/bin/start-llama-server-devstral.sh \
    > "$OUTDIR/llama-server-devstral-v7.log" 2>&1 &
  disown
  for i in $(seq 1 60); do
    nc -z -G3 localhost 8091 2>/dev/null && break
    sleep 10
  done
fi

if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] ABORT: llama-server never came up on 8091" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,resell-tracker-mobile-upload,ABORT_NO_LLAMA_SERVER,0,false,0,no,no,no,0,0,65536" >> "$RESULTS_CSV"
  exit 1
fi
echo "[$TAG] llama-server up on 8091" >> "$DRIVER_LOG"

WT_DIR="$WT_BASE/resell-tracker/devstral-24b"
LOG="$OUTDIR/devstral-24b-resell-tracker-mobile-upload-${TAG}.log"
VERIFY="npm run build"

git -C "$WT_DIR" reset --hard "$BASELINE_resell_tracker" >/dev/null 2>&1
git -C "$WT_DIR" clean -fd >/dev/null 2>&1

if [ ! -f "$WT_DIR/$TARGET_FILE" ]; then
  echo "[$TAG] ABORT: $TARGET_FILE missing at baseline -- task premise broken" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,resell-tracker-mobile-upload,ABORT_NO_TARGET,0,false,0,no,no,no,0,0,65536" >> "$RESULTS_CSV"
  exit 1
fi

echo "[$TAG] preflight: '$VERIFY' on pristine resell-tracker/devstral-24b ..." >> "$DRIVER_LOG"
if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
  echo "[$TAG] ABORT: pristine-tree verify FAILED -- not dispatching" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,resell-tracker-mobile-upload,ABORT_PREFLIGHT,0,false,0,no,no,no,0,0,65536" >> "$RESULTS_CSV"
  exit 1
fi
git -C "$WT_DIR" reset --hard "$BASELINE_resell_tracker" >/dev/null 2>&1
git -C "$WT_DIR" clean -fd >/dev/null 2>&1

START_TS=$(date +%s)
echo "[$TAG] START: devstral / resell-tracker-mobile-upload @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

python3 "$WORKER" \
  --model devstral --host "$LLAMA_HOST" --api openai \
  --cwd "$WT_DIR" --task "$TASK_MOBILE" --verify "$VERIFY" \
  --max-iters 30 --num-ctx 65536 --temperature 0.2 \
  > "$LOG" 2>&1 &
WPID=$!
( sleep 1800 && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
wait "$WPID" 2>/dev/null; EXIT=$?
kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

DUR=$(( $(date +%s) - START_TS ))
ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
TO="false"; [ "$DUR" -ge 1800 ] && TO="true"

# Graded against the pinned baseline, identically to bakeoff-v7-lib.sh --
# working-tree-only grading scores a model that commits its work as a no-op.
# Keep these four lines in sync with the lib if either changes.
B="$BASELINE_resell_tracker"
NEWF=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | grep -c '^??' | tr -d ' ')
TRACKED=$(git -C "$WT_DIR" diff --name-only "$B" 2>/dev/null | wc -l | tr -d ' ')
FILES=$((TRACKED + NEWF))
TOUCHED="no"; git -C "$WT_DIR" diff --name-only "$B" -- "$TARGET_FILE" 2>/dev/null | grep -q . && TOUCHED="yes"
CAPTURE="no"; git -C "$WT_DIR" diff "$B" -- "$TARGET_FILE" 2>/dev/null | grep -qE '^\+.*capture=' && CAPTURE="yes"
BH=$(git -C "$WT_DIR" show "$B:$TARGET_FILE" 2>/dev/null | grep -c 'group-hover' | tr -d ' ')
NH=$(cat "$WT_DIR/$TARGET_FILE" 2>/dev/null | grep -c 'group-hover' | tr -d ' ')
HOVER="no"; [ "${NH:-0}" -lt "${BH:-0}" ] && HOVER="yes"

echo "[$TAG] DONE: devstral / resell-tracker-mobile-upload exit=$EXIT dur=${DUR}s timedout=$TO files=$FILES touched_target=$TOUCHED capture=$CAPTURE fixed_hover=$HOVER new_files=$NEWF iters=$ITERS" >> "$DRIVER_LOG"
echo "devstral:24b,$BACKEND,resell-tracker-mobile-upload,$EXIT,$DUR,$TO,$FILES,$TOUCHED,$CAPTURE,$HOVER,$NEWF,$ITERS,65536" >> "$RESULTS_CSV"
echo "=== $TAG DEVSTRAL V7 COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
