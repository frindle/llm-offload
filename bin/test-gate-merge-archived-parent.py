#!/usr/bin/env python3
"""Regression test (2026-10-05, regate-47d71a149da5): a regate/review whose PARENT was
cleared (its <parent>.gate.json moved to LOG_DIR/archive/ by a run-status clear or the
janitor's stage-dedup) used to have its verdict DROPPED by merge_review ("no parent
record"), so re-running that regate could never land a verdict.

Now: the verdict is merged into the ARCHIVED record (regate=done, gate_authority set,
merged_after_clear=True) and NONE of the downstream tail runs (no autofix / pipeline /
slice feed / notify / janitor) -- a cleared row must never be acted on again.
A live (un-archived) parent still runs the downstream tail exactly as before, and a
parent with no record anywhere is still a no-op.

Revert-test: GATE_SRC=<backup> python3 test-gate-merge-archived-parent.py -> the
archived-parent checks FAIL (verdict never recorded).
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["GATE_TEST_MODE"] = "1"
GATE = Path(os.environ.get("GATE_SRC") or Path(__file__).resolve().parent / "gate-on-complete.py")
failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def load():
    from importlib.machinery import SourceFileLoader   # a .bak-* path has no .py suffix
    spec = importlib.util.spec_from_loader("goc_arch", SourceFileLoader("goc_arch", str(GATE)))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


REPORT = "# Review: refimpl.py\n\n## VERDICT: PASS\n\nNo findings.\n"
PRE = {"verdict": "concerns", "review_verdict": "PASS WITH CAVEATS", "regate": "pending",
       "regate_label": "regate-PARENT", "pregate_verdict": "concerns", "issues": [],
       "counts": {"input": 0, "code": 0}, "job_label": "auto-refine-x-r1"}


def run_case(m, td, where):
    out = Path(td) / "logs"
    (out / "archive").mkdir(parents=True)
    rdir = out / "PARENT-regate"
    rdir.mkdir()
    (rdir / "report.md").write_text(REPORT)
    if where == "archive":
        (out / "archive" / "PARENT.gate.json").write_text(json.dumps(PRE))
    elif where == "live":
        (out / "PARENT.gate.json").write_text(json.dumps(PRE))
    calls = []
    m._finalize_review = lambda *a, **k: calls.append("finalize") or 0
    m.finding_check_consider = lambda *a, **k: calls.append("finding_check") or False
    m.defer_intermediate_concerns = lambda *a, **k: calls.append("defer")
    a = SimpleNamespace(job_label="regate-PARENT", cwd=str(rdir), job_id="REGATEJOB")
    rc = m.merge_review(a, out, prefix="regate-", authoritative=True)
    return rc, out, calls


def main():
    m = load()
    with tempfile.TemporaryDirectory() as td:
        rc, out, calls = run_case(m, td, "archive")
        rec = json.loads((out / "archive" / "PARENT.gate.json").read_text())
        ok("archived parent: rc 0", rc == 0)
        ok("archived parent: authoritative verdict recorded (regate=done)",
           rec.get("regate") == "done")
        ok("archived parent: gate_authority set", rec.get("gate_authority") == "studio-27b-regate")
        ok("archived parent: review merged (review_verdict=PASS)", rec.get("review_verdict") == "PASS")
        ok("archived parent: flagged merged_after_clear", rec.get("merged_after_clear") is True)
        ok("archived parent: NO downstream tail ran", calls == [])
        ok("archived parent: no live record resurrected", not (out / "PARENT.gate.json").exists())
    with tempfile.TemporaryDirectory() as td:
        rc, out, calls = run_case(m, td, "live")
        rec = json.loads((out / "PARENT.gate.json").read_text())
        ok("live parent: verdict merged", rec.get("regate") == "done")
        ok("live parent: downstream tail still runs", "finalize" in calls)
        ok("live parent: not flagged merged_after_clear", "merged_after_clear" not in rec)
    with tempfile.TemporaryDirectory() as td:
        rc, out, calls = run_case(m, td, "none")
        ok("no record anywhere: rc 0, nothing written, no tail",
           rc == 0 and calls == [] and not list(out.rglob("*.gate.json")))
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
