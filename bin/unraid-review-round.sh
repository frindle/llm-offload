#!/bin/bash
# Unraid review-bench round: baseline single-pass (parity with Studio's methodology)
# AND the new two-pass verifier technique, same models, so we get a real before/after.
# Priority order per the owner: ornith-1.5:9b (Unraid coding winner), qwen3.5:9b (second),
# qwen3:8b (top research-agent tool-reliability pick). Runs after everything else queued
# on Unraid today clears -- one GPU, no concurrent jobs.
cd "/Users/user/Desktop/GitHub Projects"
log(){ echo "[review-round $(date '+%m-%d %H:%M:%S')] $*"; }

while pgrep -f "unraid-3080-round2.sh|unraid-research-round.sh" >/dev/null; do sleep 60; done
log "prior Unraid queues finished, starting review round"

for M in "ornith-1.5:9b" "qwen3.5:9b" "qwen3:8b"; do
  if ! curl -s -m 15 http://198.51.100.145:11434/api/show -d "{\"model\":\"$M\"}" | grep -q model_info; then
    log "$M not pulled -- skipping"; continue
  fi
  log "=== $M spillover check @32k ==="
  curl -s -m 120 http://198.51.100.145:11434/api/generate \
    -d "{\"model\":\"$M\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_ctx\":32768,\"num_predict\":1},\"keep_alive\":\"3m\"}" >/dev/null 2>&1
  FITS=$(curl -s -m 10 http://198.51.100.145:11434/api/ps | python3 -c "
import sys,json
d=json.load(sys.stdin); ms=d.get('models',[])
print('yes' if ms and ms[0]['size']==ms[0]['size_vram'] else 'no')")
  curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
  if [ "$FITS" != "yes" ]; then log "$M SPILLS -- skipping"; continue; fi

  log "=== $M baseline (pass 1 only) ==="
  OLLAMA_HOST_URL="http://198.51.100.145:11434" bash machine-config/bin/run-review-unraid.sh "$M" \
    || log "$M baseline FAILED (continuing)"

  log "=== $M two-pass verifier ==="
  OLLAMA_HOST_URL="http://198.51.100.145:11434" bash machine-config/bin/run-review-unraid-v2pass.sh "$M" \
    || log "$M v2pass FAILED (continuing)"

  curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
  log "$M done"
done
log "UNRAID REVIEW ROUND COMPLETE"
