#!/bin/bash
# Shared runner for the v2 bake-off (2026-08-22, fixed harness).
#
# v1 is discarded as a measurement of models. It measured the harness:
#   Bug 1  namespaced-model manifest path      -> MFDoom never loaded (0s)
#   Bug 2  raw newlines in JSON tool calls     -> real write_file dropped
#   Bug 3  131072 ctx spilled 9.6GB to CPU     -> deepseek-r1:7b timeout
#   Bug 4  worktree slug dots                  -> 2 models silently SKIPped
#   Bug 5  prisma generate never run           -> build could not pass
#   Bug 6  no list_files tool                  -> models guessed paths, then
#                                                 invented Flask/MERN stacks
#   Bug 7  SYSTEM_PROMPT omitted edit_file and
#          pushed web_search                   -> models web-searched the
#                                                 LOCAL codebase
#
# What v2 does differently, per dispatch:
#   * PRE-FLIGHT: runs the real verify command on a pristine tree and
#     refuses to dispatch unless it exits 0. Subsumes the whole
#     npm-install / prisma-generate class instead of checking for one
#     known-missing binary.
#   * Records ITERATION COUNT. A timeout with ~4 iterations is a
#     throughput problem (qwen3-coder-next: 52GB model, ~7.5min/iter), not
#     a convergence problem. v1 could not tell these apart.
#   * Records the /api/ps FOOTPRINT after each task, so VRAM spill is
#     evidence rather than something re-measured later.
#   * NEVER silently skips. A missing worktree writes an ABORT row so the
#     model cannot vanish from the results table.
#   * Per-model timeout and context, set from measured behaviour.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
WORKER="/Users/user/bin/ollama-worker.py"
DRIVER_LOG="$OUTDIR/driver.log"

# Run tag. Bumped whenever the HARNESS changes, so results from different
# harness versions can never land in the same file or overwrite each
# other's logs. v2 -> v3 on 2026-08-22 when render_manual_tools_block
# gained the explicit tool-name constraint (deepseek-r1:7b went from 0/4
# to 4/4 schema-valid calls with it) plus a one-call-at-a-time rule.
# Mixing harness versions in one dataset is the mistake v1 made.
RUN_TAG="${RUN_TAG:-v6}"
RESULTS_CSV="$OUTDIR/results-${RUN_TAG}.csv"

[ -f "$RESULTS_CSV" ] || echo "model,backend,task,exit_code,duration_s,timed_out,files_changed,iterations,ctx" > "$RESULTS_CSV"

# Pinned per-repo baseline commits. `git reset --hard` with no argument resets
# to whatever HEAD happens to be -- and HEAD DRIFTS: prior runs' output was
# committed onto two worktree branches, so resetting restored a previous
# model's answer to its own task (an Express-style photo-upload implementation
# in a Next.js repo) before the model started. Preflight cannot catch it: those
# files are not in the build graph, so `next build` still passes. Resetting to a
# pinned SHA makes that impossible regardless of what lands on a branch.
BASELINE_resell_tracker="271aa9e"
BASELINE_clamshell="8803d67"

baseline_for_repo() {
  case "$1" in
    resell-tracker) echo "$BASELINE_resell_tracker" ;;
    clamshell)      echo "$BASELINE_clamshell" ;;
    *)              echo "" ;;
  esac
}

TASK_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

TASK_CLAMSHELL="Build a new Swift module for this Clamshell project called ConfirmationBridge that implements challenge-signed remote confirmation using P-256 (ECDSA). Purpose: let a privileged action on the host require an explicit signed approval from a human physically at the client, not just anyone who can reach the host. This should be a standalone module, not yet wired into the real streaming protocol.

Requirements:
1. A device can produce a signed response to a challenge using a P-256 key.
2. A correctly-signed response for a given challenge verifies successfully.
3. A replayed signature/nonce (reusing a previous valid response) must be rejected.
4. An expired challenge/nonce must be rejected -- the challenge has a limited validity window.

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Wire the self-test up so it is runnable (e.g. as a CLI subcommand or test target), consistent with how this project already organizes its code. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code."

NUDGE="

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."

# run_model <model> <slug> <ctx> <timeout> <manual:yes|no> <nudge:yes|no> <backend-label> <host>
run_model() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" MANUAL="$5" USE_NUDGE="$6" BACKEND="$7" HOST="$8"
  local TAG="${RUN_TAG}-$BACKEND"

  # Sampling. v4 change: the qwen family drops to temperature 0.2.
  #
  # 0.6 came from the model cards' generation-config recommendation, which
  # targets reasoning/chat -- not agentic tool use. Evidence it was too high
  # here: qwen2.5-coder:7b ran the SAME model on the SAME task with byte-
  # identical inputs twice and produced 5 correct files one run and 0 the
  # next, because a single high-temperature wrong turn (guessing a path that
  # didn't exist) was unrecoverable. Loop detection now catches the second
  # half of that; lower temperature attacks the first half.
  #
  # deepseek-r1 stays at 0.6 deliberately -- DeepSeek's own guidance is an
  # explicit 0.5-0.7 for R1 and its distills, and deviating risks the
  # repetition/incoherence that range exists to prevent.
  local SAMPLING
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
    # devstral: 0.2 is the setting that was actually proven working for this
    # model earlier in the session (via llama-server/openai API). It runs
    # native through Ollama here -- confirmed capabilities ['completion',
    # 'tools'] with a real tool template -- so llama-server is not needed.
    devstral*)              SAMPLING=(--temperature 0.2 --top-p 0.95) ;;
    *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
  esac

  local EXTRA=()
  [ "$MANUAL" = "yes" ] && EXTRA+=(--manual-tools)

  _one_task "$MODEL" "$SLUG" "$CTX" "$TMO" "resell-tracker" "resell-tracker-photo-upload" \
            "npm run build" "$TASK_RESELL" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" \
            "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
  _one_task "$MODEL" "$SLUG" "$CTX" "$TMO" "clamshell" "clamshell-confirmation-bridge" \
            "swift build" "$TASK_CLAMSHELL" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" \
            "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
}

_one_task() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" REPO="$5" TASK_NAME="$6"
  local VERIFY="$7" TASK_TEXT="$8" USE_NUDGE="$9" TAG="${10}" HOST="${11}" BACKEND="${12}"
  shift 12
  local ARGS=("$@")

  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-${TAG}.log"

  if [ ! -d "$WT_DIR" ]; then
    echo "[$TAG] ABORT (worktree missing): $MODEL / $TASK_NAME -- $WT_DIR" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_NO_WORKTREE,0,false,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi

  local BASE_SHA; BASE_SHA=$(baseline_for_repo "$REPO")
  if [ -n "$BASE_SHA" ]; then
    git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  else
    echo "[$TAG] ABORT: no pinned baseline for repo '$REPO' -- refusing to dispatch" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_NO_BASELINE,0,false,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  echo "[$TAG] preflight: '$VERIFY' on pristine $REPO/$SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED in $WT_DIR -- environment broken, not dispatching" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_PREFLIGHT,0,false,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi
  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local FULL_TASK="$TASK_TEXT"
  [ "$USE_NUDGE" = "yes" ] && FULL_TASK="${TASK_TEXT}${NUDGE}"

  local START_TS; START_TS=$(date +%s)
  echo "[$TAG] START: $MODEL / $TASK_NAME ctx=$CTX timeout=${TMO}s @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" --host "$HOST" --cwd "$WT_DIR" \
    --task "$FULL_TASK" --verify "$VERIFY" \
    --max-iters 30 --num-ctx "$CTX" \
    "${ARGS[@]}" \
    > "$LOG" 2>&1 &
  local WPID=$!
  ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) &
  local WATCH=$!
  wait "$WPID" 2>/dev/null
  local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local END_TS DUR TIMED_OUT FILES ITERS PS
  END_TS=$(date +%s); DUR=$((END_TS - START_TS))
  TIMED_OUT="false"; [ "$DUR" -ge "$TMO" ] && TIMED_OUT="true"
  FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  PS=$(curl -s --max-time 10 "$HOST/api/ps" 2>/dev/null | tr -d '\n' | head -c 400)

  echo "[$TAG] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES iters=$ITERS" >> "$DRIVER_LOG"
  echo "[$TAG] ps-after: $PS" >> "$DRIVER_LOG"
  echo "$MODEL,$BACKEND,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES,$ITERS,$CTX" >> "$RESULTS_CSV"
}

unload_model() {
  curl -s --max-time 30 "$2/api/generate" \
    -d "{\"model\":\"$1\",\"keep_alive\":0}" >/dev/null 2>&1
  echo "[unload] $1 on $2" >> "$DRIVER_LOG"
}
