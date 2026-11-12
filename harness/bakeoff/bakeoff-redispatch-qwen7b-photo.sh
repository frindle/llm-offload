#!/bin/bash
# Clean re-dispatch of qwen2.5-coder:7b / resell-tracker-photo-upload on Unraid.
#
# Why: the 13:07 run's verify failed with `sh: next: command not found` (exit
# 127) because `npm install` did not land in this worktree until 13:14:48 --
# after that run's verify step. The result is an environment artifact, not a
# model signal, so it cannot stand. See Ollama-Dispatch-Log.md, "Worktree
# environment-parity bug", 2026-08-22.
#
# Waits for the unraid-7b driver to finish before starting, so this does not
# contend with deepseek-r1:7b for the 3080's VRAM.
#
# Settings are copied verbatim from bakeoff-driver-unraid-7b.sh for
# qwen2.5-coder:7b: 32K context, native tool-calling (no --manual-tools),
# --temperature 0.6 --top-p 0.95 --top-k 20, no deepseek-style nudge.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
OUTDIR="$BASE/model-buildoff-2026-08-22"
WT_DIR="$BASE/bakeoff-build-2026-08-22/resell-tracker/qwen2.5-coder-7b-unraid"
HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"
MODEL="qwen2.5-coder:7b"
TASK_NAME="resell-tracker-photo-upload"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"
LOG="$OUTDIR/qwen2.5-coder-7b-unraid-$TASK_NAME-UNRAID7B-RERUN.log"

TASK="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

echo "[redispatch] waiting for unraid-7b driver to finish before starting @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "UNRAID 7B BAKE-OFF COMPLETE" "$DRIVER_LOG"; do
  sleep 30
done

# Guard the environment bug that caused this re-run in the first place.
if [ ! -x "$WT_DIR/node_modules/.bin/next" ]; then
  echo "[redispatch] ABORT: $WT_DIR/node_modules/.bin/next missing -- run npm install first" >> "$DRIVER_LOG"
  exit 1
fi

# Reset the worktree: the invalid run left 8 untracked Flask files behind.
# -fd (not -fdx) so gitignored node_modules survives.
git -C "$WT_DIR" reset --hard >/dev/null 2>&1
git -C "$WT_DIR" clean -fd >/dev/null 2>&1

START_TS=$(date +%s)
echo "[redispatch] START: $MODEL / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

python3 "$WORKER" \
  --model "$MODEL" \
  --host "$HOST" \
  --cwd "$WT_DIR" \
  --task "$TASK" \
  --verify "npm run build" \
  --max-iters 30 \
  --num-ctx 32768 \
  --temperature 0.6 --top-p 0.95 --top-k 20 \
  > "$LOG" 2>&1 &
WORKER_PID=$!

( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
WATCHER_PID=$!

wait "$WORKER_PID" 2>/dev/null
EXIT=$?
kill "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null

END_TS=$(date +%s); DUR=$((END_TS - START_TS))
TIMED_OUT="false"; [ "$DUR" -ge "$TIMEOUT_S" ] && TIMED_OUT="true"
FILES_CHANGED=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

echo "[redispatch] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES_CHANGED" >> "$DRIVER_LOG"
echo "$MODEL-RERUN,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES_CHANGED" >> "$RESULTS_CSV"
echo "=== QWEN7B PHOTO-UPLOAD RE-DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
