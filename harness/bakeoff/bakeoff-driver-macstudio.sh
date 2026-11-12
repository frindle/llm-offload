#!/bin/bash
# Model bakeoff driver -- Mac Studio local models, 18 repos each, biggest model first.
# Mirrors bakeoff-driver.sh (same repos/task/idempotency/timeout pattern) but targets
# localhost Ollama instead of Unraid's.
#
# Run 3 (2026-08-21, later same night): Mac Studio tests the FULL model roster
# (everything used anywhere tonight) since its 64GB is unified memory, not a
# hard-capped discrete VRAM pool like Unraid's ~11-12GB card -- Unraid only
# reasonably fits the smaller 14B-class models (see bakeoff-driver.sh).
#
# Before each model's repo loop: ensure_model_available() checks local first,
# then copies from Unraid's mounted shared store over LAN (much faster than a
# fresh internet pull) via copy-ollama-model-from-unraid.py, and only falls
# back to a real `ollama pull` if the model exists nowhere yet -- in which
# case, after pulling, it's pushed to Unraid's shared store too (LAN copy,
# not a second redundant internet download) so Unraid's library stays complete.
#
# After each model's repo loop: unload from memory, confirm (or wait for) a
# copy on Unraid, then delete the local copy before the next model loads --
# one model's disk footprint resident at a time, never delete the only copy.
# Unraid's Ollama is NOT at its host management IP (203.0.113.33), it's at its
# own dedicated IP, 192.0.2.82:11434.

set -u

BASE="/Users/user/Desktop/GitHub Projects"
OUTDIR="$BASE/model-bakeoff-2026-08-21-macstudio"
HOST="http://localhost:11434"
UNRAID_HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"
COPY_HELPER="/Users/user/bin/copy-ollama-model-from-unraid.py"
DEVSTRAL_PROMPT="/Users/user/bin/devstral-system-prompt.txt"
TIMEOUT_S=1800
UNRAID_WAIT_TIMEOUT_S=3600

# Lock: same real incident as bakeoff-driver.sh tonight -- a duplicate launch
# raced the original for ~19 minutes before being caught. Never again silently.
LOCKFILE="/tmp/bakeoff-driver-macstudio.lock"
if [ -f "$LOCKFILE" ]; then
  EXISTING_PID=$(cat "$LOCKFILE" 2>/dev/null)
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "ERROR: bakeoff-driver-macstudio.sh is already running (pid $EXISTING_PID). Refusing to start a second instance." >&2
    exit 1
  fi
  echo "Stale lockfile found (pid $EXISTING_PID not running) -- removing and continuing." >&2
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Biggest to smallest -- the full roster used anywhere tonight (Mac Studio's
# 64GB unified memory can reasonably handle all of it, one at a time).
MODELS=(
  "qwen3-coder-next:q4_K_M"
  "deepseek-r1:70b"
  "deepseek-r1:32b-qwen-distill-q8_0"
  "qwen3.8:27b-q8_0"
  "deepseek-r1:32b"
  "devstral:24b"
  "qwen3-14b-agentic"
  "qwen2.5-coder:14b"
  "deepseek-r1:14b"
  "MFDoom/deepseek-r1-tool-calling:14b"
)

# Checks local first, then Unraid's mounted shared store (LAN copy, fast),
# then falls back to a real `ollama pull` (internet) -- and if it had to
# pull fresh, pushes the result to Unraid's shared store afterward so a
# model Mac Studio discovers first doesn't stay Mac-Studio-only. Returns
# non-zero (model unavailable) only if every path fails.
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

  echo "[ensure] pushing freshly-pulled $MODEL to Unraid's shared store (LAN copy, not a second internet pull)..." >> "$DRIVER_LOG"
  if mount | grep -q "on /Volumes/data "; then
    python3 "$COPY_HELPER" push "$MODEL" >> "$DRIVER_LOG" 2>&1 &
  else
    echo "[ensure] /Volumes/data not mounted -- cannot push to Unraid right now, Unraid's library stays incomplete for this model" >> "$DRIVER_LOG"
  fi
  return 0
}

REPOS=(
  award-search
  clamshell
  drivecam-backup
  ev-dashboard
  firmware
  flight-staffing-forecast
  homelab-database
  meshtastic-bitchat-bridge
  mitm-control
  MS-Shifts-To-ICS
  network-bandwidth-monitor
  Pentair-Homebridge
  plex-automation
  resell-tracker
  resell-tracker-extension
  teams-shifts-staffing-export
  vacation-planner
  vm-hotswap
)

TASK="You are reviewing the codebase in the current working directory. First, explore its structure -- use run_bash to check for a README and to list the main source files (e.g. find . -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.py' -o -name '*.js' -o -name '*.go' -o -name '*.swift' \) | grep -v node_modules | grep -v '/\.git/' | head -60). Then read the most important/complex source files -- prioritize files handling authentication, payments, external API calls, credential/secret handling, or core business logic over boilerplate/config/tests. You do not need to read every file; use judgment about what's worth reviewing given the codebase's apparent size and purpose.

Identify real, specific bugs, security vulnerabilities, and significant code-quality issues. Do NOT invent generic advice -- every finding must cite a specific file path and, where possible, a line number or function name, and must describe a concrete failure scenario (not just \"this could be improved\").

When you are done, respond with a structured written report and no further tool calls:
1. A one-paragraph summary of what this project does.
2. A numbered list of findings, each as: [SEVERITY] file:line -- one-sentence description of the concrete problem.
3. If you found nothing concerning after a genuine review, say so explicitly rather than inventing filler findings.

This is a review only -- do not modify any files."

# Per-model sampling settings, added 2026-08-22: every model here was
# running at --temperature 0 (greedy decoding). Both Qwen's and DeepSeek-R1's
# own model cards explicitly warn against this -- documented to cause
# endless-repetition failures -- confirmed live tonight as the actual root
# cause of a real runaway-generation crash (qwen3-coder-next generated
# 35,000+ tokens without stopping on a large-file edit). Mistral's Devstral
# card recommends 0.15-0.2, a different value again -- not one-size-fits-all.
task_for_model() {
  local MODEL="$1"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      # DeepSeek-R1's own guidance: no system prompt (conflicts with its
      # RL-tuned reasoning) -- so this goes in the task text instead.
      echo "$TASK

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."
      ;;
    devstral:*)
      # The OpenHands system prompt alone was NOT enough (confirmed live
      # 2026-08-21/22: devstral still gave zero tool calls in run2 with only
      # the system prompt applied) -- the documented recipe needs this
      # task-level instruction too, system-prompt-only placement isn't reliable.
      echo "$TASK

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."
      ;;
    *)
      echo "$TASK"
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
[ -f "$RESULTS_CSV" ] || echo "model,repo,exit_code,duration_s,timed_out" > "$RESULTS_CSV"

TOTAL=$((${#MODELS[@]} * ${#REPOS[@]}))
N=0

for MODEL in "${MODELS[@]}"; do
  SLUG=$(echo "$MODEL" | tr ':' '-' | tr '.' '-' | tr '/' '-')
  MODEL_DIR="$OUTDIR/$SLUG"
  mkdir -p "$MODEL_DIR"

  MODEL_AVAILABLE=1
  ensure_model_available "$MODEL" || MODEL_AVAILABLE=0

  MODEL_TASK=$(task_for_model "$MODEL")
  read -ra SAMPLING_ARGS <<< "$(sampling_args_for_model "$MODEL")"

  EXTRA_ARGS=()
  if [ "$MODEL" = "devstral:24b" ]; then
    EXTRA_ARGS+=(--system-prompt-file "$DEVSTRAL_PROMPT")
  fi

  for REPO in "${REPOS[@]}"; do
    N=$((N + 1))
    LOG="$MODEL_DIR/$REPO.log"

    if [ "$MODEL_AVAILABLE" -eq 0 ]; then
      echo "[$N/$TOTAL] SKIP (model unavailable): $MODEL / $REPO" >> "$DRIVER_LOG"
      continue
    fi

    if [ -s "$LOG" ]; then
      echo "[$N/$TOTAL] SKIP (log exists): $MODEL / $REPO" >> "$DRIVER_LOG"
      continue
    fi

    REPO_PATH="$BASE/$REPO"
    if [ ! -d "$REPO_PATH" ]; then
      echo "[$N/$TOTAL] SKIP (repo not found): $MODEL / $REPO" >> "$DRIVER_LOG"
      continue
    fi

    START_TS=$(date +%s)
    echo "[$N/$TOTAL] START: $MODEL / $REPO @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

    # ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} not "${EXTRA_ARGS[@]}" -- the latter
    # throws "unbound variable" under set -u on an empty array on bash 3.2
    # (macOS's system /bin/bash, confirmed live 2026-08-21 as the actual
    # cause of a real incident that deleted ~175GB of models). This script
    # has always been invoked via `bash bakeoff-driver-macstudio.sh` using
    # whichever bash resolves first in PATH, not guaranteed to be the
    # Homebrew 5.3 that doesn't have this bug -- the script itself must be
    # safe regardless of which bash actually runs it.
    python3 "$WORKER" \
      --model "$MODEL" \
      --host "$HOST" \
      --cwd "$REPO_PATH" \
      --task "$MODEL_TASK" \
      --max-iters 15 \
      --num-ctx 32768 \
      "${SAMPLING_ARGS[@]+"${SAMPLING_ARGS[@]}"}" \
      "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" \
      > "$LOG" 2>&1 &
    WORKER_PID=$!

    ( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
    WATCHER_PID=$!

    wait "$WORKER_PID" 2>/dev/null
    EXIT=$?

    kill "$WATCHER_PID" 2>/dev/null
    wait "$WATCHER_PID" 2>/dev/null

    END_TS=$(date +%s)
    DUR=$((END_TS - START_TS))
    TIMEDOUT="false"
    [ "$EXIT" -eq 143 ] && TIMEDOUT="true"

    echo "[$N/$TOTAL] DONE: $MODEL / $REPO exit=$EXIT dur=${DUR}s timedout=$TIMEDOUT" >> "$DRIVER_LOG"
    echo "$MODEL,$REPO,$EXIT,$DUR,$TIMEDOUT" >> "$RESULTS_CSV"
  done

  if [ "$MODEL_AVAILABLE" -eq 0 ]; then
    echo "[cleanup] SKIPPED -- $MODEL was never available, nothing to unload/delete" >> "$DRIVER_LOG"
    continue
  fi

  # Per the owner: one model resident at a time -- unload from memory, then confirm
  # (or wait for) a copy on Unraid, then delete the local copy, before the next
  # model's batch starts. Never delete the only copy of a model.
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
    echo "[unraid-check] not present ($ON_UNRAID) -- trying LAN push to Unraid's mounted share first, then waiting on a pull (up to ${UNRAID_WAIT_TIMEOUT_S}s)..." >> "$DRIVER_LOG"
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

echo "=== MAC STUDIO BAKEOFF COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
