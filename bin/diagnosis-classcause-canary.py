#!/usr/bin/env python3
"""Canary: a CLASS is driven by its methods running, never by being imported.

A class BODY executes at import, unconditionally. Its frame's co_name IS the
class name, so before this was fixed the execution probe read "the module was
imported" as "the test drives this class" -- and a changed-but-never-exercised
class was ACCEPTED as the cause of a symptom it could not produce.

The fix has two halves and BOTH arms below are needed, because either half alone
breaks the other case:
  - ignoring non-CO_OPTIMIZED frames kills the import-time signal, but alone it
    means nothing named `Formatter` ever runs (that is hole (b));
  - matching a class by its methods' co_qualname restores real driving.

Arms:
  1. dispatched class, genuinely exercised  -> ACCEPTED   (hole (b) stays closed)
  2. changed class, NEVER exercised         -> REJECTED   (the false accept)
  3. unchanged class, exercised             -> REJECTED   (fault-path gate)
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

BIN = Path(os.environ.get("DIAG_BIN", Path(__file__).resolve().parent)).expanduser()
spec = importlib.util.spec_from_file_location("dc", BIN / "diagnosis-check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

ANCHOR = '''
class Untouched:
    def helper(self, x):
        return x

class Exercised:
    def render(self, rows):
        return ", ".join(rows)

class NeverCalled:
    def render(self, rows):
        return "|".join(rows)

registry = {"fmt": Exercised}

def run(kind, rows):
    Untouched().helper(1)
    return registry[kind]().render(rows)
'''
HEAD = (ANCHOR.replace('return ", ".join(rows)', 'return ",".join(rows)')
              .replace('return "|".join(rows)', 'return "/".join(rows)'))

TEST = ('import lib\n'
        'assert lib.run("fmt", ["a","b"]) == "a, b", lib.run("fmt", ["a","b"])\n')


def build():
    d = Path(tempfile.mkdtemp(prefix="classcause_"))
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
                    "commit", "-qm", "change both separators"], cwd=d)
    (d / "t_repro.py").write_text(TEST)
    return d, anchor


ARMS = [
    ("1 dispatched class, exercised (hole b)", "Exercised", True),
    ("2 changed class, NEVER exercised     ", "NeverCalled", False),
    ("3 unchanged class, exercised         ", "Untouched", False),
]

fails = 0
for label, sym, want_ok in ARMS:
    d, anchor = build()
    r = dc.check(d, "t_repro.py", sym, anchor, target_file="lib.py")
    accepted = str(r.get("verdict", "")).startswith("ACCEPTED")
    ok = accepted == want_ok
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}  verdict={r.get('verdict'):<20} "
          f"exec={str(r.get('symbol_executed')):5} "
          f"changed={r.get('symbol_changed_in_range')}")

print()
if fails:
    print(f"CLASS-CAUSE CANARY FAILED ({fails} arm(s))")
    sys.exit(1)
print("CLASS CAUSE HANDLED: a class is driven by its METHODS running, not by "
      "being imported; unexercised and unchanged classes both rejected")
