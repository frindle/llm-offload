#!/usr/bin/env python3
"""cwd guard tests (no network, no GPU): python3 ~/bin/test_mini_cwdguard.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def need(cond, msg, r=None):
    if not cond:
        print("FAIL " + msg)
        if r is not None:
            print(r.stdout[-1500:], r.stderr[-800:])
        sys.exit(1)
    print("ok   " + msg)


def main():
    spec_ = importlib.util.spec_from_file_location("mswe", HERE / "mini-swe-runner.py")
    m = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(m)
    with tempfile.TemporaryDirectory() as root:
        root = Path(root).resolve()
        job = root / "job"
        job.mkdir()
        task = job / "TASK.md"
        task.write_text("# TASK\nkeep me\n")
        sp = job / "spec.json"
        base = root / "base"
        base.mkdir()
        sp.write_text(json.dumps({"mode": "diagnose", "allowed": [], "baseline_from": str(base), "task": str(task)}))
        local = root / "local.json"
        local.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k"}))
        env = {**os.environ, "DARKBLOOM_LOCAL_JSON": str(local)}

        # cwd == dir holding the task file: fail fast, nothing touched
        r = subprocess.run([sys.executable, str(HERE / "mini-swe-runner.py"), "--cwd", str(job), "--task-file", str(sp)],
                           capture_output=True, text=True, timeout=120, env=env)
        need(r.returncode != 0 and "RESULT FAIL bad-cwd" in r.stdout and "contains the task file" in r.stdout, "cwd containing task -> RESULT FAIL bad-cwd", r)
        need(task.read_text() == "# TASK\nkeep me\n" and sp.exists(), "task file and spec still exist, untouched")
        need("FileNotFoundError" not in r.stderr, "no crash")

        # ancestor cwd, and cwd containing baseline_from / spec
        need(m.cwd_guard(root, {"task": str(task)}, None), "ancestor cwd flagged")
        need("baseline_from" in (m.cwd_guard(root, {"baseline_from": str(base)}, None) or ""), "cwd containing baseline_from flagged")
        need("spec file" in (m.cwd_guard(job, {}, str(sp)) or ""), "cwd containing spec flagged")

        # separate cwd unchanged
        work = root / "work"
        work.mkdir()
        need(m.cwd_guard(work, {"task": str(task), "baseline_from": str(base)}, str(sp)) is None, "separate cwd -> no complaint")
        need(m.cwd_guard(work, {"task": "TASK.md"}, str(sp)) is None, "relative task name ignored")
    print("ALL PASS")


if __name__ == "__main__":
    main()
