#!/bin/bash
# Dispatches the 3 Unraid-sized remaining build-off models (deepseek-r1:14b,
# MFDoom/deepseek-r1-tool-calling:14b, qwen2.5-coder:14b) against Unraid's
# native Ollama (no llama-server there -- confirmed 2026-08-22, see
# Ollama-Dispatch-Log.md). deepseek-r1:32b is deliberately excluded here --
# too large for Unraid's 12GB RTX 3080, belongs on Mac Studio instead.
#
# Task text, task_for_model()/sampling_args_for_model() logic, and verify
# commands are copied verbatim from bakeoff-driver-buildoff.sh for exact
# methodological consistency with the other 5 models already tested. The
# one deliberate difference from that driver: per-model --num-ctx instead
# of a hardcoded 32768 (that hardcode would silently truncate the
# deepseek-r1 family, which needs 131072 -- their real native context,
# researched and confirmed in the vault, not assumed), and --max-iters 10
# instead of 30 (the owner's ask, for faster turnaround this round).
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"

MODELS=(
  "deepseek-r1:14b"
  "MFDoom/deepseek-r1-tool-calling:14b"
  "qwen2.5-coder:14b"
)

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

task_for_model() {
  local BASE_TASK="$1"
  local MODEL="$2"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      echo "$BASE_TASK

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."
      ;;
    *)
      echo "$BASE_TASK"
      ;;
  esac
}

sampling_args_for_model() {
  local MODEL="$1"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      echo "--temperature 0.6 --top-p 0.95"
      ;;
    qwen*)
      echo "--temperature 0.6 --top-p 0.95 --top-k 20"
      ;;
    *)
      echo "--temperature 0.6"
      ;;
  esac
}

# Per-model context, researched and written to Ollama-Dispatch-Log.md
# 2026-08-22 -- NOT the driver's old hardcoded 32768.
numctx_for_model() {
  local MODEL="$1"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) echo "131072" ;;
    qwen2.5-coder:*) echo "32768" ;;
    *) echo "32768" ;;
  esac
}

verify_cmd_for_repo() {
  case "$1" in
    resell-tracker) echo "npm run build" ;;
    clamshell) echo "swift build" ;;
  esac
}

mkdir -p "$OUTDIR"
[ -f "$RESULTS_CSV" ] || echo "model,task,exit_code,duration_s,timed_out,files_changed" > "$RESULTS_CSV"

TOTAL=$((${#MODELS[@]} * 2))
N=0

run_task() {
  local MODEL="$1" TASK_NAME="$2" TASK_TEXT="$3" REPO="$4" SLUG="$5"
  N=$((N + 1))
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-UNRAID.log"

  if [ -s "$LOG" ]; then
    echo "[$N/$TOTAL] SKIP (log exists): $MODEL / $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi
  if [ ! -d "$WT_DIR" ]; then
    echo "[$N/$TOTAL] SKIP (worktree missing): $MODEL / $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi

  local MODEL_TASK
  MODEL_TASK=$(task_for_model "$TASK_TEXT" "$MODEL")
  local SAMPLING_ARGS
  read -ra SAMPLING_ARGS <<< "$(sampling_args_for_model "$MODEL")"
  local EXTRA_ARGS=()
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      EXTRA_ARGS+=(--manual-tools)
      ;;
  esac
  local NUMCTX
  NUMCTX=$(numctx_for_model "$MODEL")

  local START_TS
  START_TS=$(date +%s)
  echo "[$N/$TOTAL] START (Unraid): $MODEL / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  local VERIFY_CMD
  VERIFY_CMD=$(verify_cmd_for_repo "$REPO")

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$HOST" \
    --cwd "$WT_DIR" \
    --task "$MODEL_TASK" \
    --verify "$VERIFY_CMD" \
    --max-iters 10 \
    --num-ctx "$NUMCTX" \
    "${SAMPLING_ARGS[@]+"${SAMPLING_ARGS[@]}"}" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" \
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

  echo "[$N/$TOTAL] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT" >> "$DRIVER_LOG"
  echo "$MODEL,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES_CHANGED" >> "$RESULTS_CSV"
}

for MODEL in "${MODELS[@]}"; do
  SLUG=$(echo "$MODEL" | tr '/:' '-')
  run_task "$MODEL" "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "$SLUG"
  run_task "$MODEL" "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell" "$SLUG"
done

echo "=== UNRAID REMAINING-MODELS DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
