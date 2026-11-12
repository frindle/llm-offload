#!/usr/bin/env python3
"""A live NEXT-STEP 'continue' declaration must not hold the GPU lane past SLICER_GAP_YIELD_S when the bundle
has no GPU row (2026-10-10, rt-costco-receipt-attach: 7 jobs idle 00:14-00:33 behind a 30-min declaration hold).
Run: python3 test-nextstep-gap-yield.py [--bin DIR]  -> NEXTSTEP_GAP_YIELD_OK"""
import argparse, importlib.util as u, sys
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sp = u.spec_from_file_location("q_nsg", BIN / "ollama-queue.py"); q = u.module_from_spec(sp); sp.loader.exec_module(q)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
NOW = 1_000_000.0
pk = lambda j: j.get("bundle")
q.SLICER_GAP_YIELD_S = 45.0
def decl(age): return {"kind": "continue", "at": NOW - age, "expires_at": NOW + 30, "hard_until": NOW + 900, "actor": "auto"}
def plan(since): return {"known": True, "driver_live": True, "driver_since": since, "live": [("B", "s3", "pending")]}
def st(age, p=None, chain=None, jobs=(), key="B"):
    return q.bundle_commit_status(key, list(jobs), pk, p, chain or {}, next_step=decl(age), now=NOW)
# the live 2026-10-10 shape: driver alive since 23:17 (4586s), last GPU row ended `age` s ago
chk("fresh declaration (10s) + slicer driver: holds the lane", st(10, plan(NOW - 4586))[0::2] == ("working", True))
r = st(120, plan(NOW - 4586))
chk("120s declaration + slicer driver, no GPU row: waiting, not moving", r[0] == "waiting" and r[2] is False)
chk("...the gap is measured from the last GPU row, not the driver start (age 20s, driver 4586s -> still holds)",
    st(20, plan(NOW - 4586))[0] == "working")
chk("declaration NEWER than driver start wins; driver older than yield but row ended 10s ago holds",
    st(10, plan(NOW - 100))[0] == "working")
chk("NO slicer plan at all (chain-only bundle): 120s declaration, chain driver live -> waiting",
    st(120, None, {"driver_live": True, "why": "chain driver advancing"})[0] == "waiting")
chk("chain-only, fresh declaration: still holds", st(10, None, {"driver_live": True})[0] == "working")
chk("a running row of the bundle still holds it", st(900, plan(NOW - 4586), jobs=[{"id": "r", "status": "running", "bundle": "B"}])[0] == "working")
chk("a pending runnable row still holds it", st(900, plan(NOW - 4586), jobs=[{"id": "p", "status": "pending", "bundle": "B"}])[0] == "working")
q.SLICER_GAP_YIELD_S = 0
chk("0 switches the yield off (old behaviour)", st(900, plan(NOW - 4586))[0] == "working")
q.SLICER_GAP_YIELD_S = 45.0
# commit step: the yielded bundle is parked cpu_wait, another bundle takes the lane, the yielded one resumes FIRST
status = {"B": st(120, plan(NOW - 4586))[:3], "C": ("working", "1 live", True)}
parked = {}
commit, ev = q.bundle_commit_step({"key": "B", "since": NOW - 500, "empty_since": None, "idle_since": None}, parked,
                                  None, ["C"], lambda k: status[k], NOW)
chk("step: B parked cpu_wait, lane goes to C", parked.get("B", {}).get("kind") == "cpu_wait" and (commit or {}).get("key") == "C")
status["B"] = ("working", "1 live row(s)", True)
commit, ev = q.bundle_commit_step(commit, parked, None, ["C"], lambda k: status[k], NOW + 5)
chk("step: when its next row exists B takes the lane back FIRST", (commit or {}).get("key") == "B" and "B" not in parked)
print("NEXTSTEP_GAP_YIELD_OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
