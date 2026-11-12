#!/usr/bin/env python3
"""test-queue-self-test.py -- run `ollama-queue.py --self-test` (900+ checks: slot_decision,
state-file I/O, gate-preempt, lane routing, bundle commitment, ...) as a canary seam.

WHY (adversarial canary review 2026-10-13): the self-test was the ONLY guard for large parts of the
queue and the canary never ran it, so it sat RED for weeks (6 gate-preempt checks) unnoticed.
Those 6 were not stale logic: they depended on the host's live ~/.darkbloom/local.json (the
'studio' pref resolves to the Darkbloom lane only when a record exists), so they failed on any
machine/HOME without one. The self-test now pins a fixture record; this seam proves it stays
hermetic by running it under an EMPTY HOME (no local.json, no provider.toml) and requiring green.

Usage: python3 ~/bin/test-queue-self-test.py [--queue PATH]     Prints QUEUE_SELFTEST_OK."""
import argparse, os, subprocess, sys, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
q = Path(A.queue).resolve()
home = Path(tempfile.mkdtemp(prefix="qselftest-"))
(home / "bin").symlink_to(q.parent)          # the worker/profile modules resolve via $HOME/bin
env = dict(os.environ, HOME=str(home), OLLAMA_QUEUE_NO_NOTIFY="1")
p = subprocess.run([sys.executable, str(q), "--self-test"], cwd=str(home), env=env,
                   capture_output=True, text=True, timeout=300)
out = p.stdout
fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
npass = sum(1 for ln in out.splitlines() if ln.startswith("PASS"))
for ln in fails:
    print(ln)
if p.returncode == 0 and "SELF_TEST_OK" in out and not fails and npass >= 800:
    print(f"{npass} checks passed under an empty HOME")
    print("QUEUE_SELFTEST_OK")
else:
    print(f"FAIL - self-test rc={p.returncode} passes={npass} fails={len(fails)}\n{p.stderr[-500:]}")
    sys.exit(1)
