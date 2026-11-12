#!/bin/bash
# Bonsai-2-27B PTQ1_0 (1.75 bpw ternary) on the v10 cell-E bulk-codemod fixture,
# through PrismML's PATCHED llama.cpp llama-server -- NOT ollama. Stock ollama
# cannot load ggml tensor type 143 at all (`tensor "output.weight" size overflow`),
# so a row against :11434 would be a fabrication and is hard-aborted below.
#
# SIBLING of bakeoff-v10-bulk-macstudio.sh, not a patch: same fixture, same scorer,
# same 65536 window (ctx_for() clamps to min(native, 65536) for every cell-E arm),
# but its OWN separately-labelled CSV. Bonsai publishes no Q4_K_M, so it can never
# be a quant-matched roster arm -- it is a standalone ternary datapoint and stays
# labelled as one (macstudio-llamaserver-ternary).
set -uo pipefail
export RUN_TAG=v10-bonsai-ternary
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

BASE_DIR="/Users/user/Desktop/GitHub Projects/bakeoff"  # absolute: the fixture tree is UNTRACKED, so it exists only in the main checkout
SCORER="$BASE_DIR/bakeoff-v10-bulk-score.py"
TASKGEN="$BASE_DIR/bakeoff-v10-bulk-task.py"

MODEL_LABEL="bonsai-2-27b-ternary-PTQ1_0"
BONSAI_BACKEND="${BONSAI_BACKEND:-macstudio-llamaserver-ternary}"
NUM_CTX=65536        # matches every cell-E arm; that is what makes the row comparable
NATIVE_CTX=262144    # the GGUF's own qwen35.context_length, HARDCODED on purpose: llama-server does not answer ollama's /api/show, so it cannot be read from the running server

# Host-parameterizable endpoint: default is the Mac Studio, but BONSAI_ENDPOINT +
# BONSAI_PORT (+ BONSAI_SKIP_LAUNCH=1) point this at a llama-server on another host.
HOST_URL="${BONSAI_ENDPOINT:-http://127.0.0.1}:${BONSAI_PORT:-8092}"

BONSAI_LAUNCHER="${BONSAI_LAUNCHER:-/Users/user/bin/start-llama-server-bonsai.sh}"
BONSAI_WORKER="${BONSAI_WORKER:-$WORKER}"
BONSAI_CSV="${BONSAI_CSV:-$OUTDIR/results-v10-bulk-bonsai-ternary.csv}"
BONSAI_STAGE_BASE="${BONSAI_STAGE_BASE:-$WT_BASE/bulk-codemod-v10-bonsai}"
BONSAI_REPS="${BONSAI_REPS:-3}"
BONSAI_TIMEOUT="${BONSAI_TIMEOUT:-5400}"
BONSAI_PIDFILE="${BONSAI_PIDFILE:-/tmp/llama-server-bonsai.pid}"
BONSAI_HEALTH_CMD="${BONSAI_HEALTH_CMD:-curl -sf -m 3 "$HOST_URL/health"}"
BONSAI_SKIP_LAUNCH="${BONSAI_SKIP_LAUNCH:-0}"

# Stop the llama-server we launched: kill the pid in $BONSAI_PIDFILE if alive.
stop_bonsai_server() {
  [ "$BONSAI_SKIP_LAUNCH" = "1" ] && return 0
  local pid; pid="$(cat "$BONSAI_PIDFILE" 2>/dev/null)" || true
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
}

# --- RESUME (2026-09-18) ----------------------------------------------------
# WHY: this driver is ONE long Studio-lane queue job, and the queue can end it
# at any moment for reasons that have nothing to do with the model -- a daemon
# restart (29c99f632d64 died mid-rep2 on `received signal 15, shutting down`) or
# a gate/lane preemption SIGTERM (a3ada445c578 died 13s into its first rep,
# exit 241 = -SIGTERM & 0xff). Without resume, every re-queue restarted at rep 1:
# it redid 32 minutes of already-measured work, OVERWROTE rep 1's transcript log,
# and never reached reps 2-3. The CSV is the run's own durable state, so use it.
#
# A rep counts as DONE only if it has a real measurement row. ABORT_STAGE rows
# (staging failure) and rows this driver never wrote are not measurements, so
# they are retried. Field 1/2/3 are model/backend/rep and field 4 is exit_code --
# none of them can contain a comma, so a plain awk split is safe here.
already_done() {
  local rep="$1"
  [ -f "$BONSAI_CSV" ] || return 1
  awk -F, -v m="$MODEL_LABEL" -v b="$BONSAI_BACKEND" -v r="$rep" \
      '$1==m && $2==b && $3==r && $4!="ABORT_STAGE" { found=1 }
       END { exit found ? 0 : 1 }' "$BONSAI_CSV"
}

# Count of reps 1..BONSAI_REPS that still need to run.
reps_remaining() {
  local n=0 rep
  for rep in $(seq 1 "$BONSAI_REPS"); do
    already_done "$rep" || n=$((n + 1))
  done
  echo "$n"
}

# The server is started ONCE for the whole run, but the run outlives its own
# process here: a preemption, an OOM, or the queue's memory eviction can take the
# llama-server out from under rep 2 while rep 1's row is already banked. Re-check
# before every rep and relaunch if it is gone, so a later rep never benches
# against a dead endpoint and reports that as a model result.
ensure_bonsai_server_up() {
  ${BONSAI_HEALTH_CMD} >/dev/null 2>&1 && return 0
  if [ "$BONSAI_SKIP_LAUNCH" = "1" ]; then
    echo "[$RUN_TAG] llama-server at $HOST_URL is DOWN and BONSAI_SKIP_LAUNCH=1 -- cannot relaunch" >> "$DRIVER_LOG"
    return 1
  fi
  echo "[$RUN_TAG] llama-server at $HOST_URL is DOWN -- relaunching before the next rep" >> "$DRIVER_LOG"
  export BONSAI_PORT="${BONSAI_PORT:-8092}"
  export BONSAI_CTX="$NUM_CTX"
  "$BONSAI_LAUNCHER" || return 1
  ${BONSAI_HEALTH_CMD} >/dev/null 2>&1
}

# A SIGTERM to this driver (queue preemption / daemon shutdown) must stop the
# llama-server and leave the CSV exactly as it is -- the next re-queue resumes at
# the first rep with no row. Without this the EXIT trap alone still fired, but the
# run looked like a model failure instead of an interrupted one.
on_terminate() {
  echo "[$RUN_TAG] INTERRUPTED by signal -- stopping llama-server; re-queue resumes at the first rep with no CSV row" >> "$DRIVER_LOG"
  stop_bonsai_server
  exit 143
}
trap 'on_terminate' TERM INT

# --- HARD ABORTS (each is a separate verify case) ---------------------------
case "$HOST_URL" in
  *:11434*) echo "ABORT: endpoint $HOST_URL points at ollama :11434 -- stock ollama cannot load PTQ1_0 ternary weights; this row would be a fabrication" >&2; exit 1 ;;
esac

CSV_BASE="$(basename "$BONSAI_CSV")"
case "$CSV_BASE" in
  results-v10-bulk-cellE.csv|results-v9.csv|results-v9-r3.csv|results-v8.csv|results-v10catchup.csv|results-v10parity.csv)
    echo "ABORT: $BONSAI_CSV is a pinned/frozen comparability CSV -- bonsai writes to its own results-v10-bulk-bonsai-ternary.csv" >&2; exit 1 ;;
esac
if [ "$BONSAI_CSV" = "$RESULTS_CSV" ]; then
  echo "ABORT: BONSAI_CSV collides with the lib's results file ($RESULTS_CSV)" >&2
  exit 1
fi

# Re-run calibration at dispatch time. An oracle that passed an hour ago is not
# evidence about the oracle that is about to grade this row.
if ! python3 "$SCORER" --calibrate; then
  echo "ABORT: calibration FAILED -- bonsai ternary must not dispatch (prereg §5)" >&2
  exit 1
fi

[ -f "$BONSAI_CSV" ] || echo "model,backend,rep,exit_code,duration_s,iterations,stop_reason,num_ctx,native_ctx,sites_correct,sites_total,ident_correct,ident_total,meta_correct,meta_total,sites_missed,collateral_files,transcript" > "$BONSAI_CSV"

TASK=$(python3 "$TASKGEN") || { echo "ABORT: task generation failed" >&2; exit 1; }
[ ${#TASK} -gt 3000 ] || { echo "ABORT: task text implausibly short (${#TASK} chars)" >&2; exit 1; }

echo "=== $RUN_TAG BONSAI TERNARY START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "[$RUN_TAG] results -> $BONSAI_CSV (endpoint $HOST_URL)" >> "$DRIVER_LOG"

# Nothing left to measure -> do NOT load 27B of ternary weights onto the GPU for
# a run that would skip every rep. Checked BEFORE the launch, not inside the loop.
REMAINING="$(reps_remaining)"
echo "[$RUN_TAG] reps: $REMAINING of $BONSAI_REPS still to run (resume reads $BONSAI_CSV)" >> "$DRIVER_LOG"
if [ "$REMAINING" -eq 0 ]; then
  echo "[$RUN_TAG] all $BONSAI_REPS reps already have rows in $BONSAI_CSV -- nothing to do" >> "$DRIVER_LOG"
  echo "bonsai ternary: all $BONSAI_REPS reps already recorded in $BONSAI_CSV -- no server launched"
  exit 0
fi

# Launch the patched llama-server unless a remote endpoint is already serving.
if [ "$BONSAI_SKIP_LAUNCH" != "1" ]; then
  export BONSAI_PORT="${BONSAI_PORT:-8092}"
  # The server's --ctx-size MUST be at least the worker's --num-ctx; the launcher's
  # own default is only 32768, which would silently truncate a 65536 run.
  export BONSAI_CTX="$NUM_CTX"
  if ! "$BONSAI_LAUNCHER"; then
    echo "ABORT: $BONSAI_LAUNCHER failed -- no llama-server for bonsai ternary" >&2
    exit 1
  fi
  # Stop the server even on an error path (health failure, worker crash, ...).
  trap 'stop_bonsai_server' EXIT
fi

if ! ${BONSAI_HEALTH_CMD}; then
  echo "ABORT: llama-server health check failed at $HOST_URL" >&2
  stop_bonsai_server
  exit 1
fi

# CONTEXT PRE-FLIGHT (2026-09-19): /health only proves the server answers, NOT
# that it was launched with a window big enough for this run. The Unraid arm
# (BONSAI_SKIP_LAUNCH=1, so the launcher's `export BONSAI_CTX=$NUM_CTX` never
# ran) served a llama-server started with --ctx-size 16384 while the worker was
# driven at --num-ctx 65536. The worker's own "context: N/65536" display reads
# the CLIENT number, so nothing looked wrong until the prompt crossed 16384 mid
# conversation and llama.cpp answered HTTP 400 exceed_context_size_error. All 3
# reps died that way and wrote fabricated 0/49 rows. llama.cpp exposes its REAL
# window at /props (default_generation_settings.n_ctx), so check it before any
# GPU time is spent. Unreadable /props is only a warning -- never a hard abort
# on an introspection endpoint that a future build might drop.
SERVER_CTX="$(python3 - "$HOST_URL" <<'PROBE' 2>/dev/null
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/props", timeout=10) as r:
        print(int(json.load(r)["default_generation_settings"]["n_ctx"]))
except Exception:
    pass
PROBE
)"
if [ -z "${SERVER_CTX:-}" ]; then
  echo "[$RUN_TAG] WARN: could not read n_ctx from $HOST_URL/props -- context pre-flight skipped" >> "$DRIVER_LOG"
elif [ "$SERVER_CTX" -lt "$NUM_CTX" ]; then
  echo "[$RUN_TAG] ABORT: llama-server at $HOST_URL was launched with --ctx-size $SERVER_CTX but this run needs $NUM_CTX -- relaunch it with --ctx-size $NUM_CTX. No rows written." >> "$DRIVER_LOG"
  echo "ABORT: $HOST_URL n_ctx=$SERVER_CTX < required num_ctx=$NUM_CTX (relaunch llama-server with --ctx-size $NUM_CTX)" >&2
  stop_bonsai_server
  exit 1
fi
echo "[$RUN_TAG] context pre-flight OK: server n_ctx=${SERVER_CTX:-unknown} >= num_ctx=$NUM_CTX" >> "$DRIVER_LOG"

for REP in $(seq 1 "$BONSAI_REPS"); do
  WT="$BONSAI_STAGE_BASE/rep$REP"
  # $BONSAI_BACKEND is part of the filename, not just $RUN_TAG: two backend arms
  # (macstudio-llamaserver-ternary vs unraid-llamaserver-ternary) share this one
  # driver and the same $RUN_TAG, so a backend-less name made rep N of one arm
  # overwrite rep N of the other -- that is how the Studio arm's original 49/49
  # rep-1 transcript was destroyed by the Unraid run on 2026-09-19.
  # Safe for resume: already_done() matches (model, backend, rep) in the CSV and
  # never reads the log filename; the CSV's `transcript` column only records
  # basename "$LOG", so pre-existing rows keep pointing at their historical names.
  LOG="$OUTDIR/bonsai-2-27b-ternary-bulk-codemod-$RUN_TAG-$BONSAI_BACKEND-r${REP}.log"

  # RESUME: this rep already has a measurement row -- leave it and its transcript
  # log untouched. Rep 1's row (converged, 49/49) is a pinned comparability
  # artifact; a re-queue must never re-run it or overwrite its log.
  if already_done "$REP"; then
    echo "[$RUN_TAG] SKIP: $MODEL_LABEL rep $REP already recorded in $BONSAI_CSV" >> "$DRIVER_LOG"
    continue
  fi

  # The endpoint must be alive for THIS rep, not just at driver start.
  if ! ensure_bonsai_server_up; then
    echo "[$RUN_TAG] ABORT: llama-server unavailable for rep $REP -- no row written; re-queue resumes here" >> "$DRIVER_LOG"
    break
  fi

  # Fresh tree every rep. Answer key absent by construction (py/ and ts/ only).
  if ! python3 "$SCORER" --stage "$WT" >/dev/null; then
    echo "[$RUN_TAG] ABORT: staging failed for rep $REP" >> "$DRIVER_LOG"
    echo "$MODEL_LABEL,$BONSAI_BACKEND,$REP,ABORT_STAGE,0,0,ABORT_STAGE,$NUM_CTX,$NATIVE_CTX,0,49,0,26,0,23,49,0,none" >> "$BONSAI_CSV"
    continue
  fi

  echo "[$RUN_TAG] START: $MODEL_LABEL rep $REP ctx=$NUM_CTX/$NATIVE_CTX timeout=${BONSAI_TIMEOUT}s" >> "$DRIVER_LOG"
  T0=$(date +%s)
  # --api openai is mandatory: llama-server speaks /v1/chat/completions, not ollama's /api/chat.
  # --preserve-reasoning is mandatory: Bonsai is a THINKING model whose output arrives in
  # reasoning_content with content empty -- without it the run reads as null output and
  # converges having done nothing. Sampling matches the cell-E qwen* case (GGUF arch qwen35).
  python3 "$BONSAI_WORKER" \
    --model bonsai-2-27b-ternary --host "$HOST_URL" --api openai \
    --cwd "$WT" --task "$TASK" --verify "python3 -m py_compile py/*.py" \
    --max-iters 30 --num-ctx "$NUM_CTX" \
    --temperature 0.2 --top-p 0.95 --top-k 20 \
    --preserve-reasoning > "$LOG" 2>&1 &
  WPID=$!
  ( sleep "$BONSAI_TIMEOUT" && kill -TERM "$WPID" 2>/dev/null ) & WATCH=$!
  wait "$WPID" 2>/dev/null; RC=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null
  DUR=$(( $(date +%s) - T0 ))
  # grep -c PRINTS 0 and ALSO exits 1 on no match -- `|| echo 0` would make ITERS the
  # two-line string "0\n0" and split the CSV row. Plain assignment + default instead.
  ITERS=$(grep -ac "iteration " "$LOG" 2>/dev/null); ITERS=${ITERS:-0}

  TIMED_OUT=false; [ "$DUR" -ge "$BONSAI_TIMEOUT" ] && TIMED_OUT=true

  # INFRASTRUCTURE ABORT, not a model result. Two shapes, both observed live:
  #   * the worker was killed by a signal that was NOT our timeout watchdog --
  #     the queue preempted this job or the daemon shut down (RC>=128);
  #   * the worker exited non-zero and the llama-server is now gone -- the server
  #     was killed mid-generation, so the model never got to finish.
  # Writing a 0/49 row for either would fabricate a bad measurement AND, with
  # already_done, permanently freeze that fabrication into the CSV. Write nothing,
  # stop the loop, and let the next re-queue resume at this rep.
  if { [ "$TIMED_OUT" != "true" ] && [ "$RC" -ge 128 ]; } \
     || { [ "$RC" -ne 0 ] && ! ${BONSAI_HEALTH_CMD} >/dev/null 2>&1; }; then
    echo "[$RUN_TAG] ABORT: rep $REP ended by infrastructure (rc=$RC dur=${DUR}s, server_up=$(${BONSAI_HEALTH_CMD} >/dev/null 2>&1 && echo yes || echo no)) -- NO row written; re-queue resumes at rep $REP" >> "$DRIVER_LOG"
    break
  fi

  if [ "$RC" -eq 3 ] || grep -aq "LOAD FAILED" "$LOG" 2>/dev/null; then
    STOP="load_failed"
  elif grep -aq "CONTEXT CEILING" "$LOG" 2>/dev/null; then
    if [ "$NUM_CTX" -ge "$NATIVE_CTX" ]; then STOP="native_ceiling"; else STOP="config_ceiling"; fi
  elif [ "$TIMED_OUT" = "true" ]; then
    STOP="timeout"
  elif [ "$RC" -eq 0 ]; then
    STOP="converged"
  elif [ "$RC" -eq 2 ]; then
    STOP="iter_cap"
  else
    STOP="none"
  fi

  # Score OUT of the model's reach, after the run -- same parse as the cell-E driver.
  S=$(python3 "$SCORER" --score "$WT" 2>/dev/null)
  SC=$(echo "$S" | grep -oE "sites [0-9]+/[0-9]+" | head -1 | tr -d 'sites ')
  IC=$(echo "$S" | grep -oE "ident [0-9]+/[0-9]+" | head -1 | sed 's/ident //')
  MC=$(echo "$S" | grep -oE "meta [0-9]+/[0-9]+" | head -1 | sed 's/meta //')
  MISS=$(echo "$S" | grep -oE "missed [0-9]+" | head -1 | sed 's/missed //')
  COLL=$(echo "$S" | grep -oE "collateral [0-9]+" | head -1 | sed 's/collateral //')

  echo "$MODEL_LABEL,$BONSAI_BACKEND,$REP,$RC,$DUR,$ITERS,$STOP,$NUM_CTX,$NATIVE_CTX,${SC%%/*},${SC##*/},${IC%%/*},${IC##*/},${MC%%/*},${MC##*/},${MISS:-0},${COLL:-0},$(basename "$LOG")" >> "$BONSAI_CSV"
  echo "[$RUN_TAG] DONE: $MODEL_LABEL rep $REP $S dur=${DUR}s iters=$ITERS stop=$STOP" >> "$DRIVER_LOG"
done

STILL="$(reps_remaining)"
echo "=== $RUN_TAG BONSAI TERNARY COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ($STILL of $BONSAI_REPS rep(s) still missing) ===" >> "$DRIVER_LOG"
echo "bonsai ternary rows: $BONSAI_CSV ($((BONSAI_REPS - STILL))/$BONSAI_REPS reps recorded)"
# A partial run is a partial run: exit non-zero so the queue does not record an
# interrupted bake-off as a clean completion. Re-queue to resume at the next rep.
[ "$STILL" -eq 0 ] || exit 4
