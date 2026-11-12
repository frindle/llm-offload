#!/usr/bin/env python3
"""EXHAUSTED focus-release log line is transition-only AND its stamp persists across ticks.

Live 2026-10-09: 'focus: RELEASED bundle strata-h2h -- EXHAUSTED' printed 133x in ~14 days
(~245 EXHAUSTED lines overall) because the dedupe stamp was set on a state that the next
tick reloads from disk without `changed` ever saving it. Simulates N ticks over a JSON
round-trip (the real persistence) and requires exactly one line per release transition.
"""
import importlib.util, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("oq_frl", HERE / "ollama-queue.py")
oq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oq)
fails = []
def check(n, c, d=""):
    if not c: fails.append(f"{n}: {d}")

def tick(disk, pre, act, why):
    st = json.loads(disk["s"])
    msg, chg = oq.focus_release_log_step(st, pre, act, why)
    if chg:                       # only `changed` persists state, exactly like the daemon
        disk["s"] = json.dumps(st)
    return msg

disk = {"s": "{}"}
W = "10 consecutive non-PASS terminal jobs (limit 2)"
lines = [tick(disk, "strata-h2h", "aw", W) for _ in range(20)]
check("20 ticks, same release -> one line", sum(1 for l in lines if l) == 1, str(lines.count(None)))
check("line names bundle+cause", lines[0] and "strata-h2h" in lines[0] and W in lines[0])
check("streak count change same pair stays quiet", tick(disk, "strata-h2h", "aw", "11 consecutive") is None)
check("lane target changes -> new line", tick(disk, "strata-h2h", "rt", W) is not None)
check("release ends -> no line, stamp cleared", tick(disk, "x", "x", "") is None
      and json.loads(disk["s"]).get("_focus_release_logged") is None)
check("re-release after clear logs again", tick(disk, "strata-h2h", "rt", W) is not None)
# the tick must persist the stamp (changed=True) and use the helper
src = (HERE / "ollama-queue.py").read_text()
m = re.search(r"focus_release_log_step\(\s*state, _pre_release, _active, _released_why\)\s*\n\s*if _rel_chg:\s*\n\s*changed = True", src)
check("tick wires helper and sets changed", bool(m))
if fails:
    print("FAIL:\n  " + "\n  ".join(fails)); sys.exit(1)
print("FOCUS_RELEASE_LOG_ONCE_OK")
