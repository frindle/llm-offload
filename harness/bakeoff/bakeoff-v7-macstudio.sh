#!/bin/bash
# v7 — DEPTH, not breadth. Signed off by Fable 2026-08-23 after a HOLD whose four
# blockers were cleared and verified firing (context guard, crash-safe transcripts,
# bug 11 on the llama-server path, empty_retries reset + json.loads wrap).
#
# WHAT v7 MEASURES, and why it is different from v1-v6.1:
# every prior round could only say "the model got it wrong". v7 separates the two
# defects that look identical in a CSV:
#   * EXPLORATION failure -- never gathered the evidence (never listed Sources/,
#     never saw components/OrderAttachments.tsx)
#   * TRANSFER failure    -- had the instruction AND the evidence on screen and
#     still applied the wrong pattern
# They have different fixes, and one of them cannot be fixed by prompting.
# A third class, INERT (no meaningful action at all), covers 5 of the 9 v6.1 runs
# that never saw the target -- Fable's correction to my original framing.
#
# n=3 ON 5 MODELS, not 11 models x 1 run. deepseek-r1:32b alone spans
# destructive / inert / inert across three runs of ONE task; a single run cannot
# classify a model. The aggregate split is already stated from v6.1's 22 runs --
# what is missing is per-model confidence.
#
# ROSTER RATIONALE:
#   qwen3.8:27b-q8_0    ctx 65536 -- FABLE'S SIGN-OFF CONDITION. Its photo cell has
#                       NEVER been measured: bug 11 killed it in v6, context
#                       overflow killed it in v6.1 (31,623/32,768 at iteration 11,
#                       truncation dropped the sole user message, template rejected
#                       the list -> HTTP 500). At 32k the new guard would only turn
#                       that crash into a labelled context_ceiling; only MORE
#                       CONTEXT turns it into a measurement.
#   qwen3-14b-agentic   the second confirmed TRANSFER-failure candidate -- it read
#                       Package.swift in full at iteration 4 and misfiled anyway.
#                       This is what falsified "transfer failure is concentrated in
#                       the most capable model".
#   deepseek-r1:32b     the EXPLORATION representative, and the model the agenda
#                       explicitly says needs n>=3 before any statement is safe.
#   qwen2.5-coder:14b   second exploration representative; one of only 3 models
#                       that ever listed Sources/, yet still failed.
#   qwen3-coder-next    the transfer probe: the only model that gathered BOTH kinds
#                       of evidence and still failed both tasks. Runs LAST at
#                       3600s -- ~7.5 min/iteration, it dominates the ETA wherever
#                       it sits, so everything cheaper should land first.
#
# NOT CHANGED, deliberately: sampling, tasks, worktrees, nudges. v7 changes the
# MEASUREMENT, not the treatment. The one exception is the worker itself
# (instrument v7.1 -- context guard + reasoning stripped from re-sent history),
# which is why v7 rows are not directly comparable to v6.1 on context growth.
#
# photo-upload is scored as COMPREHENSION, not as a feature build: the feature
# already exists at baseline and did in every prior round. Score = evidence column
# + whether components/OrderAttachments.tsx was touched. verify_passed is secondary.
set -uo pipefail
export RUN_TAG=v7
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG MACSTUDIO START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

for REP in 1 2 3; do
  echo "=== $RUN_TAG REPEAT $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

  #            model                       slug                        ctx     tmo   manual nudge backend    host     rep
  run_model_v7 "qwen3.8:27b-q8_0"          "qwen3.8-27b-q8_0"          65536   1800  no    no    "$BACKEND" "$HOST" "$REP"
  unload_model "qwen3.8:27b-q8_0" "$HOST"

  run_model_v7 "qwen3-14b-agentic"         "qwen3-14b-agentic"         32768   1800  no    no    "$BACKEND" "$HOST" "$REP"
  unload_model "qwen3-14b-agentic" "$HOST"

  run_model_v7 "deepseek-r1:32b"           "deepseek-r1-32b"           131072  1800  yes   yes   "$BACKEND" "$HOST" "$REP"
  unload_model "deepseek-r1:32b" "$HOST"

  run_model_v7 "qwen2.5-coder:14b"         "qwen2.5-coder-14b"         32768   1800  no    no    "$BACKEND" "$HOST" "$REP"
  unload_model "qwen2.5-coder:14b" "$HOST"

  run_model_v7 "qwen3-coder-next:q4_K_M"   "qwen3-coder-next-q4_K_M"   32768   3600  no    no    "$BACKEND" "$HOST" "$REP"
  unload_model "qwen3-coder-next:q4_K_M" "$HOST"
done

echo "=== $RUN_TAG MACSTUDIO COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
