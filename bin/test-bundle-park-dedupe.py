#!/usr/bin/env python3
"""BUNDLE PARKED dedupe (2026-10-03).

_bundle_park_alert appended an identical row each time the same bundle re-parked
(idle-pipeline-test 4x, plan-gen-replay-endorse 2x, a minute apart), and the
SessionStart hook counts each one as another stuck item. Asserts, in a temp HOME:
  * the same park twice -> ONE open row
  * a different reason for the same bundle -> a second row (new information)
  * a different bundle -> its own row
  * once the row is ticked [x], a re-park alerts again
Usage: python3 ~/bin/test-bundle-park-dedupe.py
Revert: QUEUE=~/bin/ollama-queue.py.bak-parkdedupe python3 this.py -> FAIL
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE", HERE / "ollama-queue.py"))
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


loader = SourceFileLoader("queue_pd", str(QUEUE))
spec = importlib.util.spec_from_loader("queue_pd", loader)
q = importlib.util.module_from_spec(spec)
loader.exec_module(q)

T = Path(tempfile.mkdtemp(prefix="parkdd-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
IDX = T / ".ollama-dispatch" / "escalations" / "ESCALATIONS.md"


def open_rows():
    try:
        return [l for l in IDX.read_text().splitlines() if l.startswith("- [ ] ")]
    except OSError:
        return []


WHY = "f02404b2cdba: auto-refine-idle-test-testfile-py-r1 needs_opus"
q._bundle_park_alert("idle-pipeline-test", WHY, now=1000)
q._bundle_park_alert("idle-pipeline-test", WHY, now=1061)
chk("the same park twice -> ONE open row", len(open_rows()), 1)
q._bundle_park_alert("idle-pipeline-test", WHY + "; 5f865e95009e: r2 needs_opus", now=1100)
chk("a different reason for the same bundle -> a new row", len(open_rows()), 2)
q._bundle_park_alert("plan-gen-x", WHY, now=1200)
chk("a different bundle -> its own row", len(open_rows()), 3)
IDX.write_text(IDX.read_text().replace("- [ ]", "- [x]"))
q._bundle_park_alert("idle-pipeline-test", WHY, now=1300)
chk("after the row is ticked, a re-park alerts again", len(open_rows()), 1)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
