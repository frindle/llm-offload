#!/usr/bin/env python3
"""HERMETICITY guard for the pipeline self-tests (2026-10-06).

A self-test must never shell out to the LIVE pipeline tools (ollama-queue.py,
handoff-emit.py, signoff.py, the slicer, notify) at their real ~/bin paths. Found:
the slicer self-test and three test-slice-*.py suites ran the real
`ollama-queue.py status` and `ollama-queue.py plan-clear --group slice-alpha` on
fake plans; earlier the gate self-test ran a real `ollama-queue.py escalate` on
fake ids.

How: every child python gets a sitecustomize (via PYTHONPATH) that wraps
subprocess.Popen and appends any argv naming a real tool path to a log. Each
target runs; the test FAILS if the log is non-empty or the target fails.

    python3 test-selftest-hermetic.py              # default targets
    python3 test-selftest-hermetic.py --bin DIR    # run the targets found in DIR
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REAL_BIN = Path.home() / "bin"
REAL_TOOLS = ("ollama-queue.py", "handoff-emit.py", "signoff.py", "ollama-dispatch-slice",
              "notify-owner.py", "dispatch-ack-reconcile.py", "dispatch-escalation-watcher.py",
              "ollama-dispatch-auto")
TARGETS = [
    ("gate-on-complete.py", ["--self-test"]),
    ("gate.py", ["--self-test"]),
    ("ollama-dispatch-slice", ["--self-test"]),
    ("dispatch-escalation-watcher.py", ["--self-test"]),
    ("dispatch-ack-reconcile.py", ["--self-test"]),
    ("test-slice-failed-autoheal.py", []),
    ("test-slice-chain-state-bugs.py", []),
    ("test-slice-review-notes.py", []),
    ("test-slice-escalation-msg.py", []),
]

SITECUSTOMIZE = r'''
import os, subprocess
_LOG = os.environ.get("HERMETIC_TRACE_LOG")
_REAL = [os.path.join(os.environ.get("HERMETIC_REAL_BIN", ""), t)
         for t in os.environ.get("HERMETIC_REAL_TOOLS", "").split(",") if t]
_P = subprocess.Popen
class _Traced(_P):
    def __init__(self, args, *a, **k):
        try:
            s = " ".join(map(str, args if isinstance(args, (list, tuple)) else [args]))
            if _LOG and any(r in s for r in _REAL):
                with open(_LOG, "a") as fh:
                    fh.write(s[:240] + "\n")
        except Exception:
            pass
        super().__init__(args, *a, **k)
subprocess.Popen = _Traced
'''


def run(bin_dir: Path) -> bool:
    ok = True
    with tempfile.TemporaryDirectory(prefix="hermetic-") as td:
        sc = Path(td) / "sc"
        sc.mkdir()
        (sc / "sitecustomize.py").write_text(SITECUSTOMIZE)
        for name, args in TARGETS:
            path = bin_dir / name
            if not path.exists():
                print(f"SKIP {name} (not in {bin_dir})")
                continue
            log = Path(td) / f"{name}.log"
            env = {**os.environ, "PYTHONPATH": str(sc), "HERMETIC_TRACE_LOG": str(log),
                   "HERMETIC_REAL_BIN": str(REAL_BIN), "HERMETIC_REAL_TOOLS": ",".join(REAL_TOOLS)}
            env.pop("GATE_TEST_MODE", None)
            cp = subprocess.run([sys.executable, str(path), *args], capture_output=True,
                                text=True, env=env, cwd=str(bin_dir), timeout=900)
            calls = log.read_text().splitlines() if log.exists() else []
            if cp.returncode != 0:
                ok = False
                print(f"FAIL {name}: exited {cp.returncode}: {(cp.stdout + cp.stderr)[-300:]}")
            elif calls:
                ok = False
                print(f"FAIL {name}: {len(calls)} call(s) to the LIVE pipeline, e.g. {calls[0]}")
            else:
                print(f"PASS {name}: hermetic (0 live pipeline calls)")
    return ok


if __name__ == "__main__":
    b = REAL_BIN
    if "--bin" in sys.argv:
        b = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
    good = run(b)
    print("HERMETIC_OK" if good else "HERMETIC_FAILED")
    sys.exit(0 if good else 1)
