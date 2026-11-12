#!/bin/bash
# v8 BASE ROUND — Mac Studio. n=3, six models, three tasks (photo, clamshell,
# plex debug). 17 cells x 3 reps = 51 runs, ~22-26h expected.
#
# The one-line summary of why v8 exists: v7 produced 30 runs and 19 usable rows,
# and the 11 losses were the instrument, not the models. v8 changes the
# instrument. See bakeoff-v8-lib.sh for the per-change rationale.
#
# ROSTER AND CONTEXT
# ------------------
# num_ctx is DERIVED (ctx_for -> min(65536, native)), never passed by hand. The
# native numbers were read live from /api/show on 2026-08-23:
#
#   model                     native    num_ctx   note
#   qwen3.8:27b-q8_0          262144    65536     the only model to produce a `correct`
#                                                 outcome; peaks ~72% of 64k on photo
#   qwen3-14b-agentic          40960    40960     NATIVE-limited. "64k for all" would have
#                                                 run it 24k past its own spec.
#   deepseek-r1:32b           131072    65536     v7 ran this at 131072; v8 lowers it to
#                                                 65536 so every model is measured at the
#                                                 same window where native allows, and the
#                                                 CSV records both numbers either way.
#   qwen2.5-coder:14b          32768    32768     NATIVE-limited. Photo cell RETIRED --
#                                                 ceiling x3 in v7 at 32768, which IS its
#                                                 maximum, so there is no larger window to
#                                                 retry at. Clamshell cell still runs.
#   qwen3-coder-next:q4_K_M   262144    65536     the transfer probe: the only model that
#                                                 gathered BOTH kinds of evidence and still
#                                                 failed both tasks.
#
# WALL CLOCK — SIZED, NOT GUESSED, AND NO LONGER LOAD-BEARING
# -----------------------------------------------------------
# Fable's rule: the wall must never be the binding constraint; max_iters is the
# budget; size the wall from FRESH-HOST iteration rate. v7 rep 1 was the
# freshest host of the round, so its rates are the basis:
#
#   qwen3.8          56 s/iter (photo)      qwen3-14b-agentic   49 s/iter
#   deepseek-r1:32b  89 s/iter (photo)      qwen3-coder-next    77 s/iter (clam)
#   qwen2.5-coder     7 s/iter
#
#   wall = 900s warmup budget + (fresh_rate x 30 iters x 1.8 safety)
#
# The 900s is real, not padding: B3 restarts the server before every run, so
# every run now pays a cold model load, and WARMUP_TIMEOUT_S in the worker is
# 900. Warmup and loop are recorded as SEPARATE columns so that budget can never
# be mistaken for working time.
#
# IMPORTANT: with B3 holding the host fresh, these walls should never be reached.
# A `timed_out=true` row in v8 is an INSTRUMENT DEFECT TO INVESTIGATE, not a
# model outcome -- that is the whole lesson of the 3600s recovery run, which
# timed out at double the v7 budget because the host, not the clock, was the
# constraint.
set -uo pipefail
export RUN_TAG=v10catchup
export ONLY_TASK=debug
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG MACSTUDIO R1 DEBUG-CELL ROUND START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
echo "=== host: total $(python3 "$HOSTTEL" --total)MB, readiness gate is per-model (resident x1.2 + KV) ===" >> "$DRIVER_LOG"

# model|slug|timeout|manual|nudge
ROSTER=(
  # CATCH-UP ROSTER, 2026-08-25. The owner: "all models across all projects are
  # equal in their status" -- these three had ZERO coverage while the six
  # incumbents had debug + clamshell + R3. Fable approved the catch-up and
  # ordered it FIRST, ahead of the v10 bulk cell.
  #
  # MANUAL=no / NUDGE=no for all three: each passed a native tool_call smoke
  # check on this host before being scored (Fable's mandatory per-model gate).
  # gpt-oss additionally cleared the harmony re-confirmation on the Studio's
  # Ollama 0.32.14 -- native tool_calls, content len 0, zero marker leakage.
  # That was an outstanding probe-window item, not an assumption.
  #
  # Timeouts sized against the incumbents by class, not guessed at random:
  # qwen2.5-coder:14b=1800 (14B dense), qwen3-14b-agentic=3600,
  # qwen3-coder:30b=3600 (MoE), qwen3.8:27b=4200.
  "ornith-1.5:9b|ornith-1.5-9b|2400|no|no"
  "ornith-1.5:35b|ornith-1.5-35b|3600|no|no"
  "gpt-oss:20b|gpt-oss-20b|3600|no|no"
)

for REP in 1 2 3; do
  echo "=== $RUN_TAG REPEAT $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

  # Randomised order, seeded by rep. In v7 the five models ran in the same order
  # every rep, so the last slot was always the most degraded host and "rep 3"
  # and "ran last" were the same variable. B3 removes most of that degradation;
  # shuffling removes the rest, and seeding keeps the round reproducible.
  ORDER=$(shuffled_roster "$REP" "${ROSTER[@]}")
  echo "[$RUN_TAG] rep $REP order: $(echo "$ORDER" | cut -d'|' -f1 | tr '\n' ' ')" >> "$DRIVER_LOG"

  while IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG; do
    [ -z "$MODEL" ] && continue
    run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" "$BACKEND" "$HOST" "$REP" "base" </dev/null
    unload_model "$MODEL" "$HOST"
  done <<< "$ORDER"
done

echo "=== $RUN_TAG MACSTUDIO R1 DEBUG-CELL ROUND COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
