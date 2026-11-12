#!/usr/bin/env python3
"""Evidence for hole (a): does the AST path let a WRONG symbol through, and would
gating it on changed_in_range falsely reject a RIGHT one?

Hole (a): drives_symbol/data_dependent can be established by AST name-resolution
alone, which never consults the execution probe or the fault path. So a test that
imports the WRONG symbol and asserts on it can satisfy criterion 3 by name.

The proposed fix is to require symbol_changed_in_range regardless of HOW driving
was established. Before doing that I need the case that would make it too
aggressive:

  ARM A (wrong symbol, AST-named): names `helper`, which is NOT the cause and is
        unchanged in the range, but imports it directly so AST resolves it.
        Currently ACCEPTED? -> that is hole (a).

  ARM B (right symbol, unchanged definition): the cause is that `load` now feeds
        different data to `render`. `render` itself is BYTE-IDENTICAL across the
        range. A diagnosis naming `render` is arguably WRONG (the change is in
        load), so rejecting it is correct -- but a diagnosis naming `load`, whose
        definition DID change, must still pass.

  ARM C (right symbol, changed definition): the control. Must stay ACCEPTED.

If ARM A is accepted today, hole (a) is real. If the changed_in_range gate would
also flip ARM C, it is too aggressive and must not ship.
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
spec = importlib.util.spec_from_file_location("dc", BIN / "diagnosis-check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

ANCHOR_LIB = '''
def helper(x):
    return x                      # bystander, NEVER changes

def render(rows):
    return ",".join(str(r) for r in rows)   # NEVER changes

def load(name):
    return [name, name]           # pre-bug: two elements

def dispatch(name):
    return render(helper(load(name)))
'''

HEAD_LIB = ANCHOR_LIB.replace("return [name, name]           # pre-bug: two elements",
                              "return [name]                  # BUG: one element")


def build():
    d = Path(tempfile.mkdtemp(prefix="holea_"))
    (d / "lib.py").write_text(ANCHOR_LIB)
    subprocess.run(["git", "init", "-q"], cwd=d)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "anchor"], cwd=d)
    anchor = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                            capture_output=True, text=True).stdout.strip()
    (d / "lib.py").write_text(HEAD_LIB)
    subprocess.run(["git", "add", "-A"], cwd=d)
    subprocess.run(["git", "-c", "user.email=c@x", "-c", "user.name=c",
                    "commit", "-qm", "introduce the bug"], cwd=d)
    return d, anchor


ARMS = [
    ("A wrong symbol, AST-named  (`helper`)", "helper",
     'import lib\nh = lib.helper\nassert h(1) == 1\nassert lib.dispatch("x") == "x,x"\n'),
    ("B right-ish, unchanged def (`render`)", "render",
     'import lib\nr = lib.render\nassert r(["a","a"]) == "a,a"\nassert lib.dispatch("x") == "x,x"\n'),
    ("C TRUE cause, changed def  (`load`)  ", "load",
     'import lib\nassert lib.load("x") == ["x","x"], lib.load("x")\n'),
]

WANT = {"A": False, "B": False, "C": True}      # C is the only true cause
fails = 0
for label, sym, tsrc in ARMS:
    d, anchor = build()
    (d / "t_repro.py").write_text(tsrc)
    r = dc.check(d, "t_repro.py", sym, anchor, target_file="lib.py")
    accepted = str(r.get("verdict", "")).startswith("ACCEPTED")
    want = WANT[label.strip()[0]]
    ok = accepted == want
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}  verdict={r.get('verdict'):<22} "
          f"drives={r.get('drives_symbol')}/{r.get('drives_symbol_via')} "
          f"changed_in_range={r.get('symbol_changed_in_range')}")

print()
if fails:
    print(f"HOLE-A CANARY FAILED ({fails} arm(s))"); sys.exit(1)
print("FAULT-PATH GATE HOLDS ON THE AST PATH: wrong symbol and unchanged "
      "bystander rejected, true cause accepted")
