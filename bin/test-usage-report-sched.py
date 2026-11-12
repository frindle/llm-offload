#!/usr/bin/env python3
"""test-usage-report-sched.py -- usage_report_sched.py seams (fake clock, fake child). Prints USAGE_SCHED_OK."""
import importlib.util, sys, tempfile, time
from pathlib import Path

BIN = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("urs", str(BIN / "usage_report_sched.py"))
M = importlib.util.module_from_spec(spec); sys.modules["urs"] = M; spec.loader.exec_module(M)
FAILS = []
at = lambda h, d=20: time.mktime((2026, 10, d, h, 0, 0, 0, 0, -1))


def check(n, ok):
    print(("ok   " if ok else "FAIL ") + n)
    if not ok:
        FAILS.append(n)


class P:
    pid = 1
    rc = None
    def poll(self): return self.rc


class IO:
    def __init__(self): self.spawned = 0; self.notes = []; self.killed = 0; self.p = None
    def hour(self, now): return time.localtime(now).tm_hour
    def spawn(self): self.spawned += 1; self.p = P(); return self.p
    def kill(self, p): self.killed += 1
    def notify(self, t, m, k): self.notes.append(t)


td = Path(tempfile.mkdtemp())
io = IO(); s = M.Scheduler(io, td / "s.json", env={})
N = at(4)
check("never run + in window -> start", s.tick(N) == "start" and io.spawned == 1)
check("running while child alive", s.tick(N + 10) == "running")
io.p.rc = 0
s.tick(N + 20)
check("ok recorded", s._state["last_ok"] == N + 20 and s.proc is None)
check("fresh for a week", s.tick(N + 3 * 86400) == "fresh")
D = N + 7 * 86400 + 20
check("due but outside window waits", s.decide(at(12, 27)) == "outside-window")
s.last_check = 0
check("due in window starts", s.tick(D) == "start" and io.spawned == 2)
io.p.rc = 3
s.tick(D + 5)
check("failure notifies", io.notes == ["Weekly usage report FAILED"] and s._state["last_result"] == "failed")
s.last_check = 0
check("retry-wait after failure", s.tick(D + 100) == "retry-wait")
s.last_check = 0
check("retry after an hour", s.tick(D + 3700) == "start")
s.tick(D + 3700 + M.TIMEOUT_S + 301)
check("timeout kills", io.killed == 1 and s._state["last_result"] == "timeout")
check("forced after 8d outside window", M.Scheduler(IO(), td / "s.json", env={}).decide(at(14, 28) + 86400 * 3) == "start")
check("disabled env", M.Scheduler(IO(), td / "x.json", env={"QUEUE_USAGE_REPORT": "0"}).decide(N) == "disabled")
class Boom(IO):
    def hour(self, now): raise RuntimeError("x")
check("tick never raises", M.Scheduler(Boom(), td / "y.json", env={}).tick(N) == "error")
print("USAGE_SCHED_OK" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
