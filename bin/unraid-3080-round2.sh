#!/bin/bash
# Round 2 Unraid f2p coding bench: gemma4:12b-it-qat, sam860/LFM2:8b (community tag,
# real official lfm2.5:8b-a1b does not exist), deepseek-r1:7b (already spillover-clean,
# only ran the research/Q&A bench before, never the actual coding bench).
# One at a time -- single 3080, spillover corrupts results otherwise.
cd "/Users/user/Desktop/GitHub Projects"
log(){ echo "[round2 $(date '+%m-%d %H:%M:%S')] $*"; }

for M in "gemma4:12b-it-qat" "sam860/LFM2:8b" "deepseek-r1:7b"; do
  log "waiting for $M to be pulled..."
  for i in $(seq 1 120); do
    curl -s -m 15 http://198.51.100.145:11434/api/show -d "{\"model\":\"$M\"}" | grep -q model_info && break
    sleep 15
  done
  if ! curl -s -m 15 http://198.51.100.145:11434/api/show -d "{\"model\":\"$M\"}" | grep -q model_info; then
    log "$M NEVER FINISHED PULLING -- skipping"; continue
  fi
  log "=== $M spillover check @32k ==="
  curl -s -m 120 http://198.51.100.145:11434/api/generate \
    -d "{\"model\":\"$M\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_ctx\":32768,\"num_predict\":1},\"keep_alive\":\"3m\"}" >/dev/null 2>&1
  PS=$(curl -s -m 10 http://198.51.100.145:11434/api/ps)
  echo "$PS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for m in d.get('models',[]):
    t=m['size']; v=m['size_vram']
    print(f\"  {m['name']} total={t/1e9:.2f}GB vram={v/1e9:.2f}GB {'FULL GPU' if t==v else 'SPILL '+str(round((t-v)/1e9,2))+'GB'}\")
"
  if echo "$PS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ms=d.get('models',[])
sys.exit(0 if ms and ms[0]['size']==ms[0]['size_vram'] else 1)
"; then
    log "$M fits clean, running f2p bench"
    curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
    OLLAMA_HOST_URL="http://198.51.100.145:11434" \
      bash machine-config/bin/run-f2p-unraid.sh machine-config/benchmarks/tasks-f2p.tsv "$M" \
      || log "$M FAILED (continuing)"
  else
    log "$M SPILLS -- skipping f2p bench (contaminated results otherwise)"
  fi
  curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
  log "$M done"
done
log "ROUND 2 COMPLETE"
