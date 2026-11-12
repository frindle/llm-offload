#!/bin/bash
# v7c — replication + the middle rung. Unraid only, qwen2.5-coder:7b only.
#
# WHY
# v7b overturned this model's verdict on a SINGLE RUN, and this is the model
# with the worst documented run-to-run variance in the whole bake-off: 5
# correct files on one run and 0 on the next, byte-identical inputs, recorded
# in the guide as the reason the qwen family dropped to temperature 0.2. A
# routing recommendation resting on n=1 from that model is not evidence, it
# is an anecdote that happened to be good.
#
# TWO QUESTIONS, both cheap (~1 min per run on a 3080):
#
# 1. REPLICATION (arms r2/r3/r4, MODE=nosearch). Does "hands it the file ->
#    correct compiling edit" reproduce? If 4/4 land touched_target=yes with a
#    passing build, the supervised-editing verdict is solid. If it is 1/4, the
#    v7b result was a lucky sample and the verdict must be withdrawn.
#
# 2. THE MIDDLE RUNG (arm p1/p2, MODE=pathonly). v7 gave it nothing and it
#    failed; v7b gave it path AND contents and it worked. Naming the file but
#    withholding the contents isolates path CONSTRUCTION from search in
#    general, and answers the practical routing question: must a caller paste
#    the file in, or is naming it enough? Naming-is-enough is a much more
#    useful tool than paste-it-in.
#
# deepseek-r1:7b is deliberately NOT here. It failed v7b -- the easiest form
# of the task, answer visible in the prompt -- by keeping the hover bug,
# sizing the button to 240px, and injecting raw CSS into a Tailwind class
# string. Nothing about that is a sampling accident worth four more minutes.
set -uo pipefail
export RUN_TAG=v7c
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7b-nosearch-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"
M="qwen2.5-coder:7b"
SLUG="qwen2.5-coder-7b-unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# --- replication of the v7b condition (identical task text) ---
for r in r2 r3 r4; do
  MODE=nosearch SUFFIX="-$r" run_model_v7b "$M" "$SLUG" 32768 900 no no "$BACKEND" "$HOST"
done

# --- the middle rung: path named, contents withheld ---
for p in p1 p2; do
  MODE=pathonly SUFFIX="-$p" run_model_v7b "$M" "$SLUG" 32768 900 no no "$BACKEND" "$HOST"
done

unload_model "$M" "$HOST"
echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
