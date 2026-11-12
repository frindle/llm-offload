#!/usr/bin/env python3
"""Prove the duplicate-label guard in ollama-queue.py cmd_enqueue BITES and is
not over-broad. Runs entirely against an isolated HOME (temp state file); the
live queue is never opened.

Both-ways proof:
  1. BITES:      a second enqueue of a label whose job is LIVE (pending) is
                 refused, exit 7, first stdout line `duplicate-label <id> <label>`.
  2. NOT INERT:  the same second enqueue SUCCEEDS once the first job is terminal
                 (failed) -- a retry of a failed label must still work.
  3. NOT INERT:  a PLANNED placeholder row does not block the real enqueue.
  4. ESCAPE:     --allow-duplicate-label enqueues anyway.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

QUEUE = Path(os.environ.get("QUEUE_UNDER_TEST", str(Path.home() / "bin" / "ollama-queue.py")))
FAILS = []


def run(home, label, extra=()):
    env = dict(os.environ, HOME=str(home))
    cwd = home / "work"
    return subprocess.run(
        [sys.executable, str(QUEUE), "enqueue", "--model", "m", "--host", "studio",
         "--cwd", str(cwd), "--task-file", str(cwd / "T.md"), "--task-kind", "coding",
         "--num-ctx", "4096", "--verify", "true", "--label", label, "--no-ctx-gate",
         *extra],
        capture_output=True, text=True, env=env, timeout=180)


def fresh_home(seed_status=None, seed_label="dupe-me"):
    home = Path(tempfile.mkdtemp())
    (home / "bin").mkdir()
    (home / "work").mkdir()
    (home / "work" / "T.md").write_text("do a thing\n")
    jobs = []
    if seed_status:
        jobs.append({"id": "aaaaaaaaaaaa", "label": seed_label, "status": seed_status,
                     "cwd": str(home / "work"), "model": "m", "task_kind": "coding"})
    (home / "bin" / "ollama-queue-state.json").write_text(json.dumps({"jobs": jobs}))
    return home


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if not cond else ""))
    if not cond:
        FAILS.append(name)


# 1. BITES: live pending job under the same label
h = fresh_home("pending")
r = run(h, "dupe-me")
check("guard BITES on a live pending same-label job (exit 7)", r.returncode == 7,
      f"rc={r.returncode} out={r.stdout[-300:]} err={r.stderr[-300:]}")
check("refusal names the live job machine-readably",
      r.stdout.splitlines()[:1] == ["duplicate-label aaaaaaaaaaaa dupe-me"],
      f"first line={r.stdout.splitlines()[:1]}")
n = len(json.loads((h / "bin" / "ollama-queue-state.json").read_text())["jobs"])
check("refusal wrote NO second job", n == 1, f"jobs={n}")
shutil.rmtree(h)

# 1b. running counts as live too
h = fresh_home("running")
check("guard BITES on a RUNNING same-label job", run(h, "dupe-me").returncode == 7)
shutil.rmtree(h)

# 2. NOT INERT: a terminal prior run must still be retryable
h = fresh_home("failed")
r = run(h, "dupe-me")
check("retry of a FAILED label is still allowed", r.returncode == 0,
      f"rc={r.returncode} err={r.stderr[-400:]}")
shutil.rmtree(h)

# 3. NOT INERT: a PLANNED placeholder must not block the real job
h = fresh_home("planned")
r = run(h, "dupe-me")
check("PLANNED placeholder does not block the real enqueue", r.returncode == 0,
      f"rc={r.returncode} err={r.stderr[-400:]}")
shutil.rmtree(h)

# 3b. a DIFFERENT label is never blocked
h = fresh_home("pending")
r = run(h, "some-other-label")
check("a different label is never blocked", r.returncode == 0,
      f"rc={r.returncode} err={r.stderr[-400:]}")
shutil.rmtree(h)

# 4. escape hatch
h = fresh_home("pending")
r = run(h, "dupe-me", ["--allow-duplicate-label"])
check("--allow-duplicate-label bypasses the guard", r.returncode == 0,
      f"rc={r.returncode} err={r.stderr[-400:]}")
shutil.rmtree(h)

print("\n" + ("ALL_OK" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
