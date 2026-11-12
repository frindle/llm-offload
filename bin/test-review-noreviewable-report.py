#!/usr/bin/env python3
"""Regression test for the gate review hole (2026-10-02).

code-review-agent.py --mode review on a diff whose every file is generated/vendored
(an auto-author job that only touched tsconfig.tsbuildinfo) returned 0 WITHOUT a
report.md, so gate-on-complete left the record at verdict=pass-pending-review /
review="failed (no report produced)" -- never terminal -- and the slicer waited out
its 20-min lost-event grace on every such slice (c1b8126a2db3, 5d8e1e3e46e1, ...).

  * a generated-only diff now yields report.md with a parseable '## VERDICT: PASS';
  * the gate's own merge regex reads it as PASS;
  * a diff with a REAL source file does not take that path (it would need a model;
    we only assert it does not write the nothing-to-review report and exit 0 early).
Run: python3 test-review-noreviewable-report.py [--revert-check]
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CRA_SRC") or HERE / "code-review-agent.py")
FAILS = []
GEN = ('diff --git a/tsconfig.tsbuildinfo b/tsconfig.tsbuildinfo\n--- a/tsconfig.tsbuildinfo\n'
       '+++ b/tsconfig.tsbuildinfo\n@@ -1 +1 @@\n-{"a":1}\n+{"a":2}\n')
REAL = ('diff --git a/lib/x.ts b/lib/x.ts\n--- a/lib/x.ts\n+++ b/lib/x.ts\n@@ -1 +1 @@\n'
        '-export const a = 1;\n+export const a = 2;\n')


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def run(diff, td):
    d = Path(td) / "x.diff"
    d.write_text(diff)
    out = Path(td) / "out"
    r = subprocess.run([sys.executable, str(SRC), "--mode", "review", "--diff", str(d),
                        "--out", str(out), "--host", "http://127.0.0.1:9"],
                       capture_output=True, text=True, timeout=120)
    rep = out / "report.md"
    return r.returncode, (rep.read_text() if rep.exists() else None)


def main():
    with tempfile.TemporaryDirectory() as td:
        rc, rep = run(GEN, td)
        check("generated-only diff exits 0", rc, 0)
        check("...and WRITES report.md", rep is not None, True)
        m = re.search(r"^## VERDICT: (.+)$", rep or "", re.M)   # gate-on-complete's regex
        check("...which the gate reads as PASS", m and m.group(1).strip(), "PASS")
        check("...naming the skipped file", "tsconfig.tsbuildinfo" in (rep or ""), True)
    with tempfile.TemporaryDirectory() as td:
        rc, rep = run(REAL, td)
        check("a real source diff does not get the nothing-to-review report",
              "nothing to review" in (rep or ""), False)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no report written", '            (outdir / "report.md").write_text(\n                f"# Review: {args.diff}',
     '            (outdir / "unused.md").write_text(\n                f"# Review: {args.diff}'),
    ("unparseable verdict", 'f"# Review: {args.diff}\\n\\n## VERDICT: PASS\\n"',
     'f"# Review: {args.diff}\\n\\nVERDICT PASS\\n"'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = Path(tempfile.mkdtemp(prefix="cramut-"))
        dst = d / "code-review-agent.py"
        dst.write_text(src.replace(old, new))
        for sib in os.listdir(HERE):
            if sib.endswith(".py") and sib != "code-review-agent.py":
                try:
                    os.symlink(HERE / sib, d / sib)
                except OSError:
                    pass
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "CRA_SRC": str(dst)},
                           capture_output=True, text=True, timeout=300)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
