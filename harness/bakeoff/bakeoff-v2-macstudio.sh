#!/bin/bash
# v2 bake-off, Mac Studio (local Ollama). See bakeoff-v2-lib.sh for why v1
# is discarded and what changed.
#
# Per-model settings are DELIBERATE, not prefix-inherited:
#   ctx 131072 + --manual-tools + nudge  -> deepseek-r1 family & MFDoom
#     (the shared distill template has zero tool-call logic, ollama#8517;
#      Mac Studio's 68.7GB unified memory holds the KV cache fine)
#   ctx 32768 + native tool-calling      -> qwen family (real Hermes-style
#      template in tokenizer_config.json)
#
#   qwen3-coder-next:q4_K_M gets TIMEOUT 3600, not 1800. Measured: 52.2GB
#   resident (no spill) but ~7.5 min/iteration, so a 1800s cap allowed only
#   ~4 iterations and both prior runs died at exactly 1800s. This is a
#   throughput ceiling, not a convergence failure -- the iteration count now
#   recorded per run is what distinguishes them.
#
#   deepseek-r1:7b runs at 131072 HERE but 32768 on Unraid. That asymmetry
#   is intentional (the owner's call): the per-backend context ceiling is set by
#   hardware, and the difference is the data point we want for writing
#   backend routing rules.
set -uo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v2-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG MACSTUDIO START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

#          model                                slug                                  ctx     tmo   manual nudge
run_model "deepseek-r1:32b"                     "deepseek-r1-32b"                     131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:32b" "$HOST"

run_model "deepseek-r1:14b"                     "deepseek-r1-14b"                     131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:14b" "$HOST"

run_model "MFDoom/deepseek-r1-tool-calling:14b" "MFDoom-deepseek-r1-tool-calling-14b" 131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "MFDoom/deepseek-r1-tool-calling:14b" "$HOST"

run_model "qwen2.5-coder:14b"                   "qwen2.5-coder-14b"                   32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:14b" "$HOST"

run_model "qwen3-coder-next:q4_K_M"             "qwen3-coder-next-q4_K_M"             32768   3600  no     no   "$BACKEND" "$HOST"
unload_model "qwen3-coder-next:q4_K_M" "$HOST"

run_model "qwen3.8:27b-q8_0"                    "qwen3.8-27b-q8_0"                    32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen3.8:27b-q8_0" "$HOST"

run_model "deepseek-r1:32b-qwen-distill-q8_0"   "deepseek-r1-32b-qwen-distill-q8_0"   131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:32b-qwen-distill-q8_0" "$HOST"

run_model "qwen2.5-coder:7b"                    "qwen2.5-coder-7b-macstudio"          32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:7b" "$HOST"

run_model "deepseek-r1:7b"                      "deepseek-r1-7b-macstudio"            131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:7b" "$HOST"

echo "=== $RUN_TAG MACSTUDIO COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
