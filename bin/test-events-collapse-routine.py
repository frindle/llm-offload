#!/usr/bin/env python3
"""Routine 'done (no launcher)' events collapse to one count line; passed/failed/needs-review/needs_info never drop."""
import importlib.util as u, os, sys, tempfile
from pathlib import Path
d = tempfile.mkdtemp()
os.environ["QED_DIR"] = d; os.environ["QEL_DIR"] = d; os.environ["HOME"] = d
sp = u.spec_from_file_location("qed", Path(__file__).resolve().parent / "queue-events-deliver.py")
m = u.module_from_spec(sp); sp.loader.exec_module(m)
m.P = lambda n: os.path.join(d, n)
os.makedirs(os.path.join(d, "queue-events.sessions"), exist_ok=True)
ev = []
def add(state, label, i):
    ev.append({"seq": len(ev) + 1, "id": f"{i:012x}", "label": label, "state": state})
for i in range(5): add("done", "lane-bakeoff-judge", i)
for i in range(5, 8): add("done", f"bakeoff-arm{i}", i)
add("done", "error-digest", 8)
add("done", "real-work-change", 9)         # a non-routine done stays
add("passed", "bakeoff-x", 10)              # passed never collapses
add("failed", "lane-bakeoff-judge", 11)
add("needs-review", "error-digest", 12)
add("needs_info", "bo-thing", 13)
m.classify = lambda e, me, bridge, alive: "unstamped"
m.auto_packet = lambda e: None
m.review_hint = lambda e: None
lines, _, _ = m.deliver(ev, "s1", "sock", None)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
chk("one count line", sum(1 for l in lines if "routine done" in l) == 1)
cl = [l for l in lines if "routine done" in l][0]
chk("count is 9", cl.startswith("9 routine done"))
chk("non-routine done kept", any("real-work-change" in l for l in lines))
for st in ("passed", "failed", "needs-review", "needs_info"):
    chk(f"{st} kept", any(l.split(" ")[1] == st for l in lines))
chk("total lines = 1 + 5", len(lines) == 6)
print("FAIL: " + ", ".join(fails) if fails else "COLLAPSE_OK")
sys.exit(1 if fails else 0)
