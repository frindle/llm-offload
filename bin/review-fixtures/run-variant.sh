#!/bin/bash
# Usage: run-variant.sh <model> <tag> '<json extra task keys>'
# Runs the SCRATCH harness over every fixture as ONE queue job (bakeoff-runner.py,
# the allowlisted driver runner; DRIVER= in its task file), lane studio-db.
set -uo pipefail
FX=~/bin/review-fixtures
S=/private/tmp/claude-501/-Users-user-Desktop-GitHub-Projects/9ed09f36-847e-4f3e-95d6-8fbd14999352/scratchpad/harness
MODEL="$1"; TAG="$2"; EXTRA="${3:-{\}}"
OUT=$FX/runs/$TAG; mkdir -p $OUT
DRV=$OUT/driver.sh
python3 - "$MODEL" "$TAG" "$EXTRA" "$DRV" <<'PY'
import json,sys,os
model,tag,extra,drv=sys.argv[1:]
FX="/Users/user/bin/review-fixtures"; S="/private/tmp/claude-501/-Users-user-Desktop-GitHub-Projects/9ed09f36-847e-4f3e-95d6-8fbd14999352/scratchpad/harness"
m=json.load(open(FX+"/manifest.json")); extra=json.loads(extra)
lines=["#!/bin/bash","set -u"]
for f in m["fixtures"]:
    d=f"{FX}/runs/{tag}/{f['id']}"; os.makedirs(d,exist_ok=True)
    t=dict(mode="review",diff=FX+"/"+f["diff"],intent=f["intent"],context=m["context"],**extra)
    json.dump(t,open(d+"/task.json","w"))
    lines.append(f'[ -f "{d}/run.json" ] || python3 "{S}/code-review-agent.py" --model "{model}" --num-ctx 65536 --cwd "{d}" --task-file "{d}/task.json" > "{d}/agent.log" 2>&1')
lines.append(f'echo VARIANT_DONE {tag}')
open(drv,"w").write("\n".join(lines)+"\n")
PY
printf 'DRIVER=%s\n' "$DRV" > $OUT/bakeoff-task.txt
while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep -E "^\s*\[(running|pending|queued) *\]" | grep -q "host=studio-db"; do sleep 15; done
J=$(python3 ~/bin/ollama-queue.py enqueue --model "$MODEL" --host studio-db --num-ctx 65536 --cwd $OUT --task-file $OUT/bakeoff-task.txt \
    --runner /Users/user/bin/bakeoff-runner.py --bundle "${BUNDLE:-gemma-reviewer-bakeoff}" --allow-no-verify --allow-unisolated --label "grb:$TAG" 2>&1)
JID=$(echo "$J" | grep -oE "[0-9a-f]{12}" | head -1); echo "$(date +%T) $TAG -> ${JID:-FAILED}"; [ -z "$JID" ] && { echo "$J" | tail -5; exit 1; }
sleep 10
while python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID" | grep -qE "\[(running|pending|queued) *\]"; do sleep 10; done
python3 ~/bin/ollama-queue.py status 2>/dev/null | grep "$JID"; echo DONE $TAG
