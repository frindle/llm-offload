#!/usr/bin/env python3
"""Regression test: the auto-fix NEAR-MISS spelling hint is computed from the job's
RECORDED diff (<out_dir>/<job>.diff), not from whatever the live worktree holds when
the gate runs.

Found by pipeline-canary soak seed 26 (2026-10-06): the s3 coding round wrote
`clampHihg`; the gate built the auto-fix requeue while a concurrent self-check had
the reference impl (correct `clampHigh`) applied in the tree, so _near_miss_feedback
saw the spec spelling, named nothing, and the auto-fix task said only "absent from
the diff".

Behaviour asserted via gate-on-complete.py's autofix_build_requeue (the real
requeue builder) and _near_miss_feedback:
  1. recorded diff has `clampHihg`, live tree has the spec spelling -> the feedback
     file names `clampHihg` -> `clampHigh`.
  2. no recorded diff -> falls back to the live tree (near-miss in the tree is named).
  3. recorded diff with the CORRECT spelling -> no near-miss hint (no false hint).

Run: python3 test-gate-near-miss-recorded-diff.py   (GATE_SRC=<file> to test a copy)
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("GATE_SRC") or HERE / "gate-on-complete.py")
FAILS = []
ISSUES = [{"severity": "high", "source": "completeness", "file": "", "line": 0,
           "what": "spec names `export function clampHigh(` but it is absent from the diff"}]
GOOD = "export function clampHigh(x: number, hi: number): number {\n  return x > hi ? hi : x;\n}\n"
BAD = GOOD.replace("clampHigh", "clampHihg")


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("goc_nm", str(SRC))
    sp = importlib.util.spec_from_loader("goc_nm", ld)
    m = importlib.util.module_from_spec(sp)
    ld.exec_module(m)
    return m


def repo(root, name, live):
    r = root / name
    r.mkdir()
    for c in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *c], check=True)
    (r / "lib").mkdir()
    (r / "lib" / "calc.ts").write_text("export const one = 1;\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True)
    (r / "lib" / "calc.ts").write_text("export const one = 1;\n" + live)
    return r


def diff_text(body):
    return ("diff --git a/lib/calc.ts b/lib/calc.ts\n--- a/lib/calc.ts\n+++ b/lib/calc.ts\n"
            "@@ -1 +1,4 @@\n export const one = 1;\n"
            + "".join("+" + l + "\n" for l in body.splitlines()))


def nm(m, cwd, issues, diff_path):
    """Call _near_miss_feedback; a pre-fix gate (no diff_path) gets the old call, so a
    revert test fails on BEHAVIOUR, not a TypeError."""
    import inspect
    if "diff_path" in inspect.signature(m._near_miss_feedback).parameters:
        return m._near_miss_feedback(cwd, issues, diff_path=diff_path)
    return m._near_miss_feedback(cwd, issues)


def main():
    m = load()
    root = Path(tempfile.mkdtemp(prefix="gate-nm-"))

    # 1. recorded diff = model's near-miss, live tree = racing refimpl (correct spelling)
    wt = repo(root, "race", GOOD)
    dp = root / "job1.diff"
    dp.write_text(diff_text(BAD))
    out = nm(m, str(wt), ISSUES, dp)
    check("1 near-miss named from the recorded diff despite the live tree",
          any("`clampHihg`" in l for l in out), True)

    # 1b. through the real requeue builder: the feedback file names the near miss
    out_dir = root / "logs"
    out_dir.mkdir()
    (out_dir / "abcabcabcabc.diff").write_text(diff_text(BAD))
    task = wt / "TASK.md"
    task.write_text("# TASK: t\n\n## Must contain\n\n- `export function clampHigh(`\n")
    payload = {"cwd": str(wt), "issues": ISSUES, "task_file": str(task), "verify": "bash verify.sh",
               "model": "m", "host": "h", "label": "t-s3"}
    fb_text = ""
    try:
        _argv, fb = m.autofix_build_requeue("abcabcabcabc", payload, out_dir, 1, str(root),
                                            {"reasons": ["verify-red"]})
        fb_text = Path(fb).read_text(errors="replace")
    except Exception as e:
        print(f"note: autofix_build_requeue raised {type(e).__name__}: {e}")
    check("1b auto-fix feedback file names `clampHihg`", "you wrote `clampHihg`" in fb_text, True)

    # 2. no recorded diff -> live tree fallback
    wt2 = repo(root, "nodiff", BAD)
    out = nm(m, str(wt2), ISSUES, root / "missing.diff")
    check("2 live-tree fallback still names the near miss", any("`clampHihg`" in l for l in out), True)

    # 3. recorded diff correct -> no hint
    wt3 = repo(root, "correct", GOOD)
    dp3 = root / "job3.diff"
    dp3.write_text(diff_text(GOOD))
    out = nm(m, str(wt3), ISSUES, dp3)
    check("3 correct spelling in the recorded diff -> no near-miss hint", out, [])

    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
