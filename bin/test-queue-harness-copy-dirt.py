#!/usr/bin/env python3
"""A qagent fix worktree carries its protected harness as UNTRACKED files (qagent --harness-from).  Every one of them
must be exempt from the launch-baseline dirty count, else a correct patch is parked needs_opus as "untrusted baseline:
launch baseline was dirty" before the model has typed anything.  dispatch_env.py (the sealed fixture-helper module the
scaffold drops at the root) was the one missing from _SCAFFOLD_BASENAMES: arr-webhook s3 5f86e3da576f, 2026-10-12.
Still counted: a real stray file, and a TRACKED edit of a scaffold-named file.
usage: test-queue-harness-copy-dirt.py [--bin DIR]   prints QUEUE_HARNESS_COPY_DIRT_OK on success."""
import argparse, importlib.util, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path.home() / "bin")); a = ap.parse_args()
ld = SourceFileLoader("oq_harness_dirt", str(Path(a.bin) / "ollama-queue.py"))
oq = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_harness_dirt", ld)); ld.exec_module(oq)

bad = []
def check(name, got, want):
    if got != want:
        bad.append(f"{name}: got {got!r}, want {want!r}")

def git(d, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})

HARNESS = ["TASK.md", "verify.sh", "check_literals.py", "test_fixture.py", "dispatch_env.py", "dispatch-env.ts"]
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    git(d, "init", "-q"); (d / "app.py").write_text("x = 1\n"); (d / "dispatch_env.py").write_text("# sealed\n")
    git(d, "add", "app.py"); git(d, "commit", "-qm", "base")
    (d / "dispatch_env.py").unlink()
    for n in HARNESS:
        (d / n).write_text("harness\n")
    check("copied harness incl. dispatch_env.py is not dirt", oq.measure_baseline(d)["dirty"], 0)
    (d / "stray.py").write_text("oops\n")
    check("a real stray untracked file still counts", oq.measure_baseline(d)["dirty"], 1)
    (d / "stray.py").unlink()
    (d / "app.py").write_text("x = 2\n")
    check("a tracked edit of the target still counts", oq.measure_baseline(d)["dirty"], 1)
    git(d, "checkout", "app.py")
    git(d, "add", "-f", "dispatch_env.py"); git(d, "commit", "-qm", "track helper")
    (d / "dispatch_env.py").write_text("# edited\n")
    check("a TRACKED edit of a scaffold-named file still counts", oq.measure_baseline(d)["dirty"], 1)
if bad:
    print("\n".join("FAIL " + b for b in bad)); sys.exit(1)
print("QUEUE_HARNESS_COPY_DIRT_OK")
