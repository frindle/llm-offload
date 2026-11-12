#!/bin/bash
# v2 bake-off, Unraid (RTX 3080 12GB). See bakeoff-v2-lib.sh for why v1 is
# discarded and what changed.
#
# BOTH models run at ctx 32768 here, NOT the 131072 the deepseek family
# gets on Mac Studio. Measured live during v1:
#     deepseek-r1:7b @131072 -> total 20.6GB, vram 11.0GB, ~9.6GB on CPU
#     deepseek-r1:7b @32768  -> total  8.2GB, fully resident
# The model supports 128K; the 3080 does not hold it. KV cache, not
# weights, is the binding constraint (~121KB/token measured) -- the same
# pattern already documented for deepseek-r1:14b and qwen2.5-coder:14b on
# this card.
#
# Runs in parallel with the Mac Studio driver: different machines, no
# contention.
set -uo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v2-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

#          model               slug                          ctx    tmo   manual nudge
run_model "qwen2.5-coder:7b"  "qwen2.5-coder-7b-unraid"     32768  1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:7b" "$HOST"

run_model "deepseek-r1:7b"    "deepseek-r1-7b-unraid"       32768  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:7b" "$HOST"

echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
