#!/bin/bash
# devstral:24b re-run under its PROVEN config — llama-server + hand-written
# jinja tool template — after the v6 Mac Studio matrix finishes.
#
# WHY: v6 ran devstral through native Ollama and it produced ZERO tool calls on
# photo-upload (prose, nudge, prose, stop) and narrated instead of writing on
# clamshell. Those two rows are quarantined as INVALID-OLLAMA-NATIVE.
#
# The switch to Ollama-native was justified on `capabilities: ['completion',
# 'tools']` plus a single-turn probe that returned a correct native tool_call.
# That validated ONE imperative request, not multi-turn agentic behaviour under
# the worker's system prompt. The worker's own --system-prompt-file help already
# said so verbatim: "Needed for devstral, which produces zero tool calls on this
# Ollama build without its own OpenHands-scaffold system prompt." We had the
# answer in our own code and traded a proven config for an unproven one.
#
# The counterfactual is on disk: devstral-24b-*-FIXED.log (12:25 today) shows
# this llama-server config making real tool calls from iteration 1 and running
# 12 tool executions on photo-upload; the vault records it converging with
# verify PASSED three separate times on this exact task.
#
# NOT using --system-prompt-file: bakeoff-driver-devstral-fixed.sh records that
# the OpenHands scaffold prompt made results WORSE on a cheap verification task.
# The jinja template alone is what was proven.
#
# Labeled backend "macstudio-llamaserver" so it never silently merges with
# native-Ollama rows in the results table.
set -uo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v2-lib.sh"

LLAMA_HOST="http://localhost:8091"
BACKEND="macstudio-llamaserver"
TAG="${RUN_TAG}-${BACKEND}"

echo "[$TAG] waiting for the v6 macstudio matrix to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "v6 MACSTUDIO COMPLETE" "$DRIVER_LOG"; do sleep 30; done

# Start llama-server if it isn't already up.
if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] starting llama-server for devstral ..." >> "$DRIVER_LOG"
  nohup /Users/user/bin/start-llama-server-devstral.sh \
    > "$OUTDIR/llama-server-devstral.log" 2>&1 &
  disown
  for i in $(seq 1 60); do
    nc -z -G3 localhost 8091 2>/dev/null && break
    sleep 10
  done
fi

if ! nc -z -G3 localhost 8091 2>/dev/null; then
  echo "[$TAG] ABORT: llama-server never came up on 8091 -- see llama-server-devstral.log" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,resell-tracker-photo-upload,ABORT_NO_LLAMA_SERVER,0,false,0,0,65536" >> "$RESULTS_CSV"
  echo "devstral:24b,$BACKEND,clamshell-confirmation-bridge,ABORT_NO_LLAMA_SERVER,0,false,0,0,65536" >> "$RESULTS_CSV"
  exit 1
fi
echo "[$TAG] llama-server up on 8091" >> "$DRIVER_LOG"

# Model name is bare "devstral" here -- llama-server serves whatever model it
# was launched with, not an Ollama tag. --api openai, ctx 65536, temp 0.2:
# exactly the FIXED-run settings.
run_one() {
  local REPO="$1" TASK_NAME="$2" VERIFY="$3" TASK_TEXT="$4"
  local WT_DIR="$WT_BASE/$REPO/devstral-24b"
  local LOG="$OUTDIR/devstral-24b-$TASK_NAME-${TAG}.log"

  local BASE_SHA; BASE_SHA=$(baseline_for_repo "$REPO")
  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  echo "[$TAG] preflight: '$VERIFY' on pristine $REPO/devstral-24b ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED -- not dispatching" >> "$DRIVER_LOG"
    echo "devstral:24b,$BACKEND,$TASK_NAME,ABORT_PREFLIGHT,0,false,0,0,65536" >> "$RESULTS_CSV"
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

  local DUR FILES ITERS
  DUR=$(( $(date +%s) - START_TS ))
  FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  local TO="false"; [ "$DUR" -ge 1800 ] && TO="true"

  echo "[$TAG] DONE: devstral / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TO files=$FILES iters=$ITERS" >> "$DRIVER_LOG"
  echo "devstral:24b,$BACKEND,$TASK_NAME,$EXIT,$DUR,$TO,$FILES,$ITERS,65536" >> "$RESULTS_CSV"
}

run_one "resell-tracker" "resell-tracker-photo-upload"   "npm run build" "$TASK_RESELL"
run_one "clamshell"      "clamshell-confirmation-bridge"  "swift build"   "$TASK_CLAMSHELL"

echo "=== $TAG DEVSTRAL LLAMA-SERVER RE-RUN COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
