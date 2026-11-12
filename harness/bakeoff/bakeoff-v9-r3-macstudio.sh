#!/bin/bash
# v9 R3 -- "follow-up on own prior output". Two-turn cell, clamshell substrate.
# Fable-ruled 2026-08-25.
#
# WHY THIS DRIVER EXISTS AT ALL
# -----------------------------
# Every other v9 round reuses _one_task_v8 verbatim. R3 cannot: that function
# opens with `git reset --hard $BASE_SHA` (lib:517), and turn 2 must run against
# the worktree AS THE MODEL LEFT IT. Turn 1's work IS the input to turn 2.
#
# What makes this safe rather than a third harness: turn 1 goes through the
# NORMAL path (run_model_v8, ONLY_TASK=clam) and is scored by the normal code.
# Only turn 2 is bespoke, and its worker invocation is copied VERBATIM from
# bakeoff-v8-lib.sh:551-560 -- same argv, same --max-iters 30, same ctx_for,
# same sampling, same watchdog. The only two deliberate deltas, both approved:
#   1. no `git reset` before turn 2
#   2. a composed turn-2 task string
# Verified: lib has resets ONLY at 487/488 (preflight) and 517/518 (pre-run).
# _one_task_v8 spans 456-679 and computes files_changed at 650 from the LIVE
# tree, so turn 1's changes survive its return. Nothing resets after a run.
#
# DEFECT DELIVERY IS MECHANICAL (design B2 / Fable Q2)
# ----------------------------------------------------
# Turn 2 receives the VERBATIM failing verify output captured by the harness --
# never a human description naming the function or mechanism. A prose
# description is an answer key by another route, and its helpfulness varies per
# model, which destroys comparability. The harness identifies the defect,
# identically for every model.
#
# PRE-REGISTERED DEGENERATE CASES -- no turn 2 runs, excluded from every turn-2
# denominator, and NO defect is invented to manufacture one:
#   NO_TURN1_OUTPUT  -- turn 1 produced nothing usable (no files changed)
#   NO_DEFECT_FOUND  -- turn 1 output exists and PASSES verify
# qwen3.8:27b-q8_0 is PRE-REGISTERED as expected NO_DEFECT_FOUND: it passes
# clamshell 6/6, photo 6/6, debug 3/3. If it fails turn 1, turn 2 runs normally.
#
# DELIVERABLE IS A BEHAVIOURAL CLASSIFICATION, NOT A FIX RATE
# ------------------------------------------------------------
# Each model faces its own defect of its own difficulty, so cross-model
# fix-rate is not comparable. Classes are fixed in advance so the tempting
# invalid post-hoc reading (a leaderboard) is foreclosed:
#   fixes_it / revises_own_work / re-derives_from_scratch / defends_or_denies
#
# n=2, with a pre-registered tiebreak: if a model's two reps yield DIFFERENT
# classifications, run exactly one tiebreak rep. Fixed now so it cannot be
# invented after seeing results.
set -uo pipefail
export RUN_TAG=v9
export ONLY_TASK=clam
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"
R3_CSV="$OUTDIR/results-v9-r3.csv"
R3_TURN2_VERIFY="swift build && swift run Clamshell confirmation-bridge-selftest"

[ -f "$R3_CSV" ] || echo "model,rep,turn1_verify,turn1_files,turn2_outcome,turn2_verify,turn2_files,turn2_iters,turn2_dur_s,classification,transcript" > "$R3_CSV"

echo "=== $RUN_TAG MACSTUDIO R3 TWO-TURN ROUND START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# Same roster lines, copied verbatim from bakeoff-v8-macstudio.sh.
ROSTER=(
  "qwen3.8:27b-q8_0|qwen3.8-27b-q8_0|4200|no|no"
  "qwen3-14b-agentic|qwen3-14b-agentic|3600|no|no"
  "deepseek-r1:32b|deepseek-r1-32b|6000|yes|yes"
  "qwen2.5-coder:14b|qwen2.5-coder-14b|1800|no|no"
  "qwen3-coder-next:q4_K_M|qwen3-coder-next-q4_K_M|5400|no|no"
  "qwen3-coder:30b|qwen3-coder-30b|3600|no|no"
)

# Compose the turn-2 prompt. Zero human prose about the defect: the model gets
# its own diff (what it produced) and the harness's verbatim failure text.
compose_turn2() {
  local WT="$1" VERIFY_TEXT="$2"
  local DIFF; DIFF=$(git -C "$WT" diff HEAD 2>/dev/null | head -400)
  local UNTRACKED; UNTRACKED=$(git -C "$WT" ls-files --others --exclude-standard 2>/dev/null | head -40)
  cat <<COMPOSED
You previously worked in this repository and produced the changes below. Your work does not pass its verification command.

--- YOUR CHANGES (git diff) ---
${DIFF}

--- FILES YOU CREATED ---
${UNTRACKED}

--- VERIFICATION OUTPUT ---
Command: ${R3_TURN2_VERIFY}

${VERIFY_TEXT}

Fix it. The repository is exactly as you left it.
COMPOSED
}

for M in "${ROSTER[@]}"; do
  IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG <<< "$M"
  CTX=$(ctx_for "$MODEL")
  WT_DIR="$WT_BASE/clamshell/$SLUG"
  LOG1="$OUTDIR/$SLUG-clamshell-confirmation-bridge-$RUN_TAG-$BACKEND-base-r"

  # Sampling, copied verbatim from run_model_v8 so turn 2 matches turn 1.
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
    devstral*)              SAMPLING=(--temperature 0.2 --top-p 0.95) ;;
    *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
  esac
  EXTRA=()
  [ "$MANUAL" = "yes" ] && EXTRA+=(--manual-tools)

  for REP in 1 2; do
    echo "[$RUN_TAG-r3] === $MODEL rep $REP TURN 1 ===" >> "$DRIVER_LOG"

    # --- TURN 1: the normal path, normal scoring, normal CSV row -------------
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" "$BACKEND" "$HOST" "$REP" "base" </dev/null

    T1LOG="${LOG1}${REP}.log"
    T1VERIFY="no"; grep -aq "VERIFY PASSED" "$T1LOG" 2>/dev/null && T1VERIFY="yes"
    T1FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

    # --- pre-registered degenerate cases: no turn 2, no invented defect ------
    if [ "$T1VERIFY" = "yes" ]; then
      echo "[$RUN_TAG-r3] $MODEL rep $REP -> NO_DEFECT_FOUND (turn 1 passed verify)" >> "$DRIVER_LOG"
      echo "$MODEL,$REP,yes,$T1FILES,NO_DEFECT_FOUND,na,na,na,na,na,$(basename "$T1LOG")" >> "$R3_CSV"
      unload_model "$MODEL" "$HOST"; continue
    fi
    if [ "$T1FILES" -eq 0 ]; then
      echo "[$RUN_TAG-r3] $MODEL rep $REP -> NO_TURN1_OUTPUT (nothing usable produced)" >> "$DRIVER_LOG"
      echo "$MODEL,$REP,no,0,NO_TURN1_OUTPUT,na,na,na,na,na,$(basename "$T1LOG")" >> "$R3_CSV"
      unload_model "$MODEL" "$HOST"; continue
    fi

    # Verbatim harness failure text -- everything the verify command emitted.
    VERIFY_TEXT=$(sed -n '/running verify command/,$p' "$T1LOG" 2>/dev/null | head -120)
    TASK2=$(compose_turn2 "$WT_DIR" "$VERIFY_TEXT")

    # --- TURN 2: worker invocation copied VERBATIM from lib:551-560 ----------
    # DELIBERATE DELTA 1: no `git reset` here -- turn 1's tree is the input.
    # DELIBERATE DELTA 2: --task is the composed string above.
    LOG2="$OUTDIR/$SLUG-clamshell-r3turn2-$RUN_TAG-$BACKEND-base-r${REP}.log"
    echo "[$RUN_TAG-r3] === $MODEL rep $REP TURN 2 (tree preserved, $T1FILES changed paths) ===" >> "$DRIVER_LOG"
    restart_inference_server "$HOST" "$MODEL" "$CTX" || true
    T2START=$(date +%s)

    python3 "$WORKER" \
      --model "$MODEL" --host "$HOST" --cwd "$WT_DIR" \
      --task "$TASK2" --verify "$R3_TURN2_VERIFY" \
      --max-iters 30 --num-ctx "$CTX" \
      "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}" \
      > "$LOG2" 2>&1 &
    WPID=$!
    ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
    wait "$WPID" 2>/dev/null
    kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

    T2DUR=$(( $(date +%s) - T2START ))
    T2VERIFY="no"; grep -aq "VERIFY PASSED" "$LOG2" 2>/dev/null && T2VERIFY="yes"
    T2ITERS=$(grep -ac "iteration " "$LOG2" 2>/dev/null || echo 0)
    T2FILES=$(git -C "$WT_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

    # Classification is left EMPTY for the results-read to assign from the
    # transcript. Auto-labelling it here would be the scorer deciding a
    # behavioural question from counters, which is what the debug cell just
    # taught us not to do (three distinct causes shared one CSV signature).
    echo "$MODEL,$REP,no,$T1FILES,RAN,$T2VERIFY,$T2FILES,$T2ITERS,$T2DUR,,$(basename "$LOG2")" >> "$R3_CSV"
    echo "[$RUN_TAG-r3] $MODEL rep $REP TURN 2 done verify=$T2VERIFY files=$T2FILES iters=$T2ITERS dur=${T2DUR}s" >> "$DRIVER_LOG"
    unload_model "$MODEL" "$HOST"
  done
done

echo "=== $RUN_TAG MACSTUDIO R3 TWO-TURN ROUND COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "R3 rows: $R3_CSV"
