#!/bin/bash
# Re-dispatch of qwen3-coder-next:q4_K_M on Mac Studio, both tasks.
#
# Why the original runs are invalid -- THREE compounding causes, none of
# which were the model failing to converge:
#
# 1. THROUGHPUT CEILING (the dominant one). Measured live from /api/ps:
#      qwen3-coder-next:q4_K_M  total=52.2GB  vram=52.2GB  spill=0.0GB
#    It FITS Mac Studio's 68.7GB unified memory with no spill -- but a
#    52GB model on unified memory runs at roughly 7.5 min/iteration. With
#    TIMEOUT_S=1800 it can only ever complete ~4 iterations no matter what
#    the task is. Both the 06:33 and 13:37 runs died at exactly 1800s.
#    The historic "2/18 convergence" figure for this model is very likely
#    this same ceiling, not a convergence defect.
#
# 2. Bug 7 (system prompt). Iterations 1 and 2 of the 13:37 run were
#      web_search("resell-tracker web app codebase structure framework")
#      web_fetch("https://github.com/topics/resell-tracker")
#    -- it went to the internet to learn about the LOCAL codebase, because
#    the old SYSTEM_PROMPT encouraged web_search for anything uncertain
#    and never said to explore locally first. That burned 2 of the only 4
#    iterations it could afford. It then recovered on its own (ls -la,
#    package.json, prisma/schema.prisma) -- so the model's instincts were
#    fine once it stopped being misdirected.
#
# 3. Bug 6 (no list_files tool). It had to discover structure via
#    run_bash ls, costing extra iterations it did not have.
#
# Fixes now in ollama-worker.py: list_files tool added, read_file on a
# directory returns a listing instead of an errno, SYSTEM_PROMPT names all
# seven tools, mandates list_files first, and restricts web_search to
# EXTERNAL docs only.
#
# Remaining fix, here: TIMEOUT_S 1800 -> 3600. At ~7.5 min/iteration that
# buys ~8 iterations instead of ~4, and the harness fixes mean each one is
# productive rather than spent on web searches. This model is simply slow;
# a per-model timeout is the honest accommodation, and the asymmetry is
# itself routing-rule data (see the per-backend context-ceiling note).
#
# Settings otherwise copied verbatim from
# bakeoff-driver-macstudio-remaining.sh for this model: 32768 context (the
# `*)` default), --temperature 0.6 --top-p 0.95 --top-k 20 (the `qwen*`
# case), NO --manual-tools, no retry nudge.
#
# Chained behind the qwen2.5-coder:14b re-dispatch so only one job uses
# the Mac Studio at a time.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://localhost:11434"
WORKER="/Users/user/bin/ollama-worker.py"
MODEL="qwen3-coder-next:q4_K_M"
SLUG="qwen3-coder-next-q4_K_M"
TIMEOUT_S=3600

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

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Wire the self-test up so it is runnable (e.g. as a CLI subcommand or test target), consistent with how this project already organizes its code. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code."

echo "[qwen3cn-rerun] waiting for qwen2.5-coder:14b re-dispatch to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "QWEN2.5-CODER:14B RE-DISPATCH COMPLETE" "$DRIVER_LOG"; do
  sleep 30
done

run_task() {
  local TASK_NAME="$1" TASK_TEXT="$2" REPO="$3" VERIFY="$4"
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-MACSTUDIO2-FIXEDHARNESS.log"

  if [ ! -d "$WT_DIR" ]; then
    echo "[qwen3cn-rerun] ABORT (worktree missing): $TASK_NAME -- $WT_DIR" >> "$DRIVER_LOG"; return
  fi

  git -C "$WT_DIR" reset --hard >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  # Pre-flight: the real verify command must pass on a pristine tree.
  # Guards every environment failure of the npm-install / prisma-generate
  # class at once, rather than checking for one known-missing binary.
  echo "[qwen3cn-rerun] preflight: '$VERIFY' on pristine $REPO/$SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[qwen3cn-rerun] ABORT: pristine-tree verify FAILED in $WT_DIR -- not dispatching" >> "$DRIVER_LOG"
    return
  fi
  echo "[qwen3cn-rerun] preflight OK for $REPO/$SLUG" >> "$DRIVER_LOG"
  git -C "$WT_DIR" reset --hard >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local START_TS; START_TS=$(date +%s)
  echo "[qwen3cn-rerun] START: $MODEL / $TASK_NAME timeout=${TIMEOUT_S}s @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$HOST" \
    --cwd "$WT_DIR" \
    --task "$TASK_TEXT" \
    --verify "$VERIFY" \
    --max-iters 30 \
    --num-ctx 32768 \
    --temperature 0.6 --top-p 0.95 --top-k 20 \
    > "$LOG" 2>&1 &
  local WORKER_PID=$!
  ( sleep "$TIMEOUT_S" && kill -TERM "$WORKER_PID" 2>/dev/null ) &
  local WATCHER_PID=$!
  wait "$WORKER_PID" 2>/dev/null
  local EXIT=$?
  kill "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null

  local END_TS DUR TIMED_OUT FILES ITERS
  END_TS=$(date +%s); DUR=$((END_TS - START_TS))
  TIMED_OUT="false"; [ "$DUR" -ge "$TIMEOUT_S" ] && TIMED_OUT="true"
  FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null || echo 0)

  # Iteration count is the diagnostic that mattered here -- a timeout with
  # very few iterations is a throughput problem, not a convergence problem.
  echo "[qwen3cn-rerun] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES iters=$ITERS" >> "$DRIVER_LOG"
  echo "$MODEL-FIXEDHARNESS,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES" >> "$RESULTS_CSV"
}

run_task "resell-tracker-photo-upload" "$TASK1_RESELL" "resell-tracker" "npm run build"
run_task "clamshell-confirmation-bridge" "$TASK2_CLAMSHELL" "clamshell" "swift build"

echo "=== QWEN3-CODER-NEXT FIXED-HARNESS RE-DISPATCH COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
