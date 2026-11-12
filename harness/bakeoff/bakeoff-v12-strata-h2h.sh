#!/bin/bash
# v12 STRATA HEAD-TO-HEAD -- Strata (Qwen3.8-Flash-Next 125B IQ2_XS, native CUDA on the
# claude-sandbox RTX 3080) vs the current dispatch model qwen3.6-35b-a3b-vl-mtp-mxfp8 on
# local Darkbloom. Plan: STRATA-H2H-PLAN.md (same dir).
#
# REUSE, NOT A NEW HARNESS. Both cells are existing, calibrated bake-off cells:
#   debug  plex-release-group-debug  -- bakeoff-v8-lib.sh's _one_task_v8 verbatim: same
#          TASK_DEBUG prompt, same must-FAIL preflight, VACUOUS_PASS probe, test-restoring
#          RUN_VERIFY, diff + new-file archiving, 28-column CSV.
#   bulk   bulk-codemod-v10 (cell E) -- same TASKGEN prompt, same py_compile gate, same
#          out-of-reach byte-diff oracle, same 18-column CSV, same infra-abort rule as the
#          bonsai ternary driver.
# Identical for BOTH arms: prompt, tools (the worker's TOOLS), worker (current
# ollama-worker.py, --api openai -- the production Darkbloom path), sampling (the lib's
# qwen* case: both are Qwen-family), --max-iters 60, --max-tokens 16384, wall 3600s, grader.
#
# DIFFERS, DECLARED (measure behaviour, control error -- feedback_measure_behavior_control_error):
#   num_ctx      strata 32768 (the most the 12 GB card holds; peak VRAM 11.6 GB in the
#                trial) vs qwen 65536 (lib rule min(65536, native)). A Strata
#                CONTEXT CEILING is therefore `config_ceiling` (OUR box), never a model
#                property -- recorded, not hidden.
#   reasoning    strata: server-side reasoning_budget_tokens (STRATA_REASONING_BUDGET,
#                default 4096) because the trial burned whole budgets thinking. Darkbloom
#                runs as production does (no budget). The owner asked for the budget.
#   host         Mac Studio unified memory vs a 3080 + 96 GB host RAM: this IS the
#                deployment question (could Strata be a dispatch lane on that card).
#
# HOW IT RUNS (never by hand):
#   qwen arm   -> `ollama-queue.py enqueue --runner bakeoff-runner.py --host studio-db`,
#                 task file ROSTER_ONLY=qwen:<cell>  (holds the Darkbloom lane)
#   strata arm -> `ollama-queue.py enqueue-gpu` running ~/bin/strata-h2h-arm.sh <cell>,
#                 which starts Strata (guarded), tunnels it to 127.0.0.1:18180 and calls
#                 this driver with H2H_ARM=strata (holds the unraid lane = the 3080).
# Arm/cells come from H2H_ARM/H2H_CELLS, else from ROSTER_ONLY="<arm>:<cell>[,<cell>]"
# (bakeoff-runner.py passes the task file's ROSTER_ONLY= line through as env).
#
# RESUME: a (model, backend, cell, rep) with a measurement row is skipped, so a re-queue
# continues. An infrastructure end (signal that is not our wall, or the endpoint gone)
# writes NO row and exits 4 -- same rule as the bonsai driver.
#
# H2H_DRY_RUN=1: calibrate, stage, preflight, health-check -- no worker, no rows.
set -uo pipefail
export RUN_TAG=v12-strata-h2h
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

BAKEOFF="$BASE/bakeoff"
SCORER="$BAKEOFF/bakeoff-v10-bulk-score.py"
TASKGEN="$BAKEOFF/bakeoff-v10-bulk-task.py"
CALIBRATE="$BAKEOFF/bakeoff-v12-calibrate.py"
DEBUG_REPO="$BASE/plex-automation/plex-automation-debugcell"
PYBIN="/Users/user/bin/plex-bakeoff-venv/bin/python"
WORKER="/Users/user/bin/ollama-worker.py"      # NOT the lib's pinned v7: Darkbloom auth + --api-key-file live here
BULK_CSV="$OUTDIR/results-v12-strata-h2h-cellE.csv"
REGRADE_CSV="$OUTDIR/results-v12-strata-h2h-regrade.csv"
META="$OUTDIR/results-v12-strata-h2h.META.md"
REPS="${H2H_REPS:-3}"
TMO="${H2H_TIMEOUT:-3600}"
DRY="${H2H_DRY_RUN:-0}"

# ---- arm + cells ----------------------------------------------------------------------
ARM="${H2H_ARM:-}"; CELLS="${H2H_CELLS:-}"; KEYFILE=""
if [ -z "$ARM" ] && [ -n "${ROSTER_ONLY:-}" ] && [[ "$ROSTER_ONLY" == *:* ]]; then
  ARM="${ROSTER_ONLY%%:*}"; CELLS="${ROSTER_ONLY#*:}"
fi
CELLS="${CELLS:-debug,bulk}"
case "$ARM" in
  strata)
    MODEL="qwen3.8-flash-next-iq2_xs"; SLUG="strata-iq2-xs"; BACKEND="sandbox3080-strata"
    ARM_URL="${H2H_ARM_URL:-http://127.0.0.1:18180}"; CTX=65536; H2H_NATIVE=262144
    KEYFILE="${H2H_KEY_FILE:-}"
    if [ "$DRY" != "1" ] && { [ -z "$KEYFILE" ] || [ ! -s "$KEYFILE" ]; }; then
      echo "ABORT: strata arm needs H2H_KEY_FILE (0600 file with the Strata key)" >&2; exit 1
    fi
    ARM_ARGS=(--api openai --max-tokens 16384 --max-iters 60 --chat-timeout 3600 ${KEYFILE:+--api-key-file "$KEYFILE"})
    SAMPLING=(--temperature 1.0 --top-p 0.95 --top-k 20 --repeat-penalty 1.0) ;;   # Strata/Qwen3.8-Flash-Next card
  qwen)
    MODEL="qwen3.6-35b-a3b-vl-mtp-mxfp8"; SLUG="qwen3.6-35b-a3b-darkbloom"; BACKEND="studio-darkbloom"
    ARM_URL="${H2H_ARM_URL:-http://127.0.0.1:8000}"; CTX=65536; H2H_NATIVE=262144
    ARM_ARGS=(--api openai --max-tokens 16384 --max-iters 60)
    SAMPLING=(--temperature 0.6 --top-p 0.95 --top-k 20 --repeat-penalty 1.0) ;;   # qwen3.6 thinking-coding card
  *) echo "ABORT: H2H_ARM must be strata|qwen (or ROSTER_ONLY=<arm>:<cells>), got '$ARM'" >&2; exit 1 ;;
esac

# Only through the queue (feedback_all_gpu_work_through_queue). The queue stamps this on
# every process it launches (worker or runner); the GPU-exclusive runner inherits it too.
if [ "$DRY" != "1" ] && [ -z "${OLLAMA_DISPATCH_VIA_QUEUE:-}" ]; then
  echo "ABORT: not launched by ollama-queue.py (enqueue it -- see STRATA-H2H-PLAN.md)" >&2; exit 1
fi

# ---- lib overrides: these endpoints are OpenAI-style servers we do not manage --------
native_ctx_for() { echo "$H2H_NATIVE"; }
ctx_for() { echo "$CTX"; }
endpoint_up() { curl -fs -m 10 "$ARM_URL/health" >/dev/null 2>&1; }
# The lib's version RESTARTS brew ollama for any 127.0.0.1 host -- wrong service, and on a
# tunnel URL it would bounce the Mac's Ollama mid-run. Here it only reports health.
restart_inference_server() {
  if endpoint_up; then echo "[$RUN_TAG] host-control: $ARM_URL healthy (not managed here)" >> "$DRIVER_LOG"; return 0; fi
  echo "[$RUN_TAG] host-control: $ARM_URL NOT healthy" >> "$DRIVER_LOG"; return 1
}
unload_model() { :; }

# ---- results paths: never a frozen file ------------------------------------------------
case "$RESULTS_CSV" in */results-v12-strata-h2h.csv) ;; *) echo "ABORT: RESULTS_CSV=$RESULTS_CSV" >&2; exit 1 ;; esac
for F in "$BULK_CSV" "$RESULTS_CSV" "$REGRADE_CSV"; do
  case "$(basename "$F")" in results-v12-strata-h2h*) ;; *) echo "ABORT: $F" >&2; exit 1 ;; esac
done
[ -f "$BULK_CSV" ] || echo "model,backend,rep,exit_code,duration_s,iterations,stop_reason,num_ctx,native_ctx,sites_correct,sites_total,ident_correct,ident_total,meta_correct,meta_total,sites_missed,collateral_files,transcript" > "$BULK_CSV"
[ -f "$REGRADE_CSV" ] || echo "model,backend,cell,rep,verify_rerun,test_file_writes,chat_retries,http_errors,ceiling,reasoning_budget,log" > "$REGRADE_CSV"

# ---- calibration (grader canary) BEFORE anything is staged -----------------------------
if ! python3 "$CALIBRATE" >> "$DRIVER_LOG" 2>&1; then
  echo "ABORT: v12 grader calibration FAILED -- see $DRIVER_LOG" >&2; exit 1
fi
TASK_BULK=$(python3 "$TASKGEN") || { echo "ABORT: cell-E task generation failed" >&2; exit 1; }
[ ${#TASK_BULK} -gt 3000 ] || { echo "ABORT: cell-E task text implausibly short" >&2; exit 1; }
sha() { printf '%s' "$1" | shasum -a 256 | cut -c1-16; }
{
  echo "# v12 strata h2h metadata (appended per launch)"
  echo "- $(date -u +%FT%TZ) arm=$ARM model=$MODEL backend=$BACKEND url=$ARM_URL cells=$CELLS reps=$REPS wall=${TMO}s"
  echo "  num_ctx=$CTX native=$H2H_NATIVE worker=$WORKER sampling='${SAMPLING[*]}' args='${ARM_ARGS[*]}'"
  echo "  prompt sha256: debug=$(sha "$TASK_DEBUG") bulk=$(sha "$TASK_BULK") (must match across arms)"
  echo "  reasoning budget: ${STRATA_REASONING_BUDGET:-server default} (strata arm only; set server-side by strata-serve.sh)"
} >> "$META"

if ! endpoint_up; then
  echo "ABORT: $ARM endpoint $ARM_URL is not healthy" >&2
  [ "$DRY" = "1" ] || exit 4
fi

infra_end() {  # rc timed_out [iters] -> 0 when the run was ended by infrastructure, not the model
  { [ "$2" != "true" ] && [ "$1" -ge 128 ]; } || { [ "$1" -ne 0 ] && ! endpoint_up; } \
    || [ "${3:-1}" -eq 0 ] || [ "$1" -eq 3 ]   # no turn ever completed (preflight timeout/crash), or load_failed/HTTP 422
}
MISSING=0

regrade() {    # cell rep wt log verify -> one REGRADE_CSV row (independent of the worker's say-so)
  local CELL="$1" REP="$2" WT="$3" LOG="$4" VERIFY="$5" VR TW CR HE CEIL TR
  # bounded: the verify runs model-edited code (a catastrophic-backtracking regex or a loop in it
  # would otherwise hang the whole queue row after the cell finished)
  if ( cd "$WT" && timeout 300 bash -c "$VERIFY" >/dev/null 2>&1 < /dev/null ); then VR=pass; else VR=fail; fi
  TR=$(grep -o "/Users/user/bin/ollama-worker-logs/[0-9TZ]*\.json" "$LOG" 2>/dev/null | tail -1)
  TW=$(timeout 60 python3 "$BAKEOFF/bakeoff-v12-testwrites.py" "$TR" 2>/dev/null || echo na)
  CR=$(grep -ac "retry [0-9]*/" "$LOG" 2>/dev/null); CR=${CR:-0}
  HE=$(grep -acE "HTTP Error [45][0-9][0-9]|request failed" "$LOG" 2>/dev/null); HE=${HE:-0}
  CEIL=no; grep -aq "CONTEXT CEILING" "$LOG" 2>/dev/null && CEIL=yes
  echo "$MODEL,$BACKEND,$CELL,$REP,$VR,$TW,$CR,$HE,$CEIL,${STRATA_REASONING_BUDGET:-na},$(basename "$LOG")" >> "$REGRADE_CSV"
}

# ---- debug cell (lib's _one_task_v8, row captured so infra ends are not recorded) -----
debug_done() {
  awk -F, -v m="$MODEL" -v b="$BACKEND" -v r="$1" \
    '$1==m && $2==b && $3=="plex-release-group-debug" && $5==r && $6 !~ /^ABORT/ {f=1} END{exit f?0:1}' "$RESULTS_CSV"
}
run_debug() {
  local WT="$WT_BASE/plex-automation/$SLUG"
  if [ ! -d "$WT" ]; then
    git -C "$DEBUG_REPO" worktree add -q --detach "$WT" "$BASELINE_plex_automation" >> "$DRIVER_LOG" 2>&1 \
      || { echo "[$RUN_TAG] ABORT: cannot create debug worktree $WT" >> "$DRIVER_LOG"; MISSING=1; return; }
  fi
  local VERIFY="git checkout $BASELINE_plex_automation -- test_release_group_numeric.py test_release_group.py && $PYBIN test_release_group_numeric.py && $PYBIN test_release_group.py"
  for REP in $(seq 1 "$REPS"); do
    if debug_done "$REP"; then echo "[$RUN_TAG] SKIP debug $MODEL rep $REP (recorded)" >> "$DRIVER_LOG"; continue; fi
    if [ "$DRY" = "1" ]; then
      git -C "$WT" reset -q --hard "$BASELINE_plex_automation"; git -C "$WT" clean -qfd
      ( cd "$WT" && $PYBIN test_release_group_numeric.py >/dev/null 2>&1 ) \
        && echo "DRY debug: PREMISE BROKEN (repro passes on pristine $WT)" \
        || echo "DRY debug: staged $WT, repro fails on pristine (ok); would run $MODEL rep $REP"
      break
    fi
    endpoint_up || { echo "[$RUN_TAG] ABORT: endpoint down before debug rep $REP -- no row" >> "$DRIVER_LOG"; MISSING=1; return; }
    local REAL="$RESULTS_CSV" ROWF; ROWF=$(mktemp)
    RESULTS_CSV="$ROWF"
    PREFLIGHT_MUST_FAIL=yes _one_task_v8 "$MODEL" "$SLUG" "$CTX" "$TMO" "plex-automation" "plex-release-group-debug" \
      "$PYBIN test_release_group_numeric.py" "$VERIFY" "$TASK_DEBUG" "no" "${RUN_TAG}-$BACKEND" "$ARM_URL" \
      "$BACKEND" "$REP" "base" "${SAMPLING[@]}" "${ARM_ARGS[@]}" </dev/null
    RESULTS_CSV="$REAL"
    local ROW; ROW=$(tail -1 "$ROWF"); rm -f "$ROWF"
    [ -n "$ROW" ] || { echo "[$RUN_TAG] debug rep $REP produced no row" >> "$DRIVER_LOG"; MISSING=1; return; }
    local RC TOUT DIT; RC=$(cut -d, -f6 <<< "$ROW"); TOUT=$(cut -d, -f12 <<< "$ROW"); DIT=$(cut -d, -f14 <<< "$ROW")
    if [[ "$RC" =~ ^[0-9]+$ ]] && infra_end "$RC" "$TOUT" "${DIT:-1}"; then
      echo "[$RUN_TAG] ABORT: debug rep $REP ended by infrastructure (rc=$RC) -- NO row; re-queue resumes" >> "$DRIVER_LOG"
      MISSING=1; return
    fi
    echo "$ROW" >> "$RESULTS_CSV"
    [[ "$RC" =~ ^ABORT ]] && { MISSING=1; continue; }
    regrade debug "$REP" "$WT" "$OUTDIR/$SLUG-plex-release-group-debug-${RUN_TAG}-$BACKEND-base-r${REP}.log" "$VERIFY"
  done
}

# ---- cell E (bulk codemod), the bonsai/v10 loop with this arm's endpoint --------------
bulk_done() {
  awk -F, -v m="$MODEL" -v b="$BACKEND" -v r="$1" '$1==m && $2==b && $3==r && $7!="ABORT_STAGE" {f=1} END{exit f?0:1}' "$BULK_CSV"
}
run_bulk() {
  local WT="$WT_BASE/bulk-codemod-v12-h2h/$SLUG"
  for REP in $(seq 1 "$REPS"); do
    if bulk_done "$REP"; then echo "[$RUN_TAG] SKIP bulk $MODEL rep $REP (recorded)" >> "$DRIVER_LOG"; continue; fi
    if ! python3 "$SCORER" --stage "$WT" >/dev/null; then
      echo "$MODEL,$BACKEND,$REP,ABORT_STAGE,0,0,ABORT_STAGE,$CTX,$H2H_NATIVE,0,49,0,26,0,23,49,0,none" >> "$BULK_CSV"; MISSING=1; continue
    fi
    if [ "$DRY" = "1" ]; then
      echo "DRY bulk: staged $WT ($(python3 "$SCORER" --score "$WT" 2>/dev/null | grep -oE 'sites [0-9]+/[0-9]+' | head -1) pristine); would run $MODEL rep $REP"
      break
    fi
    endpoint_up || { echo "[$RUN_TAG] ABORT: endpoint down before bulk rep $REP -- no row" >> "$DRIVER_LOG"; MISSING=1; return; }
    local LOG="$OUTDIR/$SLUG-bulk-codemod-$RUN_TAG-$BACKEND-r${REP}.log" T0 RC DUR ITERS TIMED_OUT STOP S
    echo "[$RUN_TAG] START: bulk $MODEL rep $REP ctx=$CTX/$H2H_NATIVE wall=${TMO}s" >> "$DRIVER_LOG"
    T0=$(date +%s)
    python3 "$WORKER" --model "$MODEL" --host "$ARM_URL" --cwd "$WT" --task "$TASK_BULK" \
      --verify "python3 -m py_compile py/*.py" --max-iters 60 --num-ctx "$CTX" \
      "${SAMPLING[@]}" "${ARM_ARGS[@]}" > "$LOG" 2>&1 &
    local WPID=$!
    ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
    wait "$WPID" 2>/dev/null; RC=$?
    kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null
    DUR=$(( $(date +%s) - T0 ))
    ITERS=$(grep -ac -- '--- iteration ' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
    TIMED_OUT=false; [ "$DUR" -ge "$TMO" ] && TIMED_OUT=true
    if infra_end "$RC" "$TIMED_OUT" "$ITERS"; then
      echo "[$RUN_TAG] ABORT: bulk rep $REP ended by infrastructure (rc=$RC) -- NO row; re-queue resumes" >> "$DRIVER_LOG"
      MISSING=1; return
    fi
    if [ "$RC" -eq 3 ] || grep -aq "LOAD FAILED" "$LOG"; then STOP=load_failed
    elif grep -aq "CONTEXT CEILING" "$LOG"; then [ "$CTX" -ge "$H2H_NATIVE" ] && STOP=native_ceiling || STOP=config_ceiling
    elif [ "$TIMED_OUT" = true ]; then STOP=timeout
    elif [ "$ITERS" -ge 60 ]; then STOP=iter_cap
    elif [ "$RC" -eq 0 ]; then STOP=converged
    elif [ "$RC" -eq 2 ]; then STOP=iter_cap
    else STOP=none; fi
    S=$(python3 "$SCORER" --score "$WT" 2>/dev/null)
    local SC IC MC MISS COLL
    SC=$(echo "$S" | grep -oE "sites [0-9]+/[0-9]+" | head -1 | tr -d 'sites ')
    IC=$(echo "$S" | grep -oE "ident [0-9]+/[0-9]+" | head -1 | sed 's/ident //')
    MC=$(echo "$S" | grep -oE "meta [0-9]+/[0-9]+" | head -1 | sed 's/meta //')
    MISS=$(echo "$S" | grep -oE "missed [0-9]+" | head -1 | sed 's/missed //')
    COLL=$(echo "$S" | grep -oE "collateral [0-9]+" | head -1 | sed 's/collateral //')
    echo "$MODEL,$BACKEND,$REP,$RC,$DUR,$ITERS,$STOP,$CTX,$H2H_NATIVE,${SC%%/*},${SC##*/},${IC%%/*},${IC##*/},${MC%%/*},${MC##*/},${MISS:-0},${COLL:-0},$(basename "$LOG")" >> "$BULK_CSV"
    echo "[$RUN_TAG] DONE: bulk $MODEL rep $REP $S dur=${DUR}s iters=$ITERS stop=$STOP" >> "$DRIVER_LOG"
    regrade bulk "$REP" "$WT" "$LOG" "python3 -m py_compile py/*.py"
  done
}

echo "=== $RUN_TAG $ARM ($MODEL @ $ARM_URL) cells=$CELLS START @ $(date '+%F %T') ===" >> "$DRIVER_LOG"
IFS=',' read -r -a _CELLS <<< "$CELLS"
for C in "${_CELLS[@]}"; do
  case "$C" in
    debug) run_debug ;;
    bulk)  run_bulk ;;
    *) echo "ABORT: unknown cell '$C'" >&2; exit 1 ;;
  esac
done
echo "=== $RUN_TAG $ARM cells=$CELLS END @ $(date '+%F %T') (missing=$MISSING) ===" >> "$DRIVER_LOG"
[ "$DRY" = "1" ] && exit 0
[ "$MISSING" -eq 0 ] || exit 4
