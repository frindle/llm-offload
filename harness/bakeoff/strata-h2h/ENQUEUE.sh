#!/bin/bash
# STAGED, NOT RUN. The v12 Strata head-to-head's four queue jobs, one bundle (strata-h2h).
# Run this when BOTH lanes are idle: no rt-egift-link-s1 job left on studio-db, and no gate
# running or pinned on unraid (the 3080 is the Unraid pre-gate's GPU). Never --front.
#   python3 ~/bin/ollama-queue.py status | grep -E 'running|pending'
# Each GPU job holds the unraid lane (and so the pre-gate) for up to 4 h:
# 3 reps x <=3600 s + ~4 min model load.
set -euo pipefail
Q="python3 $HOME/bin/ollama-queue.py"
D="/Users/user/Desktop/GitHub Projects/bakeoff/strata-h2h"
VRAM="ssh -o BatchMode=yes claude-sandbox nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"
STOP="ssh -o BatchMode=yes claude-sandbox 'STRATA_RUN_DIR=/tmp/strata-h2h-abort ~/strata/strata-serve.sh stop'"

# qwen3.6 arms (Darkbloom lane)
for c in debug bulk; do
  $Q enqueue --runner "$HOME/bin/bakeoff-runner.py" --model qwen3.6-35b-a3b-vl-mtp-mxfp8 \
    --host studio-db --cwd "$D" --task-file "$D/task-qwen36-$c.txt" --task-kind research \
    --bundle strata-h2h --label "bo-v12-qwen36-$c"
done
# Strata arms (GPU-exclusive on the 3080; the runner evicts Unraid Ollama first)
for c in debug bulk; do
  $Q enqueue-gpu --bundle strata-h2h --host unraid --label "bo-v12-strata-$c" --timeout 14400 \
    --cmd "bash $HOME/bin/strata-h2h-arm.sh $c" --on-abort "$STOP" \
    --vram-check "$VRAM" --vram-max-used-mib 1024 \
    --summary "v12 h2h: Strata IQ2_XS on the 3080, cell $c, 3 reps (reasoning budget 4096)"
done
