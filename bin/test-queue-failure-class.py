#!/usr/bin/env python3
"""FAILURE CLASSIFICATION + INTERRUPTED-ATTEMPT + NEXT-STEP regressions (smoke 2026-10-09).

  D4  stale-base only when the commits since the launch baseline touch the task's files
  D5  an interrupted row (force_stopped / exit 3 = graceful SIGTERM pause) is not a failed
      attempt: it does not move the bundle fail-streak that exhausts focus / feeds escalation
  D6  an authoring round's final-check "TASK.md still has TODO placeholders" is a model
      nonconvergence (unfinished deliverable), not a `spec` defect; earlier echoes of it never
      classify
  D7  a nonconverged authoring job whose self-check failure keeps changing/shrinking is
      class nonconvergence_progressing with a continuation_allowance
  ping-pong  an author job and its continuation keep the lane across TWO hops; a PARKED
      (needs_opus) declared job stops holding the lane at once (no 120s wait behind it)

Usage: python3 test-queue-failure-class.py [--queue PATH]     (revert: --queue <bak>)
Marker: ALL PASS."""
import argparse, importlib.util, json, os, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="failclass-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ld = SourceFileLoader("queue_fc", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_fc", ld))
ld.exec_module(q)
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:   # an API the reverted code lacks is a FAIL, not a crash
        return f"EXC {type(e).__name__}: {e}"


# ---------------------------------------------------------------- D4 stale-base needs scope evidence
class CP:
    def __init__(self, rc, out): self.returncode, self.stdout = rc, out

wt = T / "wt"; wt.mkdir()
(wt / "TASK.md").write_text("Only edit `sidecar/src/costco.js`, `sidecar/src/loginFlow.js`; do not edit `verify.sh`.\n")
(wt / ".dispatch-harness.json").write_text(json.dumps({"target": "sidecar/src/costco.js"}))
log = T / "job.log"
log.write_text("--- iteration 24/24 ---\n[worker] verify stdout:\n  FAIL: new test file(s) failed (exit 1)\n"
               "[worker] VERIFY FAILED (exit 1) (4 new failure(s) attributable to this diff). Do not trust this output as-is.\n")


def git_stub(changed):
    def run(argv, **kw):
        if "symbolic-ref" in argv: return CP(0, "refs/remotes/origin/main\n")
        if "rev-list" in argv: return CP(0, "23\n")
        if "diff" in argv: return CP(0, "\n".join(changed) + "\n")
        return CP(1, "")
    return run


def stamp(changed, label="coding-job", verify="bash verify.sh", logp=None):
    job = {"terminal_reason": "nonconvergence", "log_path": str(logp or log), "exit_code": 1, "cwd": str(wt),
           "label": label, "verify": verify, "launch_baseline": {"head": "c3581f8ff15f"}}
    real_run = q.subprocess.run
    q.subprocess.run = git_stub(changed)
    try:
        q._stamp_failure_class(job)
    finally:
        q.subprocess.run = real_run
    return job

j = safe(stamp, ["README.md", "app/unrelated.ts"])
chk("main moved 23 commits, none in scope -> nonconvergence stays model (not stale-base)",
    (j.get("failure_class"), "stale_base" in j) if isinstance(j, dict) else j, ("model", False))
# 2026-10-13 (prose-sticky arm C rerun): "N new failure(s) attributable to this diff" means the
# failures appear WITH the model's diff and not at the pinned baseline, so main moving cannot
# have caused them -> model nonconvergence, even when a scope file landed upstream.
j = safe(stamp, ["README.md", "sidecar/src/loginFlow.js"])
chk("scope file moved BUT failures attributable to the model's own diff -> stays model, not stale-base",
    (j.get("failure_class"), "stale_base" in j) if isinstance(j, dict) else j, ("model", False))
log_plain = T / "plain.log"
log_plain.write_text("--- iteration 24/24 ---\n[worker] verify stdout:\n  FAIL: new test file(s) failed (exit 1)\n"
                     "[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n")
j = safe(stamp, ["README.md", "sidecar/src/loginFlow.js"], logp=log_plain)
chk("a landed commit touching a scope file -> stale-base",
    j.get("failure_class") if isinstance(j, dict) else j, "stale-base")
chk("...and the touched file is recorded",
    (j.get("stale_base") or {}).get("touched") if isinstance(j, dict) else j, ["sidecar/src/loginFlow.js"])

# ---------------------------------------------------------------- D6 TODO placeholders in an authoring round
final_todo = ("--- iteration 24/24 ---\n[worker] verify stdout:\n  FAIL: TASK.md still has TODO placeholders -- fill them in\n\n"
              "[worker] VERIFY FAILED (exit 1) (1 new failure(s) attributable to this diff). Do not trust this output as-is.\n")
chk("authoring round, final check = TODO placeholders -> model (unfinished authoring)",
    safe(lambda: q.classify_failure("nonconvergence", final_todo, 1, authoring=True)[0]), "model")
chk("non-authoring with that marker keeps spec",
    safe(lambda: q.classify_failure("nonconvergence", final_todo, 1, authoring=False)[0]), "spec")
echo = ('[worker] tool run_bash({"command": "python3 auto-harness-check.py"}) -> {"exit_code": 1, "stdout": '
        '"  FAIL: TASK.md still has TODO placeholders -- fill them in\\n"}\n')
nofinal = "--- iteration 3/24 ---\n" + echo + "--- iteration 24/24 ---\n[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n"
chk("an earlier self-check echo of the TODO FAIL (model fixed it later) does not classify spec",
    safe(lambda: q.classify_failure("nonconvergence", nofinal, 1)[0]), "model")
alog = T / "author.log"; alog.write_text(final_todo + "[worker] TERMINAL REASON: nonconvergence\n")
aj = {"terminal_reason": "nonconvergence", "log_path": str(alog), "exit_code": 1, "label": "auto-author-x-c1",
      "verify": "python3 auto-harness-check.py"}
safe(q._stamp_failure_class, aj)
chk("_stamp_failure_class on the smoke's egift/order-mismatch c1 shape -> not spec", aj.get("failure_class") != "spec", True)

# ---------------------------------------------------------------- D7 progressing nonconvergence
def tool(fails_):
    return ('[worker] tool run_bash({"command": "python3 auto-harness-check.py 2>&1"}) -> {"exit_code": 1, "stdout": "'
            + "".join(f"  FAIL: {f}\\n" for f in fails_) + '", "stderr": ""}\n')

def fin(fails_):
    return ("--- iteration 24/24 ---\n[worker] verify stdout:\n" + "".join(f"  FAIL: {f}\n" for f in fails_)
            + "\n[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n[worker] TERMINAL REASON: nonconvergence\n")

def stamp_author(text, **kw):
    p = T / f"a{abs(hash(text))}.log"; p.write_text(text)
    job = dict({"terminal_reason": "nonconvergence", "log_path": str(p), "exit_code": 1, "max_iters": 24,
                "label": "auto-author-x", "verify": "python3 auto-harness-check.py"}, **kw)
    q._stamp_failure_class(job)
    return job

costco = tool(["refimpl does not make verify print OK"]) * 3 + fin(["new test file(s) failed (exit 1)", "not ok 6 - logout link matches"])
j = safe(stamp_author, costco)
chk("costco shape (flat generic failure, then a NEW concrete one) -> nonconvergence_progressing",
    j.get("failure_class") if isinstance(j, dict) else j, "nonconvergence_progressing")
ca = (j.get("continuation_allowance") or {}) if isinstance(j, dict) else {}
chk("...continuation_allowance: extra_iterations/rounds/kind/signatures", (ca.get("extra_iterations"), ca.get("rounds"), ca.get("kind"), len(ca.get("signatures") or [])), (12, 1, "changing", 3))
shrink = tool(["a one", "b two", "c three"]) + tool(["b two", "c three"]) + fin(["c three"])
j = safe(stamp_author, shrink)
chk("strictly shrinking failing set -> progressing/shrinking",
    ((j.get("failure_class"), (j.get("continuation_allowance") or {}).get("kind")) if isinstance(j, dict) else j),
    ("nonconvergence_progressing", "shrinking"))
flat = tool(["same failure"]) * 3 + fin(["same failure"])
j = safe(stamp_author, flat)
chk("the same failure every time -> stays model, no allowance",
    ((j.get("failure_class"), "continuation_allowance" in j) if isinstance(j, dict) else j), ("model", False))
j = safe(stamp_author, costco, label="plain-coding", verify="bash verify.sh")
chk("a non-authoring job is never progressing", j.get("failure_class") if isinstance(j, dict) else j, "model")

# ---------------------------------------------------------------- D5 interrupted rows are not failed attempts
pk = lambda j: j.get("bundle")
st = {}
safe(q.update_bundle_fail_streaks, [
    {"id": "c0", "bundle": "costco", "label": "auto-author-costco", "status": "failed", "terminal_reason": "nonconvergence",
     "exit_code": 1, "launched_at": "2026-10-09T01:00:00+00:00"},
    {"id": "c1", "bundle": "costco", "label": "auto-author-costco-c1", "status": "failed", "terminal_reason": "force_stopped",
     "exit_code": 3, "launched_at": "2026-10-09T02:00:00+00:00"}], st, pk, verdict=lambda j: None)
chk("real failure then a SIGTERM-paused (exit 3) row: streak stays 1, focus NOT exhausted",
    (st, safe(lambda: q.bundle_focus_exhausted("costco", st, progress={})[0])), ({"costco": 1}, False))
st2 = {}
safe(q.update_bundle_fail_streaks, [
    {"id": "c0", "bundle": "b", "label": "auto-author-b", "status": "failed", "terminal_reason": "nonconvergence", "exit_code": 1,
     "launched_at": "2026-10-09T01:00:00+00:00"},
    {"id": "c1", "bundle": "b", "label": "auto-author-b-c1", "status": "failed", "terminal_reason": "nonconvergence", "exit_code": 1,
     "launched_at": "2026-10-09T02:00:00+00:00"}], st2, pk, verdict=lambda j: None)
chk("two REAL failures still exhaust the bundle (guard unchanged)",
    (st2, safe(lambda: q.bundle_focus_exhausted("b", st2, progress={})[0])), ({"b": 2}, True))

# ---------------------------------------------------------------- ping-pong + parked declaration
A_, B_ = "bundle-a", "bundle-b"
NOW = 1000.0
runs = T / "runs"; runs.mkdir()
def chain(bundle, job):
    (runs / f"{bundle}.json").write_text(json.dumps({"phase": "waiting", "pid": os.getpid(), "job": job,
        "phase_since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW))}))
chain(A_, "a0"); chain(B_, "b0")
a0 = {"id": "a0", "label": "auto-author-a", "bundle": A_, "status": "failed"}
b0 = {"id": "b0", "label": "auto-author-b", "bundle": B_, "status": "pending"}
decl0 = safe(q.next_step_for_terminal, a0, {"known": True, "driver_live": True, "waiting": True}, NOW)
def lane_after(jobs, decl_by_bundle):
    state = {"jobs": jobs, "_bundle_commit": {"key": A_, "since": 0.0, "empty_since": None, "idle_since": None},
             "_bundle_next": {k: dict(v) for k, v in decl_by_bundle.items()}}
    k, ev = q._apply_bundle_commit(state, pk, None, [B_], NOW + 26, runs_dir=runs, chain_dir=runs, hooks={},
                                   cpu_outstanding={}, kick=lambda *a: True, alert=lambda *a: None)
    return k, state
# hop 1: author failed, c1 not yet enqueued -> A keeps the lane
k, _ = safe(lane_after, [a0, b0], {A_: decl0}) if isinstance(decl0, dict) else ("EXC", None)
chk("ping-pong hop 1: failed author, continuation not yet enqueued -> lane stays with its bundle", k, A_)
# hop 2: c1 ran and failed too; c2 not yet enqueued -> still A (no bounce to B between continuations)
a1 = {"id": "a1", "label": "auto-author-a-c1", "bundle": A_, "status": "failed"}
decl1 = safe(q.next_step_for_terminal, a1, {"known": True, "driver_live": True, "waiting": True}, NOW + 10)
chain(A_, "a1")
k, _ = safe(lane_after, [a0, a1, b0], {A_: decl1}) if isinstance(decl1, dict) else ("EXC", None)
chk("ping-pong hop 2: continuation failed, next continuation not yet enqueued -> lane STILL with its bundle", k, A_)
# parked: the driver escalated the declared job (needs_opus) -> declaration drops at once
a1p = dict(a1, status="needs_opus")
state = {"jobs": [a0, a1p, b0], "_bundle_next": {A_: dict(decl1)}}
exp = safe(q.settle_next_steps, state, pk, NOW + 30, chain_dir=runs)
chk("a PARKED (needs_opus) declared job drops its continue declaration this tick (no 120s hold)",
    state.get("_bundle_next"), {})
chk("...and it was not reported as an expired miss", exp, [])

# all-cases-unsat authoring round (2026-10-10, aw-codec-floor s3): refimpl fails EVERY fixture case
_au = ("--- iteration 24/24 ---\n[worker] verify stdout:\n  FAIL: x\n=== FAILING CASES (6 of 6 failed with "
       "refimpl.py applied) ===\n  FAILED CASE: a\n\n[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n")
chk("authoring round, refimpl fails ALL 6 of 6 cases -> spec (re-author fixture), not model",
    safe(lambda: q.classify_failure("nonconvergence", _au, 1, authoring=True)[0]), "spec")
chk("...2 of 6 failing stays model",
    safe(lambda: q.classify_failure("nonconvergence", _au.replace("(6 of 6", "(2 of 6"), 1, authoring=True)[0]), "model")
chk("...non-authoring job untouched",
    safe(lambda: q.classify_failure("nonconvergence", _au, 1, authoring=False)[0]), "model")

# adversarial review 2026-10-13: a CRASHED worker is harness (infra, retry-safe), never a model
# nonconvergence -- with or without a traceback in the tail, even after iterations ran
_it = "--- iteration 3/24 ---\n[worker] tool read_file(a.py)\n"
chk("terminal_reason=crashed, no traceback (SIGKILL/OOM) -> harness",
    safe(lambda: q.classify_failure("crashed", _it, -9)[0]), "harness")
chk("worker traceback in the tail after iterations ran -> harness, not model",
    safe(lambda: q.classify_failure("nonconvergence", _it + "Traceback (most recent call last):\n  File \"/x/ollama-worker.py\", line 1\nKeyError: 'x'\n", 1)[0]),
    "harness")
_cj = {"id": "crash1", "terminal_reason": "crashed", "exit_code": -9, "log_path": str(T / "crash.log")}
(T / "crash.log").write_text(_it)
safe(q._stamp_failure_class, _cj)
chk("_stamp_failure_class on a crashed row -> failure_class harness", _cj.get("failure_class"), "harness")

src = Path(A.queue).read_text()
chk("reap path still declares at the terminal write", "_declare_terminal_next_step(state, job)" in src, True)


# parse_runner_result: ' class=INFRA' (mini-swe-runner infra tag) is infra; model and spec classes unchanged (2026-10-24 mini-submit gate)
_pr = q.parse_runner_result
chk("parse_runner_result: ' class=INFRA' -> infra", (_pr("RESULT FAIL APIConnectionError boom class=INFRA") or (None, None))[1], "infra")
chk("parse_runner_result: ' class=MODEL' stays model", (_pr("RESULT FAIL mode=two-phase test_green=FAIL class=MODEL") or (None, None))[1], "model")
chk("parse_runner_result: bad-spec stays spec", (_pr("RESULT FAIL bad-spec nope class=SPEC") or (None, None))[1], "spec")
chk("parse_runner_result: a finish-claimed refusal (mini-finish holds the run dir) is infra",
    (_pr("RESULT FAIL bad-cwd finish-claimed: job abc123def456 is being finished in place by Claude (mini-finish) in /x/work; "
         "a run here would wipe it. Move /x/finish aside only if that work is no longer wanted class=INFRA") or (None, None))[1], "infra")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
