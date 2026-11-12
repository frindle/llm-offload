#!/bin/bash
# v10 PARITY ROUND — Mac Studio. Closes the coverage gaps so every model has the
# same base-arm cells. The owner: "all models across all projects are equal in their
# status" / "just get everything to parity for models".
#
# WHAT THE AUDIT FOUND (coverage matrix over results-v8/v9/v10catchup)
# --------------------------------------------------------------------
#   cell                    | state
#   plex-release-group-debug| n=6 for all nine models      -> ALREADY AT PARITY
#   twoturn                 | n=2 for all nine models      -> ALREADY AT PARITY
#   clamshell (base)        | 2..6 -- newcomers at 2, mode is 5
#   photo (base)            | 3 for five models, ZERO for the three newcomers
#
# So this round runs exactly two things: the photo cell for the three newcomers,
# and a clamshell top-up to bring them from 2 to 5.
#
# WHAT IS DELIBERATELY *NOT* IN SCOPE
# ------------------------------------
# 1. The `repomap` and `apisurface` arms. These are NOT parity cells -- they are
#    targeted hypothesis tests with chosen subjects and qwen3.8 as a CONTROL
#    (bakeoff-v8-arms.sh:23-58). Running them on all nine models would not create
#    parity; it would destroy the control structure that makes them readable, and
#    the arms' own driver already drops a model to save ~3h. Left alone.
#
# 2. `qwen2.5-coder:14b`'s photo cell. Its zero is NOT a gap -- it is RETIRED and
#    settled (bakeoff-v8-lib.sh:404-413): native_ceiling 32768, hit x3 in v7, and
#    32768 IS its maximum, so there is no larger window to retry at. Running it
#    would spend ~30 min/rep reproducing a known ceiling. Excluding it is what
#    parity-by-information means here, not an omission.
#
# CONTEXT PRE-CHECK -- run before writing this file, not assumed
# ---------------------------------------------------------------
# The photo cell peaks at ~72% of 64k, and a model whose native window is below
# that produces config_ceiling rows instead of a measurement -- which is exactly
# why qwen2.5-coder's photo cell was retired. Read live from /api/show:
#
#   gpt-oss:20b      native 131072  -> ctx_for gives 65536
#   ornith-1.5:9b    native 262144  -> ctx_for gives 65536
#   ornith-1.5:35b   native 262144  -> ctx_for gives 65536
#
# All three clear it, at the SAME 65536 window the incumbents were measured at.
# No config_ceiling risk from context on this roster.
#
# RESULTS PATH -- ONBOARDING-A-MODEL.md silent-failure gate 1
# ------------------------------------------------------------
# RUN_TAG is v10parity, so RESULTS_CSV resolves to results-v10parity.csv
# (bakeoff-v8-lib.sh:72). It must NOT append into results-v10catchup.csv: that
# dataset is complete and already written up, and a cloned driver with a
# hardcoded path has nearly corrupted a frozen dataset once already. Asserted
# below rather than trusted.
set -uo pipefail
export RUN_TAG=v10parity
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

# Gate 1, mechanical: refuse to start if the results path is not the new file.
case "$RESULTS_CSV" in
  */results-v10parity.csv) ;;
  *) echo "ABORT: RESULTS_CSV is '$RESULTS_CSV', expected results-v10parity.csv" >&2
     exit 1 ;;
esac
for FROZEN in results-v10catchup.csv results-v9.csv results-v9-r3.csv results-v8.csv; do
  if [ "$RESULTS_CSV" = "$OUTDIR/$FROZEN" ]; then
    echo "ABORT: would write into frozen dataset $FROZEN" >&2; exit 1
  fi
done

ROSTER=(
  "ornith-1.5:9b|ornith-1.5-9b|2400|no|no"
  "ornith-1.5:35b|ornith-1.5-35b|3600|no|no"
  "gpt-oss:20b|gpt-oss-20b|3600|no|no"
)

echo "=== $RUN_TAG MACSTUDIO PARITY ROUND START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "[$RUN_TAG] results -> $RESULTS_CSV" >> "$DRIVER_LOG"

# ---- PHASE 1: photo cell, n=3, the true zero-coverage gap ------------------
export ONLY_TASK=photo
for REP in 1 2 3; do
  echo "=== $RUN_TAG PHOTO REP $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG <<< "$M"
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" \
                 "$BACKEND" "$HOST" "$REP" "base" </dev/null
  done
done

# ---- PHASE 2: clamshell top-up, reps 3-5, to match the roster mode of 5 -----
# The newcomers already have base reps 1-2 from the catch-up round. Numbering
# continues at 3 so the rep column stays a real identifier and the two rounds
# can be pooled without collision.
export ONLY_TASK=clam
for REP in 3 4 5; do
  echo "=== $RUN_TAG CLAMSHELL REP $REP/5 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG <<< "$M"
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" \
                 "$BACKEND" "$HOST" "$REP" "base" </dev/null
  done
done

echo "=== $RUN_TAG MACSTUDIO PARITY ROUND COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "parity rows: $RESULTS_CSV"
