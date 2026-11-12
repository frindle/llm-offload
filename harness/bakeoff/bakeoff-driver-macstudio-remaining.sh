#!/bin/bash
# Completes the real 2-task build-off on Mac Studio: models never run,
# models that timed out/crashed for infra reasons on Unraid, and false-pass
# retries (explored but never wrote code) with a stronger nudge.
#
# Per-model num-ctx and --manual-tools follow the corrected pattern from
# bakeoff-driver-unraid-remaining.sh (131072 for the deepseek-r1 family +
# MFDoom, not the original buildoff.sh's stale 32768 hardcode -- that
# hardcode was already identified as a real bug by the time that script was
# written). max-iters 30 / TIMEOUT_S 1800, matching the original full
# bakeoff (not the reduced max-iters=10 used for the Unraid speed-run).
#
# devstral runs separately at the end via llama-server (its Ollama chat
# template doesn't render tool calls correctly) -- start_devstral/
# stop_devstral wrap it so Ollama-loaded models stay unloaded from VRAM
# while llama-server is up and vice versa.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://localhost:11434"
LLAMA_HOST="http://localhost:8091"
WORKER="/Users/user/bin/ollama-worker.py"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"

OLLAMA_MODELS=(
  "deepseek-r1:32b"
  "deepseek-r1:14b"
  "MFDoom/deepseek-r1-tool-calling:14b"
  "qwen2.5-coder:14b"
  "qwen3-coder-next:q4_K_M"
  "qwen3.8:27b-q8_0"
  "deepseek-r1:32b-qwen-distill-q8_0"
  "qwen2.5-coder:7b"
  "deepseek-r1:7b"
)

TASK1_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

TASK2_CLAMSHELL="Build a new Swift module for this Clamshell project called ConfirmationBridge that implements challenge-signed remote confirmation using P-256 (ECDSA). Purpose: let a privileged action on the host require an explicit signed approval from a human physically at the client, not just anyone who can reach the host. This should be a standalone module, not yet wired into the real streaming protocol.

Requirements:
1. A device can produce a signed response to a challenge using a P-256 key.
2. A correctly-signed response for a given challenge verifies successfully.
3. A replayed signature/nonce (reusing a previous valid response) must be rejected.
4. An expired challenge/nonce must be rejected -- the challenge has a limited validity window.

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Wire the self-test up so it is runnable (e.g. as a CLI subcommand or test target), consistent with how this project already organizes its code. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code."

task_for_model() {
  local BASE_TASK="$1" MODEL="$2" RETRY_NUDGE="$3"
  local TEXT="$BASE_TASK"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      TEXT="$TEXT

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."
      ;;
  esac
  if [ "$RETRY_NUDGE" = "yes" ]; then
    TEXT="$TEXT

This is a retry. A previous attempt explored the codebase thoroughly but stopped without writing any code at all. Exploration alone is not a complete answer -- you must actually call write_file or edit_file to implement the feature/module before responding with a final summary. A summary describing what you *would* build, without having built it, is not acceptable."
  fi
  echo "$TEXT"
}

sampling_args_for_model() {
  case "$1" in
    deepseek-r1:*|MFDoom/*) echo "--temperature 0.6 --top-p 0.95" ;;
    qwen*) echo "--temperature 0.6 --top-p 0.95 --top-k 20" ;;
    *) echo "--temperature 0.6" ;;
  esac
}

numctx_for_model() {
  case "$1" in
    deepseek-r1:*|MFDoom/*) echo "131072" ;;
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

unload_model() {
  local MODEL="$1"
  echo "[unload] stopping $MODEL in local Ollama memory..." >> "$DRIVER_LOG"
  curl -s -m 30 -X POST "$HOST/api/generate" -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" > /dev/null 2>&1
}

run_task() {
  local MODEL="$1" TASK_NAME="$2" TASK_TEXT="$3" REPO="$4" RETRY_NUDGE="$5" HOST_OVERRIDE="$6"
  local SLUG
  SLUG=$(echo "$MODEL" | tr '/:' '-')
  case "$MODEL" in
    qwen2.5-coder:7b|deepseek-r1:7b) SLUG="${SLUG}-macstudio" ;;
  esac
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-MACSTUDIO2.log"

  if [ -s "$LOG" ]; then
    echo "[macstudio2] SKIP (log exists): $MODEL / $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi
  if [ ! -d "$WT_DIR" ]; then
    echo "[macstudio2] SKIP (worktree missing): $MODEL / $TASK_NAME" >> "$DRIVER_LOG"
    return
  fi

  local MODEL_TASK
  MODEL_TASK=$(task_for_model "$TASK_TEXT" "$MODEL" "$RETRY_NUDGE")
  local SAMPLING_ARGS
  read -ra SAMPLING_ARGS <<< "$(sampling_args_for_model "$MODEL")"
  local EXTRA_ARGS=()
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) EXTRA_ARGS+=(--manual-tools) ;;
  esac
  local NUMCTX
  NUMCTX=$(numctx_for_model "$MODEL")
  local USE_HOST="${HOST_OVERRIDE:-$HOST}"

  local START_TS
  START_TS=$(date +%s)
  echo "[macstudio2] START: $MODEL / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  local VERIFY_CMD
  VERIFY_CMD=$(verify_cmd_for_repo "$REPO")

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$USE_HOST" \
    --cwd "$WT_DIR" \
    --task "$MODEL_TASK" \
    --verify "$VERIFY_CMD" \
    --max-iters 30 \
    --num-ctx "$NUMCTX" \
    "${SAMPLING_ARGS[@]+"${SAMPLING_ARGS[@]}"}" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" \
    > "$LOG" 2>&1 &
  local WORKER_PID=$!

  ( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
  local WATCHER_PID=$!

  wait "$WORKER_PID" 2>/dev/null
  local EXIT=$?
  kill "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null

  local END_TS DUR TIMED_OUT FILES_CHANGED
  END_TS=$(date +%s); DUR=$((END_TS - START_TS))
  TIMED_OUT="false"; [ "$DUR" -ge "$TIMEOUT_S" ] && TIMED_OUT="true"
  FILES_CHANGED=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

  echo "[macstudio2] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES_CHANGED" >> "$DRIVER_LOG"
  echo "$MODEL,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES_CHANGED" >> "$RESULTS_CSV"
}

# --- main Ollama loop ---
# qwen3.8 and deepseek-r1:32b-qwen-distill-q8_0 already resident/attempted --
# unload everything first for a clean, predictable RAM budget.
unload_model "devstral:24b"
unload_model "qwen3.8:27b-q8_0"

for MODEL in "${OLLAMA_MODELS[@]}"; do
  RETRY_NUDGE="no"
  [ "$MODEL" = "qwen3.8:27b-q8_0" ] && RETRY_NUDGE="yes"
  run_task "$MODEL" "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "$RETRY_NUDGE" ""
  run_task "$MODEL" "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell" "$RETRY_NUDGE" ""
  unload_model "$MODEL"
done

# --- devstral, llama-server, photo-upload retry only ---
echo "[macstudio2] starting llama-server for devstral retry..." >> "$DRIVER_LOG"
if /Users/user/bin/start-llama-server-devstral.sh >> "$DRIVER_LOG" 2>&1; then
  run_task "devstral:24b" "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "yes" "$LLAMA_HOST"
  if [ -f /tmp/llama-server-devstral.pid ]; then
    kill "$(cat /tmp/llama-server-devstral.pid)" 2>/dev/null
    rm -f /tmp/llama-server-devstral.pid
  fi
else
  echo "[macstudio2] llama-server failed to start, skipping devstral retry" >> "$DRIVER_LOG"
fi

echo "=== MAC STUDIO REMAINING-MODELS DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
