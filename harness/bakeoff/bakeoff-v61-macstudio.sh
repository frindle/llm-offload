#!/bin/bash
# v6.1 Mac Studio leg — both tasks, patched worker. See lib header for why v6
# was stopped.
#
# ORDER IS DELIBERATE. qwen3.8:27b-q8_0 runs FIRST: it is the only model that
# has ever completed a task in this bake-off, and it is the model whose
# photo-upload run bug 11 destroyed. Its patched re-run is the single most
# informative result in this leg, so it should arrive in minutes rather than
# hours. qwen3-14b-agentic runs SECOND: native + thinking, never successfully
# measured in any round, and the model most likely to have been silently hit
# by bug 11 had this leg run unpatched.
#
# qwen3-coder-next runs LAST at 3600s x2 -- 52.2GB resident, ~7.5 min per
# iteration, so it dominates the ETA no matter where it sits.
#
# devstral:24b is NOT here: it needs llama-server, not native Ollama (bug 10).
# See bakeoff-v61-devstral-llamaserver.sh.
set -uo pipefail
export RUN_TAG=v61
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v61-clamshell-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG MACSTUDIO START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

#             model                                slug                                  ctx     tmo   manual nudge
run_model_v61 "qwen3.8:27b-q8_0"                   "qwen3.8-27b-q8_0"                    32768   1800  no    no   "$BACKEND" "$HOST"
unload_model  "qwen3.8:27b-q8_0" "$HOST"

run_model_v61 "qwen3-14b-agentic"                  "qwen3-14b-agentic"                   32768   1800  no    no   "$BACKEND" "$HOST"
unload_model  "qwen3-14b-agentic" "$HOST"

run_model_v61 "deepseek-r1:32b"                    "deepseek-r1-32b"                     131072  1800  yes   yes  "$BACKEND" "$HOST"
unload_model  "deepseek-r1:32b" "$HOST"

run_model_v61 "deepseek-r1:14b"                    "deepseek-r1-14b"                     131072  1800  yes   yes  "$BACKEND" "$HOST"
unload_model  "deepseek-r1:14b" "$HOST"

run_model_v61 "MFDoom/deepseek-r1-tool-calling:14b" "MFDoom-deepseek-r1-tool-calling-14b" 131072 1800  yes   yes  "$BACKEND" "$HOST"
unload_model  "MFDoom/deepseek-r1-tool-calling:14b" "$HOST"

run_model_v61 "qwen2.5-coder:14b"                  "qwen2.5-coder-14b"                   32768   1800  no    no   "$BACKEND" "$HOST"
unload_model  "qwen2.5-coder:14b" "$HOST"

run_model_v61 "deepseek-r1:32b-qwen-distill-q8_0"  "deepseek-r1-32b-qwen-distill-q8_0"   131072  1800  yes   yes  "$BACKEND" "$HOST"
unload_model  "deepseek-r1:32b-qwen-distill-q8_0" "$HOST"

run_model_v61 "qwen2.5-coder:7b"                   "qwen2.5-coder-7b-macstudio"          32768   1800  no    no   "$BACKEND" "$HOST"
unload_model  "qwen2.5-coder:7b" "$HOST"

run_model_v61 "deepseek-r1:7b"                     "deepseek-r1-7b-macstudio"            131072  1800  yes   yes  "$BACKEND" "$HOST"
unload_model  "deepseek-r1:7b" "$HOST"

run_model_v61 "qwen3-coder-next:q4_K_M"            "qwen3-coder-next-q4_K_M"             32768   3600  no    no   "$BACKEND" "$HOST"
unload_model  "qwen3-coder-next:q4_K_M" "$HOST"

echo "=== $RUN_TAG MACSTUDIO COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
