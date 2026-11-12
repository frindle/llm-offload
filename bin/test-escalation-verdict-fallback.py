#!/usr/bin/env python3
"""#15 (2026-10-03): escalation rows that said only "(no VERDICT line -- read the
review file)" -- 33 of 143 real reviews. Excerpts below are verbatim from real
review files (20261003T160147Z-job-f02404b2cdba, 20261002T153026Z-...-s5-...).
  * an explicit VERDICT: line still wins, unchanged
  * a review that classified in prose but never wrote the line -> the LAST explicit
    classification, marked "(inferred, no VERDICT line)"
  * no classification at all -> the placeholder PLUS the escalation's own reason
  * no classification, no reason -> the exact old placeholder (index format stable)
  * a throwaway first line is still never promoted to a verdict
Usage: python3 ~/bin/test-escalation-verdict-fallback.py
Revert: WATCHER=~/bin/dispatch-escalation-watcher.py.bak-readyland python3 this.py -> FAIL
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCHER = Path(os.environ.get("WATCHER", HERE / "dispatch-escalation-watcher.py"))
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


loader = SourceFileLoader("watch_vf", str(WATCHER))
spec = importlib.util.spec_from_loader("watch_vf", loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)


def vl(review, reason=None):
    try:
        return w.verdict_line(review, reason)
    except TypeError:            # the .bak takes no reason
        return w.verdict_line(review)


PH = "(no VERDICT line -- read the review file)"
REAL_C = ("Let me re-read the escalation context file and the referenced evidence.\n"
          "...\nSo my verdict is (c) - a harness/dispatch defect.\n...\n"
          "My verdict is (c) - a harness/dispatch defect.\n")
REAL_B = ("Given that 6/35 mutants survived, and the spec only specifies a few behavioral "
          "cases, I think (b) is the most likely diagnosis. The spec needs to be "
          "re-authored to include more adversarial cases.\n\nSo (a) is not the diagnosis.\n")

chk("explicit VERDICT line wins, unchanged",
    vl("x\n**VERDICT: c -- harness defect**\nMy verdict is (b)", "r"), "VERDICT: c -- harness defect")
chk("prose 'my verdict is (c)' -> inferred (c)",
    vl(REAL_C, "r").startswith("verdict (inferred, no VERDICT line): My verdict is (c)"), True)
chk("prose '(b) is the most likely diagnosis' -> inferred (b), not the later '(a) is not'",
    vl(REAL_B, "r").startswith("verdict (inferred, no VERDICT line): (b) is the most likely"), True)
chk("no classification -> placeholder + escalation reason",
    vl("You've hit your session limit", "vacuous must_contain gate: every literal ..."),
    PH + " -- escalation reason: vacuous must_contain gate: every literal ...")
chk("no classification, no reason -> the exact old placeholder", vl("", None), PH)
chk("a throwaway first line is never a verdict",
    vl("The file search I started in the background has now finished.\nIt isn't needed."), PH)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
