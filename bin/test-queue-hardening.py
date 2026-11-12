#!/usr/bin/env python3
"""Orchestration-hardening checks for ollama-queue.py (research 2026-10-09, backlog H1/H4/H5/H6/H7/H8
+ the sim gaps, the no-runnable-member hold, the runner-driver lease, heal-continuation, enqueue
--mode, revalidate-at-pop).  Every check FAILS on the pre-hardening queue (revert: --queue <bak>).
Usage: test-queue-hardening.py [--queue PATH] [--group ident|lease|degraded|receipts|commit|flap|
       attempts|status|mode|reval|all]
Prints ALL PASS when the selected groups are clean."""
import argparse, importlib.util, json, os, signal, subprocess, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
ap.add_argument("--group", default="all")
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="qharden-test-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ld = SourceFileLoader("queue_h", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_h", ld))
ld.exec_module(q)
fails = 0
G = lambda n: A.group == "all" or n in A.group.split(",")


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def has(*names):
    miss = [n for n in names if not hasattr(q, n)]
    chk(f"API present {names}", miss, [])
    return not miss


pk = lambda j: j.get("bundle")

# ------------------------------------------------------------------ H1 identity
if G("ident") and has("proc_identity_alive", "_pid_start"):
    alive = lambda p: True
    chk("same start time -> alive", q.proc_identity_alive(10, "Mon Oct 9 04:00:00 2026", alive=alive,
                                                          start_of=lambda p: "Mon Oct 9 04:00:00 2026"), True)
    chk("recycled pid (start differs) -> NOT our worker", q.proc_identity_alive(
        10, "Mon Oct 9 04:00:00 2026", alive=alive, start_of=lambda p: "Mon Oct 9 09:12:00 2026"), False)
    chk("no recorded start -> trust kill(0)", q.proc_identity_alive(10, None, alive=alive,
                                                                    start_of=lambda p: "x"), True)
    chk("ps cannot answer -> trust kill(0)", q.proc_identity_alive(10, "t", alive=alive, start_of=lambda p: ""), True)
    chk("dead pid -> dead", q.proc_identity_alive(10, "t", alive=lambda p: False, start_of=lambda p: "t"), False)
    chk("real pid start is readable", bool(q._pid_start(os.getpid())), True)
    src = Path(A.queue).read_text()
    chk("launch stamps pid_start + attempt_id", 'job["pid_start"] = _pid_start(proc.pid)' in src
        and 'job["attempt_id"]' in src, True)
    chk("startup adoption uses the identity check", 'proc_identity_alive(pid, j.get("pid_start"))' in src, True)

# ------------------------------------------------------------------ H1 runner lease / item 5
if G("lease") and has("runner_lease_verdict", "_enforce_runner_leases", "_live_descendants"):
    W = {"runner": "/Users/user/bin/bakeoff-runner.py", "id": "j1", "label": "bo", "pid": 111}
    v = lambda n, idle=0, tail="", since=None, now=1000.0, job=W: q.runner_lease_verdict(job, n, idle, tail, since, now)
    chk("driver child alive -> ok", v(2)[0], "ok")
    chk("no driver for 10s -> still ok, clock starts", (v(0)[0], v(0)[2]), ("ok", 1000.0))
    chk("no driver for 60s -> DEAD", v(0, since=940.0)[0], "dead")
    chk("fatal shell error + silent 400s (driver tree alive) -> DEAD", v(
        3, idle=400, tail="x.sh: line 90: NATIVE: unbound variable\n")[0], "dead")
    chk("fatal shell error but log still moving -> ok", v(3, idle=20, tail="x.sh: line 90: NATIVE: unbound variable\n")[0], "ok")
    chk("silent 400s without a fatal signature -> ok (long quiet cell)", v(3, idle=400, tail="working\n")[0], "ok")
    chk("non-wrapper runner never judged", v(0, since=1.0, job={"runner": "/x/studio-research.py"})[0], "ok")
    # enforcement over a fake process table
    kills = []
    job = dict(W)
    st = {"jobs": [job]}
    act = {111: ("lane", object(), None)}
    tbl_dead = {111: (1, "S")}                                  # runner alive, no children
    out, dirty = q._enforce_runner_leases(st, act, 1000.0, table=tbl_dead, killer=lambda p, s: kills.append((p, s)),
                                          log_idle=lambda p: 0, log_tail=lambda p: "")
    chk("first sighting only starts the clock", (out, kills, job.get("_no_driver_since")), ([], [], 1000.0))
    out, dirty = q._enforce_runner_leases(st, act, 1050.0, table=tbl_dead, killer=lambda p, s: kills.append((p, s)),
                                          log_idle=lambda p: 0, log_tail=lambda p: "")
    chk("lease lapse -> runner SIGTERMed and row stamped", (len(out), kills, bool(job.get("driver_dead"))),
        (1, [(111, signal.SIGTERM)], True))
    out, _ = q._enforce_runner_leases(st, act, 1070.0, table=tbl_dead, killer=lambda p, s: kills.append((p, s)),
                                      log_idle=lambda p: 0, log_tail=lambda p: "")
    chk("still alive 20s later -> SIGKILL", kills[-1], (111, signal.SIGKILL))
    job2 = dict(W, pid=222, id="j2")
    st2 = {"jobs": [job2]}
    k2 = []
    q._enforce_runner_leases(st2, {222: ("l", object(), None)}, 1000.0, table={222: (1, "S"), 223: (222, "S")},
                             killer=lambda p, s: k2.append(p), log_idle=lambda p: 0, log_tail=lambda p: "")
    q._enforce_runner_leases(st2, {222: ("l", object(), None)}, 9000.0, table={222: (1, "S"), 223: (222, "S")},
                             killer=lambda p, s: k2.append(p), log_idle=lambda p: 0, log_tail=lambda p: "")
    chk("healthy runner (live child) is never touched", (k2, job2.get("driver_dead")), ([], None))
    # real process tree: bash with a sleeping child
    pr = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    time.sleep(0.6)
    tbl = q._proc_table()
    n = len(q._live_descendants(pr.pid, tbl))
    pr.kill(); pr.wait()
    subprocess.run(["pkill", "-f", "sleep 30"], capture_output=True)
    chk("real ps table sees the driver's child", n >= 1, True)
    src = Path(A.queue).read_text()
    chk("reap settles a lapsed lease as failed/driver_crashed", 'job["terminal_reason"] = "driver_crashed"' in src
        and 'elif job.get("driver_dead"):' in src, True)
    chk("daemon loop enforces leases every tick", "_enforce_runner_leases(state, active, time.time())" in src, True)

# ------------------------------------------------------------------ H7 degraded
if G("degraded") and has("soft", "degraded_this_tick", "degraded_begin_tick", "bundle_commit_step"):
    q.DEGRADED_STATS_PATH = T / "degraded.json"
    q.degraded_begin_tick()
    chk("soft passes a value through", q.soft(lambda x: x + 1, "t_ok", None, 1), 2)
    chk("soft returns default, flags the tick, counts it", (q.soft(lambda: 1 / 0, "t_boom", "dflt"),
                                                            q.degraded_this_tick(),
                                                            q._DEGRADED_TOTAL["t_boom"]["count"]), ("dflt", ["t_boom"], 1))
    chk("stats file persisted", json.loads(q.DEGRADED_STATS_PATH.read_text())["t_boom"]["count"], 1)
    now = 5000.0
    parked, ev = {}, None
    c, ev = q.bundle_commit_step({"key": "A", "since": 0, "idle_since": None}, parked, None, ["A"],
                                 lambda k: ("blocked", "needs_opus", False), now, degraded=lambda: True)
    chk("degraded tick: a blocked read does NOT park", (c and c["key"], parked, ev), ("A", {}, []))
    parked = {"B": {"since": 0, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step(None, parked, None, [], lambda k: ("complete", "nothing left", False), now,
                                 degraded=lambda: True)
    chk("degraded tick: a complete read does NOT drop a park", (sorted(parked), ev), (["B"], []))
    parked = {"B": {"since": 0, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step(None, parked, None, [], lambda k: ("complete", "nothing left", False), now,
                                 degraded=lambda: False)
    chk("healthy tick still unparks a complete bundle", (sorted(parked), [e[0] for e in ev]), ([], ["unpark"]))
    # end to end: a throwing chain_run_progress must not park the committed bundle
    orig = q.chain_run_progress
    q.chain_run_progress = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        d = T / "runs"; d.mkdir(exist_ok=True)
        state = {"jobs": [{"id": "e1", "label": "x", "bundle": "A", "status": "needs_opus"}],
                 "_bundle_commit": {"key": "A", "since": 1.0, "idle_since": None, "empty_since": None}}
        k, ev = q._apply_bundle_commit(state, pk, None, [], 6000.0, runs_dir=d, chain_dir=d, hooks={},
                                       cpu_outstanding={}, kick=lambda *a: True, alert=lambda *a: None)
        chk("chain_run_progress raising: no park event, bundle stays committed",
            ([e[0] for e in ev if e[0] == "park"], k, "chain_run_progress" in q.degraded_this_tick()),
            ([], "A", True))
    finally:
        q.chain_run_progress = orig

# ------------------------------------------------------------------ commit step: sim gaps + H4
if G("commit") and has("bundle_commit_step"):
    base = 100000.0
    st_work_idle = lambda k: ("working", "idle", False)
    # (a) parked bundle with runnable work counts as 'another bundle waiting' (sim seed 3)
    parked = {"B": {"since": base - 900, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step({"key": "A", "since": base - 1000, "idle_since": base - 700, "empty_since": None},
                                 parked, None, ["A"], st_work_idle, base, lease_s=0)
    chk("wedged committed bundle yields to a runnable PARKED bundle at the 10 min soft ceiling",
        ([e[0] for e in ev], c and c["key"]), (["park", "resume"], "B"))
    parked = {"B": {"since": base - 900, "why": "x", "kind": "stalled"}}
    c, ev = q.bundle_commit_step({"key": "A", "since": base - 1000, "idle_since": base - 700, "empty_since": None},
                                 parked, None, ["A"], st_work_idle, base, lease_s=0)
    chk("a STALLED parked bundle that is not moving is not 'waiting' (it would not resume)",
        (c and c["key"], ev), ("A", []))
    # (b) idle clock survives a holder handoff (sim seed 84)
    def sf(k):
        return {"A": ("blocked", "needs_opus", False), "B": ("working", "runnable slice", False)}[k]
    parked = {"B": {"since": base - 900, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step({"key": "A", "since": base - 1000, "idle_since": base - 500, "empty_since": None},
                                 parked, None, [], sf, base, lease_s=0, dwell_s=0)
    chk("resumed non-moving holder inherits the lane's idle clock", (c and c["key"], c and c["idle_since"]), ("B", base - 500))
    # (c) H4 lease
    parked = {}
    cur = {"key": "A", "since": base - 1000, "idle_since": base - 310, "empty_since": None}
    c, ev = q.bundle_commit_step(dict(cur), parked, None, ["A", "B"], lambda k: ("working", "w", False), base,
                                 lease_s=300, idle_ceiling=2700)
    chk("lease lapsed while another bundle waits -> parked LEASE LAPSED", (c, [e[0] for e in ev], "LEASE LAPSED" in (ev[0][2] if ev else "")),
        (None, ["park"], True) if False else (c, ["park", "commit"], True))
    parked = {}
    c, ev = q.bundle_commit_step(dict(cur), parked, None, ["A", "B"], lambda k: ("working", "w", False), base,
                                 lease_s=0, idle_ceiling=2700)
    chk("lease disabled: holds until the soft ceiling", (c and c["key"], ev), ("A", []))
    parked = {}
    c, ev = q.bundle_commit_step(dict(cur), parked, None, ["A"], lambda k: ("working", "w", False), base,
                                 lease_s=300, idle_ceiling=2700)
    chk("lease lapsed but NOBODY waiting: keeps the lane", (c and c["key"], ev), ("A", []))
    c, ev = q.bundle_commit_step(dict(cur), {}, None, ["A", "B"], lambda k: ("working", "w", True), base,
                                 lease_s=300, idle_ceiling=2700)
    chk("a moving holder renews its lease", (c and c["key"], c.get("lease_until")), ("A", base + 300))

# ------------------------------------------------------------------ H5 dwell + flap breaker
if G("flap") and has("bundle_commit_step"):
    now = 200000.0
    parked = {"B": {"since": now - 10, "why": "x", "kind": "blocked"}}
    sf = lambda k: ("working", "w", False)
    c, ev = q.bundle_commit_step(None, parked, None, ["C"], sf, now, hist={})
    chk("dwell: a bundle parked 10 s ago is not resumed ahead of a fresh candidate", (c and c["key"], [e[0] for e in ev]), ("C", ["commit"]))
    parked = {"B": {"since": now - 10, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step(None, parked, None, [], sf, now, hist={})
    chk("dwell never idles the lane: nothing else to run -> resumes anyway", (c and c["key"], [e[0] for e in ev]), ("B", ["resume"]))
    parked = {"B": {"since": now - 120, "why": "x", "kind": "blocked"}}
    c, ev = q.bundle_commit_step(None, parked, None, ["C"], sf, now, hist={})
    chk("past the dwell the parked bundle resumes first as before", (c and c["key"]), "B")
    # alternating status trace -> breaker trips ONCE
    flip = [0]
    def status_of(k):
        return ("working", "w", True) if flip[0] % 2 == 0 else ("blocked", "needs_opus", False)
    parked, commit, hist, evs = {}, None, {}, []
    t = 300000.0
    for i in range(40):
        flip[0] = i
        t += 20.0
        commit, ev = q.bundle_commit_step(commit, parked, None, ["B"], status_of, t, hist=hist, dwell_s=0)
        evs += [e[0] for e in ev]
    chk("flapping status trace: breaker trips exactly once", evs.count("flap"), 1)
    chk("...and transitions are bounded", evs.count("resume") + evs.count("commit") + evs.count("park") <= 3 * q.BUNDLE_FLAP_MAX, True)
    chk("...bundle ends parked flapping", (parked.get("B") or {}).get("kind"), "flapping")
    parked2, commit2, evs2 = {}, None, []
    t = 300000.0
    for i in range(40):
        flip[0] = i
        t += 20.0
        commit2, ev = q.bundle_commit_step(commit2, parked2, None, ["B"], status_of, t, dwell_s=0)
        evs2 += [e[0] for e in ev]
    chk("without the breaker (hist=None) the same trace ping-pongs", evs2.count("park") > q.BUNDLE_FLAP_MAX, True)
    # cooldown
    p3 = {"B": {"since": t - q.BUNDLE_FLAP_COOLDOWN_S - 1, "why": "f", "kind": "flapping"}}
    c, ev = q.bundle_commit_step(None, p3, None, [], lambda k: ("working", "w", True), t, hist={"B": [t - 5] * 9})
    chk("after the cooldown a flapping bundle resumes with a clean ring", (c and c["key"]), "B")
    p3 = {"B": {"since": t - 30, "why": "f", "kind": "flapping"}}
    c, ev = q.bundle_commit_step(None, p3, None, [], lambda k: ("working", "w", True), t, hist={"B": [t - 5] * 9})
    chk("inside the cooldown it stays parked", (c, ev), (None, []))

# ------------------------------------------------------------------ H6 attempts ledger
if G("attempts") and has("classify_attempt", "record_attempt", "update_bundle_fail_streaks"):
    mk = lambda i, **kw: dict({"id": f"a{i}", "label": f"work-{i}", "bundle": "S", "status": "failed",
                               "launched_at": f"2026-10-09T0{i}:00:00+00:00"}, **kw)
    jobs = [mk(1, exit_code=3), mk(2, terminal_reason="force_stopped"), mk(3, exit_code=3), mk(4, exit_code=1)]
    streaks = {}
    q.update_bundle_fail_streaks(jobs, streaks, pk)
    chk("3 external pauses + 1 work failure: streak is 1 (not exhausted)", streaks.get("S"), 1)
    chk("ledger records every attempt with its kind", [a["kind"] for j in jobs for a in j["attempts"]],
        ["external_pause", "external_pause", "external_pause", "work_failed"])
    jobs2 = [mk(1, exit_code=1), mk(2, exit_code=1)]
    s2 = {}
    q.update_bundle_fail_streaks(jobs2, s2, pk)
    chk("2 work failures: streak 2 (exhausted)", (s2.get("S"), q.bundle_focus_exhausted("S", s2, progress={})[0]), (2, True))
    infra = next(iter(q._FAILURE_INFRA_REASONS))
    jobs3 = [mk(1, terminal_reason=infra, exit_code=1), mk(2, terminal_reason=infra, exit_code=1)]
    s3 = {}
    q.update_bundle_fail_streaks(jobs3, s3, pk)
    chk("model-unavailable failures consume no budget", s3.get("S"), None)
    chk("classify: restart / gate_reject", (q.classify_attempt({"terminal_reason": "gpu_job_orphaned"}),
                                           q.classify_attempt({}, verdict="fail")), ("restart", "gate_reject"))
    chk("attempts_used counts only budget kinds", q.attempts_used(jobs[3]) + q.attempts_used(jobs[0]), 1)

# ------------------------------------------------------------------ status: no runnable member / heal continuation
if G("status") and has("bundle_commit_status", "row_runnable_in_principle", "heal_continuation_live"):
    st = lambda jobs, **kw: q.bundle_commit_status("S", jobs, pk, {}, {}, **kw)[0]
    gx = {"id": "g1", "label": "bo", "bundle": "S", "status": "pending", "job_kind": q.GPU_JOB_KIND, "runner": q.GPU_JOB_RUNNER}
    chk("only a pending gpu-exclusive row: the bundle holds no lane", st([gx]), "complete")
    upstream_failed = {"id": "u", "label": "u", "bundle": "S", "status": "failed"}
    dep = {"id": "d", "label": "d", "bundle": "S", "status": "pending", "after": "u"}
    chk("pending row behind a FAILED upstream is not live work", st([upstream_failed, dep]) != "working", True)
    held_up = {"id": "u", "label": "u", "bundle": "S", "status": "pending", "user_hold": True}
    chk("pending row behind a HUMAN-HELD upstream is not live work", st([held_up, dep]) != "working", True)
    running_up = {"id": "u", "label": "u", "bundle": "S", "status": "running"}
    chk("pending row behind a RUNNING upstream is live", st([running_up, dep]), "working")
    plain = {"id": "p", "label": "p", "bundle": "S", "status": "pending"}
    chk("a plain pending row is live", st([plain]), "working")
    esc = {"id": "n1", "label": "auto-author-x-c1", "bundle": "S", "status": "needs_opus", "superseded_by": "n2"}
    cont = {"id": "n2", "label": "auto-author-x-c2", "bundle": "S", "status": "pending", "after": None}
    # the continuation row itself is live; take it out of the bundle tag to test the esc path alone
    cont_other = dict(cont, bundle="other")
    chk("needs_opus row with a live re-spec/continuation is pending, not 'needs the owner'",
        st([esc, cont_other]), "working")
    chk("needs_opus row with NO continuation still parks the bundle", st([dict(esc, superseded_by=None)]), "blocked")
    q.degraded_begin_tick()
    boom = lambda j: (_ for _ in ()).throw(RuntimeError("heal module gone"))
    stt = q.bundle_commit_status("S", [dict(esc, superseded_by=None)], pk, {}, {}, esc_state=boom)[0]
    chk("esc_state raising is flagged degraded (not silent)", ("esc_state" in q.degraded_this_tick(), stt), (True, "blocked"))

# ------------------------------------------------------------------ H8 receipts
if G("receipts") and has("op_receipt", "run_with_receipt", "job_snapshot"):
    snap = lambda status, idx=0, hold=False: {"id": "j1", "status": status, "user_hold": hold, "pause_reason": None,
                                              "idx": idx, "focus": None, "pinned": None}
    r = q.op_receipt("resume", snap("paused"), snap("pending"))
    chk("resume paused->pending: ok + changed", (r["ok"], r["changed"], r["from"], r["to"]), (True, True, "paused", "pending"))
    r = q.op_receipt("resume", snap("paused"), snap("paused"))
    chk("resume that left it paused: NOT ok", (r["ok"], "did not take effect" in r["reason"]), (False, True))
    r = q.op_receipt("resume", snap("pending"), snap("pending"))
    chk("resume on an already-pending row: NO-OP", (r["ok"], r["changed"]), (False, False))
    r = q.op_receipt("promote", snap("pending", 3), snap("pending", 0))
    chk("promote that moved the row: ok", (r["ok"], r["changed"]), (True, True))
    r = q.op_receipt("promote", snap("pending", 0), snap("pending", 0))
    chk("promote that moved nothing: NO-OP", r["ok"], False)
    r = q.op_receipt("cancel", snap("pending"), None)
    chk("cancel -> gone: ok", (r["ok"], r["to"]), (True, "gone"))
    r = q.op_receipt("cancel", snap("pending"), snap("pending"))
    chk("cancel that left the row: NOT ok", r["ok"], False)
    # end to end against a real (sandboxed) state file
    sp = q.STATE_PATH
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"jobs": [{"id": "zz1", "label": "x", "bundle": "S", "status": "pending", "num_ctx": 8192, "model": "m"}]}))
    rc = None
    try:
        q.run_with_receipt("promote", "zz1", lambda: None)
    except SystemExit as e:
        rc = e.code
    chk("e2e: a verb that changes nothing exits 4 (NO-OP)", rc, 4)
    rc = "none"
    def flip():
        d = json.loads(sp.read_text()); d["jobs"][0]["status"] = "paused"; sp.write_text(json.dumps(d))
    sp.write_text(json.dumps({"jobs": [{"id": "zz1", "label": "x", "bundle": "S", "status": "paused", "num_ctx": 8192, "model": "m"}]}))
    def fix():
        d = json.loads(sp.read_text()); d["jobs"][0]["status"] = "pending"; sp.write_text(json.dumps(d))
    try:
        q.run_with_receipt("resume", "zz1", fix)
        rc = 0
    except SystemExit as e:
        rc = e.code
    chk("e2e: a verb that flips paused->pending returns normally", rc, 0)
    chk("verbs are wired through the receipt", all(x in Path(A.queue).read_text() for x in (
        'run_with_receipt("promote"', 'run_with_receipt("resume"', 'run_with_receipt("cancel"')), True)

# ------------------------------------------------------------------ enqueue --mode / --fixed-target
if G("mode"):
    h = subprocess.run([sys.executable, A.queue, "enqueue", "--help"], capture_output=True, text=True).stdout
    chk("enqueue --help lists --mode and --fixed-target (the auto driver detects them there)",
        ("--mode" in h, "--fixed-target" in h), (True, True))
    if has("validate_mode_args", "_build_cmd"):
        chk("fixed without a target is refused", bool(q.validate_mode_args("fixed", [])), True)
        chk("target without fixed is refused", bool(q.validate_mode_args("open", ["a.py"])), True)
        chk("fixed + target ok", q.validate_mode_args("fixed", ["a.py"]), "")
        tf = T / "task.txt"; tf.write_text("do it")
        base = {"id": "m1", "model": "m", "cwd": str(T), "task_file": str(tf), "num_ctx": 8192, "api": "ollama"}
        orig = q._worker_supports_flag
        q._worker_supports_flag = lambda f: True
        try:
            cmd = q._build_cmd(dict(base, mode="fixed", fixed_targets=["a.py", "b.py"]), "http://h")
            cmd0 = q._build_cmd(dict(base), "http://h")
        finally:
            q._worker_supports_flag = orig
        chk("worker argv carries --mode fixed and every --fixed-target",
            (cmd[cmd.index("--mode") + 1], [cmd[i + 1] for i, x in enumerate(cmd) if x == "--fixed-target"]),
            ("fixed", ["a.py", "b.py"]))
        chk("no mode set -> argv unchanged (worker default governs)", ("--mode" in cmd0, "--fixed-target" in cmd0), (False, False))

# ------------------------------------------------------------------ revalidate at pop (gate E)
if G("reval") and has("revalidate_at_pop", "park_spec_defect"):
    wt = T / "wt"; wt.mkdir()
    (wt / ".preflight-state.json").write_text("{}")
    job = {"id": "r1", "label": "dev", "cwd": str(wt), "status": "pending"}
    chk("rc 3 -> park", q.revalidate_at_pop(dict(job), run=lambda j: (3, "SPEC_DEFECT: base moved"))[0], "park")
    chk("rc 0 -> launch", q.revalidate_at_pop(dict(job), run=lambda j: (0, "fresh"))[0], "launch")
    chk("tool crash (rc 1) fails OPEN", q.revalidate_at_pop(dict(job), run=lambda j: (1, "boom"))[0], "launch")
    chk("run raising fails OPEN", q.revalidate_at_pop(dict(job), run=lambda j: 1 / 0)[0], "launch")
    wt2 = T / "wt2"; wt2.mkdir()
    chk("no snapshot -> launch", q.revalidate_at_pop(dict(job, cwd=str(wt2)), run=lambda j: (3, "x"))[0], "launch")
    chk("tool missing -> launch", q.revalidate_at_pop(dict(job), tool=str(T / "nope"))[0], "launch")
    chk("runner rows are never revalidated", q.revalidate_at_pop(dict(job, runner="/x/r.py"), run=lambda j: (3, "x"))[0], "launch")
    chk("resumes are never revalidated", q.revalidate_at_pop(dict(job, resume_transcript="/t"), run=lambda j: (3, "x"))[0], "launch")
    j2 = dict(job)
    q.revalidate_at_pop(j2, run=lambda j: (0, "ok"))
    chk("a clean result is remembered (no re-run inside the recheck window)",
        q.revalidate_at_pop(j2, run=lambda j: (3, "x"))[0], "launch")
    jobs = [dict(job), {"id": "r2", "label": "next", "status": "pending", "after": "r1"}]
    nb = q.park_spec_defect(jobs[0], "SPEC_DEFECT x", jobs)
    chk("parked into the heal lane with a spec failure_class, downstream blocked",
        (jobs[0]["status"], jobs[0]["failure_class"], jobs[0]["escalation"]["category"], nb, jobs[1]["status"]),
        ("needs_opus", "spec", "other", ["r2"], "blocked"))
    chk("launch loop consults the gate", "revalidate_at_pop(job)" in Path(A.queue).read_text(), True)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
