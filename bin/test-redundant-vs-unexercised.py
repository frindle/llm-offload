#!/usr/bin/env python3
"""redundant_survivor_lines must not call an UNEXERCISED branch "redundant"
(2026-10-06, rt-bfmr-tls-fingerprint refine r1: the fixture never drove loggedFetch
on a BFMR URL, and the refine prompt told the model to DELETE the core routing
line `const res: Response = isBfmr` as redundant).
--revert-check restores the old one-rule body -> RED."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("oda_rvu", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_rvu", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    F = "lib/apiCallLog.ts"
    S = lambda line, cls="other": {"file": F, "line": line, "class": cls, "mutation": "m", "snippet": "x"}
    # real shapes from 39c89cf01c4a: condition plus its unobserved neighbours
    tls = [S(135), S(136, "cond-force-false"),            # isBfmr computed + routed
           S(78, "cond-force-false"), S(79),               # if (opts.body) / req.write
           S(89, "cond-force-false"), S(90)]               # if (sig) / addEventListener
    check("unexercised branches are NOT flagged redundant", m.redundant_survivor_lines(tls), [])
    rp = m.refine_prompt(F, tls, [], lang="typescript")
    check("refine prompt carries no deletion instruction", "LIKELY REDUNDANT" in rp, False)
    # isolated force-false survivor (its neighbours' mutants all died): still redundant
    lone = [S(40, "cond-force-false"), S(40, "compare-flip"), S(60)]
    check("isolated force-false survivor is still redundant", m.redundant_survivor_lines(lone), [(F, 40)])
    rp2 = m.refine_prompt(F, lone, [], lang="typescript")
    check("redundant hint warns about coverage holes", "coverage hole" in rp2, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    src = AUTO.read_text()
    a = src.index("    # UNEXERCISED != REDUNDANT")
    b = src.index("    return out\n", a)
    old = ('    out = []\n    for s in survivors or []:\n'
           '        if isinstance(s, dict) and s.get("class") == "cond-force-false":\n'
           '            k = (s.get("file"), s.get("line"))\n'
           '            if k not in out:\n                out.append(k)\n')
    with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
        f.write(src[:a] + old + src[b:])
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": old heuristic -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
