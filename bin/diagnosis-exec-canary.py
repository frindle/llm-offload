#!/usr/bin/env python3
"""Adversarial canary for diagnosis-check's EXECUTION fallback.

The fallback exists because AST name-resolution cannot see framework dispatch: a
Flask view reached via client.get('/route') is never named by the test, so a
correct diagnosis was REJECTED (measured on job be4dc794f19c). But "the symbol
executed" is a weaker property than "the test proves something about it", so the
widening must not open a hole.

Arms:
  1. sound-indirect  -- drives the target through dispatch, asserts the symptom.
                        MUST be accepted (this is the case the fallback is for).
  2. executed-then-assert-false -- calls the target, then `assert False`.
                        MUST be rejected: executing is not proving.
  3. never-touches-target -- fails for an unrelated reason.
                        MUST be rejected: the fallback must not fire at all.
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
spec = importlib.util.spec_from_file_location("dc", BIN / "diagnosis-check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

LIB = '''
def dispatch(name):
    """Stand-in for a framework router: reaches target() without naming it."""
    return {"go": target}[name]()

def target(x=3):
    return x * 0        # the "bug": should be x * 2
'''

ARMS = [
    ("1 sound-indirect (must ACCEPT)", '''
import lib
r = lib.dispatch("go")
assert r == 6, f"expected 6, got {r}"
''', True),
    ("2 executed-then-assert-false (must REJECT)", '''
import lib
lib.dispatch("go")
assert False
''', False),
    ("3 never-touches-target (must REJECT)", '''
assert 1 == 2, "unrelated failure"
''', False),
]

fails = 0
for label, test_src, want_ok in ARMS:
    d = Path(tempfile.mkdtemp(prefix="diagcanary_"))
    (d / "lib.py").write_text(LIB)
    subprocess.run(["git", "init", "-q"], cwd=d)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "pre-bug: target returns x*2"], cwd=d)
    # anchor = correct version
    (d / "lib.py").write_text(LIB.replace("return x * 0", "return x * 2"))
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "anchor"], cwd=d)
    anchor = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                            capture_output=True, text=True).stdout.strip()
    # HEAD = buggy version
    (d / "lib.py").write_text(LIB)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "introduce the bug"], cwd=d)
    (d / "t_repro.py").write_text(test_src)

    r = dc.check(d, "t_repro.py", "target", anchor, target_file="lib.py")
    accepted = str(r.get("verdict", "")).startswith("ACCEPTED")
    ok = (accepted == want_ok)
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<44} verdict={r.get('verdict')} "
          f"(drives={r.get('drives_symbol')}/{r.get('drives_symbol_via')} "
          f"uncond_ok={r.get('not_unconditional')} exec={r.get('symbol_executed')})")

print()
if fails:
    print(f"DIAGNOSIS EXECUTION CANARY FAILED ({fails} arm(s))")
    sys.exit(1)
print("EXECUTION FALLBACK DISCRIMINATES: indirect drive accepted, "
      "executed-but-vacuous rejected, untouched target rejected")
