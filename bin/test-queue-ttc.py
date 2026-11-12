#!/usr/bin/env python3
"""TEST-TIME-COMPUTE queue regressions (2026-10-09).

Groups (run one with --group NAME, default all):
  cont      progress continuation: ONE capped in-place resume round with the allowance's extra
            iterations; shadow by default (audited, not applied); never for a repeat/flapping
            signature, a spent cap, a retired bundle or a missing transcript; audit trail on the row
  nsample   per-bundle N-sample + verify-gated selection: off by default, shadow logs only, live
            spawns fresh siblings (verifier-facts brief, no transcript, T>=0.6, one at a time),
            first green wins / pass > smallest diff, unrun siblings removed, R5 escalation (model
            from config), R6 exhaustion, GPU-spend cap
  reasons   fixed_lane_* terminal reasons in the failure classifier (+ ledger tags)
  retire    a retired bundle's rows raise no Needs-attention alarm, tagged or keyed (API)
  profile   thinking_policy + escalation config in model_profiles.yaml via model_profile.py

Usage: python3 test-queue-ttc.py [--queue PATH] [--api PATH] [--group G]   Marker: ALL PASS
(revert: point --queue/--api at a mutated copy; every canary mutant must make this FAIL)."""
import argparse, importlib.util, json, os, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
ap.add_argument("--api", default=str(HERE / "ollama-queue-api.py"))
ap.add_argument("--group", default="all")
ap.add_argument("--profile-src", default=str(HERE / "model_profile.py"))
ap.add_argument("--profiles-yaml", default=str(HERE / "model_profiles.yaml"))
ap.add_argument("--ledger", default=str(HERE / "failure_ledger.py"))
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="ttc-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
for k in ("QUEUE_TTC_CONTINUATION_MODE", "QUEUE_TTC_NSAMPLE_MODE", "QUEUE_CONTINUATION_MAX_ROUNDS",
          "BLOOMCTL_DRAIN_S", "MODEL_THINKING_ARM"):
    os.environ.pop(k, None)
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:      # an API a reverted copy lacks is a FAIL, not a crash
        return f"EXC {type(e).__name__}: {e}"


ld = SourceFileLoader("queue_ttc", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_ttc", ld))
ld.exec_module(q)
want = A.group
G = lambda n: want in ("all", n)


def audit_rows():
    p = Path(q.TTC_AUDIT_PATH)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------ cont
def selfcheck_log(path, sets, transcript):
    def tool(fails):
        return ('[worker] tool run_bash({"command": "python3 auto-harness-check.py 2>&1"}) -> {"exit_code": 1, "stdout": "'
                + "".join(f"  FAIL: {f}\\n" for f in fails) + '", "stderr": ""}\n')
    txt = "".join("--- iteration %d/24 ---\n" % (i + 1) + tool(s) for i, s in enumerate(sets[:-1]))
    txt += ("--- iteration 24/24 ---\n[worker] verify stdout:\n" + "".join(f"  FAIL: {f}\n" for f in sets[-1])
            + "\n[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n")
    txt += f"[worker] CHECKPOINT TRANSCRIPT: {transcript}\n[worker] TERMINAL REASON: nonconvergence\n"
    Path(path).write_text(txt)


def failed_author(name, sets, bundle="b-cont", mi=24, **extra):
    tr = T / f"{name}.transcript.json"
    tr.write_text("{}")
    lg = T / f"{name}.log"
    selfcheck_log(lg, sets, tr)
    j = {"id": name, "label": f"auto-author-{name}", "status": "failed", "terminal_reason": "nonconvergence",
         "log_path": str(lg), "exit_code": 1, "max_iters": mi, "bundle": bundle,
         "verify": "python3 auto-harness-check.py", "cwd": str(T), "model": "m", "task_file": str(T / "t.md"),
         "active_s": 100.0, "pid": None, "lane": None}
    j.update(extra)
    q._stamp_failure_class(j)
    return j, tr


if G("cont"):
    shrink = [["a one", "b two", "c three"], ["b two", "c three"], ["c three"]]
    chg = [["x one"], ["x one"], ["y fake dom detail"]]
    j, tr = failed_author("c1", chg)
    chk("fixture: classifier marks it progressing with an allowance",
        (j["failure_class"], j["continuation_allowance"]["extra_iterations"]), ("nonconvergence_progressing", 12))
    # default mode is SHADOW: decision audited, nothing applied
    chk("default ttc mode is shadow", q.ttc_mode("continuation"), "shadow")
    msg = safe(q._apply_progress_continuation, j, jobs=[j])
    rows = audit_rows()
    chk("shadow: no continuation applied (row stays failed, no resume)",
        (msg, j["status"], j.get("resume_transcript"), "continuations" in j), (None, "failed", None, False))
    chk("shadow: the decision is audited as would-continue, applied=False",
        [(r["kind"], r["action"], r["mode"], r["applied"]) for r in rows if r.get("job_id") == "c1"],
        [("continuation", "continue", "shadow", False)])
    chk("shadow: row records what it would have done", j.get("continuation_shadow", {}).get("would_extend_by"), 12)
    # LIVE
    os.environ["QUEUE_TTC_CONTINUATION_MODE"] = "live"
    j, tr = failed_author("c2", chg)
    msg = safe(q._apply_progress_continuation, j, jobs=[j])
    chk("live: failed -> pending, resuming its own checkpoint transcript",
        (j["status"], j["resume_transcript"], j["continuation_active"]), ("pending", str(tr), True))
    chk("live: failure fields cleared, allowance moved into the audit trail",
        (j["failure_class"], j["terminal_reason"], j["continuation_allowance"]), (None, None, None))
    rec = (j.get("continuations") or [{}])[0]
    chk("audit trail on the row: n / extra / kind / prior failure / log copy",
        (rec.get("n"), rec.get("extra_iterations"), rec.get("kind"),
         rec.get("prev", {}).get("failure_class"), Path(str(rec.get("prev", {}).get("log_copy"))).is_file()),
        (1, 12, "changing", "nonconvergence_progressing", True))
    chk("the continuation launches with --max-iters = the extra budget, not the row's own",
        (q.effective_max_iters(j), j["max_iters"]), (12, 24))
    j["continuation_active"] = False
    chk("a normal (non-continuation) launch keeps the row's max_iters", q.effective_max_iters(j), 24)
    j["continuation_active"] = True
    cmd = None
    try:
        tf = T / "t.md"; tf.write_text("task")
        j.update(num_ctx=8192, cwd=str(T), task_file=str(tf), model="m", verify=None, api=None)
        cmd = q._build_cmd(j, "http://127.0.0.1:1")
    except Exception as e:
        cmd = ["EXC", repr(e)]
    chk("_build_cmd: --max-iters 12 and --resume <transcript>",
        (cmd[cmd.index("--max-iters") + 1] if "--max-iters" in cmd else None,
         cmd[cmd.index("--resume") + 1] if "--resume" in cmd else None), ("12", str(tr)))
    # cap: a second failure after the continuation is a plain model failure, no 2nd allowance
    j["status"] = "running"; j["continuation_active"] = False
    j["terminal_reason"] = "nonconvergence"; j["failure_class"] = None
    lg = T / "c2.log"
    selfcheck_log(lg, [["x one"], ["y two"], ["z three"]], tr); j["log_path"] = str(lg)
    q._stamp_failure_class(j)
    chk("after the one round is spent, a progressing failure is class model with no allowance",
        (j["failure_class"], j.get("continuation_allowance")), ("model", None))
    # refusals
    j, tr = failed_author("c3", [["p one"], ["q two"], ["p one"]])
    chk("flapping signature gets no allowance in the first place", j["failure_class"], "model")
    j, tr = failed_author("c4", chg)
    j["continuations"] = [{"n": 1, "extra_iterations": 12, "signatures": [["o1", "o2", "o3"]]}]
    chk("cap: one round already used -> refused",
        safe(q.continuation_decide, j, True)[0], "skip")
    os.environ["QUEUE_CONTINUATION_MAX_ROUNDS"] = "2"
    j["continuation_allowance"]["rounds"] = 2
    jr, _ = failed_author("c4a", chg)
    jr["continuation_allowance"]["rounds"] = 2
    jr["continuations"] = [{"n": 1, "extra_iterations": 12, "signatures": [["y fake dom detail"]]}]
    chk("a repeat of an earlier continuation's latest signature is refused even under a 2-round cap",
        safe(q.continuation_decide, jr, True)[1].split(" (")[0], "latest failing set repeats an earlier continuation's")
    j2, _ = failed_author("c4b", chg)
    j2["continuation_allowance"]["rounds"] = 2
    j2["continuations"] = [{"n": 1, "extra_iterations": 12, "signatures": [["o1", "o2", "o3"]]}]
    chk("...but a NEW signature under a 2-round cap is granted, bounded by the 2x ceiling (24-12=12 left)",
        safe(q.continuation_decide, j2, True)[:2], ("continue", 12))
    os.environ.pop("QUEUE_CONTINUATION_MAX_ROUNDS")
    j5, _ = failed_author("c5", chg)
    chk("no checkpoint transcript -> refused", safe(q.continuation_decide, j5, False)[0], "skip")
    chk("per-task GPU cap reached -> refused",
        safe(q.continuation_decide, j5, True, None, 50.0)[0], "skip")
    j6, _ = failed_author("c6", chg, active_s=100.0)
    sup = {"b-cont": {"at": "x"}}
    q.bundle_is_retired.__defaults__  # exists
    real = q.bundle_is_retired
    q.bundle_is_retired = lambda key, superseded=None, cancelled=None: real(key, superseded=sup, cancelled=lambda k: None)
    safe(q._apply_progress_continuation, j6, jobs=[j6])
    q.bundle_is_retired = real
    chk("a RETIRED bundle never gets a continuation",
        (j6["status"], j6.get("continuation_refused")), ("failed", "bundle is retired"))
    os.environ.pop("QUEUE_TTC_CONTINUATION_MODE")
    Path(q.CONTINUATION_DISABLE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(q.CONTINUATION_DISABLE_FILE).write_text("")
    chk("kill switch file -> 0 rounds", q.continuation_max_rounds(), 0)
    Path(q.CONTINUATION_DISABLE_FILE).unlink()
    j7, _ = failed_author("c7", chg, max_iters=24)
    j7["continuation_allowance"]["extra_iterations"] = 500
    chk("extra iterations are clamped (24 ceiling, and <= base budget)",
        safe(q.continuation_decide, j7, True, 1, 10 ** 6)[1], 24)
    # seal stands down for an in-place continuation (it resumes its own tree)
    import subprocess as _sp
    rp = T / "sealrepo"; rp.mkdir()
    for c in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _sp.run(["git", "-C", str(rp)] + c, capture_output=True)
    (rp / "a.txt").write_text("1"); _sp.run(["git", "-C", str(rp), "add", "."], capture_output=True)
    _sp.run(["git", "-C", str(rp), "commit", "-qm", "init"], capture_output=True)
    (rp / "own-dirt.txt").write_text("the continuing job's own uncommitted work")
    sj = {"label": "auto-refine-x-r1", "continuation_active": True, "cwd": str(rp), "id": "sj1"}
    head0 = _sp.run(["git", "-C", str(rp), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    chk("seal_prev_round_baseline stands down for an in-place continuation (own dirt not committed)",
        (safe(q.seal_prev_round_baseline, sj),
         _sp.run(["git", "-C", str(rp), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head0), (None, True))

# ------------------------------------------------------------------ nsample
if G("nsample"):
    cfgdata = {"bundle-n": {"enabled": True, "k": 3, "gpu_cap_s": 1000}}
    chk("N-sample is OFF by default (no config -> nothing)", safe(q.nsample_cfg, "bundle-n", data={}), None)
    chk("disabled flag -> off", safe(q.nsample_cfg, "bundle-n", data={"bundle-n": {"enabled": False}}), None)
    cfg = q.nsample_cfg("bundle-n", data=cfgdata)
    chk("cfg: k, cap, default temperatures >= 0.6",
        (cfg["k"], cfg["gpu_cap_s"], min(cfg["temperatures"]) >= 0.6), (3, 1000.0, True))
    chk("k is clamped to NSAMPLE_MAX_K", q.nsample_cfg("b", data={"b": {"enabled": True, "k": 99}})["k"], q.NSAMPLE_MAX_K)

    def code_job(i, **kw):
        lg = T / f"n{i}.log"
        lg.write_text("--- iteration 9/9 ---\n[worker] verify stdout:\n  FAIL: parse handles tabs\n  AssertionError: tabs\n"
                      "[worker] VERIFY FAILED (exit 1).\nSECRET-MODEL-REASONING-DO-NOT-LEAK\n[worker] TERMINAL REASON: nonconvergence\n")
        tf = T / f"n{i}.md"; tf.write_text("ORIGINAL TASK TEXT")
        j = {"id": f"n{i}", "label": f"slice-a-s{i}", "status": "failed", "failure_class": "model",
             "terminal_reason": "nonconvergence", "verify": "bash verify.sh", "bundle": "bundle-n", "model": "m",
             "cwd": str(T), "task_file": str(tf), "log_path": str(lg), "active_s": 100.0, "launch_baseline": {"head": "h", "dirty": 0}}
        j.update(kw)
        return j

    j = code_job(1)
    chk("R0: a harness/spec failure is never sampled",
        "not model-side" in str(safe(q.nsample_should_sample, dict(j, failure_class="harness"), cfg)), True)
    chk("a model failure with a verify command is sampled", safe(q.nsample_should_sample, j, cfg), None)
    chk("no verify -> not sampled", bool(safe(q.nsample_should_sample, dict(j, verify=None), cfg)), True)
    chk("a sample is never sampled again", bool(safe(q.nsample_should_sample, dict(j, nsample_of="x"), cfg)), True)
    # tick: default mode shadow
    st = {"jobs": [code_job(2)]}
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata)
    chk("shadow: nothing enqueued, one 'would' line", (len(st["jobs"]), "shadow" in "".join(lines)), (1, True))
    chk("shadow: audited applied=False",
        [(r["kind"], r["mode"], r["applied"]) for r in audit_rows() if r.get("job_id") == "n2"], [("nsample", "shadow", False)])
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata)
    chk("shadow: not re-audited every tick", len([r for r in audit_rows() if r.get("job_id") == "n2"]), 1)
    # tick: live
    st = {"jobs": [code_job(3)]}
    lines = safe(q.nsample_tick, st, cfg_data={}, mode="live")
    chk("no bundle enabled -> live tick is a no-op", len(st["jobs"]), 1)
    st = {"jobs": [code_job(3)]}
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live")
    sibs = [x for x in st["jobs"] if x.get("nsample_of")]
    chk("live: K=3 siblings in the same bundle, pending, fresh labels",
        (len(sibs), {s["bundle"] for s in sibs}, {s["status"] for s in sibs}, len({s["label"] for s in sibs})),
        (3, {"bundle-n"}, {"pending"}, 3))
    chk("each sample has its own seed and explicit temperature >= 0.6",
        (len({s["seed"] for s in sibs}), all(s["temperature_explicit"] and s["temperature"] >= 0.6 for s in sibs)), (3, True))
    brief = Path(sibs[0]["task_file"]).read_text()
    chk("brief = original task + verifier facts only (checks, first error)",
        ("ORIGINAL TASK TEXT" in brief, "parse handles tabs" in brief, "fresh attempt #1" in brief), (True, True, True))
    chk("brief never carries the failed transcript / model reasoning and stays <= 15 lines",
        ("SECRET-MODEL-REASONING" in brief, len(brief.split("## Facts")[1].strip().splitlines()) <= 15), (False, True))
    chk("the original is marked sampled with the group and members",
        (st["jobs"][0]["nsample"]["state"], len(st["jobs"][0]["nsample"]["members"])), ("open", 3))
    s1, s2, s3 = sorted(sibs, key=lambda s: s["nsample_index"])
    chk("siblings run ONE AT A TIME: #2 waits for #1", bool(q.nsample_launch_blocker(s2, st["jobs"])), True)
    chk("#1 may launch", q.nsample_launch_blocker(s1, st["jobs"]), None)
    s1["status"] = "running"; s2["status"] = "failed"
    chk("while #1 runs, #3 is blocked too", bool(q.nsample_launch_blocker(s3, st["jobs"])), True)
    # first green wins; unrun siblings removed
    s2["status"] = "pending"; s1["status"] = "done"; s1["nsample_diff_lines"] = 10
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live")
    ids = {x["id"] for x in st["jobs"]}
    chk("win: unrun siblings removed, winner + original kept",
        (s1["id"] in ids, s2["id"] in ids, s3["id"] in ids), (True, False, False))
    chk("win recorded on both rows", (st["jobs"][0]["nsample"]["state"], st["jobs"][0]["nsample"]["winner"],
                                      s1["nsample"]["state"]), ("won", s1["id"], "won"))
    # selection function
    chk("select: pass beats fail, then smallest diff, then earliest",
        safe(q.nsample_select, [{"id": "a", "passed": True, "diff_lines": 30, "order": 1},
                                {"id": "b", "passed": True, "diff_lines": 5, "order": 2},
                                {"id": "c", "passed": False, "diff_lines": 1, "order": 0}]), "b")
    chk("select: tie on diff -> earliest", safe(q.nsample_select, [
        {"id": "a", "passed": True, "diff_lines": 5, "order": 2}, {"id": "b", "passed": True, "diff_lines": 5, "order": 1}]), "b")
    chk("select: nothing passed -> None, no vote", safe(q.nsample_select, [{"id": "a", "passed": False}]), None)
    # exhausted (different signatures -> R4 keep sampling until all fail)
    st = {"jobs": [code_job(4)]}
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    sibs = sorted([x for x in st["jobs"] if x.get("nsample_of")], key=lambda s: s["nsample_index"])
    for k, s in enumerate(sibs):
        s["status"] = "failed"; s["nsample_sig"] = [f"sig-{k}"]
    st["jobs"][0]["nsample"]["sig"] = ["orig-sig"]
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live")
    chk("R4/R6: all samples failed with differing signatures -> exhausted, original stands",
        (st["jobs"][0]["nsample"]["state"], st["jobs"][0]["status"]), ("exhausted", "failed"))
    # R5: two attempts end with the same signature -> cancel unrun, ONE escalation attempt from config
    st = {"jobs": [code_job(5)]}
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    st["jobs"][0]["nsample"]["sig"] = ["same-sig"]
    sibs = sorted([x for x in st["jobs"] if x.get("nsample_of")], key=lambda s: s["nsample_index"])
    sibs[0]["status"] = "failed"; sibs[0]["nsample_sig"] = ["same-sig"]
    sibs[0]["active_s"] = 10.0
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live", esc_of=lambda job: "big-model")
    esc = [x for x in st["jobs"] if x.get("nsample_escalation")]
    chk("R5: unrun samples cancelled, exactly ONE escalation sibling on the configured model",
        (len([x for x in st["jobs"] if x.get("nsample_of") and not x.get("nsample_escalation") and x["status"] == "pending"]),
         [(e["model"], e["nsample_index"]) for e in esc]), (0, [("big-model", 99)]))
    esc[0]["status"] = "failed"; esc[0]["nsample_sig"] = ["same-sig"]
    esc[0]["active_s"] = 10.0
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live", esc_of=lambda job: "big-model")
    chk("R6: the escalation ending with the same signature closes the group as respec (no 2nd escalation)",
        (st["jobs"][0]["nsample"]["state"], len([x for x in st["jobs"] if x.get("nsample_escalation")])), ("respec", 1))
    st = {"jobs": [code_job(6)]}
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    st["jobs"][0]["nsample"]["sig"] = ["s"]
    sb = sorted([x for x in st["jobs"] if x.get("nsample_of")], key=lambda s: s["nsample_index"])
    sb[0]["status"] = "failed"; sb[0]["nsample_sig"] = ["s"]
    q.nsample_tick(st, cfg_data=cfgdata, mode="live", esc_of=lambda job: None)
    chk("R5 with no escalation model configured -> respec, not a crash",
        st["jobs"][0]["nsample"]["state"], "respec")
    # GPU spend cap
    st = {"jobs": [code_job(7, active_s=600.0)]}
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live")
    chk("cap: 1000s cap with 600s/attempt leaves room for exactly 1 sample", len([x for x in st["jobs"] if x.get("nsample_of")]), 1)
    st = {"jobs": [code_job(8, active_s=5000.0)]}
    lines = safe(q.nsample_tick, st, cfg_data=cfgdata, mode="live")
    chk("cap: an attempt costing more than the cap spawns no samples",
        (len(st["jobs"]), st["jobs"][0].get("nsample", {}).get("state")), (1, "capped"))
    st = {"jobs": [code_job(9)]}
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    sb = [x for x in st["jobs"] if x.get("nsample_of")]
    sb[0]["status"] = "failed"; sb[0]["active_s"] = 1200.0; sb[0]["nsample_sig"] = ["z"]
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    chk("cap: sampled spend over the bundle cap closes the group and removes unrun samples",
        (st["jobs"][0]["nsample"]["state"], len([x for x in st["jobs"] if x.get("nsample_of") and x["status"] == "pending"])),
        ("capped", 0))
    # retired bundle is not sampled
    st = {"jobs": [code_job(10)]}
    real = q.bundle_is_retired
    q.bundle_is_retired = lambda key, superseded=None, cancelled=None: True
    q.nsample_tick(st, cfg_data=cfgdata, mode="live")
    q.bundle_is_retired = real
    chk("a retired bundle is never sampled", len(st["jobs"]), 1)
    chk("tick wired into the daemon loop and the launch loop honours the sibling gate",
        ("nsample_tick(state)" in Path(A.queue).read_text(), "nsample_launch_blocker(job, state[\"jobs\"])" in Path(A.queue).read_text()),
        (True, True))

# ------------------------------------------------------------------ reasons
if G("reasons"):
    for r in ("fixed_lane_exhausted", "fixed_lane_apply_failed"):
        chk(f"{r} -> class context", safe(q.classify_failure, r, "--- iteration 3/9 ---\n")[0], "context")
    cls = safe(q.classify_failure, "fixed_lane_transport_error", "--- iteration 3/9 ---\n")
    chk("fixed_lane_transport_error -> infra (harness class, retry-safe detail)",
        (cls[0], "retry" in cls[1] and "infra" in cls[1]), ("harness", True))
    chk("reasons present in _FAILURE_CONTEXT_REASONS",
        {"fixed_lane_exhausted", "fixed_lane_apply_failed"} <= set(q._FAILURE_CONTEXT_REASONS), True)
    ldg = SourceFileLoader("fl_ttc", A.ledger)
    fl = importlib.util.module_from_spec(importlib.util.spec_from_loader("fl_ttc", ldg))
    ldg.exec_module(fl)
    for r, tag in (("fixed_lane_exhausted", "fixed-lane-exhausted"), ("fixed_lane_apply_failed", "fixed-lane-apply-failed"),
                   ("fixed_lane_transport_error", "fixed-lane-transport-error")):
        chk(f"ledger signature for {r}", safe(fl.classify, {"terminal_reason": r}, {}, {})[0], tag)

# ------------------------------------------------------------------ retire
if G("retire"):
    sys.argv = [A.api]
    (T / "bin").mkdir(exist_ok=True)
    rb = Path("/Users/user/bin")
    for f in rb.iterdir():
        if f.is_file() and not (T / "bin" / f.name).exists():
            (T / "bin" / f.name).symlink_to(f)
    (T / "bin" / "ollama-queue.py").unlink(missing_ok=True)
    (T / "bin" / "ollama-queue.py").symlink_to(Path(A.queue).resolve())
    sys.path.insert(0, str(Path(os.environ.get("REAL_HOME", "/Users/user")) / "bin"))
    (T / "bin" / "ollama-queue-api.py").unlink(missing_ok=True)
    (T / "bin" / "ollama-queue-api.py").write_text(Path(A.api).read_text())
    spec = importlib.util.spec_from_file_location("api_ttc", str(T / "bin" / "ollama-queue-api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    sys.path.insert(0, str(HERE))
    rows = [{"id": "k1", "status": "failed", "label": "x-s1", "group_key": "ret-b", "bundle": "ret-b"},
            {"id": "k2", "status": "failed", "label": "y-s1", "group_key": None, "bundle": "ret-b"},
            {"id": "k6", "status": "failed", "label": "v-s1", "group_key": "ret-b", "bundle": None},
            {"id": "k3", "status": "failed", "label": "z-s1", "group_key": None, "bundle": "live-b"},
            {"id": "k4", "status": "failed", "label": "w-s1", "group_key": "ret-live", "bundle": "ret-live"},
            {"id": "k5", "status": "pending", "label": "w-s2", "group_key": "ret-live", "bundle": "ret-live"}]
    sup = {"ret-b": {"at": "t"}, "ret-live": {"at": "t"}}
    got = safe(api._needs_attention_ids, rows, state_of=lambda k: None, verdict_of=lambda *a, **k: None,
               superseded_map=sup)
    chk("retired bundle: keyed AND tag-only failed rows raise no Needs attention; unretired and live-job rows do",
        sorted(got) if isinstance(got, set) else got, ["k3", "k4"])

# ------------------------------------------------------------------ profile
if G("profile"):
    if not (T / "Desktop").exists():
        (T / "Desktop").symlink_to("/Users/user/Desktop")
    os.environ["MODEL_PROFILES_PATH"] = A.profiles_yaml
    sys.path.insert(0, str(Path(A.profile_src).parent))
    lp = SourceFileLoader("mp_ttc", A.profile_src)
    mp = importlib.util.module_from_spec(importlib.util.spec_from_loader("mp_ttc", lp))
    sys.modules["mp_ttc"] = mp
    lp.exec_module(mp)
    mp.PROFILES_PATH = Path(A.profiles_yaml)
    M = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    chk("policy default = defer to the profile (no behaviour change)", safe(mp.thinking_policy, "author", "tool_mechanical"), None)
    chk("hybrid arm: mechanical tool turns off, first turn on",
        (safe(mp.thinking_policy, "author", "tool_mechanical", "hybrid"), safe(mp.thinking_policy, "author", "tool_first", "hybrid")),
        (False, True))
    chk("gate/review roles are pinned OFF and the experiment arm cannot override them",
        (safe(mp.thinking_policy, "gate", "oneshot"), safe(mp.thinking_policy, "gate", "oneshot", "always_on")), (False, False))
    chk("request fields honour the arm per turn kind (and an explicit think= still wins)",
        (safe(mp.build_request_fields, M, "author", "openai", turn_kind="tool_mechanical", arm="hybrid")["chat_template_kwargs"]["enable_thinking"],
         safe(mp.build_request_fields, M, "author", "openai", think=True, turn_kind="tool_mechanical", arm="hybrid")["chat_template_kwargs"]["enable_thinking"]),
        (False, True))
    chk("reasoning_effort is never sent", "reasoning_effort" in safe(mp.build_request_fields, M, "author", "openai", turn_kind="oneshot", arm="always_on"), False)
    chk("escalation models come from config", safe(mp.escalation_models, "author")[:1], [M])
    chk("check_profiles stays clean", safe(mp.check_profiles, A.profiles_yaml), [])
    chk("an unknown arm / unprofiled escalation model is a check_profiles problem",
        len(safe(mp.check_policy, {"roles": {"author": "thinking_coding"}, "models": {},
                                   "thinking_policy": {"roles": {"author": "nope"}},
                                   "escalation": {"roles": {"author": {"models": ["ghost"]}}}})), 2)
    os.environ["MODEL_THINKING_ARM"] = "always_off"
    chk("MODEL_THINKING_ARM env selects the arm for ab_roles", safe(mp.thinking_policy, "coding", "oneshot"), False)
    os.environ.pop("MODEL_THINKING_ARM")

if G("profile"):
    chk("the A/B arm rides to the worker as MODEL_THINKING_ARM, only from the allow-list",
        (safe(q.job_env_overrides, {"thinking_arm": "hybrid"}), safe(q.job_env_overrides, {"thinking_arm": "rm -rf"}),
         safe(q.job_env_overrides, {})), ({"MODEL_THINKING_ARM": "hybrid"}, {}, {}))
    chk("launch merges them into the worker env", "_env.update(job_env_overrides(job))" in Path(A.queue).read_text(), True)

print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
