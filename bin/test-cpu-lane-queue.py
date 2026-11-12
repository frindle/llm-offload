#!/usr/bin/env python3
"""test-cpu-lane-queue.py -- an outstanding CPU stage (remote Unraid runner job, local-stage
marker) must NOT hold the GPU lanes or the bundle
commitment (CPU LANE, 2026-10-08).

Proven on stubbed state (DISPATCH_VERIFY_SANDBOX=1; the real queue, its state file and the
real cpu-jobs store are never touched):
  1. bundle_commit_status: no running/live row + a cpu_wait -> "waiting"; with a RUNNING or a
     live (pending) row the bundle is still "working"; without cpu_wait nothing changed;
  2. a committed bundle that is only waiting on its CPU stage releases the commitment (parked
     kind cpu_wait, NO alert) and the next bundle commits and launches;
  3. the waiting bundle resumes FIRST (before a brand-new bundle) once its result lands and it
     has a row again;
  4. a gate-on-complete hook alone still holds its bundle (nobounce); a CPU stage inside it yields;
  5. the real store feeds it: outstanding_by_bundle() lists uploading/pending/running remote
     jobs and live local markers, drops done/cancelled/expired ones and aged-out orphans.

Usage: test-cpu-lane-queue.py [--queue PATH] [--lane PATH]. Exit 0 = all pass.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(Path.home() / "bin" / "ollama-queue.py"))
    ap.add_argument("--lane", default=str(Path.home() / "bin" / "cpu_lane.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="q-cpulane-"))
    os.environ["CPU_LANE_DIR"] = str(tmp / "cpu-jobs")
    sys.path.insert(0, str(Path(a.lane).parent))
    q = load(a.queue, "oq_cpulane")
    q.STATE_PATH, q.LOCK_PATH, q.LOG_DIR = tmp / "state.json", tmp / "state.lock", tmp / "logs"
    pk = lambda j: j.get("bundle")
    rd = tmp / "runs"
    rd.mkdir()

    # ---- 1. pure status -------------------------------------------------------------
    st = lambda jobs, **kw: q.bundle_commit_status("bA", jobs, pk, {}, {}, **kw)[0]
    check("1 status: only a CPU stage in flight -> waiting (holds no lane)", st([], cpu_wait=["j1"]), "waiting")
    check("1 status: ...a RUNNING row of it -> still working",
          st([{"bundle": "bA", "status": "running"}], cpu_wait=["j1"]), "working")
    check("1 status: ...a live pending row of it -> still working",
          st([{"bundle": "bA", "status": "pending"}], cpu_wait=["j1"]), "working")
    check("1 status: control -- a live driver with NO cpu_wait stays working (unchanged behaviour)",
          q.bundle_commit_status("bA", [], pk, {"driver_live": True}, {})[0], "working")
    check("1 status: ...but with a cpu_wait the same driver-only bundle is waiting",
          q.bundle_commit_status("bA", [], pk, {"driver_live": True}, {}, cpu_wait=["j1"])[0], "waiting")

    # ---- 2/3. the commitment ----------------------------------------------------------
    alerts = []
    state = {"jobs": [], "_bundle_commit": {"key": "bA", "since": 0.0, "empty_since": None, "idle_since": None}}

    def tick(now, running=None, cands=(), cpu=None, hooks=None):
        k, ev = q._apply_bundle_commit(state, pk, running, list(cands), now, runs_dir=rd, chain_dir=rd,
                                       kick=lambda l, p: True, alert=lambda k_, w_, n_: alerts.append((k_, w_)),
                                       hooks={} if hooks is None else hooks, log_dir=rd,
                                       cpu_outstanding=cpu or {})
        return k, [e[0] for e in ev]

    b1 = {"id": "b1", "label": "bB-s1", "bundle": "bB", "status": "pending"}
    state["jobs"] = [b1]
    k, ev = tick(10.0, cands=["bB"], cpu={"bA": ["cpu1"]})
    check("2 A (committed) only waits on its CPU job -> lanes go to B", (k, ev), ("bB", ["cpu_wait", "commit"]))
    check("2 ...A parked as cpu_wait, NOT alerted (not a failure)",
          ((state["_bundle_parked"].get("bA") or {}).get("kind"), alerts), ("cpu_wait", []))
    check("2 ...so the launch loop launches B's job (not skipped by the commitment)",
          q.focus_skips_job(b1, "bB", "bB", True, k, set(), None, None), False)
    b1["status"] = "running"
    k, ev = tick(20.0, running="bB", cands=[], cpu={"bA": ["cpu1"]})
    check("2 B running, A's CPU job still out -> B keeps the lanes, A stays parked", (k, "bA" in state["_bundle_parked"]),
          ("bB", True))
    # the cpu result lands and A's next row appears while B still runs: B's RUNNING job is not preempted
    # (2026-10-09: the commitment goes back to A at once -- B is parked 'yielded', its later jobs wait --
    # so A's next model job waits only for B's running job, not B's whole bundle; was: B kept it to completion)
    a2 = {"id": "a2", "label": "bA-s2", "bundle": "bA", "status": "pending"}
    state["jobs"] = [a2, b1]
    k, ev = tick(30.0, running="bB", cands=["bA"], cpu={})
    check("3 result landed + A has a row again, B running -> commitment returns to A; B's running job is not preempted, B parked 'yielded'",
          (k, ev[:1], (state["_bundle_parked"].get("bB") or {}).get("kind")), ("bA", ["yield"], "yielded"))
    state["jobs"] = [a2, {"id": "c1", "label": "bC-s1", "bundle": "bC", "status": "pending"}]   # B finished
    k, ev = tick(40.0, cands=["bC", "bA"], cpu={})
    check("3 B done -> A (already committed) still owns the lanes, ahead of the brand-new bundle C", k, "bA")
    check("3 ...never alerted along the way", alerts, [])

    # ---- 4. gate hook: alone it still holds its bundle (nobounce); with a CPU stage inside it, it yields
    proc = type("P", (), {"poll": lambda self: None})()
    hk = {"hA": {"bundle": "bA", "at": 499.0, "proc": proc}}
    cs = lambda: {"jobs": [{"id": "c1", "label": "bC-s1", "bundle": "bC", "status": "pending"}],
                  "_bundle_commit": {"key": "bA", "since": 0.0, "empty_since": None, "idle_since": None}}
    state2 = cs()
    k, ev = q._apply_bundle_commit(state2, pk, None, ["bC"], 500.0, runs_dir=rd, chain_dir=rd,
                                   kick=lambda l, p: True, alert=lambda *x: alerts.append(x),
                                   hooks=hk, log_dir=rd, cpu_outstanding={})
    check("4 A's gate hook alone STILL holds A (nobounce policy unchanged)", (k, [e[0] for e in ev]), ("bA", []))
    state2 = cs()
    k, ev = q._apply_bundle_commit(state2, pk, None, ["bC"], 500.0, runs_dir=rd, chain_dir=rd,
                                   kick=lambda l, p: True, alert=lambda *x: alerts.append(x),
                                   hooks=hk, log_dir=rd, cpu_outstanding={"bA": ["cpu-relevance"]})
    check("4 ...but the hook's relevance-mutation CPU stage (registered via run_cpu_stage) releases the lanes to C",
          (k, [e[0] for e in ev]), ("bC", ["cpu_wait", "commit"]))

    # ---- 5. the store -------------------------------------------------------------------
    import cpu_lane
    clk = {"t": 1000.0}
    s = cpu_lane.Store(tmp / "own", clock=lambda: clk["t"])
    jid = s.create({"cmd": "true", "timeout_s": 60}, "l", "verify", "bA")
    check("5 store: an uploading remote job counts", s.outstanding_by_bundle(), {"bA": [jid]})
    s.cancel(jid)
    check("5 store: ...cancelled -> no longer counts", s.outstanding_by_bundle(), {})
    j2 = s.create({"cmd": "true", "timeout_s": 60}, "l", "verify", "bB")
    clk["t"] += 3600
    check("5 store: an orphan older than timeout+30min is ignored (a crashed caller cannot wedge a bundle)",
          s.outstanding_by_bundle(), {})
    loc = s.begin_local("preflight", "bC", 100)
    check("5 store: a live local-stage marker counts", s.outstanding_by_bundle(), {"bC": [loc]})
    s.end_local(loc, 0)
    check("5 store: ...ended -> gone", s.outstanding_by_bundle(), {})
    loc2 = s.begin_local("slicer", "bD", 10)
    clk["t"] += 500
    check("5 store: an expired local marker (crashed caller) is ignored", s.outstanding_by_bundle(), {})
    # the real default store the queue daemon reads (env-pointed to tmp above)
    real = cpu_lane.Store(cpu_lane.BASE)
    r = real.begin_local("final-verify", "bZ", 60)
    check("5 queue: cpu_outstanding_by_bundle() reads the store", q.cpu_outstanding_by_bundle(), {"bZ": [r]})
    check("5 queue: a missing/broken store -> {} (queue behaves as before)",
          (os.environ.__setitem__("CPU_LANE_DIR", "/nonexistent/x"), cpu_lane.outstanding_by_bundle("/nonexistent/x"))[1], {})

    # ---- 6. a CPU-ONLY CHAIN STAGE (preflight) does not pin the GPU lanes (2026-10-09, rt-card-bonus) ----
    import json as _json
    import time as _time
    now0 = _time.time()
    iso = lambda t_: _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(t_))   # noqa: E731

    def chain_file(key, step):
        (rd / f"{key}.json").write_text(_json.dumps({"key": key, "pid": os.getpid(), "phase": "advancing",
                                                      "phase_since": iso(now0 - 30), "updated_at": iso(now0 - 30),
                                                      "step": step, "job": None, "outcome": None}))
    check("6 step classifier: preflight/self-check/harness-check/relevance are CPU-only; enqueue/author/start are not",
          [q.chain_step_cpu_only(x) for x in ("preflight r2", "author continuation c1 self-check", "harness-check",
                                              "relevance", "enqueue x", "author stage t try 1", "start", None)],
          [True, True, True, True, False, False, False, False])
    chain_file("bP", "preflight r2")
    cp = q.chain_run_progress("bP", runs_dir=rd)
    check("6 chain_run_progress: live driver on a preflight step -> driver_live AND cpu_only",
          (cp["driver_live"], cp["cpu_only"]), (True, True))
    check("6 status: nothing running/live, only the preflight stage -> 'waiting', not moving",
          q.bundle_commit_status("bP", [], pk, {}, cp)[0::2], ("waiting", False))
    chain_file("bE", "enqueue auto-refine-x-r2")
    ce = q.chain_run_progress("bE", runs_dir=rd)
    check("6 status: control -- the same driver on an 'enqueue' step still holds the lane (working, moving)",
          q.bundle_commit_status("bE", [], pk, {}, ce)[0::2], ("working", True))
    check("6 status: control -- a RUNNING model row keeps it working during a preflight step",
          q.bundle_commit_status("bP", [{"bundle": "bP", "status": "running"}], pk, {}, cp)[0], "working")
    # full tick: bP committed, only preflighting; bQ has runnable work -> bQ gets the lanes (the 17-min idle Studio lane)
    alerts.clear()
    state = {"jobs": [{"id": "q1", "label": "bQ-s1", "bundle": "bQ", "status": "pending"}],
             "_bundle_commit": {"key": "bP", "since": now0 - 100, "empty_since": None, "idle_since": None}}
    k, ev = tick(now0, cands=["bQ"])
    check("6 tick: committed bundle in a CPU-only stage releases the lanes; the other bundle commits",
          (k, ev, (state["_bundle_parked"].get("bP") or {}).get("kind"), alerts), ("bQ", ["cpu_wait", "commit"], "cpu_wait", []))
    check("6 tick: ...and the launch loop would launch bQ's job",
          q.focus_skips_job(state["jobs"][0], "bQ", "bQ", True, k, set(), None, None), False)
    state["jobs"][0]["status"] = "running"
    k, ev = tick(now0 + 5, running="bQ", cands=[])
    check("6 tick: bQ's job running while bP still preflights -> bQ keeps the commitment", (k, ev), ("bQ", []))
    # preflight done: bP's next model row exists while bQ's job is still running
    (rd / "bP.json").unlink()
    state["jobs"].append({"id": "p3", "label": "bP-r3", "bundle": "bP", "status": "pending"})
    k, ev = tick(now0 + 10, running="bQ", cands=["bP"])
    check("6 tick: bP's next model job is ready -> bP takes the commitment back at once (bQ's running job is not "
          "preempted, bQ is parked 'yielded' and resumes after)",
          (k, ev[:1], (state["_bundle_parked"].get("bQ") or {}).get("kind"), "bP" in state["_bundle_parked"]),
          ("bP", ["yield"], "yielded", False))
    check("6 tick: ...bQ's later jobs are held again (never interleave once the committed bundle has model work)",
          q.focus_skips_job({"id": "q2", "label": "bQ-s2", "bundle": "bQ", "status": "pending"}, "bQ", "bP", True, k,
                            set(), None, None), True)

    bad = RESULTS.count(False)
    print("CPU_LANE_QUEUE_TEST_OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
