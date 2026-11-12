#!/usr/bin/env python3
"""Does 5c894aa's fault-path gate make a CONSTANT-change regression undiagnosable?

The regression: a module-level constant changed. The function that READS it is
byte-identical across the range.

    TIMEOUT = 30      ->  TIMEOUT = 0        (the change)
    def fetch(...)        def fetch(...)     (UNCHANGED)

My gate rejects a diagnosis whose named symbol's definition is unchanged. So:

  ARM 1: names `fetch` (the reader, unchanged)  -- I ARGUED this is a
         manifestation site and rejecting it is correct.
  ARM 2: names `TIMEOUT` (the actual cause)     -- but symbol_changed_in_range
         resolves def/class spans by AST, and a module-level ASSIGNMENT has no
         def span. If this also rejects, then a constant-change regression cannot
         be diagnosed AT ALL, whatever the model answers -- which is a real hole,
         not a correct rejection.

Arm 2 is the one that decides whether 5c894aa over-rejects. If both arms reject,
the gate has made a whole class of true regressions ungradeable.
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
spec = importlib.util.spec_from_file_location("dc", BIN / "diagnosis-check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

ANCHOR = '''
TIMEOUT = 30

def fetch(url):
    return _get(url, timeout=TIMEOUT)

def _get(url, timeout):
    return f"{url}:{timeout}"
'''
HEAD = ANCHOR.replace("TIMEOUT = 30", "TIMEOUT = 0")


def build():
    d = Path(tempfile.mkdtemp(prefix="constcase_"))
    (d / "lib.py").write_text(ANCHOR)
    subprocess.run(["git", "init", "-q"], cwd=d)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "anchor"], cwd=d)
    anchor = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                            capture_output=True, text=True).stdout.strip()
    (d / "lib.py").write_text(HEAD)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "drop TIMEOUT to 0"], cwd=d)
    return d, anchor


TEST = 'import lib\nassert lib.fetch("u") == "u:30", lib.fetch("u")\n'

WANT = {"fetch": "REJECTED",            # manifestation site: unchanged reader
        "TIMEOUT": "CAUSE_NOT_CALLABLE", # the real cause, not evaluatable
        "_get": "REJECTED"}              # bystander
fails = 0
for sym, want in WANT.items():
    d, anchor = build()
    (d / "t_repro.py").write_text(TEST)
    r = dc.check(d, "t_repro.py", sym, anchor, target_file="lib.py")
    got = r.get("verdict")
    ok = got == want
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} symbol={sym:9} verdict={got:<22} "
          f"want={want:<22} changed_in_range={r.get('symbol_changed_in_range')}")

print()
if fails:
    print(f"CONSTANT-CAUSE CANARY FAILED ({fails})"); sys.exit(1)
print("CONSTANT CAUSE HANDLED: the changed constant routes a human "
      "(CAUSE_NOT_CALLABLE), the unchanged reader and bystander stay REJECTED")
