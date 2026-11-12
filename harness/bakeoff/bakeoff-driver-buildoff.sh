#!/bin/bash
# Build-off driver -- Mac Studio, 9-model roster (qwen3-14b-agentic excluded,
# confirmed unavailable locally and unpullable from the internet -- see
# Claude/Ollama-Dispatch-Log.md). Two REAL implementation tasks per model
# (not review-only like the earlier bakeoff): resell-tracker mobile photo
# upload, and Clamshell ConfirmationBridge. Each model x task pair runs in
# its own pre-created git worktree/branch (bakeoff-build-2026-08-22/<repo>/<slug>,
# branch bakeoff-build/<slug>) so results are isolated and diffable afterward.
# Sequential, one model resident in Ollama at a time (64GB unified memory) --
# same load -> run -> unload -> delete pattern as tonight's bakeoff.

set -u

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://localhost:11434"
UNRAID_HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"
COPY_HELPER="/Users/user/bin/copy-ollama-model-from-unraid.py"
DEVSTRAL_PROMPT="/Users/user/bin/devstral-system-prompt.txt"
TIMEOUT_S=1800
UNRAID_WAIT_TIMEOUT_S=3600

LOCKFILE="/tmp/bakeoff-driver-buildoff.lock"
if [ -f "$LOCKFILE" ]; then
  EXISTING_PID=$(cat "$LOCKFILE" 2>/dev/null)
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "ERROR: bakeoff-driver-buildoff.sh already running (pid $EXISTING_PID)." >&2
    exit 1
  fi
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

MODELS=(
  "qwen3-coder-next:q4_K_M"
  "deepseek-r1:70b"
  "deepseek-r1:32b-qwen-distill-q8_0"
  "qwen3.8:27b-q8_0"
  "deepseek-r1:32b"
  "devstral:24b"
  "qwen2.5-coder:14b"
  "deepseek-r1:14b"
  "MFDoom/deepseek-r1-tool-calling:14b"
)

ensure_model_available() {
  local MODEL="$1"
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qxF "$MODEL"; then
    echo "[ensure] $MODEL already local" >> "$DRIVER_LOG"
    return 0
  fi
  echo "[ensure] $MODEL not local -- trying Unraid's shared store over LAN..." >> "$DRIVER_LOG"
  if mount | grep -q "on /Volumes/data "; then
    if python3 "$COPY_HELPER" pull "$MODEL" >> "$DRIVER_LOG" 2>&1; then
      echo "[ensure] $MODEL copied from Unraid's shared store" >> "$DRIVER_LOG"
      return 0
    fi
    echo "[ensure] $MODEL not found on Unraid's shared store (or copy failed)" >> "$DRIVER_LOG"
  else
    echo "[ensure] /Volumes/data not mounted -- skipping LAN-copy path" >> "$DRIVER_LOG"
  fi
  echo "[ensure] pulling $MODEL fresh from the internet (found nowhere else)..." >> "$DRIVER_LOG"
  if ! ollama pull "$MODEL" >> "$DRIVER_LOG" 2>&1; then
    echo "[ensure] FAILED to pull $MODEL -- skipping this model entirely" >> "$DRIVER_LOG"
    return 1
  fi
  return 0
}

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
    devstral:*)
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
    devstral:*)
      echo "--temperature 0.2"
      ;;
    *)
      echo "--temperature 0.6"
      ;;
  esac
}

mkdir -p "$OUTDIR"
DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"
[ -f "$RESULTS_CSV" ] || echo "model,task,exit_code,duration_s,timed_out,files_changed" > "$RESULTS_CSV"

TOTAL=$((${#MODELS[@]} * 2))
N=0

verify_cmd_for_repo() {
  case "$1" in
    resell-tracker) echo "npm run build" ;;
    clamshell) echo "swift build" ;;
  esac
}

run_task() {
  local MODEL="$1" TASK_NAME="$2" TASK_TEXT="$3" REPO="$4" SLUG="$5"
  N=$((N + 1))
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME.log"

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
  if [ "$MODEL" = "devstral:24b" ]; then
    EXTRA_ARGS+=(--system-prompt-file "$DEVSTRAL_PROMPT")
  fi
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      # Native tool_calls never renders for deepseek-r1 distills (confirmed
      # github.com/ollama/ollama/issues/8517, see ollama-worker.py --help).
      # Tonight's review-only bakeoff didn't pass this and still scored
      # these models clean, but a review can be faked without real tool use
      # -- a build task cannot. Required here for real file writes.
      EXTRA_ARGS+=(--manual-tools)
      ;;
  esac

  local START_TS
  START_TS=$(date +%s)
  echo "[$N/$TOTAL] START: $MODEL / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  local VERIFY_CMD
  VERIFY_CMD=$(verify_cmd_for_repo "$REPO")

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$HOST" \
    --cwd "$WT_DIR" \
    --task "$MODEL_TASK" \
    --verify "$VERIFY_CMD" \
    --max-iters 30 \
    --num-ctx 32768 \
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
  local TIMEDOUT="false"
  [ "$EXIT" -eq 143 ] && TIMEDOUT="true"

  # Preserve whatever the model produced as a real commit on its dedicated
  # branch, whether or not the dispatch itself "succeeded" -- partial/broken
  # output is still real data for the review pass.
  local FILES_CHANGED=0
  ( cd "$WT_DIR" && git add -A > /dev/null 2>&1
    FILES_CHANGED=$(git diff --cached --name-only | wc -l | tr -d ' ')
    if [ "$FILES_CHANGED" -gt 0 ]; then
      git commit -q -m "buildoff: $MODEL / $TASK_NAME (exit=$EXIT dur=${DUR}s)" > /dev/null 2>&1
    fi
    echo "$FILES_CHANGED" > /tmp/buildoff_files_changed.$$
  )
  FILES_CHANGED=$(cat /tmp/buildoff_files_changed.$$ 2>/dev/null || echo 0)
  rm -f /tmp/buildoff_files_changed.$$

  echo "[$N/$TOTAL] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMEDOUT files_changed=$FILES_CHANGED" >> "$DRIVER_LOG"
  echo "$MODEL,$TASK_NAME,$EXIT,$DUR,$TIMEDOUT,$FILES_CHANGED" >> "$RESULTS_CSV"
}

for MODEL in "${MODELS[@]}"; do
  SLUG=$(echo "$MODEL" | tr ':' '-' | tr '.' '-' | tr '/' '-')

  # Skip the (potentially 50GB+) download entirely if both tasks for this
  # model already have real log files -- avoids re-pulling an already-done
  # model just to discover there's nothing left to do for it.
  LOG1="$OUTDIR/$SLUG-resell-tracker-photo-upload.log"
  LOG2="$OUTDIR/$SLUG-clamshell-confirmation-bridge.log"
  if [ -s "$LOG1" ] && [ -s "$LOG2" ]; then
    N=$((N + 2))
    echo "[$N/$TOTAL] SKIP (both tasks already logged, not re-pulling model): $MODEL" >> "$DRIVER_LOG"
    continue
  fi

  MODEL_AVAILABLE=1
  ensure_model_available "$MODEL" || MODEL_AVAILABLE=0

  if [ "$MODEL_AVAILABLE" -eq 1 ]; then
    run_task "$MODEL" "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "$SLUG"
    run_task "$MODEL" "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell" "$SLUG"
  else
    N=$((N + 2))
    echo "[$N/$TOTAL] SKIP (model unavailable): $MODEL both tasks" >> "$DRIVER_LOG"
  fi

  if [ "$MODEL_AVAILABLE" -eq 0 ]; then
    continue
  fi

  echo "[unload] stopping $MODEL in local Ollama memory..." >> "$DRIVER_LOG"
  curl -s -m 30 -X POST "$HOST/api/generate" -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" > /dev/null 2>&1

  echo "[unraid-check] confirming $MODEL exists on Unraid ($UNRAID_HOST) before deleting local copy..." >> "$DRIVER_LOG"
  ON_UNRAID=$(curl -s -m 15 "$UNRAID_HOST/api/tags" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    names = {m['name'] for m in d.get('models', [])}
    print('yes' if '$MODEL' in names else 'no')
except Exception:
    print('error')
" 2>/dev/null)

  if [ "$ON_UNRAID" != "yes" ]; then
    echo "[unraid-check] not present ($ON_UNRAID) -- trying LAN push, then waiting on a pull (up to ${UNRAID_WAIT_TIMEOUT_S}s)..." >> "$DRIVER_LOG"
    if mount | grep -q "on /Volumes/data "; then
      python3 "$COPY_HELPER" push "$MODEL" >> "$DRIVER_LOG" 2>&1
    fi
    ON_UNRAID=$(curl -s -m 15 "$UNRAID_HOST/api/tags" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    names = {m['name'] for m in d.get('models', [])}
    print('yes' if '$MODEL' in names else 'no')
except Exception:
    print('error')
" 2>/dev/null)
    if [ "$ON_UNRAID" != "yes" ]; then
      curl -s -m "$UNRAID_WAIT_TIMEOUT_S" -X POST "$UNRAID_HOST/api/pull" -d "{\"name\":\"$MODEL\",\"stream\":false}" >> "$DRIVER_LOG" 2>&1
      ON_UNRAID=$(curl -s -m 15 "$UNRAID_HOST/api/tags" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    names = {m['name'] for m in d.get('models', [])}
    print('yes' if '$MODEL' in names else 'no')
except Exception:
    print('error')
" 2>/dev/null)
    fi
  fi

  if [ "$ON_UNRAID" = "yes" ]; then
    echo "[delete] confirmed on Unraid -- removing $MODEL from Mac Studio local disk..." >> "$DRIVER_LOG"
    ollama rm "$MODEL" >> "$DRIVER_LOG" 2>&1
  else
    echo "[delete] SKIPPED -- could not confirm $MODEL on Unraid (status: $ON_UNRAID), keeping local copy for safety." >> "$DRIVER_LOG"
  fi
done

echo "=== BUILD-OFF COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
