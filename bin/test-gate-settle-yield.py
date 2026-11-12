#!/usr/bin/env python3
"""A committed bundle waiting ONLY on off-lane gate-on-complete hooks yields the GPU lane past
SLICER_GAP_YIELD_S (2026-10-09, rt-bg-commitments-guard). Run: python3 test-gate-settle-yield.py [--bin DIR] -> GATE_SETTLE_YIELD_OK"""
import argparse, importlib.util as u, sys
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sp = u.spec_from_file_location("q_gs", BIN / "ollama-queue.py"); q = u.module_from_spec(sp); sp.loader.exec_module(q)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
NOW = 1_000_000.0
pk = lambda j: j.get("bundle")
q.SLICER_GAP_YIELD_S = 45.0
class H:  # fake live proc
    def poll(self): return None
hooks = {"g1": {"at": NOW - 120, "proc": H(), "bundle": "B"}, "g2": {"at": NOW - 10, "proc": H(), "bundle": "C"}}
sb = q.gate_hooks_settling(NOW, hooks)
chk("settling carries the earliest hook start", getattr(sb["B"], "since", None) == NOW - 120 and list(sb["B"]) == ["g1"])
def st(k, jobs=()):
    return q.bundle_commit_status(k, list(jobs), pk, {}, {}, settling=sb.get(k), now=NOW)
r = st("B")
chk("120s of off-lane gate settling = waiting, not moving, says so", r[0] == "waiting" and r[2] is False and "gate verdict of g1" in r[1] and "off-lane" in r[1])
r = st("C")
chk("a fresh (10s) gate settle keeps the lane, with the new wording", r[0] == "working" and r[2] is True and "waiting for gate verdict of g2" in r[1])
chk("a runnable pending row still holds it", st("B", [{"id": "p", "status": "pending", "bundle": "B"}])[0] == "working")
q.SLICER_GAP_YIELD_S = 0
chk("0 switches the yield off", st("B")[0] == "working")
q.SLICER_GAP_YIELD_S = 45.0
status = {"B": st("B")[:3], "D": ("working", "1 live", True)}
parked = {}
commit, ev = q.bundle_commit_step({"key": "B", "since": NOW - 500, "empty_since": None, "idle_since": None, "holder": "x"},
                                  parked, None, ["D"], lambda k: status[k], NOW)
chk("step: committed bundle parks cpu_wait, other bundle gets the lane", parked.get("B", {}).get("kind") == "cpu_wait" and (commit or {}).get("key") == "D")
status["B"] = ("working", "1 live row(s)", True)   # verdict enqueued a refine row
commit, ev = q.bundle_commit_step(commit, parked, None, ["D"], lambda k: status[k], NOW + 5)
chk("step: verdict's next row resumes B FIRST", (commit or {}).get("key") == "B" and "B" not in parked)
status["B"] = ("complete", "nothing left", False)
parked["B"] = {"since": NOW, "why": "x", "kind": "cpu_wait"}
commit, ev = q.bundle_commit_step(None, parked, None, [], lambda k: status[k], NOW + 9)
chk("step: verdict pass -> parked record unparked, not leaked", "B" not in parked)
n = q.focus_wait_note("B", "complete", True, "", None, holder="waiting for gate verdict of g1 (runs off-lane)")
chk("banner: no bare 'complete'; says what it waits for", "-- complete" not in n["sentence"] and "gate verdict of g1" in n["sentence"])
n = q.focus_wait_note("B", "complete", True, "", None)
chk("banner: bundle with no rows and no holder still explains itself", "no queued rows" in n["sentence"] and "-- complete" not in n["sentence"])
print("GATE_SETTLE_YIELD_OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
