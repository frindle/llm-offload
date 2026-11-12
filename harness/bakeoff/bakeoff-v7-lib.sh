#!/bin/bash
# v6.1 — BOTH tasks, on the patched worker.
#
# WHY v6 WAS STOPPED AND RESTARTED
# --------------------------------
# 1. BUG 11 INVALIDATED THE BEST RUN IN THE MATRIX. A turn with content:""
#    and the real output in msg["thinking"] was read as "nothing more to
#    say" and converged the run. It ended qwen3.8:27b-q8_0's photo-upload
#    run at iteration 23/30, one turn after it announced it was about to
#    implement the fix -- and that was written up as a MODEL failure
#    ("correct diagnosis, no treatment") when the harness had cut it off.
#    Any native thinking model still queued was exposed to the same bug;
#    qwen3-14b-agentic in particular has never been successfully measured in
#    any round.
#
# 2. PHOTO-UPLOAD IS KEPT (the owner's call), AND BUG 11 IS THE REASON IT MATTERS.
#    That task can no longer be scored as a FEATURE BUILD -- the repo already
#    shipped the feature at baseline 271aa9e (attachments API +
#    OrderAttachments.tsx + prisma OrderAttachment, June 2026), so a model
#    that correctly discovers it exists scores files=0, identical to a model
#    that made no tool calls. But it is still a live measurement of CODEBASE
#    COMPREHENSION, which is exactly what separated the field: qwen3.8:27b
#    traced the whole existing implementation and deepseek-r1:32b never found
#    it. And bug 11 struck qwen3.8 ON THIS TASK, one turn after it announced
#    it was about to implement the remaining delta -- so re-running photo
#    patched is the only way to learn what it would actually have produced.
#    SCORE IT AS COMPREHENSION, NEVER AS A FEATURE BUILD. files=0 can be the
#    correct answer here; read the transcript.
#
# WHY EVERY MODEL RE-RUNS, INCLUDING THE TWO ALREADY DONE
# -------------------------------------------------------
# qwen3.8:27b-q8_0 and deepseek-r1:32b already have v6 clamshell rows, taken
# on the UNPATCHED worker. Keeping those and appending patched rows for
# everyone else would produce one table spanning two harness versions -- the
# exact mistake that made v1 worthless and that this project has cited ever
# since. Two extra runs (~20 min) buys a single-harness dataset. The old rows
# stay in results-v6.csv; nothing is deleted, and the qwen3.8 clamshell PASS
# is already fully written up in the guide.
#
# NET EFFECT: this is SLOWER than finishing v6 as planned -- both tasks are
# kept and two models re-run -- and that is a deliberate trade of time for a
# dataset that is internally consistent and free of a bug known to have
# destroyed its single most informative result.
#
# The clamshell task itself is UNCHANGED and remains valid: ConfirmationBridge
# genuinely does not exist at baseline 8803d67, and the task instructs the
# model to check first. Only the harness and the roster changed.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
DRIVER_LOG="$OUTDIR/driver.log"

# The PATCHED worker. /Users/user/bin/ollama-worker.py is deliberately left
# alone -- see the v7 lib header. Fixes carried: bug 11 (thinking-only turns
# no longer converge the run) and bug 13 (identical repeated SUCCEEDING calls
# now return a stub after the third, instead of re-injecting a full file body
# until the conversation overflows its context window and truncates the task).
WORKER="/Users/user/bin/ollama-worker-v7.py"

RUN_TAG="${RUN_TAG:-v61}"
RESULTS_CSV="$OUTDIR/results-${RUN_TAG}.csv"

[ -f "$RESULTS_CSV" ] || echo "model,backend,task,rep,exit_code,verify_passed,duration_s,timed_out,files_changed,iterations,ctx,stop_reason,transcript" > "$RESULTS_CSV"

BASELINE_clamshell="8803d67"
BASELINE_resell_tracker="271aa9e"

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

# run_model_v7 <model> <slug> <ctx> <timeout> <manual:yes|no> <nudge:yes|no> <backend> <host> <rep>
run_model_v7() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" MANUAL="$5" USE_NUDGE="$6" BACKEND="$7" HOST="$8" REP="$9"
  local TAG="${RUN_TAG}-$BACKEND"

  # Sampling unchanged from v6 so v6 and v6.1 rows stay comparable on
  # everything except the two harness fixes.
  local SAMPLING
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
    devstral*)              SAMPLING=(--temperature 0.2 --top-p 0.95) ;;
    *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
  esac
  local EXTRA=()
  [ "$MANUAL" = "yes" ] && EXTRA+=(--manual-tools)

  # Photo first, matching v6's order, so the comprehension task is not the
  # one that gets cut if the leg is stopped early again.
  _one_task_v7 "$MODEL" "$SLUG" "$CTX" "$TMO" "resell-tracker" "resell-tracker-photo-upload" \
                "npm run build" "$TASK_RESELL" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" "$REP" \
                "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
  _one_task_v7 "$MODEL" "$SLUG" "$CTX" "$TMO" "clamshell" "clamshell-confirmation-bridge" \
                "swift build" "$TASK_CLAMSHELL" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" "$REP" \
                "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
}

_one_task_v7() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" REPO="$5" TASK_NAME="$6"
  local VERIFY="$7" TASK_TEXT="$8" USE_NUDGE="$9" TAG="${10}" HOST="${11}" BACKEND="${12}" REP="${13}"
  shift 13
  local ARGS=("$@")

  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-${TAG}-r${REP}.log"

  if [ ! -d "$WT_DIR" ]; then
    echo "[$TAG] ABORT (worktree missing): $MODEL / $TASK_NAME -- $WT_DIR" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,$REP,ABORT_NO_WORKTREE,na,0,false,0,0,$CTX,na,na" >> "$RESULTS_CSV"
    return
  fi

  local BASE_SHA; BASE_SHA=$(baseline_for_repo "$REPO")
  if [ -z "$BASE_SHA" ]; then
    echo "[$TAG] ABORT: no pinned baseline for repo '$REPO'" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,$REP,ABORT_NO_BASELINE,na,0,false,0,0,$CTX,na,na" >> "$RESULTS_CSV"
    return
  fi

  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  # Premise guard, added after the photo-upload defect: refuse to dispatch a
  # "create X" task when X already exists. Only meaningful for clamshell --
  # the photo-upload premise is KNOWN broken and that task is deliberately
  # scored as comprehension instead (see header), not as a feature build.
  if [ "$REPO" = "clamshell" ] && { [ -e "$WT_DIR/Sources/ConfirmationBridge" ] || [ -e "$WT_DIR/Sources/Clamshell/Auth/ConfirmationBridge.swift" ]; }; then
    echo "[$TAG] ABORT: ConfirmationBridge already present at baseline -- task premise broken" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,$REP,ABORT_PREMISE,na,0,false,0,0,$CTX,na,na" >> "$RESULTS_CSV"
    return
  fi

  echo "[$TAG] preflight: '$VERIFY' on pristine $REPO/$SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED -- not dispatching" >> "$DRIVER_LOG"
    echo "$MODEL,$BACKEND,$TASK_NAME,$REP,ABORT_PREFLIGHT,na,0,false,0,0,$CTX,na,na" >> "$RESULTS_CSV"
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
  ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
  wait "$WPID" 2>/dev/null; local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local DUR TIMED_OUT FILES ITERS PS VPASS
  DUR=$(( $(date +%s) - START_TS ))
  TIMED_OUT="false"; [ "$DUR" -ge "$TMO" ] && TIMED_OUT="true"
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  # exit_code alone is ambiguous and has already caused two misreads. exit=2
  # is the worker's DID-NOT-CONVERGE code, NOT a failure: qwen3.8:27b-q8_0's
  # clamshell run -- the only genuine complete pass in the bake-off, whose
  # self-test passes when you run the binary -- records exit=2 with
  # "VERIFY PASSED (exit 0)" in its own log, because it was still polishing
  # when the 30-iteration ceiling hit. v7d's e2 did the same. Recording the
  # verify outcome as its own column means the CSV stops conflating "the
  # build failed" with "the model ran out of turns".
  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"
  PS=$(curl -s --max-time 10 "$HOST/api/ps" 2>/dev/null | tr -d '\n' | head -c 300)

  # Baseline-relative, so staged or committed work counts like unstaged work.
  local TRACKED NEWF
  NEWF=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | grep -c '^??' | tr -d ' ')
  TRACKED=$(git -C "$WT_DIR" diff --name-only "$BASE_SHA" 2>/dev/null | wc -l | tr -d ' ')
  FILES=$((TRACKED + NEWF))

  # Archive the diff before the next model resets this worktree. Learned in
  # v7c: one run's evidence was already gone by the time it looked worth
  # reading. The CSV says whether something changed; only the diff says
  # whether it was any good, and every real verdict here came from the diff.
  # Per-repeat, or repeats overwrite each other and only the LAST survives --
  # which would silently destroy 2/3 of the evidence in an n=3 round.
  git -C "$WT_DIR" diff "$BASE_SHA" > "$OUTDIR/$SLUG-$TASK_NAME-${TAG}-r${REP}.diff" 2>/dev/null
  git -C "$WT_DIR" status --porcelain -uall >> "$OUTDIR/$SLUG-$TASK_NAME-${TAG}-r${REP}.diff" 2>/dev/null

  echo "[$TAG] DONE: $MODEL / $TASK_NAME exit=$EXIT dur=${DUR}s timedout=$TIMED_OUT files=$FILES iters=$ITERS" >> "$DRIVER_LOG"
  echo "[$TAG] ps-after: $PS" >> "$DRIVER_LOG"
  local TRANSCRIPT; TRANSCRIPT=$(grep -o "/Users/user/bin/ollama-worker-logs/[0-9TZ]*\.json" "$LOG" | tail -1)
  local STOPR="none"
  grep -q "CONTEXT CEILING" "$LOG" && STOPR="context_ceiling"
  echo "$MODEL,$BACKEND,$TASK_NAME,$REP,$EXIT,$VPASS,$DUR,$TIMED_OUT,$FILES,$ITERS,$CTX,$STOPR,${TRANSCRIPT:-none}" >> "$RESULTS_CSV"
}

unload_model() {
  curl -s --max-time 30 "$2/api/generate" -d "{\"model\":\"$1\",\"keep_alive\":0}" >/dev/null 2>&1
  echo "[unload] $1 on $2" >> "$DRIVER_LOG"
}
