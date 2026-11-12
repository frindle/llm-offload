#!/bin/bash
# v10 CELL E — bulk/mechanical codemod. The work class the owner most wants to
# offload, at n=0 coverage for every model, so the whole roster runs.
#
# PREREGISTRATION: ollama-bakeoff/designs/v10-PREREGISTRATION.md (91a177a).
# Fixture: bakeoff-fixtures/bulk-codemod-v10 @ b2d9b21 -- 49 sites across 12 of
# 20 files, 23 metacharacter (47%), idempotent, tree hash 30a06c4a0d2f4537.
#
# THE ANSWER KEY MUST NOT REACH THE MODEL
# ----------------------------------------
# The fixture repo contains codemod.py -- the oracle -- and CHECKSUMS.sha256.
# Handing a model a worktree of that repo would let it run the oracle and score
# 49/49 having done nothing, and the CSV would read as a triumph. Staging is
# therefore a COPY of py/ and ts/ only, via bakeoff-v10-bulk-score.py --stage,
# which raises if either file appears in the destination. Every rep is staged
# fresh, so one model's edits can never seed another's.
#
# VERIFY IS A SANITY GATE, NOT THE SCORE
# ---------------------------------------
# `python3 -m py_compile py/*.py` only catches syntax damage. It cannot tell the
# model whether its replacements are correct or complete. This is deliberate: a
# verify that reported correctness would be the answer key by another route --
# the model would iterate against it until it passed, and the cell would measure
# nothing. Scoring happens afterwards, out of the model's reach.
# NOTE: the gate is Python-only because the fixture ships no TypeScript
# toolchain. It is the SAME command for every model, which is what "equal
# information" requires -- equality is across models, not across languages.
# Queued for Fable as a design confirmation, not treated as settled.
#
# CALIBRATION PASSED before this file was written to run (prereg §5):
#   known-good 49/49 sites, 0 missed, 0 collateral
#   known-bad  46/49, 3 missed, offending site NAMED
#   pristine   0/49, 49 missed  (the "did nothing" floor is correct)
set -uo pipefail
export RUN_TAG=v10-bulk
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

BASE_DIR="/Users/user/Desktop/GitHub Projects"
SCORER="$BASE_DIR/bakeoff-v10-bulk-score.py"
TASKGEN="$BASE_DIR/bakeoff-v10-bulk-task.py"
HOST="${BAKEOFF_HOST:-http://localhost:11434}"
BACKEND="macstudio"
# ⚠ MUST NOT equal $RESULTS_CSV. Sourcing the lib writes its 28-column v8 header
# to results-${RUN_TAG}.csv immediately (bakeoff-v8-lib.sh:77). RUN_TAG here is
# v10-bulk, so that path IS results-v10-bulk.csv -- the `[ -f ] || echo <header>`
# below then sees a file that already exists and skips its own header, leaving
# 18-column rows under a 28-column header.
#
# This is the SAME collision found in the tiebreak driver earlier tonight. It was
# fixed there and NOT checked for here, so it recurred. Hence the assert.
BULK_CSV="$OUTDIR/results-v10-bulk-cellE.csv"
if [ "$BULK_CSV" = "$RESULTS_CSV" ]; then
  echo "ABORT: cell E CSV collides with the lib's results file ($BULK_CSV)" >&2
  exit 1
fi
VERIFY_CMD="python3 -m py_compile py/*.py"
STAGE_BASE="$WT_BASE/bulk-codemod-v10"

# Prereg checklist item 8: assert the results path, never trust it.
case "$BULK_CSV" in
  */results-v10-bulk-cellE.csv) ;;
  *) echo "ABORT: BULK_CSV is '$BULK_CSV', expected results-v10-bulk-cellE.csv" >&2; exit 1 ;;
esac
for FROZEN in results-v9.csv results-v9-r3.csv results-v8.csv results-v10catchup.csv results-v10parity.csv; do
  [ "$BULK_CSV" = "$OUTDIR/$FROZEN" ] && { echo "ABORT: would write into frozen $FROZEN" >&2; exit 1; }
done

# Re-run calibration at dispatch time. An oracle that passed an hour ago is not
# evidence about the oracle that is about to grade 27 runs.
if ! python3 "$SCORER" --calibrate; then
  echo "ABORT: calibration FAILED -- cell E must not dispatch (prereg §5)" >&2
  exit 1
fi

TASK=$(python3 "$TASKGEN") || { echo "ABORT: task generation failed" >&2; exit 1; }
[ ${#TASK} -gt 3000 ] || { echo "ABORT: task text implausibly short (${#TASK} chars)" >&2; exit 1; }

# CELL METADATA — Fable's mandatory addendum to the Q3 ruling (2026-08-25).
# Recorded as a sidecar so no future results-read can mistake "verify passed"
# for "syntactically clean tree".
#
# The in-loop verify covers 6 of the 20 fixture files. It is Python-only because
# the fixture ships no TypeScript toolchain, and it is the SAME command for every
# model, which is what equal information requires -- equality is across models,
# not across languages.
#
# Under the VACUOUS_PASS standing rule, this cell is compliant NOT because its
# verify fails on the pristine tree (it does not -- pristine Python compiles) but
# because `verify_passed` is NOT this cell's success metric. The byte-diff oracle
# is, and it runs after the fact, out of the model's reach. That is the compliant
# shape: a cell may have a non-discriminating gate provided a separate scorer
# carries the outcome.
cat > "$OUTDIR/results-v10-bulk-cellE.META.md" <<'META'
# cell E metadata

- **verify_command**: `python3 -m py_compile py/*.py`
- **verify_discriminating**: NO — pristine Python compiles, so the gate passes on an untouched tree.
- **verify_coverage**: 6 of 20 fixture files (Python only; the fixture ships no TypeScript toolchain).
- **success_metric**: NOT `verify_passed`. The byte-diff oracle (`bakeoff-v10-bulk-score.py`),
  run after the model finishes and out of its reach.
- **VACUOUS_PASS compliance**: satisfied by the separate scorer, not by a discriminating gate.
- **A model that changes nothing scores 0/49**, verified as a control before dispatch.
- **Do not read `verify_passed` as "syntactically clean tree"** — TypeScript damage gets no in-loop
  signal, though the byte-diff still scores it correctly.
META

[ -f "$BULK_CSV" ] || echo "model,backend,rep,exit_code,duration_s,iterations,stop_reason,num_ctx,native_ctx,sites_correct,sites_total,ident_correct,ident_total,meta_correct,meta_total,sites_missed,collateral_files,transcript" > "$BULK_CSV"

# Timeouts sized from each model's measured PHOTO median x ~2, not guessed.
# 4th field is MANUAL — textual tool-schema injection, for models whose native
# tool_calls do not parse. Carried over from the v8/v9 rosters, where ONLY
# deepseek-r1:32b needs it.
#
# The first version of this roster had three fields and never passed
# --manual-tools at all. deepseek-r1:32b then scored 0/49 in BOTH reps, quitting
# after 2 iterations, and the log shows why: it narrates "I will systematically
# apply each replacement using the edit_file tool", emits nothing parseable,
# takes the corrective nudge, apologises, emits nothing parseable again, and the
# loop treats that as final. That is a DIALECT failure, not a capability
# failure, and scoring it 0/49 would launder a harness bug into evidence about
# the model -- the same error the config_ceiling ruling exists to prevent.
# Its v8 timeout was also 6000s, not 3600s; restored.
ROSTER=(
  "qwen2.5-coder:14b|qwen2-5-coder-14b|1800|no"
  "gpt-oss:20b|gpt-oss-20b|1800|no"
  "ornith-1.5:35b|ornith-1.5-35b|2400|no"
  "ornith-1.5:9b|ornith-1.5-9b|2400|no"
  "qwen3-coder-next:q4_K_M|qwen3-coder-next-q4_K_M|2400|no"
  "qwen3-14b-agentic|qwen3-14b-agentic|2400|no"
  "deepseek-r1:32b|deepseek-r1-32b|6000|yes"
  "qwen3-coder:30b|qwen3-coder-30b|3600|no"
  "qwen3.8:27b-q8_0|qwen3-8-27b-q8_0|4200|no"
)

echo "=== $RUN_TAG CELL E START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "[$RUN_TAG] results -> $BULK_CSV" >> "$DRIVER_LOG"

for REP in 1 2 3; do
  echo "=== $RUN_TAG REP $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO MANUAL <<< "$M"
    CTX=$(ctx_for "$MODEL")
    NATIVE=$(native_ctx_for "$MODEL")
    if [ "$CTX" = "0" ]; then
      echo "[$RUN_TAG] ABORT: no native context recorded for $MODEL" >> "$DRIVER_LOG"
      continue
    fi
    WT="$STAGE_BASE/$SLUG"
    LOG="$OUTDIR/$SLUG-bulk-codemod-$RUN_TAG-$BACKEND-r${REP}.log"

    # Fresh tree every rep. Answer key absent by construction.
    if ! python3 "$SCORER" --stage "$WT" >/dev/null; then
      echo "[$RUN_TAG] ABORT: staging failed for $MODEL" >> "$DRIVER_LOG"
      echo "$MODEL,$BACKEND,$REP,ABORT_STAGE,0,0,ABORT_STAGE,$CTX,$NATIVE,0,49,0,26,0,23,49,0,none" >> "$BULK_CSV"
      continue
    fi

    EXTRA=()
    [ "${MANUAL:-no}" = "yes" ] && EXTRA+=(--manual-tools)

    case "$MODEL" in
      deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
      qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
      devstral*)              SAMPLING=(--temperature 0.2 --top-p 0.95) ;;
      *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    esac

    echo "[$RUN_TAG] START: $MODEL rep $REP ctx=$CTX/$NATIVE timeout=${TMO}s" >> "$DRIVER_LOG"
    restart_inference_server "$HOST" "$MODEL" "$CTX" || true
    T0=$(date +%s)
    python3 "$WORKER" \
      --model "$MODEL" --host "$HOST" --cwd "$WT" \
      --task "$TASK" --verify "$VERIFY_CMD" \
      --max-iters 30 --num-ctx "$CTX" \
      "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}" > "$LOG" 2>&1 &
    WPID=$!
    ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
    wait "$WPID" 2>/dev/null; RC=$?
    kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null
    DUR=$(( $(date +%s) - T0 ))
    ITERS=$(grep -ac "iteration " "$LOG" 2>/dev/null || echo 0)
    STOP=$(grep -aoE "stop=[a-z_]+" "$LOG" 2>/dev/null | tail -1 | cut -d= -f2)
    [ -z "$STOP" ] && STOP="none"

    # Score OUT of the model's reach, after the run.
    S=$(python3 "$SCORER" --score "$WT" 2>/dev/null)
    SC=$(echo "$S" | grep -oE "sites [0-9]+/[0-9]+" | head -1 | tr -d 'sites ' )
    IC=$(echo "$S" | grep -oE "ident [0-9]+/[0-9]+" | head -1 | sed 's/ident //')
    MC=$(echo "$S" | grep -oE "meta [0-9]+/[0-9]+" | head -1 | sed 's/meta //')
    MISS=$(echo "$S" | grep -oE "missed [0-9]+" | head -1 | sed 's/missed //')
    COLL=$(echo "$S" | grep -oE "collateral [0-9]+" | head -1 | sed 's/collateral //')
    echo "$MODEL,$BACKEND,$REP,$RC,$DUR,$ITERS,$STOP,$CTX,$NATIVE,${SC%%/*},${SC##*/},${IC%%/*},${IC##*/},${MC%%/*},${MC##*/},${MISS:-0},${COLL:-0},$(basename "$LOG")" >> "$BULK_CSV"
    echo "[$RUN_TAG] DONE: $MODEL rep $REP $S dur=${DUR}s iters=$ITERS stop=$STOP" >> "$DRIVER_LOG"
    unload_model "$MODEL" "$HOST"
  done
done

echo "=== $RUN_TAG CELL E COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "bulk rows: $BULK_CSV"
