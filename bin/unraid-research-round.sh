#!/bin/bash
# Research/reasoning round: qwen3:8b, nemotron-3-nano:4b, qwen3.5:9b -- runs after round 2's
# coding bench finishes (one 3080, no concurrent jobs). Research/Q&A bench, not coding.
cd "/Users/user/Desktop/GitHub Projects"
log(){ echo "[research-round $(date '+%m-%d %H:%M:%S')] $*"; }

while pgrep -f "unraid-3080-round2.sh" >/dev/null; do sleep 60; done
log "round 2 coding bench finished, starting research round"

for M in "qwen3:8b" "nemotron-3-nano:4b" "qwen3.5:9b" "nemotron-mini:4b"; do
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
  FITS=$(echo "$PS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ms=d.get('models',[])
print('yes' if ms and ms[0]['size']==ms[0]['size_vram'] else 'no')
")
  curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
  if [ "$FITS" != "yes" ]; then
    log "$M SPILLS -- skipping"
    continue
  fi
  log "$M fits clean, running research bench"
  OUT=".research-bench/results-$(echo $M | tr ':/.' '---').log"
  for QID_Q in \
    "3080-vram|Does the NVIDIA RTX 3080 exist in a 12GB VRAM configuration, or only 10GB? If a 12GB variant exists, when was it released. Use web search to confirm. State your final answer clearly." \
    "dflash|What is DFlash (sometimes written dFlash or DFlash2)? Which lab published it, what conference/venue is it associated with, and is it directly compatible with Ollama for local LLM inference? Use web search. State your final answer clearly." \
    "rivian-session|What is the typical session/token lifetime for Rivian's mobile app API before it requires re-authentication? Use web search to find community documentation. State your final answer clearly." \
    "firewalla-msp|Does Firewalla's MSP (managed service provider) tier require a paid subscription, and roughly what does it cost per year? Use web search. State your final answer clearly."
  do
    QID="${QID_Q%%|*}"; Q="${QID_Q#*|}"
    CWD=".research-bench/$(echo $M | tr ':/.' '---')-$QID"
    mkdir -p "$CWD"
    echo "--- $M / $QID ---" >> "$OUT"
    python3 /Users/user/bin/ollama-worker-v7.py \
      --model "$M" --host "http://198.51.100.145:11434" --cwd "$CWD" \
      --task "$Q" --max-iters 8 --num-ctx 32768 --temperature 0.3 \
      >> "$OUT" 2>&1
  done
  curl -s -m 8 http://198.51.100.145:11434/api/generate -d "{\"model\":\"$M\",\"keep_alive\":0}" >/dev/null 2>&1
  log "$M done"
done
log "RESEARCH ROUND COMPLETE"
