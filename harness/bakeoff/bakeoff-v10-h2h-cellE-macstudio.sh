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
export RUN_TAG=v10-h2h
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

BASE_DIR="/Users/user/Desktop/GitHub Projects/bakeoff"  # moved into bakeoff/ 2026-09-02 with its scorer+taskgen; v8-lib is still sourced from root by absolute path on line 36
SCORER="$BASE_DIR/bakeoff-v10-bulk-score.py"
TASKGEN="$BASE_DIR/bakeoff-v10-bulk-task.py"
HOST="http://localhost:11434"
BACKEND="macstudio"
# ⚠ MUST NOT equal $RESULTS_CSV. Sourcing the lib writes its 28-column v8 header
# to results-${RUN_TAG}.csv immediately (bakeoff-v8-lib.sh:77). RUN_TAG here is
# v10-bulk, so that path IS results-v10-bulk.csv -- the `[ -f ] || echo <header>`
# below then sees a file that already exists and skips its own header, leaving
# 18-column rows under a 28-column header.
#
# This is the SAME collision found in the tiebreak driver earlier tonight. It was
# fixed there and NOT checked for here, so it recurred. Hence the assert.
# Opt-in diagnostic side-run (BAKEOFF_LAGUNA_DIAG=1) writes to its OWN csv so the
# pinned cellE results are never touched. Default path unchanged.
BULK_CSV="$OUTDIR/results-v10-h2h-cellE.csv"
if [ "$BULK_CSV" = "$RESULTS_CSV" ]; then
  echo "ABORT: cell E CSV collides with the lib's results file ($BULK_CSV)" >&2
  exit 1
fi
VERIFY_CMD="python3 -m py_compile py/*.py"
STAGE_BASE="$WT_BASE/bulk-codemod-v10"

# Prereg checklist item 8: assert the results path, never trust it.
case "$BULK_CSV" in
  */results-v10-h2h-cellE.csv) ;;
  *) echo "ABORT: BULK_CSV is '$BULK_CSV', expected results-v10-h2h-cellE.csv" >&2; exit 1 ;;
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
cat > "$OUTDIR/results-v10-h2h-cellE.META.md" <<'META'
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
  # Fresh-eval additions, 2026-09-04 (bigger models the owner wanted). Fit measured on
  # the 64GB Studio: llama3.3:70b loads fully in VRAM (47.9GB) only at num_ctx
  # 32768 -- capped in ctx_for(). laguna-xs-2.1 (33.4B, thinking) is 20.4GB at
  # 65536. Both have native tool_calls; MANUAL=no (if either emits an unparseable
  # tool dialect and scores 0/49, that is a harness/dialect finding like deepseek,
  # not model incapacity -- do not pre-tune it away). laguna-s-2.1 excluded: its
  # ~75GB q4_k_m needs a 128GB-class host, cannot run here.
  "llama3.3:70b|llama3.3-70b|6000|no"
  "laguna-xs-2.1:latest|laguna-xs-2.1|2400|no"
  # Qwen3-32B eval, 2026-09-08 (the owner: "next size up on qwen 27b"). The true next
  # dense rung above qwen3.8:27b. TWO arms:
  #   - q4_K_M is the MATCHED comparison against qwen3.8:27b-q4_K_M (isolates
  #     model size, not quant -- feedback_pin_host_and_quant_for_comparisons).
  #   - q8_0 is a SEPARATE labelled data point (quant ceiling): q8-32B vs the
  #     resident q8-27B (size at high quant) and vs q4-32B (what Q4 costs at 32B).
  # native_ctx read from /api/show on the pulled tag at fire time (preflight item
  # 3), not guessed. Timeouts: q4 sized ~= 27b-q8 (4200); q8 heavier, 5400.
  "qwen3:32b|qwen3-32b-q4_K_M|4200|no"
  "qwen3:32b-q8_0|qwen3-32b-q8_0|5400|no"
  # Coder cohort + frankenmerges, 2026-09-08 (the owner: "pull 'em and queue 'em...
  # not against more frankenmodels"). All Q4_K_M to match the roster quant.
  #   qwen2.5-coder:32b -- dedicated coder at 32B (we only had the 14B); the
  #     highest-signal clean addition. native tool_calls.
  #   devstral:24b -- Mistral's agentic-coding model, purpose-built for tool-use
  #     (our exact harness shape). qwen* sampling case does NOT match it; the
  #     devstral* case (temp 0.2/top-p 0.95) does.
  #   davidau-qwen38-mtp -- DavidAU frankenmerge of qwen3.8:27b (our workhorse +
  #     gate reviewer) w/ multi-token-prediction. GGUF import; MTP may not run
  #     cleanly in ollama -- if it 0/49s on a load/dialect failure that is a
  #     harness finding, not incapacity (cf deepseek dialect note above).
  #   rombos-coder-v2.5 -- rombodawg continuous-fine-tune merge on the
  #     Qwen2.5-Coder-32B base; a DISTINCT frankenmerge, directly comparable to
  #     qwen2.5-coder:32b. GGUF import.
  # native_ctx for all four read from /api/show at fire time (preflight item 3).
  "qwen2.5-coder:32b|qwen2.5-coder-32b|3600|no"
  "devstral-small-2:24b|devstral-small-2-24b|3600|no"
  "davidau-qwen38-mtp:q4_K_M|davidau-qwen38-mtp-q4_K_M|4200|no"
  "qwen3.8:27b-q4_K_M|qwen3.8-27b-q4_K_M|4200|no"
  "rombos-coder-v2.5:q4_K_M|rombos-coder-v2.5-32b-q4_K_M|3600|no"
)

# ROSTER_ONLY (space-separated MODEL tags) restricts this run to those models --
# additive, defaults to the full roster. Used 2026-09-04 to append ONLY the two
# fresh-eval additions without re-running the 9 completed models. Comparability
# holds because the fixture + scorer are pinned, not because of the file name.
if [ -n "${ROSTER_ONLY:-}" ]; then
  _FILTERED=()
  for _m in "${ROSTER[@]}"; do
    _tag="${_m%%|*}"
    case " $ROSTER_ONLY " in *" $_tag "*) _FILTERED+=("$_m") ;; esac
  done
  [ ${#_FILTERED[@]} -gt 0 ] || { echo "ABORT: ROSTER_ONLY='$ROSTER_ONLY' matched no roster model" >&2; exit 1; }
  ROSTER=("${_FILTERED[@]}")
fi

# --- RESUME / skip-completed (added 2026-09-09; the owner: the bake-off must run THROUGH
# the queue, and a re-queue must RESUME, not restart from rep 1). A cell counts as
# DONE when a row for this (MODEL,BACKEND,REP) already exists in $BULK_CSV whose
# stop_reason (field 7) is not ABORT_STAGE (an ABORT_STAGE row should re-run). awk
# does field-EXACT matching, so tags with regex metachars (qwen3.8:27b, ...:q4_K_M)
# match correctly. On a fresh run the CSV holds only the header -> nothing matches.
already_done() {
  [ -f "$BULK_CSV" ] || return 1
  awk -F, -v m="$1" -v b="$2" -v r="$3" \
    '$1==m && $2==b && $3==r && $7!="ABORT_STAGE" {f=1} END{exit f?0:1}' "$BULK_CSV"
}

echo "=== $RUN_TAG CELL E START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "[$RUN_TAG] results -> $BULK_CSV" >> "$DRIVER_LOG"

for REP in 1 2 3 4 5; do
  echo "=== $RUN_TAG REP $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  for M in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TMO MANUAL <<< "$M"
    if already_done "$MODEL" "$BACKEND" "$REP"; then
      echo "[$RUN_TAG] SKIP (already done -- resume): $MODEL rep $REP" >> "$DRIVER_LOG"
      continue
    fi
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

    # Opt-in diagnostic (BAKEOFF_LAGUNA_DIAG=1): Poolside's DOCUMENTED agentic
    # config for Laguna XS 2.1 -- temp 1.0, top_k 20, top_p 1.0 (HF card) -- plus
    # preserved interleaved reasoning across turns (--preserve-reasoning). Tests
    # whether the over-read/iter_cap 0-7/49 was a harness confound (reasoning
    # stripped from a model whose vendor requires it preserved) vs real incapacity.
    # OFF by default so every pinned row stays byte-identical; laguna-scoped.
    if [ "${BAKEOFF_LAGUNA_DIAG:-0}" = "1" ] && [[ "$MODEL" == laguna-xs-2.1* ]]; then
      SAMPLING=(--temperature 1.0 --top-k 20 --top-p 1.0)
      EXTRA+=(--preserve-reasoning)
    fi

    echo "[$RUN_TAG] START: $MODEL rep $REP ctx=$CTX/$NATIVE timeout=${TMO}s" >> "$DRIVER_LOG"
    # Visibility bridge (opt-in). Pre-compute the queue record id before launch.
    ANNOUNCE_ID=""
    [ "${BAKEOFF_QUEUE_ANNOUNCE:-0}" = "1" ] && ANNOUNCE_ID=$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '-' | cut -c1-12)
    restart_inference_server "$HOST" "$MODEL" "$CTX" || true
    T0=$(date +%s)
    python3 "$WORKER" \
      --model "$MODEL" --host "$HOST" --cwd "$WT" \
      --task "$TASK" --verify "$VERIFY_CMD" \
      --max-iters 30 --num-ctx "$CTX" \
      "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}" > "$LOG" 2>&1 &
    WPID=$!
    # Announce to the queue for visibility only (backgrounded, never breaks the run).
    [ -n "$ANNOUNCE_ID" ] && ( python3 "$ANNOUNCER" start --id "$ANNOUNCE_ID" \
        --label "bakeoff:$SLUG:bulk-codemod:r$REP" --model "$MODEL" --host "$HOST" \
        --pid "$WPID" --cwd "$WT" --num-ctx "$CTX" --log "$LOG" >/dev/null 2>&1 & )
    ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
    wait "$WPID" 2>/dev/null; RC=$?
    kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null
    [ -n "$ANNOUNCE_ID" ] && ( python3 "$ANNOUNCER" finish --id "$ANNOUNCE_ID" --exit "$RC" >/dev/null 2>&1 & )
    DUR=$(( $(date +%s) - T0 ))
    ITERS=$(grep -ac "iteration " "$LOG" 2>/dev/null || echo 0)
    # stop_reason: the worker records it in the transcript JSON and prints the
    # CONTEXT CEILING / LOAD FAILED markers to the log -- it never emits a `stop=`
    # token, so the old grep here silently logged every row as "none" (masking
    # config_ceiling vs native_ceiling, the whole point of the split). Mirror the
    # lib's canonical classifier: a ceiling below native ctx is config_ceiling
    # (re-runnable BUG/unmeasured), at/above native is native_ceiling (real).
    # Fixed 2026-09-04 (Opus review of laguna/llama bulk-codemod failures).
    TIMED_OUT=false; [ "$DUR" -ge "$TMO" ] && TIMED_OUT=true
    if [ "$RC" -eq 3 ] || grep -aq "LOAD FAILED" "$LOG" 2>/dev/null; then
      STOP="load_failed"
    elif grep -aq "CONTEXT CEILING" "$LOG" 2>/dev/null; then
      if [ "$CTX" -ge "$NATIVE" ]; then STOP="native_ceiling"; else STOP="config_ceiling"; fi
    elif [ "$TIMED_OUT" = "true" ]; then
      STOP="timeout"
    elif [ "$RC" -eq 0 ]; then
      STOP="converged"
    elif [ "$RC" -eq 2 ]; then
      STOP="iter_cap"
    else
      STOP="none"
    fi

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
