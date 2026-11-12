#!/usr/bin/env python3
"""Every pipeline entry point turns iCloud dataless-file MATERIALIZATION on at import,
so git / verify children read evicted .git metadata instead of failing with EDEADLK
("not a git repository: (null)"; Rivian s1, 2026-10-02 -- see dataless_policy.py).

Each tool is imported (top level only, no main) in a child whose policy starts OFF;
the child must end with policy ON (2). --revert-check removes the enable() call from
each tool in turn (DP_DIR env points at a mutated copy of ~/bin) and requires RED."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = Path(os.environ.get("DP_DIR") or HERE)
TOOLS = ["ollama-worker.py",
         "dispatch-self-heal.py", "ollama-queue.py"]
# Covered TRANSITIVELY (it imports ollama-queue.py at load), so it carries no call of
# its own -- checked in main(), not mutated in --revert-check.
TRANSITIVE = ["gate-on-complete.py"]
FAILS = []

CHILD = r'''
import ctypes, importlib.util, sys
from importlib.machinery import SourceFileLoader
c = ctypes.CDLL(None)
c.setiopolicy_np(3, 0, 1)
assert c.getiopolicy_np(3, 0) == 1
sys.argv = [sys.argv[1]]
ld = SourceFileLoader("t_mod", sys.argv[0])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("t_mod", ld))
try:
    ld.exec_module(m)
except SystemExit:
    pass
print("POLICY", c.getiopolicy_np(3, 0))
'''


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    if sys.platform != "darwin":
        print("SKIP: not macOS")
        return 0
    for t in TOOLS + TRANSITIVE:
        r = subprocess.run([sys.executable, "-c", CHILD, str(DIR / t)],
                           capture_output=True, text=True, timeout=120)
        pol = [l.split()[1] for l in r.stdout.splitlines() if l.startswith("POLICY ")]
        check(f"{t} turns materialization ON at import", pol[-1:], ["2"])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    bad = 0
    for t in TOOLS:
        tmp = Path(tempfile.mkdtemp(prefix="dpw-"))
        for f in TOOLS + TRANSITIVE + ["dataless_policy.py"]:
            shutil.copy2(HERE / f, tmp / f)
        # the tools import sibling modules from their own dir; link the rest of ~/bin
        for f in HERE.iterdir():
            if f.is_file() and not (tmp / f.name).exists() and f.suffix in (".py", ".json", ""):
                try:
                    (tmp / f.name).symlink_to(f)
                except OSError:
                    pass
        src = (tmp / t).read_text()
        assert src.count("_dp_m.enable()") == 1, f"anchor missing: {t}"
        (tmp / t).write_text(src.replace("_dp_m.enable()", "pass"))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "DP_DIR": str(tmp)},
                           capture_output=True, text=True, timeout=900)
        shutil.rmtree(tmp, ignore_errors=True)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{t} enable()' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
