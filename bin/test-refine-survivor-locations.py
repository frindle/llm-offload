#!/usr/bin/env python3
"""Guards the bug fixed 2026-10-02 (idle-test-duration refine round 2 parked
"did not converge", worker log 20261002T175708Z.json).

The refine prompt listed survivors as `app/main.py:22` with the MUTATED snippet
(`pass`, `if False:`). Those numbers are lines of the target AFTER refimpl.py is
applied; the model edits refimpl.py, where the code sits at an offset, and the
mutated text exists nowhere. It spent its turns hand-counting lines, hit the
output cap twice and made no tool call. The LIKELY REDUNDANT hint had the same
off-by-offset.

Fix: ollama-dispatch-auto maps each survivor to the refimpl.py line (with the
original source text), disambiguating duplicate lines by surrounding context.

Run: python3 test-refine-survivor-locations.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AUTO = Path(os.environ.get("AUTO_SRC", Path(__file__).resolve().parent / "ollama-dispatch-auto"))
FAILS = []

REFIMPL = '''import pathlib, sys
wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'app/main.py'
t = p.read_text()
OLD = '"""TODO"""'
NEW = r\'\'\'def parse(s):
    i = 0
    while i < len(s):
        if i >= len(s):
            break
        if s[i] == '-':
            raise ValueError("neg")
        i += 1
        if i >= len(s):
            return 1
    return 0
\'\'\'
p.write_text(t.replace(OLD, NEW, 1))
'''
TARGET = '# header line 1\n# header line 2\n"""TODO"""\n'


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def main():
    spec = importlib.util.spec_from_loader("oda_rsl", importlib.machinery.SourceFileLoader("oda_rsl", str(AUTO)))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        (wt / "app").mkdir()
        (wt / "app/main.py").write_text(TARGET)
        (wt / "refimpl.py").write_text(REFIMPL)
        patched = m.patched_target_text(wt, "app/main.py")
        check("patched target computed without touching the worktree",
              (patched is not None, (wt / "app/main.py").read_text()), (True, TARGET))
        pl = patched.split("\n")
        # target line of the FIRST `if i >= len(s):` (dup also later) and the raise
        t_if = [l.strip() for l in pl].index("if i >= len(s):") + 1
        t_raise = next(i for i, l in enumerate(pl) if "raise ValueError" in l) + 1
        rl = REFIMPL.split("\n")
        r_if = [l.strip() for l in rl].index("if i >= len(s):") + 1
        r_raise = next(i for i, l in enumerate(rl) if "raise ValueError" in l) + 1
        survs = [
            {"file": "app/main.py", "line": t_if, "mutation": ">= -> >", "snippet": "if i > len(s):"},
            {"file": "app/main.py", "line": t_raise, "mutation": "delete raise", "snippet": "pass"},
            {"file": "app/main.py", "line": t_if, "mutation": "force False", "snippet": "if False:",
             "class": "cond-force-false"},
        ]
        lm = m.map_survivor_lines(survs, "app/main.py", REFIMPL, patched)
        check("duplicate line resolved to the right refimpl.py line by context",
              lm[("app/main.py", t_if)]["refimpl_line"], r_if)
        check("raise mapped to its refimpl.py line", lm[("app/main.py", t_raise)]["refimpl_line"], r_raise)
        check("offset is real (target line != refimpl line)", t_raise != r_raise, True)
        prompt = m.refine_prompt("app/main.py", survs, [], "python", line_map=lm)
        check("prompt names refimpl.py:<line> for the raise", f"`refimpl.py:{r_raise}`" in prompt, True)
        check("prompt carries the ORIGINAL text, not just the mutant",
              '`raise ValueError("neg")`' in prompt, True)
        red = prompt.split("LIKELY REDUNDANT LINE(S):", 1)[1].split("--", 1)[0] if "LIKELY REDUNDANT" in prompt else ""
        check("redundant hint uses the refimpl.py line", f"`refimpl.py:{r_if}`" in red, True)
        # without a patched target, a unique original still maps
        lm2 = m.map_survivor_lines([{"file": "app/main.py", "line": 99, "original": 'raise ValueError("neg")'}],
                                   "app/main.py", REFIMPL, None)
        check("no patched text: unique `original` still maps", lm2[("app/main.py", 99)]["refimpl_line"], r_raise)
        check("legacy call (no map) still renders", "`app/main.py:" in m.refine_prompt("app/main.py", survs, []), True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("context disambiguation removed", "            if scored[0][0] > (scored[1][0] if len(scored) > 1 else -1):\n                pick = scored[0][1]\n",
     ""),
    ("prompt drops refimpl location", "            if m or orig:", "            if False:"),
    ("redundant hint back to target lines", "if (f, n) in lm else f\"`{f}:{n}`\") for f, n in red)",
     "if False else f\"`{f}:{n}`\") for f, n in red)"),
]


def revert_check():
    src = AUTO.read_text()
    bad = 0
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-oda", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
