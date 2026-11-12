#!/usr/bin/env python3
"""Regression: a row the QUEUE held behind a pending gate/regate (hold_reason 'pending gate')
auto-releases, so it is WAITING, not parked: no Needs-attention entry, no Resume button
(the owner 2026-10-09: card-bonus/giftcard/amazon showed 'parked' + Resume while only waiting on
regate-f749...).  A person-held row (any other hold_reason) still parks.
Runs the page's own JS functions under node.  Run: python3 test-gate-held-not-parked.py"""
import os, re, subprocess, sys
from pathlib import Path

SRC = Path(os.environ.get("DASHBOARD_SRC") or Path(__file__).resolve().parent.parent / "src") / "ollama-queue-api.py"
text = SRC.read_text()
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def fn(name):
    m = re.search(r"^function %s\(.*?^}|^function %s\([^\n]*\{[^\n]*\}$" % (name, name), text, re.S | re.M)
    return m.group(0) if m else ""


js = fn("isGateHeld") + "\n" + fn("jobAttentionKind") + """
const r = x => process.stdout.write(String(x) + "\\n");
r(jobAttentionKind({status:'held', hold_reason:'pending gate'}));
r(jobAttentionKind({status:'held', hold_reason:'held by operator'}));
r(jobAttentionKind({status:'held'}));
r(jobAttentionKind({status:'failed'}));
"""
out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
got = out.stdout.split()
check("node ran", out.returncode, 0)
check("gate-held standalone row -> null (waiting)", got[:1], ["null"])
check("operator-held row -> parked", got[1:2], ["parked"])
check("held w/o reason -> parked", got[2:3], ["parked"])
check("failed row -> failed", got[3:4], ["failed"])
# bundle rollup: a gate-held row makes the bundle live (so it stays in Queue, not parked)
check("bundle liveNow counts gate-held rows", "gateHeldN ||" in text and "|| gateHeldN ||" in text, True)
print("FAILED" if FAILS else "ALL OK")
sys.exit(1 if FAILS else 0)
