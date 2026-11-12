#!/usr/bin/env python3
"""Phase-2 (d) part 2 (2026-10-03): STAGED notices leave the escalation count.

The SessionStart hook counts every "- [ ] " row of ESCALATIONS.md as a "stuck
slice/job". A chain STAGED for landing is not stuck, and a VANISHED coding job was
mislabelled "READY TO LAND". Asserts, through the watcher's real run path pieces:
  * detect_integration_escalations(staged) -> notice_route -> READY-TO-LAND.md,
    NOT ESCALATIONS.md; open_escalations() (the hook's count) does not see it
  * a VANISHED job (source F) stays an open ESCALATIONS.md row, labelled
    "NOTICE (no review)", never "READY TO LAND"
  * real escalations (append_index on INDEX_MD) are unchanged

Usage: python3 ~/bin/test-escalation-ready-to-land.py
Revert: WATCHER=~/bin/dispatch-escalation-watcher.py.bak-readyland python3 this.py -> FAIL
"""
import importlib.util
import os
import shutil
import sys
import tempfile
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


loader = SourceFileLoader("watch_rtl", str(WATCHER))
spec = importlib.util.spec_from_loader("watch_rtl", loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)

T = Path(tempfile.mkdtemp(prefix="rtl-"))
IDX, READY = T / "ESCALATIONS.md", T / "READY-TO-LAND.md"
w.INDEX_MD, w.READY_MD = IDX, READY


def route(e):
    """The run_once no-review branch: notice_route when present, else the old
    hard-wired behaviour (what the .bak does)."""
    if hasattr(w, "notice_route"):
        return w.notice_route(e, IDX, READY)
    return IDX, "READY TO LAND -- %s" % e.get("reason", "")


staged = w.detect_integration_escalations(
    {"integration": {"status": w.INTEGRATION_STAGED, "staged_commit": "abc123def456",
                     "integrate_branch": "integrate/x", "onto": "main"}}, "planx")
chk("a staged chain is detected as a no-review notice",
    (len(staged), staged[0].get("no_review") if staged else None), (1, True))
p, line = route(staged[0])
w.append_index(staged[0], "/ctx.md", line, p)
chk("STAGED notice goes to READY-TO-LAND.md", p, READY)
chk("...and is NOT counted by the hook (open_escalations on ESCALATIONS.md)",
    w.open_escalations(IDX), [])
chk("...but is recorded, readable, still says READY TO LAND",
    "READY TO LAND" in (READY.read_text() if READY.exists() else ""), True)

vanished = {"source": "F", "kind": "slice", "plan": "planx", "slice_id": "s1",
            "no_review": True, "signature": "vanished:j1", "reason": "coding job j1 VANISHED"}
p, line = route(vanished)
w.append_index(vanished, "/ctx2.md", line, p)
rows = w.open_escalations(IDX)
chk("a VANISHED job stays an OPEN escalation row", len(rows), 1)
chk("...labelled as a notice, not READY TO LAND",
    ("NOTICE (no review)" in rows[0], "READY TO LAND" in rows[0]) if rows else None, (True, False))

w.append_index({"plan": "planx", "slice_id": "s2", "source": "B"}, "/c3.md", "VERDICT: x", IDX)
chk("a real escalation is still an open row in ESCALATIONS.md", len(w.open_escalations(IDX)), 2)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
