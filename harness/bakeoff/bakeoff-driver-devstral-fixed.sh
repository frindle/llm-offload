#!/bin/bash
# Real devstral build-off dispatch, now that its actual root cause (Ollama's
# GGUF shipping a chat template with zero tool-calling logic) is fixed via a
# hand-written template at ~/bin/devstral-tool-template.jinja. Requires
# llama-server already running against devstral with that template loaded
# (--jinja --chat-template-file ~/bin/devstral-tool-template.jinja).
#
# Task text and verify commands copied verbatim from bakeoff-driver-buildoff.sh
# for exact consistency with the other 8 models' results. max-iters 30
# (not the reduced 10 used for the separate Unraid speed-run).
#
# CORRECTED 2026-08-22, twice:
# Attempt 1 (no system prompt override): devstral produced zero tool calls
# on the real (complex, open-ended) resell-tracker task -- two straight
# iterations of narrating its plan in prose even after the harness's
# corrective nudge explicitly told it to call a tool now.
# Attempt 2 (devstral's own official OpenHands system prompt via
# --system-prompt-file): made things WORSE on a cheap verification task --
# it called read_file successfully on iteration 1, then flatly denied
# having any file access at all on iteration 2, right after using it.
# Reverted -- that prompt actively conflicts with something here, not a fix.
# Attempt 3 (this one): back to the DEFAULT system prompt (confirmed
# working cleanly for a full tool-call round-trip in isolation), plus the
# per-model task-text addition bakeoff-driver-buildoff.sh already had for
# devstral specifically ("your first response must contain a tool call,
# not just text") that got dropped when this dedicated script was written
# instead of reused. Untested combination, not a guess carried over blind.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://localhost:8091"
WORKER="/Users/user/bin/ollama-worker.py"
DEVSTRAL_PROMPT="/Users/user/bin/devstral-system-prompt.txt"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"

TASK1_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

TASK2_CLAMSHELL="Build a new Swift module for this Clamshell project called ConfirmationBridge that implements challenge-signed remote confirmation using P-256 (ECDSA). Purpose: let a privileged action on the host require an explicit signed approval from a human physically at the client, not just anyone who can reach the host. This should be a standalone module, not yet wired into the real streaming protocol.

Requirements:
1. A device can produce a signed response to a challenge using a P-256 key.
2. A correctly-signed response for a given challenge verifies successfully.
3. A replayed signature/nonce (reusing a previous valid response) must be rejected.
4. An expired challenge/nonce must be rejected -- the challenge has a limited validity window.

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Wire the self-test up so it is runnable (e.g. as a CLI subcommand or test target), consistent with how this project already organizes its code. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, whether it actually compiles, and whether the self-test actually passes all three checks."

verify_cmd_for_repo() {
  case "$1" in
    resell-tracker) echo "npm run build" ;;
    clamshell) echo "swift build" ;;
  esac
}

mkdir -p "$OUTDIR"
[ -f "$RESULTS_CSV" ] || echo "model,task,exit_code,duration_s,timed_out,files_changed" > "$RESULTS_CSV"

run_task() {
  local TASK_NAME="$1" TASK_TEXT="$2" REPO="$3"
  local WT_DIR="$WT_BASE/$REPO/devstral-24b"
  local LOG="$OUTDIR/devstral-24b-$TASK_NAME-FIXED.log"

  if [ -s "$LOG" ]; then
    echo "[devstral-fixed] SKIP (log exists): $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi
  if [ ! -d "$WT_DIR" ]; then
    echo "[devstral-fixed] SKIP (worktree missing): $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi

  local START_TS
  START_TS=$(date +%s)
  echo "[devstral-fixed] START: devstral:24b / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  local VERIFY_CMD
  VERIFY_CMD=$(verify_cmd_for_repo "$REPO")

  local FULL_TASK="$TASK_TEXT

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."

  python3 "$WORKER" \
    --model devstral \
    --host "$HOST" \
    --api openai \
    --cwd "$WT_DIR" \
    --task "$FULL_TASK" \
    --verify "$VERIFY_CMD" \
    --max-iters 30 \
    --num-ctx 65536 \
    --temperature 0.2 \
    > "$LOG" 2>&1 &
  local WORKER_PID=$!

  ( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
  local WATCHER_PID=$!

  wait "$WORKER_PID" 2>/dev/null
  local EXIT=$?

  kill "$WATCHER_PID" 2>/dev/null
  wait "$WATCHER_PID" 2>/dev/null

  local END_TS
  END_TS=$(date +%s)
  local DUR=$((END_TS - START_TS))
  local TIMED_OUT="false"
  [ "$DUR" -ge "$TIMEOUT_S" ] && TIMED_OUT="true"

  local FILES_CHANGED
  FILES_CHANGED=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

  echo "[devstral-fixed] DONE: devstral:24b / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT" >> "$DRIVER_LOG"
  echo "devstral:24b-FIXED,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES_CHANGED" >> "$RESULTS_CSV"
}

run_task "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker"
run_task "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell"

echo "=== DEVSTRAL-FIXED DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
