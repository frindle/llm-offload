#!/bin/bash
# Waits for pre-staging to finish, verifies every v6 model is actually served
# by Ollama, then launches both v6 legs. Refuses to launch if staging left a
# model unavailable -- dispatching then would charge a multi-GB SMB copy
# against that task's 1800s timeout and score it as a model timeout.
set -uo pipefail
OUT="/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22"
LOG="$OUT/prestage.log"; DRIVER_LOG="$OUT/driver.log"

while ! grep -aq "PRESTAGE COMPLETE" "$LOG"; do sleep 15; done

MISSING=$(python3 - <<'PY'
import json, urllib.request
want = ["devstral:24b","qwen3.8:27b-q8_0","deepseek-r1:32b","deepseek-r1:14b",
        "MFDoom/deepseek-r1-tool-calling:14b","qwen2.5-coder:14b","qwen3-coder-next:q4_K_M",
        "deepseek-r1:32b-qwen-distill-q8_0","qwen2.5-coder:7b","deepseek-r1:7b","qwen3-14b-agentic"]
have = {m["name"] for m in json.load(urllib.request.urlopen("http://localhost:11434/api/tags", timeout=15))["models"]}
have = {n if ":" in n else n+":latest" for n in have}
norm = lambda s: s if ":" in s else s+":latest"
print(",".join(m for m in want if norm(m) not in have))
PY
)
if [ -n "$MISSING" ]; then
  echo "[v6-launch] ABORT: not served by Ollama after staging: $MISSING" >> "$DRIVER_LOG"
  exit 1
fi
echo "[v6-launch] all 11 macstudio models verified served -- launching" >> "$DRIVER_LOG"

cd "/Users/user/Desktop/GitHub Projects"
nohup ./bakeoff-v6-macstudio.sh > "$OUT/v6-macstudio-stdout.log" 2>&1 &
nohup ./bakeoff-v6-unraid.sh    > "$OUT/v6-unraid-stdout.log"    2>&1 &
echo "[v6-launch] both legs started @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"
