#!/usr/bin/env python3
"""Wrapper-runner (gpu-exclusive / bakeoff) failures are classified by what happened, not as
'worker produced no iterations'; the VRAM check retries a transient ssh failure; the bake-off
runner can cap a hung driver. Live 2026-10-09: the v12 Strata h2h rows (signal 15, ssh timeout in
the vram check, driver exit 4, a driver hung 2h52 after its DONE line) were ALL labelled
'worker produced no iterations (exit N)'.  Prints ALL PASS."""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, fn):
    sp = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


oq = load("oq_runner_cls", "ollama-queue.py")
gx = load("gx_runner", "gpu-exclusive-runner.py")
br = load("br_runner", "bakeoff-runner.py")
fails = []


def check(name, cond, detail=""):
    if not cond:
        fails.append(f"{name}: {detail}")


gj = {"runner": oq.GPU_JOB_RUNNER, "exit_code": 1, "job_kind": oq.GPU_JOB_KIND,
      "task_file": "/nonexistent/x.json", "log_path": "/nonexistent/log"}
r = oq.classify_runner_failure(gj, "", {"phase": "finished", "aborted": True,
                                        "abort_reason": "signal 15", "duration_s": 1787.7, "rc": 130})
check("abort names the signal", r and "signal 15" in r[1] and "no iterations" not in r[1], r)
r = oq.classify_runner_failure(gj, "", {"phase": "vram", "why": "vram_check failed (ssh: Operation timed out)"})
check("vram step failure carries the cause", r and "Operation timed out" in r[1] and "never started" in r[1], r)
r = oq.classify_runner_failure(gj, "[09:03:51] driver exit 4\n", {"phase": "finished", "rc": 4, "duration_s": 5})
check("driver exit 4 explained with last arm line", r and "rc=4" in r[1] and "driver exit 4" in r[1]
      and "resumes" in r[1], r)
r = oq.classify_runner_failure({"runner": "/x/bakeoff-runner.py", "exit_code": 241}, "")
check("bakeoff-runner signal exit", r and "signal 15" in r[1] and "hang" in r[1], r)
check("worker row untouched", oq.classify_runner_failure({"runner": "/x/ollama-worker.py"}, "") is None)

# end to end through _stamp_failure_class: the generic fallback must be replaced
job = dict(gj, label="x", status="failed", terminal_reason=None)
oq._stamp_failure_class(job)
check("stamp: no longer 'worker produced no iterations'",
      "worker produced no iterations" not in str(job.get("failure_detail")), job.get("failure_detail"))
check("stamp: still class harness", job.get("failure_class") == "harness", job.get("failure_class"))
wj = {"runner": "/Users/x/bin/ollama-worker.py", "exit_code": 2, "label": "w", "status": "failed"}
oq._stamp_failure_class(wj)
check("stamp: a worker row keeps the worker wording",
      str(wj.get("failure_detail")).startswith("worker produced no iterations"), wj.get("failure_detail"))

# vram retry
calls = []


def flaky(c):
    calls.append(1)
    if len(calls) < 3:
        raise RuntimeError("ssh: connect to host x port 22: Operation timed out")
    return 4


check("vram: transient reader errors retried", gx.wait_vram_free("x", 500, poll=0, sleep=lambda s: None,
                                                                  reader=flaky)[0] is True)
t = [0]


def clk():
    t[0] += 50
    return t[0]


def dead(c):
    raise RuntimeError("down")


check("vram: dead reader still fails closed",
      gx.wait_vram_free("x", 500, wait_s=120, poll=0, sleep=lambda s: None, clock=clk, reader=dead)[0] is False)

check("bakeoff-runner MAX_S parsed", br.parse_max_s("A=b\nMAX_S=7\n") == 7)
print("FAIL: " + "; ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
