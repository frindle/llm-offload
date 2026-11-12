#!/usr/bin/env python3
"""Run Status: offload (mini-swe-runner) rows are not "research/diagnosis output" (2026-11-03). An offload job never
writes a gate.json, so the day-old fold for gate-less rows used to take them too: a failed local-model job older than a
day dropped out of "needs your eyes", and every old pass said "research/diagnosis output (no code gate)". Now a failed
offload row needs eyes at any age, like a gated FAIL; passed, cancelled and verdict-less ones fold after a day with a
reason that says what they are. Gate-less rows from anything else fold as before.
Drives the API's own _completed_summary and _annotate_run_groups on synthetic rows (no queue, no sidecars).
Run: python3 tests/test-runstatus-runner-fold.py [--src PATH] [--mutants]
  --mutants applies each revert mutant to the source and expects this test to FAIL on each."""
import importlib.util, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []
OLD = "2026-09-01T00:00:00Z"
RUNNER = "/Users/me/bin/mini-swe-runner"

MUTANTS = [
    ("the summary never marks an offload row",
     '"runner_gated": "mini-swe-runner" in str(r.get("runner") or ""),', '"runner_gated": False,'),
    ("a failed offload row folds after a day",
     '                if _runner_row_failed(r):\n                    r["group"], r["handled_reason"] = "needs_eyes", ""\n'
     '                    continue\n', ""),
    ("a FAIL verdict on a job that did not fail is not a failure",
     'return row.get("status") == "failed" or v.startswith("FAIL")', 'return row.get("status") == "failed"'),
    ("a failed job with no verdict is not a failure",
     'return row.get("status") == "failed" or v.startswith("FAIL")', 'return v.startswith("FAIL (")'),
    ("offload rows get the research reason",
     '            if r.get("runner_gated"):\n                if _runner_row_failed(r):',
     '            if False:\n                if _runner_row_failed(r):'),
]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(src):
    home = tempfile.mkdtemp(prefix="runner-fold-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_runner_fold", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_runner_fold", ld))
    ld.exec_module(m)
    return m


def row(m, rid, *, runner=RUNNER, status="done", verdict=None, ts=OLD, gate=None):
    """A durable job result as q._load_job_result shapes it, rendered by the API's own _completed_summary."""
    res = {"id": rid, "label": f"lbl-{rid}", "status": status, "runner": runner, "runner_verdict": verdict,
           "verdict": verdict, "timestamp": ts, "gate_json": gate}
    r = m._completed_summary(res)
    r["awaiting_signoff"] = False
    return r


def groups(m, rows):
    m._annotate_run_groups(rows, [])
    return {r["id"]: (r["group"], r.get("handled_reason") or "") for r in rows}


def run(src):
    m = load(src)
    m.ho._label_is_eval = lambda lab: False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    check("an offload result is marked runner_gated", row(m, "a")["runner_gated"], True)
    check("a result from no runner is not", row(m, "b", runner=None)["runner_gated"], False)
    check("its verdict tag is the runner's", row(m, "c", verdict="FAIL (mode=two-phase; tests)")["raw_verdict"][:4], "FAIL")

    g = groups(m, [
        row(m, "fail-old", status="failed", verdict="FAIL (mode=two-phase; tests)"),
        row(m, "failed-noverdict-old", status="failed"),
        row(m, "fail-verdict-done-old", status="done", verdict="FAIL (test draft)"),
        row(m, "pass-old", verdict="PASS"),
        row(m, "cancel-old", status="cancelled"),
        row(m, "noverdict-old", status="done"),
        row(m, "fail-fresh", status="failed", verdict="FAIL (mode=two-phase; tests)", ts=now),
        row(m, "pass-fresh", verdict="PASS", ts=now),
        row(m, "diag-old", runner=None, status="done"),
        row(m, "diag-failed-old", runner=None, status="failed"),
    ])
    check("a failed offload row older than a day still needs eyes", g["fail-old"][0], "needs_eyes")
    check("so does a failed one with no runner verdict", g["failed-noverdict-old"][0], "needs_eyes")
    check("so does a FAIL verdict on a job that ended 'done'", g["fail-verdict-done-old"][0], "needs_eyes")
    check("a passed offload row older than a day folds", g["pass-old"][0], "handled")
    check("...with a reason that names the runner's gates",
          g["pass-old"][1].startswith("local-model run passed its runner's gates"), True)
    check("...and not the research/diagnosis reason", "research" in g["pass-old"][1], False)
    check("a cancelled offload row folds as cancelled", g["cancel-old"], ("handled", "local-model run cancelled -- folded after a day"))
    check("a verdict-less finished offload row folds as such",
          g["noverdict-old"], ("handled", "local-model run ended without a runner verdict -- folded after a day"))
    check("a fresh failed offload row needs eyes", g["fail-fresh"][0], "needs_eyes")
    check("a fresh passed offload row is good to go", g["pass-fresh"][0], "good_to_go")
    check("an old gate-less non-offload row folds as research, as before",
          g["diag-old"], ("handled", "research/diagnosis output (no code gate) -- read & clear"))
    check("...even when it failed (unchanged)", g["diag-failed-old"][0], "handled")


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else HERE.parent / "src" / "ollama-queue-api.py"
    if "--mutants" in sys.argv:
        text = src.read_text()
        bad = 0
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                print(f"FAIL: mutant anchor missing or ambiguous: {name}"); bad += 1; continue
            tmp = src.parent / ".mutant-runner-fold-ollama-queue-api.py"     # next to its sibling modules
            tmp.write_text(text.replace(old, new, 1))
            try:
                r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True)
            finally:
                tmp.unlink(missing_ok=True)
            red = r.returncode != 0 and "FAIL:" in r.stdout and "crashed" not in r.stdout
            print(("ok  " if red else "FAIL") + f": mutant goes red -- {name}")
            bad += 0 if red else 1
        return 1 if bad else 0
    try:
        run(src)
    except Exception as e:
        import traceback; traceback.print_exc(); FAILS.append(f"crashed: {e}")
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nALL PASSED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
