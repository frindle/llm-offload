#!/bin/bash
# Usage: run-set.sh <model> <runner> <tag>  -- enqueue every fixture ONE AT A TIME via the queue,
# waiting for each job to finish before the next (lane may be shared). Results in runs/<tag>/<id>/.
set -uo pipefail
FX=~/bin/review-fixtures
MODEL="$1"; RUNNER="$2"; TAG="$3"
OUT=$FX/runs/$TAG; mkdir -p $OUT
IDS=$(python3 -c "import json;print(' '.join(f['id'] for f in json.load(open('$FX/manifest.json'))['fixtures']))")
for ID in $IDS; do
  D=$OUT/$ID; mkdir -p $D
  [ -f $D/run.json ] && continue
  python3 - "$ID" "$D/task.json" <<'PY'
import json,sys
m=json.load(open("/Users/user/bin/review-fixtures/manifest.json"))
f=next(x for x in m["fixtures"] if x["id"]==sys.argv[1])
json.dump({"mode":"review","diff":"/Users/user/bin/review-fixtures/"+f["diff"],"intent":f["intent"],"context":m["context"]},open(sys.argv[2],"w"))
PY
  # wait while studio-db has anything running/pending
  while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep -E "^\s*\[(running|pending|queued) *\]" | grep -q "host=studio-db"; do sleep 15; done
  J=$(python3 ~/bin/ollama-queue.py enqueue --model "$MODEL" --host studio-db --num-ctx 65536 --cwd $D --task-file $D/task.json \
      --runner "$RUNNER" --bundle gemma-reviewer-bakeoff --allow-no-verify --allow-unisolated --label "grb:$TAG:$ID" 2>&1)
  JID=$(echo "$J" | grep -oE "[0-9a-f]{12}" | head -1)
  echo "$(date +%T) $ID -> $JID"
  [ -z "$JID" ] && { echo "$J" | tail -5; continue; }
  sleep 10
  while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID" | grep -qE "\[(running|pending|queued) *\]"; do sleep 10; done
  python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID" | sed 's/^/   /'
done
echo DONE $TAG
