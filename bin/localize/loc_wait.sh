#!/bin/bash
# wait until no "localize" job is running/pending in the queue
while python3 ~/bin/cpu_lane.py | python3 -I -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(str(r.get("owner","")).startswith("localize") for r in d["running"]+d["pending"]) else 1)'; do sleep 30; done
echo drained
