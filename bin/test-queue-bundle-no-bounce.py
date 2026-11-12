#!/usr/bin/env python3
"""test-queue-bundle-no-bounce.py -- replay of the 2026-10-05 bundle-bounce incident.

THE INCIDENT (ollama-queue-daemon.log ~40720-40821). Bundle resell-bfmr-link-feedback
(LF) was COMMITTED. Its author round d78def02d2dd finished, and its chain driver was
"about to advance" (preflight r1). Nothing of LF was launchable for that tick, so the
idle-lane backfill let plan-gen bfe0699081c4 (bundle plan-gen-rt-bfmr-push-via-sidecar)
take studio-db. That job ran to its 12-iteration cap, and LF r1 waited behind it.
Later the regate of c228c9eb80fe wrote verdict CONCERNS and the chain ended. About 90s
later, on a timer, the queue logged "LF COMPLETE (nothing left) -- released" with no
park and no alert, then committed rt-giftcard-copy-remaining.

The owner's rule: a started bundle runs to completion. No other bundle's job takes a lane
while it is committed. A bundle stays working until its gate verdict is written. A
NON-PASS final verdict parks the bundle loudly, and that bundle resumes first once
cleared.

Everything here runs on STUBBED state: temp chain/slice/log dirs, an in-memory state
dict, and stub processes. DISPATCH_VERIFY_SANDBOX=1 and OLLAMA_QUEUE_NO_NOTIFY=1 are
set. No job is ever enqueued and no real queue state is touched.

Usage:  test-queue-bundle-no-bounce.py [--queue PATH] [--plan PATH] [--gate PATH]
Defaults are the installed ~/bin copies. The revert test passes the .bak files and
must FAIL. Exit 0 = all checks pass.
"""
import argparse
import datetime as _dt
import importlib.machinery
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("GATE_TEST_MODE", None)

BIN = Path.home() / "bin"
LF = "resell-bfmr-link-feedback"
PG = "plan-gen-rt-bfmr-push-via-sidecar"       # the tag the incident's plan-gen rows carry
GC = "rt-giftcard-copy-remaining"
NEW = "some-new-bundle"

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def guarded(name, fn):
    """Run one scenario. An exception (e.g. an API the old code lacks) is a FAIL,
    not a crash, so the revert run reports every check."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        RESULTS.append(False)
        print(f"FAIL {name}: raised {type(e).__name__}: {e}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def iso(t):
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Proc:
    """Stand-in for the gate-on-complete Popen handle."""
    def __init__(self):
        self.rc = None

    def poll(self):
        return self.rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(BIN / "ollama-queue.py"))
    ap.add_argument("--plan", default=str(BIN / "ollama-dispatch-plan"))
    ap.add_argument("--gate", default=str(BIN / "gate-on-complete.py"))
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="q-nobounce-"))
    chain_dir, slice_dir, log_dir = tmp / "auto-runs", tmp / "slice-runs", tmp / "logs"
    for d in (chain_dir, slice_dir, log_dir):
        d.mkdir()
    q = load(a.queue, "oq_nobounce")
    # Sandbox every path the commitment code can read or write.
    q.STATE_PATH, q.LOCK_PATH = tmp / "state.json", tmp / "state.lock"
    q.LOG_DIR, q.CHAIN_RUNS_DIR, q.SLICE_RUNS_DIR = log_dir, chain_dir, slice_dir
    if hasattr(q, "_GATE_HOOKS"):
        q._GATE_HOOKS.clear()
    alerts, kicks = [], []
    pk = lambda j: j.get("bundle")     # every incident row carried its bundle tag

    def tick(state, now, cands, running=None):
        k, ev = q._apply_bundle_commit(
            state, pk, running, list(cands), now, runs_dir=slice_dir, chain_dir=chain_dir,
            kick=lambda l, p: (kicks.append(l) or True),
            alert=lambda k_, w_, n_: alerts.append((k_, w_)))
        return k, [e[0] for e in ev]

    def hold_for(commit):
        # cmd_run's hold under a commitment. The new code factors it out as
        # commit_hold_decision. The old code computed it inline as
        # `not commit.get("waiting_on_failure")`, and that expression is reproduced
        # here only so the revert run measures the old behavior.
        if hasattr(q, "commit_hold_decision"):
            return q.commit_hold_decision(commit)
        return not (commit or {}).get("waiting_on_failure")

    def launched(state, commit_key):
        """cmd_run's launch-loop gate for one tick, on stubbed state: the jobs it would
        launch, in order (dependency/gate barriers are not in play here)."""
        jobs = state["jobs"]
        commit = state.get("_bundle_commit")
        hold = hold_for(commit)
        launchable = any(j.get("status") == "pending" and pk(j) == commit_key for j in jobs)
        bf = q.commit_backfill_ok(commit_key, launchable,
                                  [pk(j) for j in jobs if j.get("status") == "running"])
        out = []
        for j in jobs:
            if j.get("status") != "pending":
                continue
            if q.focus_skips_job(j, pk(j), commit_key, hold, commit_key, set(), None, None,
                                 backfill_ok=bf):
                continue
            out.append(j["id"])
            if pk(j) != commit_key:
                bf = False       # single-shot, as in cmd_run
            break                # one lane (studio-db)
        return out

    def chain(phase, job, now, pid=None):
        (chain_dir / f"{LF}.json").write_text(json.dumps({
            "key": LF, "label": "rt-bfmr-link-sync-feedback", "bundle": LF,
            "pid": pid, "phase": phase, "phase_since": iso(now), "updated_at": iso(now),
            "job": job, "rounds": ["d78def02d2dd", "1db468e4253c", "c228c9eb80fe"][:3],
            "outcome": "exit 0" if phase == "ended" else None}))

    T0 = time.time()
    plan_gen = {"id": "bfe0699081c4", "label": "plan-gen-rt-bfmr-push-via-sidecar-r1",
                "bundle": PG, "status": "pending"}
    giftcard = {"id": "76a1c6d6701a", "label": "rt-giftcard-copy-remaining",
                "bundle": GC, "status": "pending"}
    state = {"jobs": [plan_gen, giftcard],
             "_bundle_commit": {"key": LF, "since": T0, "empty_since": None,
                                "idle_since": None}}

    # --- 1. THE BOUNCE: LF between its author and r1 ---------------------------------
    def s1():
        chain("waiting", "d78def02d2dd", T0 + 5, pid=os.getpid())   # driver alive, about to advance
        k, ev = tick(state, T0 + 10, [PG, GC])
        check("1 between steps: LF stays committed (chain driver about to advance)", k, LF)
        check("1 between steps: NO other bundle's job takes the idle lane (plan-gen "
              "bfe0699081c4 must not run between LF's author and r1)", launched(state, k), [])
        r1 = {"id": "1db468e4253c", "label": "auto-refine-rt-bfmr-link-sync-feedback-r1",
              "bundle": LF, "status": "pending"}
        state["jobs"].append(r1)
        check("1 ...LF's r1 appears -> it is the job that launches", launched(state, LF),
              ["1db468e4253c"])
        stamped = {"id": "g-lf", "label": "gate-pruned-parent", "bundle": LF,
                   "status": "pending"}
        st2 = {"jobs": [stamped, plan_gen],
               "_bundle_commit": dict(state["_bundle_commit"])}
        # pk resolves the gate row elsewhere (its parent was pruned) but it carries LF's tag
        alt_pk = lambda j: "pruned-parent" if j["id"] == "g-lf" else j.get("bundle")
        bf = q.commit_backfill_ok(LF, False, [])
        check("1 same-bundle backfill kept: a row STAMPED with LF takes the idle lane",
              q.focus_skips_job(stamped, alt_pk(stamped), LF, True, LF, set(), None, None,
                                backfill_ok=bf), False)
        check("1 ...while a foreign bundle's row is still skipped with the same backfill_ok",
              q.focus_skips_job(plan_gen, PG, LF, True, LF, set(), None, None,
                                backfill_ok=bf), True)
        state["jobs"].remove(r1)
    guarded("1 bounce", s1)

    # --- 2. Waiting on a failure does not release the hold ---------------------------
    def s2():
        (slice_dir / "wf.json").write_text(json.dumps({
            "label": "wf", "plan_path": None, "repo": "/r", "order": ["s1", "s2"],
            "slices": {"s1": {"status": "done", "depends_on": []},
                       "s2": {"status": "failed", "depends_on": ["s1"]}}}))
        st = {"jobs": [dict(giftcard)],
              "_bundle_commit": {"key": "wf", "since": T0, "empty_since": None,
                                 "idle_since": None}}
        k, _ev = tick(st, T0 + 20, [GC])
        check("2 failed slice awaiting its retry: wf stays committed", k, "wf")
        check("2 ...it is recorded waiting_on_failure (logged state only)",
              bool(st["_bundle_commit"].get("waiting_on_failure")), True)
        check("2 ...and STILL holds: giftcard does not run mid-bundle", launched(st, k), [])
    guarded("2 waiting on failure", s2)

    # --- 3. Settlement is the tracked gate hook, not a 90s timer --------------------
    # GATE-SETTLE YIELD (f57ce5e, 2026-10-09): the hook runs OFF the GPU lane, so a committed
    # bundle holds the lane only while the hook is FRESH (< SLICER_GAP_YIELD_S), then parks
    # cpu_wait (no alert, never "complete") and resumes FIRST on its verdict row. What must
    # never happen: the bundle is dropped as complete by a timer, or a foreign job runs while
    # the hook is fresh (the original bounce window).
    proc = Proc()

    def s3():
        state["jobs"] = [plan_gen, giftcard]           # all LF rows done and pruned
        chain("ended", "c228c9eb80fe", T0 + 600)
        (log_dir / "c228c9eb80fe.gate.json").write_text(json.dumps({
            "job_id": "c228c9eb80fe", "verdict": "concerns", "regate": "done",
            "auto_fix_action": "none", "ts": iso(T0 + 590)}))
        if hasattr(q, "register_gate_hook"):
            # on the TEST clock (tick() runs at T0+600), not wall time
            q.register_gate_hook("regate-c228c9eb80fe-x", LF, proc=proc, now=T0 + 600)
        k, ev = tick(state, T0 + 600, [PG, GC])
        check("3 regate finished, its gate-on-complete hook still running -> LF holds",
              (k, "complete" in ev), (LF, False))
        check("3 ...and no foreign job takes the idle lane while the hook is fresh", launched(state, k), [])
        k, ev = tick(state, T0 + 600 + 20, [PG, GC])
        check("3 ...still holding 20s later (inside the yield window)",
              (k, "complete" in ev, "cpu_wait" in ev), (LF, False, False))
        k, ev = tick(state, T0 + 600 + 91, [PG, GC])
        check("3 ...91s into the hook: LF yields the idle lane (parked cpu_wait), NEVER "
              "'complete' (no timer drops it)",
              ("complete" in ev, "cpu_wait" in ev, LF in state.get("_bundle_parked", {}),
               (state.get("_bundle_parked", {}).get(LF) or {}).get("kind")),
              (False, True, True, "cpu_wait"))
        check("3 ...and a yielded LF is not alerted on (a wait is not a failure)",
              any(x[0] == LF for x in alerts), False)
        check("3 ...the yield goes to a bundle with real work", k in (PG, GC), True)
    guarded("3 tracked settlement", s3)

    # --- 4. Final CONCERNS parks loudly, never COMPLETE -----------------------------
    def s4():
        proc.rc = 0                                     # the hook wrote the verdict and exited
        n0 = len(alerts)
        k, ev = tick(state, T0 + 700, [PG, GC])
        check("4 hook exited, final verdict CONCERNS -> LF PARKED loudly even after yielding (not complete; parks loudly)",
              ("park" in ev, "complete" in ev, LF in state.get("_bundle_parked", {})),
              (True, False, True))
        check("4 ...with a LOUD alert naming LF and the verdict",
              any(x[0] == LF and "CONCERNS" in x[1] for x in alerts[n0:]), True)
        check("4 ...and the queue moves on to the next bundle", k in (PG, GC), True)
    guarded("4 park on non-pass", s4)

    # --- 5. A parked LF resumes FIRST once cleared ----------------------------------
    def s5():
        k0 = state["_bundle_commit"]["key"] if state.get("_bundle_commit") else None
        state["jobs"] = [{"id": "nw1", "label": "nw-s1", "bundle": NEW, "status": "pending"},
                         {"id": "lf-r3", "label": "auto-refine-rt-bfmr-link-sync-feedback-r3",
                          "bundle": LF, "status": "pending"}]
        k, ev = tick(state, T0 + 800, [NEW, LF])
        check("5 new LF work appears (a human re-ran it) -> LF RESUMES before the new "
              "bundle", (k, "resume" in ev), (LF, True))
        check("5 ...and its job is the one launched", launched(state, k), ["lf-r3"])
        check("5 (the bundle moved on to before LF was cleared was not LF)", k0 != LF, True)
    guarded("5 resume first", s5)

    # --- 6. Clearing by human accept ------------------------------------------------
    def s6():
        chain("ended", "c228c9eb80fe", T0 + 600)
        st = {"jobs": [dict(giftcard)], "_bundle_commit": None,
              "_bundle_parked": {LF: {"since": T0 + 700, "why": "x", "kind": "blocked",
                                      "commit_since": T0}}}
        k, ev = tick(st, T0 + 900, [GC])
        check("6 parked on CONCERNS, nothing new: LF STAYS parked (never silently "
              "dropped as complete)", ("unpark" in ev, LF in st["_bundle_parked"]),
              (False, True))
        st["_bundle_accepted"] = {LF: "c228c9eb80fe"}
        st["jobs"] = []              # giftcard finished (parks are re-judged between bundles)
        k, ev = tick(st, T0 + 960, [])
        check("6 human accept-bundle -> LF reads complete and is dropped from the park",
              ("unpark" in ev, LF in st["_bundle_parked"]), (True, False))
        check("6 an OLD verdict (before the commitment started) does not park newer work",
              q.chain_final_nonpass(LF, {"known": True, "phase": "ended",
                                         "job": "c228c9eb80fe"}, {}, T0 + 10_000, log_dir)
              if hasattr(q, "chain_final_nonpass") else "missing", None)
        check("6 accept-bundle is a CLI subcommand",
              "accept-bundle" in inspect.getsource(q.main) if hasattr(q, "main") else False,
              True)
    guarded("6 accept", s6)

    # --- 7. Daemon wiring (the pure pieces above are what cmd_run uses) -------------
    def s7():
        src = inspect.getsource(q.cmd_run)
        check("7 cmd_run takes its hold from commit_hold_decision",
              "_hold = commit_hold_decision(" in src, True)
        check("7 cmd_run registers this tick's gated jobs before the commitment step",
              src.find("register_gate_hook(") != -1
              and src.find("register_gate_hook(") < src.find("_apply_bundle_commit("), True)
        check("7 the gate hook Popen is tracked",
              "register_gate_hook(job.get(\"id\"), bundle, proc=_hp)"
              in inspect.getsource(q._fire_gate_on_complete), True)
    guarded("7 wiring", s7)

    # --- 8. Tagging gaps ---------------------------------------------------------------
    def s8():
        check("8 plan-gen-<label>-rN defaults to the SAME bundle as the slices (<label>)",
              q.default_bundle_from_label("plan-gen-rt-bfmr-push-via-sidecar-r1"),
              "rt-bfmr-push-via-sidecar")
    guarded("8 default bundle", s8)


    def s8c():
        g = load(a.gate, "goc_nobounce")
        wt = tmp / "wt"
        wt.mkdir()
        (wt / "TASK.md").write_text("t")
        (wt / "verify.sh").write_text("true")
        runs = []

        class _R:
            def __init__(self):
                self.returncode, self.stdout, self.stderr = 0, "enqueued abc", ""
        g.subprocess = types.SimpleNamespace(
            run=lambda cmd, **k: (runs.append(list(cmd)) or _R()))
        fields = {"cwd": str(wt), "label": "auto-author-rt-x", "bundle": LF}
        g._job_field = lambda jid, key: fields.get(key)
        g._auto_confirm_risk = lambda cwd, payload, **k: ("low", [])
        g._coding_dispatch_params = lambda jid, payload: {"model": "m", "host": "studio"}
        g.AUTO_PIPELINE_MODE, g.TEST_MODE = "live", False
        gj = tmp / "x.gate.json"
        g.advance_to_coding("jx", {}, gj)
        enq = [c for c in runs if "enqueue" in c]
        check("8 gate-on-complete auto-pipeline is retired: no coding enqueue (use mini-swe)", len(enq), 0)
    guarded("8 gate coding --bundle", s8c)

    shutil.rmtree(tmp, ignore_errors=True)
    n_ok = sum(RESULTS)
    print(f"\n{n_ok}/{len(RESULTS)} checks passed")
    print("NO_BOUNCE_TEST_OK" if all(RESULTS) else "NO_BOUNCE_TEST_FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
