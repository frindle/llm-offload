#!/bin/bash
# v6 bake-off, Unraid (RTX 3080 12GB). See bakeoff-v6-macstudio.sh for why v6
# exists (the AGENTS.md / read-a-sibling context fix).
#
# BOTH models at ctx 32768, NOT the 131072 the deepseek family gets on Mac
# Studio. Measured live in v1: deepseek-r1:7b @131072 -> size 20.6GB /
# vram 11.0GB, ~9.6GB spilled to CPU, task burned its full 1800s timeout.
# At 32768 -> 8.2GB, fully resident. ~121KB/token of KV cache here. KV
# cache, not weights, is the binding constraint on this card.
#
# The per-backend context asymmetry is deliberate and is itself the data
# point wanted for backend routing rules -- do not normalise it away.
#
# Runs in parallel with the Mac Studio driver: different machines.
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
