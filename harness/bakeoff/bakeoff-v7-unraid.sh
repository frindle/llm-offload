#!/bin/bash
# v7 Unraid leg — NOT launched automatically.
#
# Both models here are expected to fail. They are run anyway, for two reasons,
# and neither is "maybe they'll surprise us":
#
# 1. NEGATIVE CONTROL FOR THE NEW GRADER. v7 introduces touched_target and
#    has_capture as objective pass columns. They have never been exercised.
#    Two models that provably cannot do this task must score no/no; if either
#    registers yes, the grader is producing false positives and every v7 row
#    is suspect. Better to learn that from models whose failure is not in
#    question.
#
# 2. IT IS FREE. This leg runs in parallel with the Mac Studio leg, which is
#    hours longer. v6's entire Unraid leg (2 models x 2 tasks) took 24
#    minutes; v7 is one task, so ~15. It does not extend the run.
#
# Standing caution that applies here specifically: four negative 7B verdicts
# were overturned today once the harness was fixed, and a fifth was caught
# before launch. v7 is the first task with an objective pass signal, so it is
# also the first genuinely fair read either of these models has had. Score
# from the transcript regardless of what the columns say.
set -uo pipefail
export RUN_TAG=v7
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# ctx 32768, not 131072: at 131072 this card spills ~9.6GB to CPU
# (size=20.6GB vs size_vram=11.0GB on /api/ps) and burns the full timeout.
# KV cache, not weights, is the binding constraint on a 3080 12GB.
run_model_v7 "qwen2.5-coder:7b" "qwen2.5-coder-7b-unraid" 32768 1800 no  no  "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:7b" "$HOST"

run_model_v7 "deepseek-r1:7b"   "deepseek-r1-7b-unraid"   32768 1800 yes yes "$BACKEND" "$HOST"
unload_model "deepseek-r1:7b" "$HOST"

echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
