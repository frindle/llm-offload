#!/usr/bin/env python3
"""Author-burn guards (2026-10-03, replay-endorse s3-endorsement-selection: 15 author/
refine queue jobs before the owner caught it by hand).

Pinned on s3's REAL data (advance log + queue rows + attempt-1 AUTO tail):
  1. signature SHAPE: attempts 2 and 3 failed identically except for the job id ->
     streak 2 -> bound_stale_worktree_retry ESCALATES (it never did: streak stayed 1)
     ...a genuinely different reason still resets (no over-masking)
  1b. author-JOB budget: the 15 real s3 author/refine rows count, the coding job and
     other slices do not; >= budget (8) escalates; per-run override lifts it; a human
     --retry-slice starts a fresh budget
  2. converged harness + preflight NO-GO (attempt 1's real tail) -> ESCALATED with the
     worktree KEPT and a --regate resume command, not FAILED-and-wiped
     ...an UNconverged harness with the same tail still takes the normal FAILED path
  3. a directly-edited target is reset to HEAD before preflight; harness files are
     never touched; a live job in the worktree blocks it; an untracked creation
     target is left alone without error; the refine prompt no longer tells the model
     to keep a dirty target
  4. author/continue prompts say TASK.md's Scope line is for the later coding job

Usage:  python3 test-author-burn-guards.py            -> ALL PASS
        SLICE=<slice .bak> AUTO=<auto .bak> python3 ... -> FAILs (revert-check)
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE") or HERE / "ollama-dispatch-slice")
AUTO = Path(os.environ.get("AUTO") or HERE / "ollama-dispatch-auto")
fails = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        fails.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    ld.exec_module(m)
    return m


# HERMETIC (2026-10-08): refresh_author_job_ids unions in failure_ledger.lifetime_job_ids(), which reads the
# LIVE queue state + done.json sidecars -- and REAL replay-endorse/s3 has since queued more author jobs, so the
# fixed-count checks below (15 of 18, streak resets) failed against live data. Point the ledger at an empty dir.
_HERM = tempfile.mkdtemp(prefix="burn-herm-")
os.environ["FAILURE_LEDGER_QUEUE_LOGS"] = _HERM
os.environ["FAILURE_LEDGER_LIVELOGS"] = _HERM
os.environ["FAILURE_LEDGER_STATE"] = _HERM + "/none.json"
os.environ["FAILURE_LEDGER"] = _HERM + "/failures.jsonl"
sl = load(SLICE, "slice_burn")
au = load(AUTO, "auto_burn")
sl.save_state = lambda st, **k: None
sl.slice_job_awaiting_gate = lambda st, sid: None
sl.remove_worktree = lambda cwt, wt: None
sl.queue_rows = getattr(sl, "queue_rows", None) and (lambda: [])
# HERMETIC (2026-10-05, s3fix): retry_abort_reason reads the slice state from DISK by
# run label, and RUN is the real 'replay-endorse' run -- whose live s3 is 'escalated',
# so every retry here aborted against the owner's real state and 3 checks failed.
sl.read_state = lambda label: None

RUN, SID = "replay-endorse", "s3-endorsement-selection"
# the REAL last_auto_error strings of s3's attempts 1..3 (replay-endorse.advance.log)
SIG1 = ("FAIL: TASK.md still has TODO placeholders -- fill them in | AssertionError: "
        "verify.test.ts SCAFFOLD anchor not found | [auto] hit the round cap without a GO. "
        "The harness is in /Users/user/.ollama-dispatch/worktrees/wt-slice-replay-endorse-"
        "s3-endorsement-selection. Last blockers/survivors above.")
SIG2 = ("ERROR: authoring did not converge: job 95f324ed828b failed (exit 1) | FAIL: "
        "TASK.md still has TODO placeholders -- fill them in")
SIG3 = ("ERROR: authoring did not converge: job 63d688b784e8 failed (exit 1) | FAIL: "
        "TASK.md still has TODO placeholders -- fill them in")


def run_attempts(sigs, st=None):
    st = st or {"label": RUN, "slices": {SID: {"status": "failed"}}}
    s = st["slices"][SID]
    outs = []
    for sig in sigs:
        s["status"] = "failed"
        s["last_auto_error"] = sig
        outs.append(sl.bound_stale_worktree_retry(st, SID, s, "/nonexistent-wt", "/nonexistent-cwt",
                                                  "test"))
    return outs, s


# ---- 1. signature shape ------------------------------------------------------
# Phase 2 (2026-10-08) RE-SPEC: the FIRST time the identical-failure cap trips, the slice is re-specced once
# (retry allowed, counters zeroed) instead of parked. Pin that, then pin the escalation with the one respec spent.
outs, s = run_attempts([SIG1, SIG2, SIG3])
chk("first cap hit (attempt 3) is RE-SPEC'd, not parked (bounded, once)",
    (outs, s["status"], s.get("respec_count")), ([True, True, True], "failed", 1))
outs, s = run_attempts([SIG1, SIG2, SIG3], {"label": RUN, "slices": {SID: {"status": "failed",
                                                                          "respec_count": sl.RESPEC_CAP}}})
chk("s3 real sequence: attempt 3 (same failure as 2, new job id) is REFUSED",
    (outs, s["status"]), ([True, True, False], "escalated"))
chk("...the reason names the identical failure", "identical failure" in (s.get("escalation_reason") or ""), True)
outs, s = run_attempts([SIG2, SIG2.replace("TODO placeholders", "a SyntaxError in refimpl.py")])
chk("a genuinely DIFFERENT reason still resets the streak (no over-masking)",
    (outs, s.get("author_fail_streak")), ([True, True], 1))
chk("'3 survivors' vs '2 survivors' are different reasons",
    getattr(sl, "_sig_shape", lambda x: x)("FAIL: 3 survivors") ==
    getattr(sl, "_sig_shape", lambda x: x)("FAIL: 2 survivors"), False)

# ---- 1b. author-job budget ------------------------------------------------------
REAL_ROWS = [  # (id, status, label) -- s3's real queue rows + neighbours that must NOT count
    ("984db5b6a535", "failed", f"auto-author-{RUN}-{SID}"),
    ("68e3109a4616", "failed", f"auto-author-{RUN}-{SID}-c1"),
    ("497c31926bd0", "done", f"auto-author-{RUN}-{SID}-c2"),
    ("0955e911030f", "done", f"auto-refine-{RUN}-{SID}-r1"),
    ("ce0626f9e888", "done", f"auto-refine-{RUN}-{SID}-r2"),
    ("ad0dcb43f97f", "done", f"auto-refine-{RUN}-{SID}-r3"),
    ("06c2bf413cd7", "failed", f"auto-author-{RUN}-{SID}"),
    ("11b393e9e67d", "failed", f"auto-author-{RUN}-{SID}-c1"),
    ("17f8912017a9", "failed", f"auto-author-{RUN}-{SID}-c2"),
    ("95f324ed828b", "failed", f"auto-author-{RUN}-{SID}-esc"),
    ("88b5be3306d3", "failed", f"auto-author-{RUN}-{SID}"),
    ("4493c0aa3441", "failed", f"auto-author-{RUN}-{SID}-c1"),
    ("63d688b784e8", "failed", f"auto-author-{RUN}-{SID}-c2"),
    ("5903a54a9e97", "done", f"auto-author-{RUN}-{SID}"),
    ("8e9eb6297e62", "running", f"auto-refine-{RUN}-{SID}-r1"),
    ("aaaaaaaa0001", "done", f"{RUN}-{SID}"),                       # its CODING job
    ("aaaaaaaa0002", "done", f"auto-author-{RUN}-s4a-integrate-normalize"),
    ("aaaaaaaa0003", "done", f"auto-author-{RUN}-{SID}x"),           # prefix lookalike
]
st = {"label": RUN, "slices": {SID: {"status": "failed"}}}
n = getattr(sl, "refresh_author_job_ids", lambda *a, **k: 0)(st, SID, None, REAL_ROWS)
chk("only s3's own author/refine rows count (15 of 18)", n, 15)
sl.queue_rows = lambda: REAL_ROWS
outs, s = run_attempts([SIG1], {"label": RUN, "slices": {SID: {"status": "failed"}}})
chk("15 jobs >= budget 8 -> the FIRST retry is refused even with a fresh streak",
    (outs, s["status"]), ([False], "escalated"))
chk("...reason names the job budget and the override",
    "--author-job-budget" in (s.get("escalation_reason") or ""), True)
outs, s = run_attempts([SIG1], {"label": RUN, "author_job_budget": 40,
                                "slices": {SID: {"status": "failed"}}})
chk("per-run override (author_job_budget=40) lifts the budget", outs, [True])
st = {"label": RUN, "plan_path": "/x/plan.json", "order": [SID],
      "slices": {SID: {"status": "failed", "worktree": "/nonexistent-wt"}}}
sl.slice_job_inflight = lambda st, sid: None
sl.ensure_chain_worktree = lambda st: "/nonexistent-cwt"
sl.retry_slice(st, SID)
chk("a human --retry-slice starts a fresh budget",
    sl.refresh_author_job_ids(st, SID, None, REAL_ROWS) if hasattr(sl, "refresh_author_job_ids") else -1, 0)

# ---- 1c. hard cap: self-heal retries never reset the budget; lifetime cap ---------
# s3's real shape: two dispatch-self-heal --retry-slice resets, each handing it a fresh
# window -> 25 author/refine jobs in total.
def _rows(n):
    return [("%012x" % (0xbbbb00000000 + i), "failed", f"auto-author-{RUN}-{SID}-c{i}")
            for i in range(n)]
_removed = []
sl.remove_worktree = lambda cwt, wt: _removed.append(wt)
FIVE = _rows(5)
sl.queue_rows = lambda: FIVE
st = {"label": RUN, "plan_path": "/x/plan.json", "order": [SID],
      "slices": {SID: {"status": "failed", "worktree": "/nonexistent-wt"}}}
os.environ["DISPATCH_SELF_HEAL"] = "1"
try:
    sl.retry_slice(st, SID)
finally:
    os.environ.pop("DISPATCH_SELF_HEAL", None)
chk("a SELF-HEAL --retry-slice keeps the window (5 jobs still counted, not 0)",
    sl.refresh_author_job_ids(st, SID, None, FIVE), 5)
sl.queue_rows = lambda: REAL_ROWS          # 15 jobs >= budget 8
_removed.clear()
st = {"label": RUN, "plan_path": "/x/plan.json", "order": [SID],
      "slices": {SID: {"status": "failed", "worktree": "/nonexistent-wt"}}}
os.environ["DISPATCH_SELF_HEAL"] = "1"
_died = False
try:
    sl.retry_slice(st, SID)
except SystemExit:
    _died = True
finally:
    os.environ.pop("DISPATCH_SELF_HEAL", None)
chk("a SELF-HEAL --retry-slice on an exhausted budget is REFUSED, worktree kept",
    (_died, st["slices"][SID]["status"], _removed), (True, "failed", []))
TWENTY5 = _rows(25)
sl.queue_rows = lambda: TWENTY5
s = {"status": "failed", "author_jobs_at_retry": 20}   # human retry 5 jobs ago
st = {"label": RUN, "slices": {SID: s}}
_why = (getattr(sl, "author_budget_block", None) or (lambda *a, **k: None))(st, SID, s, rows=TWENTY5)
chk("LIFETIME cap: 25 jobs ever >= 3 x 8 refuses even though the window holds only 5",
    bool(_why) and "LIFETIME" in _why, True)
outs, s = run_attempts([SIG1], {"label": RUN,
                                "slices": {SID: {"status": "failed", "author_jobs_at_retry": 20}}})
chk("...and the failed->retry path escalates on it", (outs, s["status"]), ([False], "escalated"))
s = {"status": "failed", "author_jobs_at_retry": 20}
st = {"label": RUN, "author_job_budget": 10, "slices": {SID: s}}
chk("--author-job-budget 10 scales the lifetime cap to 30 (25 jobs pass)",
    (getattr(sl, "author_budget_block", None) or (lambda *a, **k: "x"))(st, SID, s, rows=TWENTY5), None)
import inspect as _insp
_ex = _insp.getsource(sl.execute)
_i_gate = _ex.find("author_budget_block(")
_i_launch = _ex.find("off chain tip ===")
chk("execute() consults the budget BEFORE every authoring launch (PENDING path too)",
    0 <= _i_gate < _i_launch, True)

# ---- 2. converged harness + preflight NO-GO is kept, not wiped ---------------
TAIL = """    converged on continuation round 2: job 497c31926bd0 done
[auto] preflight round 1/4...
    verdict=NO-GO  blockers=['baseline-clean']
[auto] refining against the gate feedback...
[auto] preflight round 4/4...
    verdict=NO-GO  blockers=['baseline-clean']

[auto] hit the round cap without a GO. The harness is in /wt. Last blockers/survivors above.

----- harness files -----
  TASK.md  (3692 bytes)
  untracked/changed:
    M lib/bfmrLinkReconcile.ts"""
with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    (wt / "TASK.md").write_text("# TASK\n\n## Must contain\n\n- `endorsed`\n")
    (wt / "verify.test.ts").write_text("test('x', () => {})\n")
    (wt / "refimpl.py").write_text("print('x')\n")
    f = getattr(sl, "converged_harness_preflight_nogo", lambda *a: "")
    chk("attempt-1 tail on a COMPLETE harness -> kept (blockers named)",
        "baseline-clean" in f(TAIL, str(wt)), True)
    (wt / "TASK.md").write_text("TODO -- state the defect\n## Must contain\n")
    chk("same tail on an UNconverged harness -> normal path ('')", f(TAIL, str(wt)), "")
    chk("an authoring failure (no round-cap line) -> normal path ('')",
        f("ERROR: authoring did not converge: job x failed", str(wt)), "")
import inspect
src = inspect.getsource(sl.execute)
i_kept = src.find("converged_harness_preflight_nogo(")
i_fail = src.find('s["status"] = FAILED\n                    save_state(st)\n                    print(f"# {sid}: FAILED during auto-authoring')
chk("execute() routes a kept harness to ESCALATED BEFORE stamping FAILED",
    i_kept != -1 and i_fail != -1 and i_kept < i_fail, True)
chk("...and the reason carries the exact resume command", "--regate %s" in src and "worktree KEPT" in src, True)

# ---- 3. target reset before preflight ----------------------------------------
def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    git(wt, "init", "-q")
    (wt / "lib").mkdir()
    (wt / "lib" / "t.ts").write_text("export const a = 1;\n")
    (wt / "refimpl.py").write_text("OLD\n")
    (wt / "TASK.md").write_text("task\n")
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (wt / "lib" / "t.ts").write_text("export const a = 2; // refimpl applied by hand\n")
    (wt / "refimpl.py").write_text("NEW refimpl\n")
    (wt / "TASK.md").write_text("filled task\n")
    fn = getattr(au, "enforce_target_at_head", None)
    if fn is None:
        chk("enforce_target_at_head exists", False, True)
    else:
        und = fn(wt, "lib/t.ts", live_fn=lambda w: ("j1", "running", "auto-refine-x-r1"))
        chk("a LIVE job in the worktree blocks the reset", (und, (wt / "lib/t.ts").read_text()),
            ([], "export const a = 2; // refimpl applied by hand\n"))
        und = fn(wt, "lib/t.ts", live_fn=lambda w: None)
        chk("directly-edited target reset to HEAD (logged)",
            (len(und), (wt / "lib/t.ts").read_text()), (1, "export const a = 1;\n"))
        chk("harness files untouched", ((wt / "refimpl.py").read_text(), (wt / "TASK.md").read_text()),
            ("NEW refimpl\n", "filled task\n"))
        chk("clean target -> nothing undone", fn(wt, "lib/t.ts", live_fn=lambda w: None), [])
        (wt / "lib" / "new.ts").write_text("stub\n")
        chk("untracked creation target: no error, left alone",
            (fn(wt, "lib/new.ts", live_fn=lambda w: None), (wt / "lib/new.ts").exists()), ([], True))
        _qs = au.QUEUE_STATE
        try:
            au.QUEUE_STATE = wt / "no-such-state.json"
            chk("unreadable queue state fails CLOSED (treated as live)",
                bool(au.live_job_on_worktree(wt)), True)
            import json as _j
            au.QUEUE_STATE = wt / "qs.json"
            au.QUEUE_STATE.write_text(_j.dumps({"jobs": [
                {"id": "j9", "status": "done", "cwd": str(wt), "label": "old"},
                {"id": "j8", "status": "running", "cwd": "/elsewhere", "label": "other"}]}))
            chk("done job here / running job elsewhere -> not live", au.live_job_on_worktree(wt), None)
            au.QUEUE_STATE.write_text(_j.dumps({"jobs": [
                {"id": "j7", "status": "running", "cwd": str(wt), "label": "auto-refine-x-r1"}]}))
            chk("a running job whose cwd is this worktree -> live",
                (au.live_job_on_worktree(wt) or ("",))[0], "j7")
        finally:
            au.QUEUE_STATE = _qs
rp = au.refine_prompt("lib/bfmrLinkReconcile.ts", [],
                      [{"check": "baseline-clean",
                        "message": "1 tracked file(s) already modified: M lib/bfmrLinkReconcile.ts"}],
                      lang="typescript")
chk("refine prompt tells the model to revert a dirty TARGET (was: 'leave your edits in place')",
    ("git checkout -- lib/bfmrLinkReconcile.ts" in rp, "leave your edits in place" in rp), (True, False))
src = inspect.getsource(au.main) if hasattr(au, "main") else ""
whole = Path(AUTO).read_text()
chk("auto's preflight loop calls the reset for non-test targets",
    "for _u in enforce_target_at_head(wt, target):" in whole, True)

# ---- 4. prompts disambiguate the Scope line ----------------------------------
a = types.SimpleNamespace(intent="x", lang="typescript")
note = getattr(au, "author_scope_note", lambda *x: "")("lib/t.ts", "verify.test.ts")
chk("scope note: addressed to the CODING job, author must replace the SCAFFOLD guard",
    ("CODING job" in note, "REPLACE the SCAFFOLD guard" in note, "must NOT" in note), (True, True, True))
chk("author + continue prompt templates carry the note",
    whole.count("{author_scope_note(target, fx"), 2)   # call gained an edit_file extras arg

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAIL'}")
sys.exit(1 if fails else 0)
