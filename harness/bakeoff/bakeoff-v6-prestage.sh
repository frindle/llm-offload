#!/bin/bash
# Pre-stage the models v6 needs that are NOT yet in Ollama's real store.
#
# WHY THIS RUNS BEFORE LAUNCH (Fable's blocker 3):
# _one_task stamps START_TS and then calls the worker, so ALL model staging is
# charged against that task's 1800s timeout. Copying deepseek-r1:70b (42.5GB)
# or the 32b distill (34.8GB) over SMB inside that window means the first task
# for those models is near-guaranteed to be scored as a timeout that is 100%
# harness staging, not model behaviour -- v1 Bug 3 in a new costume.
#
# Worse, the fallback path had a hard cap: _try_lan_copy runs the copy helper
# under a 600s subprocess timeout, which a ~40GB SMB read will blow, dropping
# through to a fresh registry /api/pull -- downloading from the internet what
# is already on the LAN.
#
# Staging here, outside any timeout, removes both failure modes. Each model is
# copied once into ~/.ollama/models (the store the running server actually
# reads -- OLLAMA_MODELS is unset, verified) and then warmed so the first real
# dispatch starts from a loaded model.
set -uo pipefail

HELPER="/Users/user/bin/copy-ollama-model-from-unraid.py"
LOG="/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22/prestage.log"
HOST="http://localhost:11434"

MODELS=(
  "qwen2.5-coder:7b"                    # 4.7GB
  "deepseek-r1:7b"                      # 4.7GB
  "qwen3-14b-agentic"                   # 9.3GB
  "deepseek-r1:32b-qwen-distill-q8_0"   # 34.8GB
  # deepseek-r1:70b removed 2026-08-22 -- dropped from the v6 matrix
  # (untestable at usable context on a 64GB box), so there is no reason
  # to spend ~10min and 42.5GB staging it.
)

# True if Ollama serves this model, comparing normalized name:tag.
served() {
  local want="$1"; case "$want" in *:*) ;; *) want="$want:latest" ;; esac
  curl -s -m10 "$HOST/api/tags" | python3 -c "
import json,sys
want=sys.argv[1]
names=[m['name'] for m in json.load(sys.stdin).get('models',[])]
names=[n if ':' in n else n+':latest' for n in names]
sys.exit(0 if want in names else 1)" "$want"
}

say() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG"; }

say "=== PRESTAGE START (5 models, ~96GB) ==="
say "disk free before: $(df -k / | awk 'NR==2{printf "%.1fGB", $4/1048576}')"

for m in "${MODELS[@]}"; do
  # Normalize to name:tag before matching -- Ollama lists a tag-less model as
  # "name:latest", so grepping for the bare name plus a closing quote
  # false-negatives every tag-less model. Same :latest bug already fixed in
  # ollama-worker.py and reintroduced here.
  if served "$m"; then
    say "SKIP (already served by Ollama): $m"
    continue
  fi
  say "COPY: $m ..."
  start=$(date +%s)
  if python3 "$HELPER" pull "$m" >> "$LOG" 2>&1; then
    say "COPY OK: $m in $(( $(date +%s) - start ))s"
  else
    say "COPY FAILED: $m -- v6 will fall back to its own staging for this one"
    continue
  fi
  # Confirm the server can actually see it now (blobs+manifest in the right store).
  # Normalize to name:tag before matching -- Ollama lists a tag-less model as
  # "name:latest", so grepping for the bare name plus a closing quote
  # false-negatives every tag-less model. Same :latest bug already fixed in
  # ollama-worker.py and reintroduced here.
  if served "$m"; then
    say "VISIBLE: $m"
  else
    say "WARNING: $m copied but NOT visible in /api/tags -- staging did not take"
  fi
  say "disk free: $(df -k / | awk 'NR==2{printf "%.1fGB", $4/1048576}')"
done

say "=== PRESTAGE COMPLETE ==="
say "models now served: $(curl -s -m10 "$HOST/api/tags" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("models",[])))')"
say "disk free after: $(df -k / | awk 'NR==2{printf "%.1fGB", $4/1048576}')"
