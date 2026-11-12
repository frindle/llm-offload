#!/bin/bash
# Usage: run-bench-variant.sh <model> <tag> <agent.py path> '<json extra task keys>'
# Runs the owner's code-review-bench (all tasks) through <agent.py> as ONE queue job
# (bakeoff-runner.py DRIVER=), lane studio-db, bundle gemma-reviewer-bakeoff2.
set -uo pipefail
FX=~/bin/review-fixtures
MODEL="$1"; TAG="$2"; AGENT="$3"; EXTRA="${4:-{\}}"
OUT=$FX/bench-runs/$TAG; mkdir -p $OUT
DRV=$OUT/driver.sh
python3 - "$MODEL" "$TAG" "$AGENT" "$EXTRA" "$DRV" "$OUT" <<'PY'
import json,sys,os
model,tag,agent,extra,drv,out=sys.argv[1:]
B="/Users/user/Desktop/GitHub Projects/code-review-bench"
spec=json.load(open(B+"/tasks.json")); extra=json.loads(extra)
lines=["#!/bin/bash","set -u"]
for t in spec["tasks"]:
    d=f"{out}/{t['id']}"; os.makedirs(d,exist_ok=True)
    json.dump(dict(mode="review",diff=f"{B}/fixtures/{t['id']}.diff",intent=spec["intent_claim"],context=spec["context"],**extra),open(d+"/task.json","w"))
    lines.append(f'[ -f "{d}/run.json" ] || python3 "{agent}" --model "{model}" --num-ctx 65536 --cwd "{d}" --task-file "{d}/task.json" > "{d}/agent.log" 2>&1')
lines.append(f"echo VARIANT_DONE {tag}")
open(drv,"w").write("\n".join(lines)+"\n")
PY
printf 'DRIVER=%s\n' "$DRV" > $OUT/bakeoff-task.txt
while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep -E "^\s*\[(running|pending|queued) *\]" | grep -q "host=studio-db"; do sleep 15; done
J=$(python3 ~/bin/ollama-queue.py enqueue --model "$MODEL" --host studio-db --num-ctx 65536 --cwd $OUT --task-file $OUT/bakeoff-task.txt \
    --runner /Users/user/bin/bakeoff-runner.py --bundle gemma-reviewer-bakeoff2 --allow-no-verify --allow-unisolated --label "grb2:$TAG" 2>&1)
JID=$(echo "$J" | grep -oE "[0-9a-f]{12}" | head -1); echo "$(date +%T) $TAG -> ${JID:-FAILED}"; [ -z "$JID" ] && { echo "$J" | tail -5; exit 1; }
sleep 10
while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID" | grep -qE "\[(running|pending|queued) *\]"; do sleep 10; done
python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID"; echo DONE $TAG
