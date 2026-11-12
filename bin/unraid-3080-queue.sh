#!/bin/bash
# Runs the remaining 3080 candidates one at a time after the in-flight 9b run exits.
# One model at a time on purpose: a single 3080, and two models at once would spill to CPU,
# which would make every duration a measurement of the offload instead of the model.
cd "/Users/user/Desktop/GitHub Projects"
log(){ echo "[queue $(date '+%m-%d %H:%M:%S')] $*"; }
while pgrep -f "run-f2p-unraid.sh" >/dev/null; do sleep 60; done
log "9b run finished, starting queue"
for M in "qwen3.5:9b" "granite4.2:8b" \
         "hf.co/ijohn07/tmax-9b-Q4_K_M-GGUF" "hf.co/pszemraj/rnj-1.5-instruct-GGUF:Q6_K"; do
  # skip anything whose pull never landed
  if ! curl -s -m 15 http://198.51.100.145:11434/api/show -d "{\"model\":\"$M\"}" | grep -q model_info; then
    log "$M NOT PULLED - skipping"; continue
  fi
  log "=== $M ==="
  OLLAMA_HOST_URL="http://198.51.100.145:11434" \
    bash machine-config/bin/run-f2p-unraid.sh machine-config/benchmarks/tasks-f2p.tsv "$M" \
    || log "$M FAILED (continuing)"
  curl -s -m 15 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null
  log "$M done"
done
log "3080 QUEUE COMPLETE"
