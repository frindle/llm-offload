#!/bin/bash
# Final clean re-run of the two Unraid resell-tracker photo-upload tasks,
# after the SECOND environment-parity bug was fixed.
#
# Bug: the 4 new 7B-round worktrees had `npm install` run but never
# `prisma generate`. resell-tracker's schema.prisma emits its client to
# ../app/generated/prisma, there is NO postinstall hook, and lib/db.ts
# imports it -- so `next build` fails with module-not-found regardless of
# what the model writes. All 9 older worktrees had it generated; only the
# 4 new ones did not. Fixed with `npx prisma generate` in all 4, and a
# pristine-tree `npm run build` then verified exit 0.
#
# This invalidated: qwen2.5-coder:7b's photo-upload RE-RUN (30s, exit 1)
# and deepseek-r1:7b's CTX32K photo-upload (34s, exit 1).
#
# LESSON ENCODED BELOW: the earlier guard checked `node_modules/.bin/next`
# exists -- i.e. it guarded the PREVIOUS known failure rather than
# verifying the build actually works. This script instead runs the real
# verify command on a pristine tree BEFORE dispatching and refuses to run
# if it does not pass. That check subsumes every environment failure of
# this class, known or not.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
HOST="http://192.0.2.82:11434"
WORKER="/Users/user/bin/ollama-worker.py"
TIMEOUT_S=1800

DRIVER_LOG="$OUTDIR/driver.log"
RESULTS_CSV="$OUTDIR/results.csv"

TASK1_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

NUDGE="

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."

run_one() {
  local MODEL="$1" SLUG="$2" NUMCTX="$3" TAG="$4"
  shift 4
  local EXTRA=("$@")
  local WT_DIR="$WT_BASE/resell-tracker/$SLUG"
  local LOG="$OUTDIR/$SLUG-resell-tracker-photo-upload-$TAG.log"

  if [ ! -d "$WT_DIR" ]; then
    echo "[envfixed] ABORT (worktree missing): $MODEL" >> "$DRIVER_LOG"; return
  fi

  git -C "$WT_DIR" reset --hard >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  # PRE-FLIGHT: the real verify command must pass on a pristine tree.
  # This is the guard that actually generalizes -- it catches missing
  # node_modules, missing generated clients, and anything else that would
  # make the verify signal meaningless.
  echo "[envfixed] preflight: running 'npm run build' on pristine $SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && npm run build >/dev/null 2>&1 ); then
    echo "[envfixed] ABORT: pristine-tree build FAILED in $WT_DIR -- environment still broken, not dispatching $MODEL" >> "$DRIVER_LOG"
    return
  fi
  echo "[envfixed] preflight OK for $SLUG" >> "$DRIVER_LOG"
  git -C "$WT_DIR" reset --hard >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local START_TS; START_TS=$(date +%s)
  echo "[envfixed] START: $MODEL / resell-tracker-photo-upload ctx=$NUMCTX @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" \
    --host "$HOST" \
    --cwd "$WT_DIR" \
    --verify "npm run build" \
    --max-iters 30 \
    --num-ctx "$NUMCTX" \
    "${EXTRA[@]}" \
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

  echo "[envfixed] DONE: $MODEL / resell-tracker-photo-upload exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES" >> "$DRIVER_LOG"
  echo "$MODEL-$TAG,resell-tracker-photo-upload,$EXIT,$DUR,$TIMED_OUT,$FILES" >> "$RESULTS_CSV"
}

# qwen2.5-coder:7b -- 32K ctx, native tool-calling, qwen sampling.
run_one "qwen2.5-coder:7b" "qwen2.5-coder-7b-unraid" 32768 "ENVFIXED" \
  --task "$TASK1_RESELL" --temperature 0.6 --top-p 0.95 --top-k 20

# deepseek-r1:7b -- 32K ctx (fits the 3080; 128K spilled ~9.6GB to CPU),
# --manual-tools (distill template has no tool-call logic), + nudge.
run_one "deepseek-r1:7b" "deepseek-r1-7b-unraid" 32768 "ENVFIXED-CTX32K" \
  --task "${TASK1_RESELL}${NUDGE}" --temperature 0.6 --top-p 0.95 --manual-tools

echo "=== ENV-FIXED UNRAID RE-RUN COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
