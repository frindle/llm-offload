#!/bin/bash
# Re-dispatch of MFDoom/deepseek-r1-tool-calling:14b, both tasks.
#
# Why: both tasks died in 0s/1s on 2026-08-22 with
#   RuntimeError: model MFDoom/deepseek-r1-tool-calling:14b not found in
#   SMB source at .../registry.ollama.ai/library/MFDoom/...
# _manifest_path() in ollama-worker.py hardcoded "library/", which is only
# correct for OFFICIAL models -- namespaced (org/user) models sit directly
# under the registry root. The model never ran; the driver recorded
# exit=1/files=0, which reads as a model failure. Fixed in ollama-worker.py
# and verified: the manifest now resolves. This model scored 18/18 in the
# earlier Mac Studio bakeoff, so the lost result is worth recovering.
#
# Waits for the macstudio-remaining driver to finish so this doesn't
# contend for memory with the models still queued there.
#
# Settings copied verbatim from bakeoff-driver-macstudio-remaining.sh for
# the MFDoom/* case: 131072 context, --manual-tools, --temperature 0.6
# --top-p 0.95, plus the deepseek-style "first response must contain a
# tool call" nudge.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://localhost:11434"
WORKER="/Users/user/bin/ollama-worker.py"
MODEL="MFDoom/deepseek-r1-tool-calling:14b"
SLUG="MFDoom-deepseek-r1-tool-calling-14b"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"

NUDGE="

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."

TASK1_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

TASK2_CLAMSHELL="Build a new Swift module for this Clamshell project called ConfirmationBridge that implements challenge-signed remote confirmation using P-256 (ECDSA). Purpose: let a privileged action on the host require an explicit signed approval from a human physically at the client, not just anyone who can reach the host. This should be a standalone module, not yet wired into the real streaming protocol.

Requirements:
1. A device can produce a signed response to a challenge using a P-256 key.
2. A correctly-signed response for a given challenge verifies successfully.
3. A replayed signature/nonce (reusing a previous valid response) must be rejected.
4. An expired challenge/nonce must be rejected -- the challenge has a limited validity window.

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Wire the self-test up so it is runnable (e.g. as a CLI subcommand or test target), consistent with how this project already organizes its code. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code."

echo "[mfdoom-rerun] waiting for macstudio-remaining driver to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "MAC STUDIO REMAINING-MODELS DISPATCH COMPLETE" "$DRIVER_LOG"; do
  sleep 30
done

run_task() {
  local TASK_NAME="$1" TASK_TEXT="$2" REPO="$3" VERIFY="$4"
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-MACSTUDIO2-RERUN.log"

  if [ ! -d "$WT_DIR" ]; then
    echo "[mfdoom-rerun] SKIP (worktree missing): $TASK_NAME -- $WT_DIR" >> "$DRIVER_LOG"
    return
  fi

  # Reset any partial state from the failed run.
  git -C "$WT_DIR" reset --hard >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local START_TS; START_TS=$(date +%s)
  echo "[mfdoom-rerun] START: $MODEL / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$HOST" \
    --cwd "$WT_DIR" \
    --task "${TASK_TEXT}${NUDGE}" \
    --verify "$VERIFY" \
    --max-iters 30 \
    --num-ctx 131072 \
    --temperature 0.6 --top-p 0.95 \
    --manual-tools \
    > "$LOG" 2>&1 &
  local WORKER_PID=$!

  ( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
  local WATCHER_PID=$!

  wait "$WORKER_PID" 2>/dev/null
  local EXIT=$?
  kill "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null

  local END_TS DUR TIMED_OUT FILES
  END_TS=$(date +%s); DUR=$((END_TS - START_TS))
  TIMED_OUT="false"; [ "$DUR" -ge "$TIMEOUT_S" ] && TIMED_OUT="true"
  FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

  echo "[mfdoom-rerun] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES" >> "$DRIVER_LOG"
  echo "$MODEL-RERUN,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES" >> "$RESULTS_CSV"
}

run_task "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "npm run build"
run_task "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell" "swift build"

echo "=== MFDOOM RE-DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
