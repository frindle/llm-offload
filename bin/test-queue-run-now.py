#!/usr/bin/env python3
"""Bundle "Run now" (2026-10-30): `ollama-queue.py run-now`, POST /api/jobs/<id>/run-now
and the dashboard's "Run this bundle now" button.

"Run next" (↑↑, promote_group_for_job(take_focus=True)) only reorders + focuses: the
running job keeps its lane, and promote_job's bundle-commitment veto stops even an
explicit preempt from pausing a COMMITTED bundle's job. Run now lifts exactly that veto:
the running job is paused gracefully (SIGTERM + promote_preempt intent, resumable), the
promoted bundle takes the human focus, and the paused job stays paused only while the
promoted bundle is busy (_beneficiary_bundle_busy), then auto-resumes. The min-progress
guard still holds (no force).

Hermetic: temp state/lock/log/slice dirs and a temp HOME; os.kill is recorded, never
sent. QUEUE_SRC / API_SRC=<path> run it against another copy (red/green proof).
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
QU = Path(os.environ.get("QUEUE_SRC") or BIN / "ollama-queue.py")
API = Path(os.environ.get("API_SRC") or BIN / "ollama-queue-api.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    sys.path.insert(0, str(BIN))
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    loader.exec_module(m)
    return m


def main():
    tmp = Path(tempfile.mkdtemp(prefix="qrunnow-"))
    home = tmp / "home"
    (home / "bin").mkdir(parents=True)
    for f in BIN.iterdir():      # the API module loads its siblings from ~/bin
        if f.name != "ollama-queue.py":
            os.symlink(f, home / "bin" / f.name)
    os.symlink(QU, home / "bin" / "ollama-queue.py")
    os.environ["HOME"] = str(home)
    q = load(QU, "q_runnow")
    q.STATE_PATH = tmp / "state.json"
    q.LOCK_PATH = tmp / "state.lock"
    q.LOG_DIR = tmp / "logs"
    q.LOG_DIR.mkdir()
    q.SLICE_RUNS_DIR = tmp / "runs"
    q.SLICE_PLANS_DIR = tmp / "plans"
    q._GROUP_INDEX_CACHE["reverse"] = None
    q.PROMOTE_REAP_WAIT_S = 0
    q.worker = lambda: None
    q._candidate_lanes = lambda job, w: ["L1"]
    q._lane_name = lambda u: u
    kills = []
    real_kill = os.kill
    os.kill = lambda pid, sig: kills.append(pid)

    def seed(launched_ago_s=3600):
        la = (datetime.now(timezone.utc) - timedelta(seconds=launched_ago_s)).isoformat()
        q.STATE_PATH.write_text(json.dumps({
            "_bundle_commit": {"key": "bA", "since": 0},
            "jobs": [
                {"id": "a1", "label": "a-one", "bundle": "bA", "status": "running",
                 "lane": "L1", "pid": 424242, "launched_at": la, "host_pref": "auto"},
                {"id": "a2", "label": "a-two", "bundle": "bA", "status": "pending", "host_pref": "auto"},
                {"id": "b1", "label": "b-one", "bundle": "bB", "status": "pending", "host_pref": "auto"},
                {"id": "b2", "label": "b-two", "bundle": "bB", "status": "pending", "host_pref": "auto"},
            ]}))
        kills.clear()

    st = lambda: json.loads(q.STATE_PATH.read_text())
    row = lambda s, i: next(j for j in s["jobs"] if j["id"] == i)
    try:
        # --- run NEXT (unchanged): reorders + focuses, pauses nothing ---------------
        seed()
        q.promote_group_for_job("b2", take_focus=True)
        s = st()
        chk("run next: nothing is signalled", kills, [])
        chk("run next: committed bundle's job keeps running", row(s, "a1")["status"], "running")
        chk("run next: bB has the focus", (s.get("_focus_override") or {}).get("key"), "bB")
        # --- explicit preempt still respects the commitment veto -------------------
        seed()
        q.promote_group_for_job("b2", preempt=True)
        chk("preempt (not run-now): a committed bundle is still not paused", kills, [])

        # --- RUN NOW: pauses the committed bundle's running job gracefully ----------
        seed()
        res = q.promote_group_for_job("b2", run_now=True)
        s = st()
        chk("run now: the running job is SIGTERM'd (graceful pause)", kills, [424242])
        chk("run now: victim stamped for auto-resume", row(s, "a1").get("preempt_intent"),
            q.PROMOTE_PREEMPT_REASON)
        chk("run now: victim serves the promoted bundle's head", row(s, "a1").get("preempted_by"), "b1")
        chk("run now: promoted bundle at the front, slice order kept",
            [j["id"] for j in s["jobs"]][:2], ["b1", "b2"])
        chk("run now: promoted bundle takes the human focus",
            (s.get("_focus_override") or {}).get("key"), "bB")
        chk("run now: reply names the paused job", (res.get("preempt") or {}).get("paused_job"), "a1")

        # --- the paused bundle resumes only after the promoted one is done ---------
        v = dict(row(s, "a1"), status="paused", pause_reason=q.PROMOTE_PREEMPT_REASON, lane=None)
        jobs_busy = [v] + [j for j in s["jobs"] if j["id"] != "a1"]
        pk = q._plan_key_map(jobs_busy)
        busy = q._beneficiary_bundle_busy(v, jobs_busy, pk, progress={}, chain={})
        chk("resume: promoted bundle still pending -> victim stays paused",
            q._promote_preempt_should_resume(v, jobs_busy, None, beneficiary_bundle_busy=busy), False)
        jobs_done = [v] + [dict(j, status="done") if j["bundle"] == "bB" else j
                           for j in s["jobs"] if j["id"] != "a1"]
        busy = q._beneficiary_bundle_busy(v, jobs_done, pk, progress={}, chain={})
        chk("resume: promoted bundle done -> victim auto-resumes",
            q._promote_preempt_should_resume(v, jobs_done, None, beneficiary_bundle_busy=busy), True)

        jobs_mid = [v] + [dict(j, status="done") if j["id"] == "b1" else j
                          for j in s["jobs"] if j["id"] != "a1"]
        busy = q._beneficiary_bundle_busy(v, jobs_mid, pk, progress={}, chain={})
        chk("resume: b1 done but b2 still pending -> victim stays paused",
            q._promote_preempt_should_resume(v, jobs_mid, None, beneficiary_bundle_busy=busy), False)

        # --- (1) a bundle never pauses itself -----------------------------------------
        s0 = {"jobs": [{"id": "a1", "label": "a-one", "bundle": "bA", "status": "running", "lane": "L1"},
                       {"id": "a2", "label": "a-two", "bundle": "bA", "status": "pending"}]}
        try:
            q._run_now_victim(s0, s0["jobs"][1], ["L1"])
            chk("self-pause: victim in the job's own bundle is refused", "no error", "QueueActionError")
        except q.QueueActionError:
            chk("self-pause: victim in the job's own bundle is refused", "QueueActionError", "QueueActionError")
        seed()
        before = q.STATE_PATH.read_text()
        try:
            q.run_bundle_now("bA")
            chk("run-now on a bundle that is already running is refused", "no error", "QueueActionError")
        except q.QueueActionError:
            chk("run-now on a bundle that is already running is refused", "QueueActionError", "QueueActionError")
        chk("...and state is untouched", q.STATE_PATH.read_text(), before)
        chk("...nothing signalled", kills, [])
        seed()        # bB already has a row running on ANOTHER lane: still refused
        q.STATE_PATH.write_text(json.dumps(dict(st(), jobs=st()["jobs"] + [
            {"id": "b0", "label": "b-zero", "bundle": "bB", "status": "running", "lane": "L9"}])))
        before = q.STATE_PATH.read_text()
        try:
            q.run_bundle_now("bB")
            chk("run-now on a bundle running elsewhere is refused", "no error", "QueueActionError")
        except q.QueueActionError:
            chk("run-now on a bundle running elsewhere is refused", "QueueActionError", "QueueActionError")
        chk("...state untouched, nothing signalled", (q.STATE_PATH.read_text() == before, kills), (True, []))

        # --- (2) multi-slot: a coder never pauses a gate --------------------------------
        s2 = {"jobs": [{"id": "g1", "label": "gate-x", "bundle": "bG", "status": "running", "lane": "L1"},
                       {"id": "a1", "label": "a-one", "bundle": "bA", "status": "running", "lane": "L1"},
                       {"id": "b1", "label": "b-one", "bundle": "bB", "status": "pending"}]}
        chk("slot kind: the coder victim is chosen over the gate",
            (q._run_now_victim(s2, s2["jobs"][2], ["L1"]) or {}).get("id"), "a1")
        s2g = {"jobs": [s2["jobs"][0], s2["jobs"][2]]}
        chk("slot kind: only a gate running -> nothing paused",
            q._run_now_victim(s2g, s2g["jobs"][1], ["L1"]), None)

        # --- (3) an idle earlier lane -> no pause on a later lane -----------------------
        seed()
        q._candidate_lanes = lambda job, w: ["L0", "L1"]
        try:
            q.promote_group_for_job("b2", run_now=True)
        finally:
            q._candidate_lanes = lambda job, w: ["L1"]
        chk("idle lane: nothing paused when a candidate lane is free", kills, [])
        chk("idle lane: still promoted + focused", (st().get("_focus_override") or {}).get("key"), "bB")

        # --- (4) refusals leave state untouched -----------------------------------------
        seed(launched_ago_s=10)
        before = q.STATE_PATH.read_text()
        try:
            q.promote_group_for_job("b2", run_now=True)
        except q.QueueActionError:
            pass
        chk("warm-up refusal: state untouched (no move, no pin)", q.STATE_PATH.read_text(), before)
        seed()
        q.STATE_PATH.write_text(json.dumps(dict(st(), jobs=[dict(j, status="held") if j["bundle"] == "bB"
                                                            else j for j in st()["jobs"]])))
        before = q.STATE_PATH.read_text()
        try:
            q.promote_group_for_job("b2", run_now=True)
            chk("no-pending refusal raised", "no error", "QueueActionError")
        except q.QueueActionError:
            chk("no-pending refusal raised", "QueueActionError", "QueueActionError")
        chk("no-pending refusal: state untouched (no pin)", q.STATE_PATH.read_text(), before)

        # --- min-progress guard holds (no force) -------------------------------------
        seed(launched_ago_s=10)
        try:
            q.promote_group_for_job("b2", run_now=True)
            chk("run now: a job in warm-up is refused", "no error", "QueueActionError")
        except q.QueueActionError as e:
            chk("run now: a job in warm-up is refused", "QueueActionError", "QueueActionError")
            chk("run now: refusal does not suggest --force", "--force" in str(e), False)
        chk("run now: refused -> nothing signalled", kills, [])
        chk("run now: refused -> victim not stamped", row(st(), "a1").get("preempt_intent"), None)

        # --- CLI: run-now <bundle key> -----------------------------------------------
        seed()
        sys.argv = ["ollama-queue.py", "run-now", "bB"]
        try:
            q.main()
        except SystemExit as e:
            chk("CLI run-now exits cleanly", e.code in (None, 0), True)
        chk("CLI run-now <bundle>: running job paused for it", kills, [424242])
        chk("CLI run-now <bundle>: focus on it", (st().get("_focus_override") or {}).get("key"), "bB")
    finally:
        os.kill = real_kill

    # --- API route + handler + dashboard button --------------------------------------
    api = load(API, "oqapi_runnow")
    calls = []

    class FakeH:
        out = None
        def _read_json_body(self):
            return {}
        def _json(self, obj):
            self.out = obj
        def _text(self, t, code=200):
            self.out = (code, t)

    api.q.promote_group_for_job = lambda jid, **kw: calls.append((jid, kw)) or {"truth": "now"}
    h = FakeH()
    fn = getattr(api.Handler, "_run_now", None)
    if fn is not None:
        fn(h, "m1")
    chk("API: _run_now delegates with run_now=True", calls[-1:] if calls else None,
        [("m1", {"run_now": True})])
    import inspect
    chk("API: POST /api/jobs/<id>/run-now is routed",
        'r"^/api/jobs/([^/]+)/run-now$"' in inspect.getsource(api.Handler.do_POST), True)
    html = api.FRONTEND_HTML
    chk("UI: bundle menu has a Run now button", "data-plan-run-now" in html, True)
    chk("UI: the button posts to /run-now", "'/run-now', {method: 'POST'}" in html, True)

    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
