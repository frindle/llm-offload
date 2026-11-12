#!/usr/bin/env python3
"""Behavioural + revert test: spawn_review must not enqueue a SECOND review for
the same subject while one is already active under an earlier context file.

Defect (2026-10-06, job 0fb392936848): context files 20261006T020328Z-job-... and
20261006T023840Z-job-... produced two pending esc-review jobs (5e901735dc37,
f69fce37a51a) because the dedup label carries the context file's timestamp.

  python3 ~/bin/test-escalation-review-dedup.py
  WATCHER_FILE=<backup> python3 ~/bin/test-escalation-review-dedup.py   # must FAIL
No real queue is touched: subprocess.run is replaced inside the module.
"""
import importlib.util
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

MOD = os.environ.get("WATCHER_FILE", str(Path.home() / "bin" / "dispatch-escalation-watcher.py"))
_l = SourceFileLoader("escwatch", MOD)
W = importlib.util.module_from_spec(importlib.util.spec_from_loader("escwatch", _l))
_l.exec_module(W)

FAILED, RAN = [], []


def check(name, cond, extra=""):
    RAN.append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


class _R:
    def __init__(self, out=""):
        self.stdout, self.stderr, self.returncode = out, "", 0


def run_case(status_rows, stem, sib_output=None):
    """status_rows: list of (state, id, label). Returns (review_text, enqueues)."""
    td = Path(tempfile.mkdtemp())
    W.ESC_DIR = td
    enq = []
    polls = {"n": 0}

    def fake(cmd, **kw):
        if "enqueue" in cmd:
            enq.append(cmd)
            Path(cmd[cmd.index("--capture-final-as") + 1]).write_text("VERDICT: b -- fresh review\n")
            return _R("enqueued bbbbbbbbbbbb  x\n")
        if "status" in cmd:
            polls["n"] += 1
            # first poll: the given rows; later polls: everything finished
            rows = status_rows if polls["n"] == 1 else [("done", i, l) for _s, i, l in status_rows]
            return _R("".join("[%-8s] %s  %s model host=studio\n" % r for r in rows))
        return _R("")

    if sib_output is not None:
        sib_stem, txt = sib_output
        (td / ("%s.local-review.txt" % sib_stem)).write_text(txt)
    real = W.subprocess.run
    W.subprocess.run = fake
    real_sleep = W.time.sleep
    W.time.sleep = lambda s: None
    try:
        ctx = td / (stem + ".md")
        ctx.write_text("context\n")
        out = W.spawn_review(ctx, timeout=60)
    finally:
        W.subprocess.run = real
        W.time.sleep = real_sleep
    return out, enq


print("escalation-watcher: same-subject review dedup")
OLD = "20261006T020328Z-job-0fb392936848"
NEW = "20261006T023840Z-job-0fb392936848"

# 1. the live bug: an earlier review for the same job is pending
txt, enq = run_case([("pending", "5e901735dc37", "esc-review-" + OLD)], NEW,
                    sib_output=(OLD, "VERDICT: c -- harness defect\n"))
check("no second review is enqueued for the same subject", enq == [], "%d enqueue(s)" % len(enq))
check("the active sibling's review is returned", txt.startswith("VERDICT: c"), txt[:60])
check("the reuse is stated on the review", "reused review job 5e901735dc37" in txt)

# 2. a different subject still gets its own review
txt2, enq2 = run_case([("pending", "5e901735dc37", "esc-review-" + OLD)],
                      "20261006T023840Z-job-aaaaaaaaaaaa")
check("a different job still enqueues its own review", len(enq2) == 1)
check("...and returns that review", txt2.startswith("VERDICT: b"))

# 3. a FINISHED sibling does not suppress a fresh review
txt3, enq3 = run_case([("done", "5e901735dc37", "esc-review-" + OLD)], NEW)
check("a finished earlier review does not block a new one", len(enq3) == 1)

# 4. the exact-label path is unchanged (wait on own job, no enqueue)
txt4, enq4 = run_case([("pending", "ccccccccccccc", "esc-review-" + NEW)], NEW,
                      sib_output=(NEW, "VERDICT: a -- own\n"))
check("own active label: no enqueue, own output, no reuse note",
      enq4 == [] and txt4.startswith("VERDICT: a") and "reused" not in txt4)

print("%d checks, %d failed" % (len(RAN), len(FAILED)))
print("REVIEW DEDUP TEST PASSED" if not FAILED else "REVIEW DEDUP TEST FAILED")
sys.exit(1 if FAILED else 0)
