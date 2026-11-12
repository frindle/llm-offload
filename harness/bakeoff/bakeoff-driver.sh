#!/bin/bash
# Model bakeoff driver -- 18 repos x 3 models, corrected rules from the 2026-08-21 incident:
# one attempt per pair (no retries), no flag deviation, idempotency skip-if-log-exists,
# 8-min hard timeout per dispatch.

set -u

BASE="/Users/user/Desktop/GitHub Projects"
OUTDIR="$BASE/model-bakeoff-2026-08-21-v3"
HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"

# Lock: prevents a second concurrent launch of this script. A real incident
# on 2026-08-21 -- a duplicate invocation ran alongside the original for
# ~19 minutes, racing the same idempotency check and double-dispatching at
# least one repo -- caught late, not by design. Never again silently.
LOCKFILE="/tmp/bakeoff-driver.lock"
if [ -f "$LOCKFILE" ]; then
  EXISTING_PID=$(cat "$LOCKFILE" 2>/dev/null)
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "ERROR: bakeoff-driver.sh is already running (pid $EXISTING_PID, lockfile $LOCKFILE). Refusing to start a second instance." >&2
    exit 1
  fi
  echo "Stale lockfile found (pid $EXISTING_PID not running) -- removing and continuing." >&2
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT
# Raised from 480s: the 8-min cap was killing dispatches mid-genuine-progress
# (confirmed live -- Pentair-Homebridge was cleanly reading auth.ts/pentairApi.ts/
# index.ts in order at iteration 6/15 when the wall clock ran out, not stuck).
# The owner's call: let everything actually finish for a fair analysis; if something
# truly hangs, deal with it then rather than pre-capping everyone.
TIMEOUT_S=1800

# Only models that reasonably fit Unraid's ~11-12GB discrete VRAM go here --
# the other 4 models pulled onto Unraid tonight (deepseek-r1:70b, the 32b
# variants, qwen3.8:27b-q8_0) would spill overwhelmingly to CPU (not a
# meaningful GPU test) and belong on Mac Studio's unified-memory setup
# instead. MFDoom/deepseek-r1-tool-calling:14b is the same size class as the
# other 14B models already here, so it's a reasonable fit once it finishes
# pulling (last in the pull queue as of 2026-08-21 -- may not be present yet).
MODELS=("qwen2.5-coder:14b" "deepseek-r1:14b" "qwen3-14b-agentic" "MFDoom/deepseek-r1-tool-calling:14b")

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
# 35,000+ tokens without stopping on a large-file edit). Settings below are
# each family's own documented recommendation, not a guess.
task_for_model() {
  local MODEL="$1"
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*)
      # DeepSeek-R1's own guidance: do NOT use a system prompt (conflicts
      # with its RL-tuned reasoning, documented to degrade performance) --
      # so the "must call a tool" instruction goes in the task text itself.
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
  SLUG=$(echo "$MODEL" | tr ':' '-' | tr '.' '-')
  MODEL_DIR="$OUTDIR/$SLUG"
  mkdir -p "$MODEL_DIR"

  MODEL_TASK=$(task_for_model "$MODEL")
  read -ra SAMPLING_ARGS <<< "$(sampling_args_for_model "$MODEL")"

  for REPO in "${REPOS[@]}"; do
    N=$((N + 1))
    LOG="$MODEL_DIR/$REPO.log"

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

    python3 "$WORKER" \
      --model "$MODEL" \
      --host "$HOST" \
      --cwd "$REPO_PATH" \
      --task "$MODEL_TASK" \
      --max-iters 15 \
      --num-ctx 32768 \
      "${SAMPLING_ARGS[@]}" \
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

  # Unload from VRAM (not from disk -- these stay in Unraid's shared model
  # library for other uses) before the next model loads. Added 2026-08-21:
  # a stale prior model left resident in VRAM would only worsen the CPU-
  # spillover problem already confirmed for these 14B models at this context
  # length, by leaving less VRAM headroom for the next one.
  echo "[unload] releasing $MODEL from VRAM before next model..." >> "$DRIVER_LOG"
  curl -s -m 30 -X POST "$HOST/api/generate" -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" > /dev/null 2>&1
done

echo "=== BAKEOFF COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
