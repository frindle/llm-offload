#!/bin/bash
# v7b — the NO-SEARCH control. Unraid only, 7B models only.
#
# THE QUESTION THIS ANSWERS
# -------------------------
# v7 asked models to find and fix an existing component. Both 7Bs failed at
# the FINDING step and never reached the fixing step:
#   * qwen2.5-coder:7b  guessed app/components/OrderForm.tsx (real dir is
#                       top-level components/), got hard-refused after 3
#                       identical failures, then re-read README.md 17 times
#   * deepseek-r1:7b    guessed src/UploadView.tsx -- there is no src/ in an
#                       App Router project -- and quit at iteration 6
# Neither transcript contains the string "OrderAttachments". So v7 measured
# their SEARCH ability and told us nothing about their EDITING ability.
#
# That distinction matters for routing. qwen2.5-coder:7b has real strengths on
# record -- it put the Swift file in the correct existing target and left
# Package.swift alone, which deepseek-r1:32b and MFDoom both got wrong at 2-4x
# the size -- and every failure we have recorded for it is a locating or
# naming failure (wrong path shape; invented ECPrivateKey instead of
# P256.Signing.PrivateKey). If it can edit code placed in front of it, it is
# useful for narrow supervised work. If it cannot, it is finished.
#
# v7b removes search entirely: the file path is named AND its full current
# contents are pasted into the prompt. No list_files or read_file is needed to
# start. Everything else -- harness, grading, sampling, verify -- matches v7,
# so the ONLY difference from the v7 row is whether search was required.
#
# DELIBERATELY A SEPARATE LIB AND A SEPARATE CSV (results-v7b.csv).
# bakeoff-v7-lib.sh must not be touched: bakeoff-v7-launch.sh is armed and
# will source it for the macstudio leg the moment v6 finishes. Editing it now
# would silently change the grading of eight models that have not run yet.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
WORKER="/Users/user/bin/ollama-worker.py"
DRIVER_LOG="$OUTDIR/driver.log"

RUN_TAG="${RUN_TAG:-v7b}"
RESULTS_CSV="$OUTDIR/results-${RUN_TAG}.csv"

[ -f "$RESULTS_CSV" ] || echo "model,backend,task,exit_code,duration_s,timed_out,files_changed,touched_target,has_capture,fixed_hover,new_files,iterations,ctx" > "$RESULTS_CSV"

BASELINE_resell_tracker="271aa9e"
TARGET_FILE="components/OrderAttachments.tsx"

NUDGE="

Your first response must contain a tool call, not just text -- start immediately by editing the file."

# build_task <worktree-dir>
# The file contents are read from the PRISTINE tree at dispatch time, after
# the reset, so the prompt can never contain a previous run's edits.
build_task() {
  local WT="$1"
  case "${MODE:-nosearch}" in
    pathonly)    build_task_pathonly;    return ;;
    notoken)     build_task_notoken;     return ;;
    exacttoken)  build_task_exacttoken;  return ;;
  esac
  local CONTENTS
  CONTENTS=$(cat "$WT/$TARGET_FILE")
  cat <<EOF
The file \`components/OrderAttachments.tsx\` in this Next.js project is the component that lets a user attach photos to an order. It works, but it was built for a desktop mouse and is unusable on an iPhone. Fix it for touch.

You do not need to search for anything. The file is at \`components/OrderAttachments.tsx\` and its full current contents are below.

Three problems to fix, all inside this one file:
1. The delete button on each attachment is \`hidden group-hover:flex\` -- it only appears on mouse hover, so on a phone it can never be seen or tapped. Make it always visible.
2. That same button is \`w-5 h-5\`, about 20px. That is too small to tap reliably. Make it a comfortable touch size.
3. The file input should let the user take a photo with the camera directly, not only pick an existing file. Keep the ability to choose an existing file as well.

Do not create any new files. Edit \`components/OrderAttachments.tsx\` in place and keep the project's existing Tailwind and TypeScript conventions.

Here is the complete current contents of \`components/OrderAttachments.tsx\`:

\`\`\`tsx
$CONTENTS
\`\`\`

When you are done, respond with a short written summary (no further tool calls) describing exactly what you changed.
EOF
}

# The middle rung between v7 (find it yourself) and v7b (path + contents).
# The file is NAMED but its contents are NOT provided, so the model must
# issue one read_file on a path it was handed. This separates "cannot
# construct a path" from "cannot search at all" -- and decides how wide the
# supervised niche is in practice: must a caller paste the file, or is naming
# it enough?
# v7d arm: requirements 1 and 2 ONLY -- the camera requirement is removed.
#
# HYPOTHESIS UNDER TEST. Every recorded failure of qwen2.5-coder:7b is the
# same shape: right concept, wrong exact identifier.
#   app/components/OrderForm.tsx   (real dir is top-level components/)
#   ECPrivateKey                   (real name is P256.Signing.PrivateKey)
#   capture="camera"               (valid values are "user" / "environment")
# Requirements 1 and 2 need no identifier the model does not already have --
# the classes to change are visible in the file it is editing. Requirement 3
# is the only one that requires recalling a token from memory, and it is the
# only one that has ever broken the build (r3, r4, p1: all three produced the
# identical wrong value).
#
# If this arm passes cleanly and repeatedly, the failure is NOT "cannot edit"
# and NOT "cannot follow instructions" -- it is specifically unknown-token
# recall, which is the one failure a caller can trivially design around.
build_task_notoken() {
  cat <<'EOF'
The file `components/OrderAttachments.tsx` in this Next.js project is the component that lets a user attach photos to an order. It works, but it was built for a desktop mouse and is unusable on an iPhone. Fix it for touch.

The file you need is `components/OrderAttachments.tsx`. Read it first, then edit it.

Two problems to fix, both inside that one file:
1. The delete button on each attachment only appears on mouse hover, so on a phone it can never be seen or tapped. Make it always visible.
2. That same button is about 20px. That is too small to tap reliably. Make it a comfortable touch size.

Do not create any new files. Edit `components/OrderAttachments.tsx` in place and keep the project's existing Tailwind and TypeScript conventions.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you changed.
EOF
}

# v7d arm: all three requirements, but requirement 3 SUPPLIES the exact token.
#
# The other half of the same hypothesis. This arm keeps the camera
# requirement -- the one that broke the build in all three prior attempts --
# and simply hands over the value the model kept getting wrong. If this
# passes where the plain pathonly arm failed, the defect is pinned precisely:
# the model can perform the edit, and only fails when it must recall an
# identifier it does not know. That converts a vague "not viable" into an
# actionable routing rule: supply exact identifiers and it works.
build_task_exacttoken() {
  cat <<'EOF'
The file `components/OrderAttachments.tsx` in this Next.js project is the component that lets a user attach photos to an order. It works, but it was built for a desktop mouse and is unusable on an iPhone. Fix it for touch.

The file you need is `components/OrderAttachments.tsx`. Read it first, then edit it.

Three problems to fix, all inside that one file:
1. The delete button on each attachment only appears on mouse hover, so on a phone it can never be seen or tapped. Make it always visible.
2. That same button is about 20px. That is too small to tap reliably. Make it a comfortable touch size.
3. The file input should let the user take a photo with the camera directly. Add the attribute `capture="environment"` to the file input. Note that "environment" is the exact value required -- React types this attribute as boolean | "user" | "environment", and any other string will fail the type check. Keep the ability to choose an existing file as well.

Do not create any new files. Edit `components/OrderAttachments.tsx` in place and keep the project's existing Tailwind and TypeScript conventions.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you changed.
EOF
}

build_task_pathonly() {
  cat <<'EOF'
The file `components/OrderAttachments.tsx` in this Next.js project is the component that lets a user attach photos to an order. It works, but it was built for a desktop mouse and is unusable on an iPhone. Fix it for touch.

The file you need is `components/OrderAttachments.tsx`. Read it first, then edit it.

Three problems to fix, all inside that one file:
1. The delete button on each attachment only appears on mouse hover, so on a phone it can never be seen or tapped. Make it always visible.
2. That same button is about 20px. That is too small to tap reliably. Make it a comfortable touch size.
3. The file input should let the user take a photo with the camera directly, not only pick an existing file. Keep the ability to choose an existing file as well.

Do not create any new files. Edit `components/OrderAttachments.tsx` in place and keep the project's existing Tailwind and TypeScript conventions.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you changed.
EOF
}

# run_model_v7b <model> <slug> <ctx> <timeout> <manual:yes|no> <nudge:yes|no> <backend> <host>
run_model_v7b() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" MANUAL="$5" USE_NUDGE="$6" BACKEND="$7" HOST="$8"
  local TAG="${RUN_TAG}-$BACKEND"
  # SUFFIX distinguishes repeat runs of the identical task so their logs do
  # not overwrite each other and their CSV rows stay individually
  # identifiable. Repeats matter for this model specifically: it has produced
  # 5 files on one run and 0 on the next from byte-identical inputs, so any
  # single run is weak evidence.
  # MODE selects which task text is used: nosearch (path + full contents) or
  # pathonly (path named, contents NOT provided -- the model must read it).
  local MODE="${MODE:-nosearch}"
  local TASK_NAME="resell-tracker-${MODE}-touch${SUFFIX:-}"

  local SAMPLING
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
    *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
  esac
  local EXTRA=()
  [ "$MANUAL" = "yes" ] && EXTRA+=(--manual-tools)

  local WT_DIR="$WT_BASE/resell-tracker/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-${TAG}.log"
  local VERIFY="npm run build"

  if [ ! -d "$WT_DIR" ]; then
    echo "[$TAG] ABORT (worktree missing): $MODEL -- $WT_DIR" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_NO_WORKTREE,0,false,0,no,no,no,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi

  git -C "$WT_DIR" reset --hard "$BASELINE_resell_tracker" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  if [ ! -f "$WT_DIR/$TARGET_FILE" ]; then
    echo "[$TAG] ABORT: $TARGET_FILE missing at baseline -- task premise broken" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_NO_TARGET,0,false,0,no,no,no,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi

  echo "[$TAG] preflight: '$VERIFY' on pristine resell-tracker/$SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED -- not dispatching" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,ABORT_PREFLIGHT,0,false,0,no,no,no,0,0,$CTX" >> "$RESULTS_CSV"
    return
  fi
  git -C "$WT_DIR" reset --hard "$BASELINE_resell_tracker" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local FULL_TASK; FULL_TASK=$(build_task "$WT_DIR")
  [ "$USE_NUDGE" = "yes" ] && FULL_TASK="${FULL_TASK}${NUDGE}"

  local START_TS; START_TS=$(date +%s)
  echo "[$TAG] START: $MODEL / $TASK_NAME ctx=$CTX timeout=${TMO}s @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" --host "$HOST" --cwd "$WT_DIR" \
    --task "$FULL_TASK" --verify "$VERIFY" \
    --max-iters 30 --num-ctx "$CTX" \
    "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}" \
    > "$LOG" 2>&1 &
  local WPID=$!
  ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
  wait "$WPID" 2>/dev/null; local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local DUR TIMED_OUT FILES ITERS PS TOUCHED CAPTURE HOVER NEWF TRACKED BH NH
  DUR=$(( $(date +%s) - START_TS ))
  TIMED_OUT="false"; [ "$DUR" -ge "$TMO" ] && TIMED_OUT="true"
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  PS=$(curl -s --max-time 10 "$HOST/api/ps" 2>/dev/null | tr -d '\n' | head -c 300)

  # Grading identical to bakeoff-v7-lib.sh: baseline-relative, -uall, and
  # fixed_hover as a NET COUNT DECREASE (a '-' diff line matches a cosmetic
  # edit to that line even when the class survives on the '+' side).
  local B="$BASELINE_resell_tracker"
  NEWF=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | grep -c '^??' | tr -d ' ')
  TRACKED=$(git -C "$WT_DIR" diff --name-only "$B" 2>/dev/null | wc -l | tr -d ' ')
  FILES=$((TRACKED + NEWF))
  TOUCHED="no"; git -C "$WT_DIR" diff --name-only "$B" -- "$TARGET_FILE" 2>/dev/null | grep -q . && TOUCHED="yes"
  CAPTURE="no"; git -C "$WT_DIR" diff "$B" -- "$TARGET_FILE" 2>/dev/null | grep -qE '^\+.*capture=' && CAPTURE="yes"
  BH=$(git -C "$WT_DIR" show "$B:$TARGET_FILE" 2>/dev/null | grep -c 'group-hover' | tr -d ' ')
  NH=$(cat "$WT_DIR/$TARGET_FILE" 2>/dev/null | grep -c 'group-hover' | tr -d ' ')
  HOVER="no"; [ "${NH:-0}" -lt "${BH:-0}" ] && HOVER="yes"

  # Archive the diff before the next run resets this worktree. These arms all
  # share one worktree and run back to back, so evidence has a lifetime of
  # about sixty seconds unless it is copied out. p1's diff was already gone by
  # the time it turned out to matter -- the columns said touched_target=yes
  # but the actual change could no longer be read.
  git -C "$WT_DIR" diff "$B" > "$OUTDIR/$SLUG-$TASK_NAME-${TAG}.diff" 2>/dev/null
  git -C "$WT_DIR" status --porcelain -uall >> "$OUTDIR/$SLUG-$TASK_NAME-${TAG}.diff" 2>/dev/null

  echo "[$TAG] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES touched_target=$TOUCHED capture=$CAPTURE fixed_hover=$HOVER new_files=$NEWF iters=$ITERS" >> "$DRIVER_LOG"
  echo "[$TAG] ps-after: $PS" >> "$DRIVER_LOG"
  echo "$MODEL,$BACKEND,$TASK_NAME,$EXIT,$DUR,$TIMED_OUT,$FILES,$TOUCHED,$CAPTURE,$HOVER,$NEWF,$ITERS,$CTX" >> "$RESULTS_CSV"
}

unload_model() {
  curl -s --max-time 30 "$2/api/generate" -d "{\"model\":\"$1\",\"keep_alive\":0}" >/dev/null 2>&1
  echo "[unload] $1 on $2" >> "$DRIVER_LOG"
}
