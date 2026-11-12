#!/bin/bash
# ONE RUNNER FOR EVERY PENDING MAC STUDIO TEST — nothing else needs starting.
#
# WHY THIS EXISTS
# ---------------
# Every pending test targets the same single Ollama server on the same host, so
# they cannot overlap -- they serialise whether or not we plan for it. Kept as
# separate scripts, a human had to notice one finished and start the next, which
# on a ~40h pipeline means idle hardware overnight. This runs the lot, in a
# deliberate order, with host control between phases and no babysitting.
#
# ORDER, AND WHY IT IS THIS ORDER
# -------------------------------
#   0. STOP vision immediately. Do NOT wait for the in-flight model.
#      gc-sweep is resume-safe by its own design -- it skips only SUCCESSFUL
#      results and deletes-and-retries failures -- so killing it mid-sweep loses
#      nothing but the single image in flight. Waiting for it would delay ~47h of
#      higher-priority work to protect progress that was never at risk. Phase 6
#      resumes it from exactly where it stopped.
#   1. B3 EXERCISE. Never run end to end (Fable blocker 4). It measures
#      clean-host available memory, which decides whether each model's gate is
#      REACHABLE or STRUCTURAL -- a classification the preregistration requires
#      be fixed BEFORE results exist. HARD STOP on failure: without it every
#      host_ready value in the CSV is unvalidated.
#   1b. RESEARCH-EVAL POSITIVE CONTROL, run as early as the server allows.
#      Validates the instrument before the round commits to it, early enough that
#      a failure can be fixed while the base round runs. Non-blocking; it only
#      gates phase 5.
#   2. CALIBRATION for qwen3-coder:30b. Newly added, no measured rate. Sizing its
#      wall by guesswork is the exact sin v8 abolished. HARD STOP if the measured
#      rate implies a wall larger than the roster's -- otherwise the calibration
#      is theatre, measured and then ignored.
#   3. BASE ROUND. The GO/NO-GO deliverable; everything else is secondary.
#   4. ARMS. Read only against base rows for the same cells, so they must follow.
#   5. RESEARCH EVAL. Different question; must not delay the routing table.
#   6. REMAINING VISION MODELS. Deferred to last on purpose -- they are
#      re-measurements, and v8 is what gates the owner's routing decisions. Running
#      them here rather than leaving them stopped is what makes this pipeline
#      "everything", so nothing is left needing a human to remember it.
#
# Every phase is SKIP_*-able so a failed phase can be resumed without repeating
# what already succeeded.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
LOG="$BASE/bakeoff-v8-ALL.log"
HOSTTEL="$BASE/bakeoff/harness/bakeoff-v8-hosttel.py"
HOST="http://localhost:11434"

say() { echo "=== $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
tel() { python3 "$HOSTTEL" | awk -F'|' '{printf "swap=%sMB avail=%sMB load=%s", $1, $2, $4}'; }

say "PIPELINE START -- host: $(tel)"

# ── 0. stop vision immediately (resume-safe; phase 6 continues it) ──────────
if [ "${SKIP_VISION_WAIT:-0}" != "1" ]; then
  if pgrep -f "queue-macstudio-vision" >/dev/null; then
    say "PHASE 0: stopping the vision QUEUE loop so no further model starts"
    pkill -f "queue-macstudio-vision" 2>/dev/null
    sleep 2
  fi
  if pgrep -f "gc-sweep.py" >/dev/null; then
    say "PHASE 0: stopping the in-flight sweep -- resume-safe, phase 6 continues it"
    pkill -f "gc-sweep.py" 2>/dev/null
    for _ in $(seq 1 30); do pgrep -f "gc-sweep.py" >/dev/null || break; sleep 2; done
    say "PHASE 0: vision stopped"
  else
    say "PHASE 0: no vision sweep running"
  fi
  sleep 60    # let the model unload and memory settle
fi

# ── 1. B3 exercise ──────────────────────────────────────────────────────────
if [ "${SKIP_B3:-0}" != "1" ]; then
  say "PHASE 1: B3 end-to-end exercise -- host: $(tel)"
  bash "$BASE/bakeoff-v8-b3check.sh" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    say "PHASE 1 FAILED (rc=$rc) -- STOPPING. B3 is the precondition for every"
    say "timing claim in the round; dispatching without it produces ~26h of rows"
    say "whose host_ready column means nothing."
    exit 1
  fi
  say "PHASE 1: complete"
fi

# ── 1b. research-eval POSITIVE CONTROL — as early as the server allows ──────
# Validates the INSTRUMENT before the round commits to it, and does so early
# enough that a failure can be repaired while the base round runs.
#
# qwen3-coder-next is the model whose fabrication motivated the metric -- a fact
# established independently of this eval. Unit tests cannot reach the whole
# question: they validate the SCORER, while this validates the TASK DESIGN --
# whether plausible absent-answer questions actually elicit fabrication rather
# than trivial abstention.
#
# TWO GATES, NOT ONE (rewritten 2026-08-23).
# The first version treated 0 fabrications as proof the eval was blind and
# skipped phase 5 outright. The 2026-08-23 run showed why that is wrong: it
# returned 0/7, and the cause was neither a blind scorer nor an honest model. The
# scorer was fine -- FABRICATED fires on every abstain item, provable offline in
# milliseconds -- and the model was busy inventing rows on four COMPUTE items in
# the very same run. The abstain items were simply string searches for tokens
# that appear nowhere, which is an easier question than any of the ones the model
# was failing. A single count cannot tell those three stories apart, so it must
# not be the thing that decides whether phase 5 runs.
#
#   gate 1  --self-test   offline, no inference. "Can FABRICATED fire at all?"
#                         A failure here IS a code fault -> phase 5 is skipped.
#   gate 2  control run   behavioural. "Do these questions tempt anyone?"
#                         0 fabrications is a WARNING about task difficulty, not
#                         a reason to discard the round -- unless the run itself
#                         failed to produce usable answers, which is a host fault
#                         and does block.
#
# n=1 on purpose: one rep answers "does this detect fabrication at all". The
# other two reps belong with the main round, for distribution.
#
# RUN ORDER: immediately after B3, not before it. B3 restarts the inference
# server, so the control gets a clean host rather than one still holding a 26GB
# vision model -- and B3 costs ~5 minutes. Earliest USEFUL is better than
# earliest possible.
#
# NON-BLOCKING by design: the base round does not depend on the research eval, so
# a failure here must not stop it. It sets a flag that phase 5 reads.
CONTROL_FLAG="$BASE/.research-eval-control-result"
CONTROL_CSV="$BASE/bakeoff/harness/research-eval-control.csv"
if [ "${SKIP_CONTROL:-0}" != "1" ]; then
  say "PHASE 1b: research-eval instrument check + POSITIVE CONTROL (qwen3-coder-next, n=1)"
  # Exit codes captured into variables, not read through a pipeline: `set -o
  # pipefail` is on, and `cmd | tee | grep -q` reports the SIGPIPE that grep's
  # early exit sends to tee, which would fail a self-test that actually passed.
  GEN_RC=0; ST_RC=0
  python3 "$BASE/bakeoff/harness/research-eval-gen.py" >>"$LOG" 2>&1 || GEN_RC=$?
  ST_OUT=$(python3 "$BASE/bakeoff/harness/research-eval-run.py" --self-test 2>&1) || ST_RC=$?
  printf '%s\n' "$ST_OUT" >>"$LOG"
  if [ "$GEN_RC" -ne 0 ]; then
    say "PHASE 1b: *** task generation FAILED -- see log. Phase 5 SKIPPED."
    echo "BLIND" > "$CONTROL_FLAG"
  elif [ "$ST_RC" -ne 0 ]; then
    printf '%s\n' "$ST_OUT"
    say "PHASE 1b: *** SELF-TEST FAILED -- the scorer cannot emit FABRICATED, so"
    say "          every fabrication rate it produces would be a zero by"
    say "          construction. This is a code fault. Phase 5 SKIPPED."
    echo "BLIND" > "$CONTROL_FLAG"
  else
    say "PHASE 1b: $ST_OUT"
    # The runner APPENDS. A control CSV left from an earlier attempt would put
    # that attempt's verdicts into this attempt's count -- and the 2026-08-23
    # file did exactly that, carrying 24 rows from an aborted run. Move it aside
    # (never delete: run outputs are evidence) so the count is this run's.
    [ -f "$CONTROL_CSV" ] && mv "$CONTROL_CSV" \
      "$BASE/research-eval-control-$(date -u +%Y%m%dT%H%M%SZ).csv"
    python3 "$BASE/bakeoff/harness/research-eval-run.py" --model "qwen3-coder-next:q4_K_M" \
      --host "$HOST" --num-ctx 32768 --temperature 0.2 --rep 0 \
      --out "$CONTROL_CSV" 2>&1 | tee -a "$LOG"
    FAB=$(awk -F, 'NR>1 && $5=="FABRICATED"' "$CONTROL_CSV" 2>/dev/null | wc -l | tr -d ' ')
    WRONG=$(awk -F, 'NR>1 && $5=="wrong"' "$CONTROL_CSV" 2>/dev/null | wc -l | tr -d ' ')
    USABLE=$(awk -F, 'NR>1 && $5!="error" && $5!="unparseable"' "$CONTROL_CSV" 2>/dev/null | wc -l | tr -d ' ')
    if [ "${USABLE:-0}" -lt 12 ]; then
      # Almost nothing came back. That is the host, not the eval -- the 13:59
      # attempt looked exactly like this while the model was loading over SMB.
      say "PHASE 1b: *** CONTROL UNUSABLE -- only $USABLE scorable answers. The"
      say "          server did not answer; this says nothing about the eval."
      say "          Phase 5 SKIPPED. Rerun with SKIP_BASE=1 SKIP_ARMS=1."
      echo "BLIND" > "$CONTROL_FLAG"
    elif [ "${FAB:-0}" -gt 0 ]; then
      say "PHASE 1b: control PASSED -- $FAB fabrications, $WRONG confidently wrong"
      echo "OK" > "$CONTROL_FLAG"
    else
      say "PHASE 1b: control WARNING -- 0 fabrications, but $WRONG confidently wrong"
      say "          answers on answerable items. The scorer is proven live by the"
      say "          self-test, so this is a statement about the abstain items being"
      say "          easier than the compute items, not about the scorer. Phase 5"
      say "          RUNS; read fabrication rate next to conf.wrong, not alone."
      echo "OK" > "$CONTROL_FLAG"
    fi
  fi
fi

# ── 2. calibration ──────────────────────────────────────────────────────────
if [ "${SKIP_CALIB:-0}" != "1" ]; then
  say "PHASE 2: qwen3-coder:30b calibration -- host: $(tel)"
  bash "$BASE/bakeoff-v8-calibrate.sh" qwen3-coder:30b 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    say "PHASE 2 FAILED (rc=$rc): the measured rate implies a wall larger than the"
    say "roster's 3600s for qwen3-coder:30b. STOPPING -- proceeding would dispatch"
    say "a model whose wall we know is too small, and a timed_out row is an"
    say "instrument defect, not a result. Fix the roster wall, then SKIP_B3=1 rerun."
    exit 1
  fi
  say "PHASE 2: complete"
fi

# ── 3. base round ───────────────────────────────────────────────────────────
if [ "${SKIP_BASE:-0}" != "1" ]; then
  say "PHASE 3: v8 BASE ROUND -- 6 models x 3 tasks x 3 reps, ~22-26h expected -- host: $(tel)"
  bash "$BASE/bakeoff/harness/bakeoff-v8-macstudio.sh" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  [ "$rc" -ne 0 ] && say "PHASE 3 returned rc=$rc -- continuing, but CHECK THE CSV before trusting it"
  say "PHASE 3: complete"
fi

# ── 4. experiment arms ──────────────────────────────────────────────────────
if [ "${SKIP_ARMS:-0}" != "1" ]; then
  say "PHASE 4: v8 EXPERIMENT ARMS -- n=3, ~10-12h expected -- host: $(tel)"
  bash "$BASE/bakeoff-v8-arms.sh" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  [ "$rc" -ne 0 ] && say "PHASE 4 returned rc=$rc -- continuing"
  say "PHASE 4: complete"
fi

# ── 5. research / analysis eval ─────────────────────────────────────────────
# BLIND means the instrument itself is unproven -- the scorer self-test failed,
# task generation failed, or the control never got answers. It does NOT mean the
# control model declined to fabricate; that is a task-difficulty warning and
# phase 1b writes OK for it. Older runs wrote a bare integer, so treat anything
# that is not the literal "OK" as blind rather than guessing.
CONTROL_FAB=$(cat "$BASE/.research-eval-control-result" 2>/dev/null || echo BLIND)
if [ "${SKIP_RESEARCH:-0}" != "1" ] && [ "$CONTROL_FAB" != "OK" ]; then
  say "PHASE 5: SKIPPED -- the research eval instrument is unvalidated"
  say "         (.research-eval-control-result = '$CONTROL_FAB'), so any scores it"
  say "         produced would be false comfort. Fix it, confirm with"
  say "         'python3 research-eval-run.py --self-test', then rerun with"
  say "         SKIP_BASE=1 SKIP_ARMS=1."
elif [ "${SKIP_RESEARCH:-0}" != "1" ]; then
  say "PHASE 5: RESEARCH / ANALYSIS EVAL (thinking models) -- host: $(tel)"
  bash "$BASE/bakeoff/harness/research-eval-macstudio.sh" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  [ "$rc" -ne 0 ] && say "PHASE 5 returned rc=$rc -- continuing"
  say "PHASE 5: complete"
fi

# ── 6. remaining vision models ──────────────────────────────────────────────
# The queue script skips any image/variant/run whose result JSON already exists,
# so re-running it resumes rather than repeating -- models already swept cost
# only the time to walk their result directories.
if [ "${SKIP_VISION_FINISH:-0}" != "1" ]; then
  say "PHASE 6: remaining vision models -- host: $(tel)"
  ( cd "$BASE/giftcard-ocr/scripts" && bash ./queue-macstudio-vision.sh ) 2>&1 | tee -a "$LOG"
  say "PHASE 6: complete"
fi

say "PIPELINE COMPLETE -- host: $(tel)"
say "results:  model-buildoff-2026-08-22/results-v8.csv"
say "          research-eval-results.csv"
say "          giftcard-ocr/results-macstudio-*"
say "score:    python3 $BASE/bakeoff-v8-score.py"
say "          python3 $BASE/bakeoff/harness/research-eval-score.py"
