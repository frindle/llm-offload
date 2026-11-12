#!/usr/bin/env python3
"""Unattended-readiness (2c): `ollama-queue.py cancel` on a plan-labelled job
cancels ONLY that job, not the whole slice plan.

Cancelling one bad row used to write <plan>.cancelled, which freezes EVERY slice of
the plan (no slicer/self-heal/sweep re-arms a cancelled plan). Plan-wide cancel is
now an explicit `--plan` opt-in (cancel and stop); the dashboard's plain DELETE
(cancel_job(explicit=True)) is job-scoped too. QUEUE_SRC=<path> for the revert-test.
Runs entirely against a temp state/lock/log/slice-runs dir -- never the live queue.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
QU = Path(os.environ.get("QUEUE_SRC") or BIN / "ollama-queue.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    sys.path.insert(0, str(path.parent))
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    loader.exec_module(m)
    return m


def main():
    tmp = Path(tempfile.mkdtemp(prefix="qcancel-"))
    q = load(QU, "q_cancel")
    q.STATE_PATH = tmp / "state.json"
    q.LOCK_PATH = tmp / "state.lock"
    q.LOG_DIR = tmp / "logs"
    q.LOG_DIR.mkdir()
    runs = tmp / "runs"
    runs.mkdir()
    q.SLICE_RUNS_DIR = runs
    sys.path.insert(0, str(QU.parent))
    import plan_cancel
    (runs / "cxplan.json").write_text(json.dumps({"label": "cxplan", "order": ["s1", "s2"],
                                                  "slices": {"s1": {"status": "enqueued"},
                                                             "s2": {"status": "pending"}}}))

    def seed(*rows):
        q.STATE_PATH.write_text(json.dumps({"jobs": [
            {"id": i, "label": "cxplan-s1-route", "bundle": "cxplan", "status": st,
             "cwd": str(tmp)} for i, st in rows]}))
        plan_cancel.clear_cancelled("cxplan", runs_dir=runs)

    plan_cancelled = lambda: plan_cancel.cancelled("cxplan", runs_dir=runs) is not None
    ids = lambda: [j["id"] for j in json.loads(q.STATE_PATH.read_text())["jobs"]]
    A = lambda **k: types.SimpleNamespace(**{"job_id": "j1", "force": False,
                                             "automated": False, "plan": False, **k})
    chk("fixture: the job is recognised as the plan's", q._plan_label_for_job(
        {"label": "cxplan-s1-route", "bundle": "cxplan"}, runs), "cxplan")

    seed(("j1", "pending"), ("j2", "pending"))
    q.cmd_cancel(A())
    chk("bare `cancel` on a plan job: the plan is NOT cancelled", plan_cancelled(), False)
    chk("...only that job is gone, its sibling stays", ids(), ["j2"])

    seed(("j1", "pending"))
    q.cmd_cancel(A(plan=True))
    chk("`cancel --plan`: the plan IS cancelled (explicit opt-in)", plan_cancelled(), True)

    seed(("j1", "pending"))
    q.cmd_cancel(A(plan=True, automated=True))
    chk("`cancel --plan --automated`: a pipeline abort never cancels the plan",
        plan_cancelled(), False)

    seed(("j1", "pending"))
    q.cmd_stop(types.SimpleNamespace(job_id="j1", plan=False))
    chk("bare `stop` on a plan job: the plan is NOT cancelled", plan_cancelled(), False)

    seed(("j1", "pending"))
    q.cmd_stop(types.SimpleNamespace(job_id="j1", plan=True))
    chk("`stop --plan`: the plan IS cancelled", plan_cancelled(), True)

    seed(("j1", "paused"))
    q.cancel_job("j1", explicit=True)
    chk("dashboard DELETE (cancel_job explicit) is job-scoped", plan_cancelled(), False)
    import subprocess
    subprocess.run(["rm", "-rf", str(tmp)])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
