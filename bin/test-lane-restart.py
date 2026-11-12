#!/usr/bin/env python3
"""Regression test: Darkbloom provider restarts must not strand jobs (2026-10-01).

Live sequence (watchdog relaunching a bad 0.9.15 three times, 16:23/16:29/16:35,
~6 min down each): in-flight requests got HTTP 500 with an empty body, then
connection refused; the worker burned CHAT_RETRIES in seconds and paused
e10334c98f27 / 55417cfbce11 with pause_reason=chat_request_failed; they sat until
resumed by hand.

Asserts BEHAVIOUR, against REPEATED ~6-minute outage windows (not one blip):
  worker  * a 5xx/conn error on the Darkbloom lane waits on /health (bounded
            backoff) and the retry after recovery is NOT charged -> a request that
            spans 3 outage windows still succeeds;
          * a 5xx while /health stays OK is charged normally (a real server error
            still fails after CHAT_RETRIES);
          * an outage longer than LANE_RESTART_WAIT_S gives up (bounded);
          * a non-Darkbloom host is untouched.
  queue   * a chat_request_failed pause on the Darkbloom lane resumes as soon as
            /health is OK (not before), at most LANE_FAIL_RESUME_MAX times, then
            surfaces; across 6 outage cycles every one is auto-resumed;
          * each resume is logged with its cause, and the cause stays on the row;
          * the stranded-pause watchdog leaves those rows to it.
Run: python3 test-lane-restart.py [--revert-check]
"""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
FAILS = []
DB = "http://127.0.0.1:8000"


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Sim:
    """A fake clock + a /health that is DOWN inside the given windows."""
    def __init__(self, windows):
        self.t = 0.0
        self.windows = windows      # [(start, end)]

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def healthy(self):
        return not any(a <= self.t < b for a, b in self.windows)


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def worker_tests(w):
    w.log = lambda *a, **k: None
    w.LANE_5XX_SETTLE_S = 0
    # --- _lane_restart_wait on a ~6 min window ---
    sim = Sim([(0, 360)])
    out = w._lane_restart_wait(DB, probe=sim.healthy, sleep=sim.sleep, clock=sim.clock)
    check("6-min outage -> recovered", out[0], "recovered")
    check("waited the outage out (>=360s, bounded)", 360 <= out[1] <= 400, True)
    sim = Sim([(0, 900)])
    out = w._lane_restart_wait(DB, probe=sim.healthy, sleep=sim.sleep, clock=sim.clock)
    check("15-min outage -> timeout at LANE_RESTART_WAIT_S", (out[0], out[1] <= w.LANE_RESTART_WAIT_S + 1),
          ("timeout", True))
    check("wait window tolerates >= 6 min", w.LANE_RESTART_WAIT_S >= 360, True)
    out = w._lane_restart_wait(DB, probe=lambda: True, sleep=sim.sleep, clock=sim.clock)
    check("healthy at once -> 'up'", out[0], "up")

    # --- call_ollama across REPEATED outage windows ---
    # Each failed attempt starts a new ~6 min outage; the 5th attempt succeeds.
    def run(failures, health_down_each=True, host=DB):
        sim = Sim([])
        seq = list(failures)
        calls = []

        def urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            if seq:
                kind = seq.pop(0)
                if health_down_each:
                    sim.windows.append((sim.t, sim.t + 360))
                if kind == 500:
                    raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(b""))
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return FakeResp(b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{}}')

        real_wait = w._lane_restart_wait
        w._lane_restart_wait = lambda h, **k: real_wait(h, probe=sim.healthy, sleep=sim.sleep, clock=sim.clock)
        orig = w.urllib.request.urlopen
        w.urllib.request.urlopen = urlopen
        try:
            r = w.call_ollama(host, "m", [{"role": "user", "content": "hi"}], 0.0, 4096,
                              tools=False, api_style="openai")
            return ("ok", r["message"]["content"], len(calls), sim.t)
        except RuntimeError as e:
            return ("err", str(e)[:80], len(calls), sim.t)
        finally:
            w.urllib.request.urlopen = orig
            w._lane_restart_wait = real_wait

    r = run([500, "conn", 500, "conn"])
    check("request spanning 3 restart windows + 1 error still succeeds", r[0], "ok")
    check("sim time covers the repeated ~6 min windows", r[3] >= 3 * 360, True)
    check("lane cause recorded for the row", "restarting" in (w._LANE_LAST_CAUSE[0] or ""), True)
    r = run([500, 500, 500], health_down_each=False)
    check("5xx with /health OK is a real error: fails after CHAT_RETRIES", (r[0], r[2]), ("err", 3))
    check("...and the cause says /health OK", "health OK" in (w._LANE_LAST_CAUSE[0] or ""), True)
    r = run([500] * 8)
    check("free retries are capped (LANE_RESTART_MAX_CYCLES)", r[0], "err")
    check("non-Darkbloom host untouched", w._is_darkbloom_lane("http://192.0.2.15:11434"), False)


def queue_tests(q):
    base = {"id": "e10334c98f27", "label": "auto-refine-rivian-s2-r3", "status": "paused",
            "pause_reason": "chat_request_failed", "host_pref": "studio-db",
            "resume_transcript": "/tmp/t.json",
            "pause_meta": {"lane_cause": "darkbloom lane down: HTTP 500; /health not OK after 480s"}}
    j = dict(base)
    check("health down -> wait", q._lane_failure_decide(j, 120, False)[0], "skip")
    check("just paused -> wait", q._lane_failure_decide(j, 5, True)[0], "skip")
    check("health OK -> resume", q._lane_failure_decide(j, 120, True)[0], "resume")
    check("Unraid lane not owned", q._lane_failure_decide(dict(base, host_pref="unraid"), 120, True)[0], "skip")
    check("operator hold respected", q._lane_failure_decide(dict(base, user_hold=True), 120, True)[0], "skip")
    check("stranded watchdog defers to lane-failure watchdog",
          q._stranded_pause_decide(dict(base), 99999), ("skip", "lane-failure watchdog owns it"))
    logf = Path(tempfile.mkdtemp()) / "auto.log"
    # 6 outage cycles: pause -> (health down for a while) -> health OK -> resume
    j = dict(base)
    resumed = 0
    for cycle in range(6):
        j.update(status="paused", pause_reason="chat_request_failed",
                 pause_meta={"lane_cause": f"darkbloom lane down cycle {cycle}"})
        for _tick in range(5):     # /health down during the outage
            q._lane_failure_apply(j, q._lane_failure_decide(j, 200, False), log_path=logf)
        check(f"cycle {cycle}: still paused while lane down", j["status"], "paused")
        if q._lane_failure_apply(j, q._lane_failure_decide(j, 400, True), log_path=logf):
            resumed += j["status"] == "pending"
    check("all 6 outage cycles auto-resumed", resumed, 6)
    check("cap tolerates >= 6 cycles", q.LANE_FAIL_RESUME_MAX >= 6, True)
    check("cause history kept on the row", j.get("lane_fail_history", [{}])[-1].get("cause"),
          "darkbloom lane down cycle 5")
    lines = logf.read_text().splitlines() if logf.exists() else []
    check("each auto-resume logged with its job id", (len(lines), all("e10334c98f27" in l for l in lines)), (6, True))
    j.update(status="paused", pause_reason="chat_request_failed", lane_fail_resume_count=q.LANE_FAIL_RESUME_MAX)
    d = q._lane_failure_decide(j, 400, True)
    check("past the cap -> surfaced, not resumed", d[0], "surface")
    check("past the cap the stranded watchdog no longer defers", q._lane_failure_owns(j), False)


def main():
    worker_tests(load(WORKER, "w_lr"))
    queue_tests(load(QUEUE, "q_lr"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("WORKER_SRC", "worker never waits on the lane", "    if not _is_darkbloom_lane(host):\n        return False\n    if code is not None and not (500",
     "    if True:\n        return False\n    if code is not None and not (500"),
    ("WORKER_SRC", "recovered retry charged", "                _budget += 1      # the lane restarted under us and is back: not charged\n",
     "                pass\n"),
    ("QUEUE_SRC", "queue never auto-resumes", '    return ("resume", f"[queue] LANE-FAILURE AUTO-RESUME',
     '    return ("skip", f"[queue] LANE-FAILURE AUTO-RESUME'),
    ("QUEUE_SRC", "resumes while /health is down", "    if not health_ok:\n        return (\"skip\"",
     "    if False:\n        return (\"skip\""),
    ("QUEUE_SRC", "auto-resume not logged", "        with open(log_path or LANE_FAIL_RESUME_LOG, \"a\") as f:",
     "        with open(os.devnull, \"a\") as f:"),
]


def revert_check():
    bad = 0
    srcs = {"WORKER_SRC": WORKER, "QUEUE_SRC": QUEUE}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
