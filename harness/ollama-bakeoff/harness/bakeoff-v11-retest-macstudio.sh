#!/bin/bash
# v11 RETEST — catch-up round. Runs AFTER v10 cell E.
#
# Scope comes from bakeoff-coverage-audit.py, which counts MEASUREMENTS rather
# than rows. Parity was miscounted three times in one evening by treating rows
# as evidence when they were not: abort rows, void-cell rows, and diff line
# counts that omitted untracked files. The audit is now the tool, not a step
# someone remembers.
#
# PHASE 1 — PHOTO RETEST, n=3, 8 models (24 runs)
# -----------------------------------------------
# The photo cell is at COUNT parity (3 valid measurements each) but its data is
# not trustworthy, for two independent reasons:
#
#   1. NON-DISCRIMINATING VERIFY. Preflight and verify are both `npm run build`,
#      and preflight must PASS on the pristine tree. So the baseline builds, and
#      a model that changes NOTHING passes verify. Four historical rows are
#      exactly that -- verify=yes with files_changed=0 -- and two of them sit
#      inside shipped routing.json verdicts. Contrast clamshell, whose preflight
#      must FAIL, so its verify genuinely discriminates.
#
#   2. THE DELIVERABLE WAS NEVER ARCHIVED. `git diff` shows tracked changes
#      only; the archive recorded the NAMES of created files but not their
#      contents, and the next run's reset destroyed them. On a greenfield cell
#      the new files ARE the work. Fixed in bakeoff-v8-lib.sh (71aed7b) -- this
#      round is the first that can actually be audited.
#
# Re-running is the only way to get inspectable greenfield data. The old rows
# stay in place; they are not deleted, they are superseded, and the results-read
# must say which set it used.
#
# qwen2.5-coder:14b is EXCLUDED from photo -- retired, not missing. native_ceiling
# x3 in v7 at 32768, which IS its maximum, so there is no larger window to retry
# at (bakeoff-v8-lib.sh:404-413).
#
# PHASE 2 — qwen3-coder:30b DEBUG, the one genuine coverage hole (3 runs)
# ----------------------------------------------------------------------
# ALL SIX of its debug runs, v8 and v9, stopped at `config_ceiling` at 65536 of a
# native 262144. Fable's ruling: config_ceiling is a BUG in how we ran it and is
# re-runnable; native_ceiling is a real measurement. So this model has ZERO valid
# debug measurements -- it was never measured on that cell, and the "0/3" I
# reported earlier was an instrument artifact read as a model failure.
#
# Run at 131072, NOT 262144: it died at 65536, so the informative step is
# doubling, not quadrupling, and it halves KV cost. This is the same probe v10
# cell C specifies; if the ceiling still trips, the answer is native, not config.
set -uo pipefail
export RUN_TAG=v11retest
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

case "$RESULTS_CSV" in
  */results-v11retest.csv) ;;
  *) echo "ABORT: RESULTS_CSV is '$RESULTS_CSV', expected results-v11retest.csv" >&2; exit 1 ;;
esac
for FROZEN in results-v8.csv results-v9.csv results-v9-r3.csv results-v10catchup.csv results-v10parity.csv results-v10-bulk.csv; do
  [ "$RESULTS_CSV" = "$OUTDIR/$FROZEN" ] && { echo "ABORT: would write into frozen $FROZEN" >&2; exit 1; }
done

# Refuse to run without the archiving fix -- the whole point of the retest is
# that the created files are captured this time.
if ! grep -q "UNTRACKED FILE CONTENTS" "$BASE/bakeoff/harness/bakeoff-v8-lib.sh"; then
  echo "ABORT: lib lacks the untracked-content archiver; retest would repeat the defect" >&2
  exit 1
fi

PHOTO_ROSTER=(
  "gpt-oss:20b|gpt-oss-20b|1800"
  "ornith-1.5:35b|ornith-1.5-35b|2400"
  "ornith-1.5:9b|ornith-1.5-9b|2400"
  "qwen3-coder-next:q4_K_M|qwen3-coder-next-q4_K_M|2400"
  "qwen3-14b-agentic|qwen3-14b-agentic|2400"
  "deepseek-r1:32b|deepseek-r1-32b|3600"
  "qwen3-coder:30b|qwen3-coder-30b|3600"
  "qwen3.8:27b-q8_0|qwen3-8-27b-q8_0|4200"
)

echo "=== $RUN_TAG RETEST START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "[$RUN_TAG] results -> $RESULTS_CSV" >> "$DRIVER_LOG"

# The VACUOUS_PASS gate (Fable, 2026-08-25) ABORTS any cell whose verify passes
# on the pristine tree while still declaring `verify_passed` as its success
# metric. The photo cell is exactly that shape, so without this declaration the
# retest would refuse to dispatch -- correctly.
#
# The replacement metric is `bakeoff-photo-score.py`: four independently
# checkable requirements from the task text (upload control, API route, order
# association, storage), scored from the ARCHIVE rather than the worktree, since
# the worktree is reset before the next run.
#
# It is a FLOOR, not a quality judgement -- it detects presence, not correctness.
# That is the whole point: it replaces a metric under which a model that changed
# ZERO files scored a pass. Reported as `requirements_present`, never `passes`.
export SUCCESS_METRIC=photo_requirements_present

export ONLY_TASK=photo
for REP in 1 2 3; do
  echo "=== $RUN_TAG PHOTO REP $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${PHOTO_ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO <<< "$M"
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "no" "no" "$BACKEND" "$HOST" "$REP" "base" </dev/null
  done
done

# Phase 2: the one real coverage hole. CTX_OVERRIDE is honoured by ctx_for's
# caller in the driver, so it is set explicitly rather than assumed.
echo "=== $RUN_TAG qwen3-coder:30b DEBUG @ 131072 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
export ONLY_TASK=debug
export CTX_OVERRIDE=131072
for REP in 1 2 3; do
  run_model_v8 "qwen3-coder:30b" "qwen3-coder-30b" 3600 "no" "no" "$BACKEND" "$HOST" "$REP" "base" </dev/null
done
unset CTX_OVERRIDE

echo "=== $RUN_TAG RETEST COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "retest rows: $RESULTS_CSV"
