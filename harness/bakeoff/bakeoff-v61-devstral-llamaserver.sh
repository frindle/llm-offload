#!/bin/bash
# devstral:24b, v6.1, both tasks, under its PROVEN config — llama-server +
# hand-written jinja tool template. Waits for the v6.1 Mac Studio leg.
#
# devstral is excluded from the native-Ollama leg because of bug 10: under
# native Ollama with the worker's system prompt it produced ZERO tool calls on
# photo-upload and narrated instead of writing on clamshell. Both v6 rows are
# quarantined as INVALID-OLLAMA-NATIVE. The switch to native was justified by
# a single-turn probe that returned a correct native tool_call -- that
# validated one imperative request, not multi-turn agentic behaviour, and the
# worker's own --system-prompt-file help already documented the failure
# verbatim. Do not "simplify" this back onto native Ollama.
#
# NOT using --system-prompt-file: the OpenHands scaffold prompt is recorded as
# making results WORSE. The jinja template alone is what was proven.
#
# Uses the PATCHED worker (bug 11 + bug 13), same as the rest of v6.1.
set -uo pipefail
export RUN_TAG=v61
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v61-clamshell-lib.sh"

LLAMA_HOST="http://localhost:8091"
BACKEND="macstudio-llamaserver"
TAG="${RUN_TAG}-${BACKEND}"

echo "[$TAG] waiting for the v6.1 macstudio leg to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "v61 MACSTUDIO COMPLETE" "$DRIVER_LOG"; do sleep 30; done

if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] starting llama-server for devstral ..." >> "$DRIVER_LOG"
  nohup /Users/user/bin/start-llama-server-devstral.sh \
    > "$OUTDIR/llama-server-devstral-v61.log" 2>&1 &
  disown
  for i in $(seq 1 60); do
    nc -z -G3 localhost 8091 2>/dev/null && break
    sleep 10
  done
fi

if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] ABORT: llama-server never came up on 8091" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,resell-tracker-photo-upload,ABORT_NO_LLAMA_SERVER,na,0,false,0,0,65536" >> "$RESULTS_CSV"
  echo "devstral:24b,$BACKEND,clamshell-confirmation-bridge,ABORT_NO_LLAMA_SERVER,na,0,false,0,0,65536" >> "$RESULTS_CSV"
  exit 1
fi
echo "[$TAG] llama-server up on 8091" >> "$DRIVER_LOG"

# Model name is bare "devstral": llama-server serves whatever it was launched
# with, not an Ollama tag. --api openai, ctx 65536, temp 0.2 are the settings
# from the FIXED run that verified PASSED three times on this exact task.
run_one_v61_llama() {
  local REPO="$1" TASK_NAME="$2" VERIFY="$3" TASK_TEXT="$4"
  local WT_DIR="$WT_BASE/$REPO/devstral-24b"
  local LOG="$OUTDIR/devstral-24b-$TASK_NAME-${TAG}.log"
  local BASE_SHA; BASE_SHA=$(baseline_for_repo "$REPO")

  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  echo "[$TAG] preflight: '$VERIFY' on pristine $REPO/devstral-24b ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED -- not dispatching" >> "$DRIVER_LOG"
    echo "devstral:24b,$BACKEND,$TASK_NAME,ABORT_PREFLIGHT,na,0,false,0,0,65536" >> "$RESULTS_CSV"
    return
  fi
  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local START_TS; START_TS=$(date +%s)
  echo "[$TAG] START: devstral / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model devstral --host "$LLAMA_HOST" --api openai \
    --cwd "$WT_DIR" --task "$TASK_TEXT" --verify "$VERIFY" \
    --max-iters 30 --num-ctx 65536 --temperature 0.2 \
    > "$LOG" 2>&1 &
  local WPID=$!
  ( sleep 1800 && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
  wait "$WPID" 2>/dev/null; local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local DUR ITERS TRACKED NEWF FILES TO VPASS
  DUR=$(( $(date +%s) - START_TS ))
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  TO="false"; [ "$DUR" -ge 1800 ] && TO="true"
  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"
  NEWF=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | grep -c '^??' | tr -d ' ')
  TRACKED=$(git -C "$WT_DIR" diff --name-only "$BASE_SHA" 2>/dev/null | wc -l | tr -d ' ')
  FILES=$((TRACKED + NEWF))

  git -C "$WT_DIR" diff "$BASE_SHA" > "$OUTDIR/devstral-24b-$TASK_NAME-${TAG}.diff" 2>/dev/null
  git -C "$WT_DIR" status --porcelain -uall >> "$OUTDIR/devstral-24b-$TASK_NAME-${TAG}.diff" 2>/dev/null

  echo "[$TAG] DONE: devstral / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TO files=$FILES iters=$ITERS" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,$TASK_NAME,$EXIT,$VPASS,$DUR,$TO,$FILES,$ITERS,65536" >> "$RESULTS_CSV"
}

run_one_v61_llama "resell-tracker" "resell-tracker-photo-upload"  "npm run build" "$TASK_RESELL"
run_one_v61_llama "clamshell"      "clamshell-confirmation-bridge" "swift build"  "$TASK_CLAMSHELL"

echo "=== $TAG DEVSTRAL V6.1 COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
