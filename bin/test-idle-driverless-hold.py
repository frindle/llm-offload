#!/usr/bin/env python3
"""A bundle whose only remaining work is slicer-owned slices with NO job and NO live driver must not hold a GPU lane
past SLICER_GAP_YIELD_S, and an EXHAUSTED focus release is honoured by the commit path (2026-10-10, live
rt-amazon-cardlink: 'RELEASED ... EXHAUSTED' then 'HOLDING ... 2 slice(s) owned by the slicer but IDLE', 4 jobs of
other bundles idle). Run: python3 test-idle-driverless-hold.py [--bin DIR]  -> IDLE_DRIVERLESS_HOLD_OK"""
import argparse, importlib.util as u, sys
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sp = u.spec_from_file_location("q_idh", BIN / "ollama-queue.py"); q = u.module_from_spec(sp); sp.loader.exec_module(q)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
NOW = 1_000_000.0
pk = lambda j: j.get("bundle")
q.SLICER_GAP_YIELD_S = 45.0
plan = {"known": True, "live": [("A", "s2", "pending"), ("A", "s3", "pending")], "plans": {"A": "/x/plan.json"}}
def st(ds=None, exy=None, p=plan, chain=None, jobs=()):
    return q.bundle_commit_status("A", list(jobs), pk, p, chain or {}, now=NOW, driverless_since=ds, exhausted_yield=exy)
chk("fresh idle (10s): still holds (working, not moving)", st(NOW - 10)[0::2] == ("working", False))
r = st(NOW - 60)
chk("idle 60s, no job/driver: waiting, holds no lane", r[0] == "waiting" and r[2] is False and "IDLE" in r[1])
chk("no clock at all: unchanged (working)", st(None)[0] == "working")
chk("EXHAUSTED + idle driverless: waiting at once", st(NOW - 1, "EXHAUSTED x")[0] == "waiting")
chk("a pending runnable row still holds", st(NOW - 600, "EXHAUSTED x", jobs=[{"id": "p", "status": "pending", "bundle": "A"}])[0] == "working")
chk("a live driver is never an exhausted/driverless yield", st(NOW - 600, "EXHAUSTED x", p=dict(plan, driver_live=True))[0] == "working")
cw = {"waiting": True, "why": "chain driver about to advance"}
chk("chain.waiting no driver: working under 45s, waiting past it",
    st(NOW - 10, p={}, chain=cw)[0] == "working" and st(NOW - 60, p={}, chain=cw)[0] == "waiting")
q.SLICER_GAP_YIELD_S = 0
chk("0 switches the gap yield off", st(NOW - 600)[0] == "working")
q.SLICER_GAP_YIELD_S = 45.0
# commit step: A parked cpu_wait, lane goes to B, A stays parked while idle, resumes FIRST when a row appears
status = {"A": st(NOW - 60)[:3], "B": ("working", "1 pending", True)}
parked = {}
commit, ev = q.bundle_commit_step({"key": "A", "since": NOW - 500, "empty_since": None, "idle_since": NOW - 60}, parked,
                                  None, ["B"], lambda k: status[k], NOW)
chk("step: A parked cpu_wait, lane to B", parked.get("A", {}).get("kind") == "cpu_wait" and (commit or {}).get("key") == "B")
commit, ev = q.bundle_commit_step(None, parked, None, ["B"], lambda k: status[k], NOW + 5)
chk("step: idle A does not bounce back", (commit or {}).get("key") == "B" and "A" in parked)
status["A"] = ("working", "1 live row(s)", True)
commit, ev = q.bundle_commit_step(commit, parked, None, ["B"], lambda k: status[k], NOW + 10)
chk("step: A resumes first once its row exists", (commit or {}).get("key") == "A")
# effectful path (_apply_bundle_commit): driverless clock, exhausted yield, parked kick
import tempfile, json
rd = Path(tempfile.mkdtemp())
q.slice_plan_runnability = lambda k, **kw: dict(plan) if k == "A" else {}
q.chain_run_progress = lambda k, **kw: {}
q.chain_final_nonpass = lambda *a, **k: None
kicks = []
state = {"jobs": [{"id": "b1", "status": "pending", "bundle": "B"}], "_bundle_commit": {"key": "A", "since": NOW - 500, "empty_since": None, "idle_since": NOW - 100}}
try:
    k, ev = q._apply_bundle_commit(state, pk, None, ["B"], NOW, kick=lambda l, p: kicks.append(l) or True, alert=lambda *a: None,
                                   runs_dir=rd, chain_dir=rd, hooks={}, cpu_outstanding={}, log_dir=rd, exhausted=lambda x: (x == "A", "3 consecutive non-PASS"))
    chk("apply: EXHAUSTED A with idle slices yields the lane to B at once", k == "B" and state["_bundle_parked"].get("A", {}).get("kind") == "cpu_wait")
    chk("apply: parked A's slicer advance is re-fired", kicks == ["A"])
    k, ev = q._apply_bundle_commit(state, pk, None, ["B"], NOW + 10, kick=lambda l, p: kicks.append(l) or True, alert=lambda *a: None,
                                   runs_dir=rd, chain_dir=rd, hooks={}, cpu_outstanding={}, log_dir=rd, exhausted=lambda x: (x == "A", "x"))
    chk("apply: next tick keeps B, no re-hold, kick rate-limited", k == "B" and kicks == ["A"])
except Exception as e:
    import traceback; traceback.print_exc(); chk("apply path ran: %r" % e, False)
# session-aware (s15): with only idle/driverless slices left the QUEUE is done, but a recently active Claude session
# keeps the held pair; only 30 min of quiet releases it (Gemma back). BLOOM_IDLE_GEMMA=0 -> immediate release.
import os, time
_h = Path(tempfile.mkdtemp())
os.environ.update({"BLOOMCTL_HOME": str(_h), "BLOOM_IDLE_GEMMA": "1", "BLOOM_CONTROL": "1"})
_bc = u.module_from_spec(u.spec_from_file_location("bc_idh", BIN / "bloom_control.py")); _bc.__spec__.loader.exec_module(_bc)
class _Ctl:
    calls = []
    def __getattr__(self, n): return getattr(_bc, n)
    def enabled(self): return True
    def release_to_bloom(self, c): _Ctl.calls.append("release"); return {"ok": True}
_c = _bc.load_cfg(); _bc.write_state(_c, mode="queue", phase="ready", last_switch_at=0)
_c["activity_file"].parent.mkdir(parents=True, exist_ok=True); _c["activity_file"].write_text("")
_t = time.time(); os.utime(_c["activity_file"], (_t - 20, _t - 20))
_r = q.bloom_queue_sync({"jobs": []}, pk, now=_t, ctl=_Ctl(), background=False)
chk("session: queue done but Claude active -> pair kept", _r is None and not _Ctl.calls)
os.utime(_c["activity_file"], (_t - 1900, _t - 1900))
_r = q.bloom_queue_sync({"jobs": []}, pk, now=_t, ctl=_Ctl(), background=False)
chk("session: 30 min quiet -> released", _r == "release")
print("IDLE_DRIVERLESS_HOLD_OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
