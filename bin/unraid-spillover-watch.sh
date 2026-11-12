#!/bin/bash
# Polls the Unraid 3080's loaded model every 60s. Any gap between size and
# size_vram means CPU spillover -- kill the offending run immediately so it
# stops burning contaminated data (see feedback_no_unraid_gpu_load). Also
# catches a model stuck resident at 0 tok/s (dead load) after 15min.
LOG=~/unraid-spillover-watch.log
while true; do
  R=$(curl -s -m 8 http://198.51.100.145:11434/api/ps 2>/dev/null)
  echo "[$(date '+%m-%d %H:%M:%S')] $R" >> "$LOG"
  echo "$R" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
for m in d.get('models',[]):
    t=m['size']; v=m['size_vram']
    if t!=v:
        print(f\"SPILLOVER: {m['name']} total={t/1e9:.2f}GB vram={v/1e9:.2f}GB spill={(t-v)/1e9:.2f}GB\")
        sys.exit(1)
" >> "$LOG" 2>&1
  if [ $? -eq 1 ]; then
    echo "[$(date '+%m-%d %H:%M:%S')] SPILLOVER DETECTED -- killing run-f2p-unraid.sh chain" >> "$LOG"
    pkill -f "run-f2p-unraid.sh" 2>/dev/null
    pkill -f "ollama-worker-v7.py.*198.51.100.145" 2>/dev/null
    curl -s -m 8 http://198.51.100.145:11434/api/ps | python3 -c "
import sys,json
for m in json.load(sys.stdin).get('models',[]):
    import urllib.request
    urllib.request.urlopen(urllib.request.Request('http://198.51.100.145:11434/api/generate', data=json.dumps({'model':m['name'],'keep_alive':0}).encode(), method='POST'))
" >> "$LOG" 2>&1
  fi
  sleep 60
done
