#!/usr/bin/env python3
"""Reconcile the ollama handoff "awaiting action" bucket WITHOUT relying on
commit-message discipline.

WHY: dispatch-ack-hook.sh (post-commit) only acks a job whose 12-hex id appears
in the commit message. Two failure modes left jobs stuck in awaiting-action:
  * a merged fix whose hand-written commit message omitted the id, and/or a
    fast-forward merge that creates no commit on the default branch at all; and
  * a read-only diagnosis/research job, which never produces a diff and so can
    NEVER trigger a commit-keyed hook.
This reconciler closes both by looking at STATE (cwd, task_kind) instead of the
message. Two rules, both SAFE -- they only ACK (clear a job from
awaiting-action); they never touch repo content and always exit 0:

  1. CODING job whose dispatch-worktree branch is now merged into its repo's
     default branch (`git merge-base --is-ancestor HEAD <origin/HEAD>`). The
     merge IS the human's acceptance (merges only happen on accepted
     dispatches), so record verdict=approve -- same contract as the post-commit
     message hook -- then --acted --merged --commit <tip>. A gate=fail job that
     was merged by an explicit human override is cleared with --override-signoff
     naming the merge as the evidence.
  2. RESEARCH job (task_kind == 'research'): read-only by design, no diff to
     merge, so clear with plain --acted (NO verdict -- it is not a code decision).

NEVER acks a coding job that is NOT merged: a 0-file coding job is a FAILED
dispatch and must stay visible. Unknown/unreadable worktree -> skip (stays for
a human). Safe no-op when nothing qualifies.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
STATE = BIN / "ollama-queue-state.json"
HANDOFF = BIN / "handoff-emit.py"
SIGNOFF = BIN / "signoff.py"
LOGS = BIN / "ollama-queue-logs"


RC_TIMEOUT, RC_NOT_RUN = 124, 127   # never a git exit code (git uses 0, 1 and 128)


def _run(cmd, cwd=None, timeout=30, kill_wait=5.0):
    """(rc, stdout, stderr). A command that did not answer in `timeout` seconds is RC_TIMEOUT and one that could
    not start is RC_NOT_RUN, never 1: `git rev-parse --verify` exits 1 for "no such branch", and landed_state reads
    a gone branch as landed (2026-11-02: while the screen was locked, launchd's git blocked on Desktop access for
    30s at a time; a timed-out verify must not look like a squash-merged branch)."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:  # pragma: no cover - defensive
        return RC_NOT_RUN, "", str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # not subprocess.run: after its kill it waits for the child without a limit, and a git inside the
        # kernel's access check only dies when that call returns (Fable review, 2026-11-03)
        p.kill()
        try:
            p.communicate(timeout=kill_wait)
        except subprocess.TimeoutExpired:
            pass
        return RC_TIMEOUT, "", f"{' '.join(map(str, cmd[:4]))} did not answer in {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        p.kill()
        return RC_NOT_RUN, "", str(e)
    return p.returncode, (out or "").strip(), (err or "").strip()


# ---- pure decision logic (unit-tested by --self-test) -----------------------
def _is_authoring_label(label) -> bool:
    """An ollama-dispatch-auto HARNESS-authoring/refine job: it writes TASK.md /
    verify.sh, never the deliverable. Mirrors gate-on-complete._is_authoring_label."""
    return str(label or "").startswith(("auto-author-", "auto-refine-"))


def decide(task_kind: str | None, merged: bool | None, files: int | None,
           label: str | None = None, new_commits: bool | None = None) -> tuple[str, str]:
    """Return (action, reason). action in
    {'ack-research','ack-merged','ack-authoring','skip'}.

    merged is the branch-is-merged truth for a coding job (None if unknown/not a
    git worktree). files is the diff file count (may be None). new_commits is
    whether the worktree HEAD moved off the job's recorded launch baseline (None =
    no baseline recorded, the pre-2026-10-06 behaviour).

    VACUOUS MERGE (2026-10-06, acted.json "merged verified 0c858aa" on 15 auto-author
    jobs incl. FAILED 87ef87235fd3): a job that made NO commit has HEAD == its launch
    baseline == the default-branch tip, so `merge-base --is-ancestor HEAD main` is
    trivially true. That recorded a verified merge AND a signoff `approve` for work
    that was never committed, let alone merged. HEAD == baseline is not a merge.
    """
    if task_kind == "research":
        return "ack-research", "read-only research/diagnosis; no diff to act on"
    if merged is True and new_commits is False:
        return "skip", ("worktree HEAD is still the job's launch baseline -- the job made "
                        "no commit, so 'HEAD is an ancestor of main' is vacuous, not a merge")
    if merged is True and _is_authoring_label(label):
        # the harness author's worktree is shared with its slice's coding job; a
        # landed coding commit there is NOT this job's deliverable, so never record a
        # merge / signoff approve for it -- clear it as superseded instead.
        return "ack-authoring", ("authoring/refine job: its slice's coding commit landed; "
                                 "the harness is not itself a merged deliverable")
    if merged is True:
        # A 0-file coding job is a FAILED dispatch and must stay visible, even
        # when its branch has landed -- landing proves nothing about work that
        # never produced files. (files None = count unknown -> ack as before.)
        if files == 0:
            return "skip", ("branch landed but the job has 0 files -- a failed dispatch; "
                            "stays visible")
        return "ack-merged", "worktree branch merged into the default branch"
    # a coding job that is not (yet) merged, or whose merge state is unknown,
    # stays visible -- especially a 0-file coding job, which is a failed dispatch.
    return "skip", (
        "coding job not merged" if merged is False
        else "merge state unknown (worktree missing or not a git repo)"
    )


# ---- IO helpers -------------------------------------------------------------
def _state_by_id():
    try:
        jobs = json.loads(STATE.read_text()).get("jobs") or []
    except Exception:
        return {}
    out = {}
    for j in jobs:
        if j.get("id"):
            out[j["id"]] = j
    return out


def _awaiting_ids():
    rc, out, _ = _run(["python3", str(HANDOFF), "--json"])
    if rc != 0:
        return []
    try:
        d = json.loads(out)
    except Exception:
        return []
    rows = d.get("complete") or d.get("awaiting_action") or []
    return [(r.get("id"), r) for r in rows if isinstance(r, dict) and r.get("id")]


def _gate(job_id):
    try:
        return json.loads((LOGS / f"{job_id}.gate.json").read_text())
    except Exception:
        return {}


def _cwd_for(job_id, state_job, gate):
    for src in (state_job.get("cwd") if state_job else None, gate.get("job_cwd")):
        if src and Path(src).is_dir():
            return Path(src)
    return None


def _default_ref(cwd: Path):
    rc, out, _ = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cwd)
    if rc == 0 and out:
        return out
    for cand in ("origin/main", "origin/master", "main", "master"):
        rc, _, _ = _run(["git", "rev-parse", "--verify", "--quiet", cand], cwd=cwd)
        if rc == 0:
            return cand
    return None


def _launch_head(state_job, gate):
    """The job's recorded launch-baseline HEAD sha, or None (older jobs)."""
    for src in (state_job or {}, gate or {}):
        lb = src.get("launch_baseline")
        if isinstance(lb, dict) and lb.get("head"):
            return str(lb["head"])
    return None


def _head_moved(cwd: Path, baseline):
    """True/False: did the worktree HEAD move off `baseline`? None if unknown."""
    if not baseline:
        return None
    rc, tip, _ = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
    rc2, base, _ = _run(["git", "rev-parse", "--verify", "--quiet", f"{baseline}^{{commit}}"],
                        cwd=cwd)
    if rc != 0 or rc2 != 0 or not tip or not base:
        return None
    return tip != base


def _branch_merged(cwd: Path):
    """(merged_bool, tip_short, repo_name) or (None, None, None) if undeterminable."""
    rc, _, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if rc != 0:
        return None, None, None
    ref = _default_ref(cwd)
    if not ref:
        return None, None, None
    _, tip, _ = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
    _, tip_short, _ = _run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
    rc, _, _ = _run(["git", "merge-base", "--is-ancestor", "HEAD", ref], cwd=cwd)
    merged = rc == 0
    _, url, _ = _run(["git", "config", "--get", "remote.origin.url"], cwd=cwd)
    repo = None
    if url:
        repo = url.rstrip("/").rsplit("/", 1)[-1]
        if repo.endswith(".git"):
            repo = repo[:-4]
    return merged, tip_short, repo


def landed_state(repo_path, branch) -> str:
    """'landed' | 'not-landed' | 'unknown' -- does the dispatch branch's work
    live in the repo's default ref?

    SQUASH-SAFE: our land flow squash-merges, so the branch tip is NEVER an
    ancestor of the squashed commit. The signal that survives a squash merge
    (and the reaping of the worktree) is the BRANCH ITSELF: the land flow
    deletes it after a successful squash-merge, so branch-gone is the normal
    terminal state. A branch that still exists and IS an ancestor of the
    default ref was fast-forward/non-squash merged (not yet cleaned). Never
    raises -- any git error or missing input yields 'unknown'.
    """
    if not repo_path or not branch:
        return "unknown"
    try:
        cwd = Path(repo_path)
        rc, _, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
        if rc != 0:
            return "unknown"
        ref = _default_ref(cwd)
        if not ref:
            return "unknown"
        rc, _, _ = _run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=cwd)
        if rc == 0:
            # branch still exists: landed only if it is an ancestor of the
            # default ref (fast-forward / non-squash merge, not yet cleaned).
            rc2, _, _ = _run(["git", "merge-base", "--is-ancestor", branch, ref], cwd=cwd)
            return {0: "landed", 1: "not-landed"}.get(rc2, "unknown")
        if rc != 1:
            # not git's "no such ref" (1): a timeout, a fatal error (128) or a git
            # that never ran says nothing about the branch.
            return "unknown"
        # branch is GONE: the land flow deletes it after a successful
        # squash-merge -- that is the normal terminal state.
        return "landed"
    except Exception:  # pragma: no cover - defensive; _run already swallows
        return "unknown"


def _gate_needs_override(gate):
    v = str(gate.get("verdict") or "").lower()
    return v in ("fail", "no-go", "nogo", "reject") or bool(gate.get("signoff_required"))


def reconcile(dry_run=False, verbose=True):
    state = _state_by_id()
    n = 0
    for job_id, row in _awaiting_ids():
        sj = state.get(job_id) or {}
        gate = _gate(job_id)
        task_kind = sj.get("task_kind")
        files = row.get("files")
        merged = tip = repo = new_commits = None
        label = row.get("label") or sj.get("label") or gate.get("job_label")
        cwd = _cwd_for(job_id, sj, gate)
        if cwd is not None and task_kind != "research":
            # LIVE worktree: the worktree-relative merge check is authoritative --
            # but only once the job actually committed something (see decide()).
            merged, tip, repo = _branch_merged(cwd)
            new_commits = _head_moved(cwd, _launch_head(sj, gate))
        elif cwd is None and task_kind != "research":
            # Worktree was REAPED after landing: recover the source repo path +
            # dispatch branch from job state (both survive reaping) and use the
            # squash-safe landed_state() instead of a worktree-relative check.
            # The ack tolerates an unknown commit (tip stays None -> "unknown").
            repo_path = sj.get("repo") or sj.get("source_repo")
            branch = sj.get("worktree_branch")
            if repo_path and branch:
                st = landed_state(repo_path, branch)
                if st == "landed":
                    merged = True
                    repo = Path(repo_path).name  # ack carries the basename
                elif st == "not-landed":
                    merged = False
                # 'unknown' leaves merged=None -> decide() keeps it visible.
        action, reason = decide(task_kind, merged, files, label, new_commits)
        if action == "skip":
            if verbose:
                print(f"[reconcile] skip {job_id} ({row.get('label')}): {reason}")
            continue
        if dry_run:
            print(f"[reconcile] WOULD {action} {job_id} ({row.get('label')}): {reason}"
                  + (f" @ {repo}@{tip}" if action == "ack-merged" else ""))
            n += 1
            continue
        if action == "ack-research":
            _run(["python3", str(HANDOFF), "--acted", job_id,
                  "--reason", f"auto-reconcile: {reason}"])
            print(f"[reconcile] {job_id} acked (research; no diff to act on)")
        elif action == "ack-authoring":
            # plain --acted, NO --merged and NO signoff approve: not a code decision.
            _run(["python3", str(HANDOFF), "--acted", job_id,
                  "--reason", f"auto-reconcile: {reason}"])
            print(f"[reconcile] {job_id} acked (authoring job superseded by its coding commit)")
        else:  # ack-merged
            _run(["python3", str(SIGNOFF), "--verdict", "approve", job_id,
                  "--conditions", f"merged {tip} (reconcile-on-push)"])
            cmd = ["python3", str(HANDOFF), "--acted", job_id,
                   "--merged", job_id, "--commit", tip or "unknown"]
            if repo:
                cmd += ["--repo", repo]
            if _gate_needs_override(gate):
                cmd += ["--override-signoff",
                        f"auto-reconcile: branch merged into default @ {tip}; "
                        "the merge is the human acceptance"]
            rc, _, err = _run(cmd)
            if rc != 0:  # last resort: force the override path
                cmd += ["--override-signoff", f"auto-reconcile: merged @ {tip}"]
                _run(cmd)
            print(f"[reconcile] {job_id} acked (merged into default @ {repo}@{tip})")
        n += 1
    if verbose and n == 0:
        print("[reconcile] nothing to reconcile")
    return n


# ---- self-test --------------------------------------------------------------
def _self_test():
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    # pure decision table
    check("research always acked", decide("research", None, 0)[0], "ack-research")
    check("research acked even if it somehow has files", decide("research", None, 3)[0], "ack-research")
    check("coding merged -> ack", decide("coding", True, 1)[0], "ack-merged")
    check("coding merged but 0-file failed dispatch -> skip", decide("coding", True, 0)[0], "skip")
    check("coding NOT merged -> skip", decide("coding", False, 1)[0], "skip")
    check("coding 0-file failed dispatch, not merged -> skip", decide("coding", False, 0)[0], "skip")
    check("coding merge-unknown -> skip", decide("coding", None, 1)[0], "skip")
    check("None task_kind + merged -> ack (treated as coding)", decide(None, True, 1)[0], "ack-merged")
    # VACUOUS MERGE (2026-10-06): HEAD never left the launch baseline -> no merge.
    check("merged but HEAD == launch baseline (no commit) -> skip, never ack-merged",
          decide("coding", True, 2, "x-fix", new_commits=False)[0], "skip")
    check("merged with a real commit off the baseline -> ack-merged",
          decide("coding", True, 2, "x-fix", new_commits=True)[0], "ack-merged")
    check("auto-author job, no commit of its own -> skip",
          decide("coding", True, 2, "auto-author-a-s1", new_commits=False)[0], "skip")
    check("auto-author job whose shared worktree's coding commit landed -> plain ack, "
          "never a merge/signoff approve",
          decide("coding", True, 2, "auto-refine-a-s1-r1", new_commits=True)[0],
          "ack-authoring")

    # git plumbing: build a repo, a merged branch and an unmerged branch
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        origin = td / "origin"
        origin.mkdir()
        _run(["git", "init", "-q", "-b", "main", "."], cwd=origin)
        _run(["git", "config", "user.email", "t@t"], cwd=origin)
        _run(["git", "config", "user.name", "t"], cwd=origin)
        (origin / "f.txt").write_text("base\n")
        _run(["git", "add", "-A"], cwd=origin)
        _run(["git", "commit", "-qm", "base"], cwd=origin)
        # a merged branch: commit then fast-forward main to it
        _run(["git", "checkout", "-q", "-b", "feat"], cwd=origin)
        (origin / "f.txt").write_text("base\nfeat\n")
        _run(["git", "commit", "-qam", "feat"], cwd=origin)
        _run(["git", "checkout", "-q", "main"], cwd=origin)
        _run(["git", "merge", "-q", "--ff-only", "feat"], cwd=origin)
        # clone so origin/HEAD exists, then make an UNMERGED branch in the clone
        clone = td / "clone"
        _run(["git", "clone", "-q", str(origin), str(clone)])
        _run(["git", "checkout", "-q", "-b", "merged-branch", "feat"], cwd=clone)
        m, tip, repo = _branch_merged(clone)
        check("merged branch detected", m, True)
        check("repo name derived", repo, "origin")
        _run(["git", "checkout", "-q", "-b", "unmerged", "main"], cwd=clone)
        (clone / "f.txt").write_text("base\nfeat\nlocal-only\n")
        _run(["git", "commit", "-qam", "local"], cwd=clone)
        m2, _, _ = _branch_merged(clone)
        check("unmerged branch detected as NOT merged", m2, False)
        # the 0c858aa shape: a fresh worktree at main's tip, no commit made.
        _run(["git", "checkout", "-q", "-b", "nocommit", "origin/main"], cwd=clone)
        _, base_sha, _ = _run(["git", "rev-parse", "HEAD"], cwd=clone)
        m3, _, _ = _branch_merged(clone)
        check("vacuous: an uncommitted job at main's tip IS 'an ancestor' (the trap)", m3, True)
        check("vacuous: _head_moved sees HEAD still at the launch baseline",
              _head_moved(clone, _launch_head({"launch_baseline": {"head": base_sha}}, {})),
              False)
        check("vacuous: end to end the job is NOT acked as merged",
              decide("coding", m3, 1, "auto-author-x-s0",
                     _head_moved(clone, base_sha))[0], "skip")
        _run(["git", "checkout", "-q", "unmerged"], cwd=clone)
        check("_head_moved: a real commit off the baseline -> True",
              _head_moved(clone, base_sha), True)
        check("_head_moved: no recorded baseline -> None (legacy behaviour)",
              _head_moved(clone, None), None)

    # landed_state: squash-safe landing detection against the same fixture
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        origin = td / "origin"
        origin.mkdir()
        _run(["git", "init", "-q", "-b", "main", "."], cwd=origin)
        _run(["git", "config", "user.email", "t@t"], cwd=origin)
        _run(["git", "config", "user.name", "t"], cwd=origin)
        (origin / "f.txt").write_text("base\n")
        _run(["git", "add", "-A"], cwd=origin)
        _run(["git", "commit", "-qm", "base"], cwd=origin)
        # a branch that IS an ancestor of main (ff-merged, not yet cleaned)
        _run(["git", "checkout", "-q", "-b", "feat"], cwd=origin)
        (origin / "f.txt").write_text("base\nfeat\n")
        _run(["git", "commit", "-qam", "feat"], cwd=origin)
        _run(["git", "checkout", "-q", "main"], cwd=origin)
        _run(["git", "merge", "-q", "--ff-only", "feat"], cwd=origin)
        # clone so origin/HEAD exists; a fresh clone has no local branches,
        # so the pending branch is created inside the clone (still-pending:
        # not an ancestor of main) and later deleted to simulate the
        # squash-merge terminal state
        clone = td / "clone"
        _run(["git", "clone", "-q", str(origin), str(clone)])
        # local feat at main's tip (== the ff-merged branch tip): exists AND
        # is an ancestor of origin/main -> the fast-forward path
        _run(["git", "branch", "feat", "origin/main"], cwd=clone)
        _run(["git", "checkout", "-q", "-b", "pending", "origin/main"], cwd=clone)
        (clone / "f.txt").write_text("base\nfeat\nlocal-only\n")
        _run(["git", "commit", "-qam", "local"], cwd=clone)
        check("landed_state: existing ancestor branch -> landed",
              landed_state(clone, "feat"), "landed")
        check("landed_state: existing non-ancestor branch -> not-landed",
              landed_state(clone, "pending"), "not-landed")
        _run(["git", "checkout", "-q", "main"], cwd=clone)
        _run(["git", "branch", "-D", "pending"], cwd=clone)
        check("landed_state: deleted branch (squash-merge terminal state) -> landed",
              landed_state(clone, "pending"), "landed")
        # a git that does not answer is not a deleted branch (2026-11-02)
        real_run = globals()["_run"]

        def _stalled_verify(cmd, cwd=None, timeout=30):
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return RC_TIMEOUT, "", "did not answer"
            return real_run(cmd, cwd=cwd, timeout=timeout)

        def _stalled_merge_base(cmd, cwd=None, timeout=30):
            if cmd[:2] == ["git", "merge-base"]:
                return RC_TIMEOUT, "", "did not answer"
            return real_run(cmd, cwd=cwd, timeout=timeout)
        globals()["_run"] = _stalled_verify
        try:
            check("landed_state: a timed-out branch check -> unknown, not landed",
                  landed_state(clone, "pending"), "unknown")
        finally:
            globals()["_run"] = real_run
        globals()["_run"] = _stalled_merge_base
        try:
            check("landed_state: a timed-out ancestry check -> unknown",
                  landed_state(clone, "feat"), "unknown")
        finally:
            globals()["_run"] = real_run
    check("_run: a command past its timeout is RC_TIMEOUT, not 1",
          _run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)[0], RC_TIMEOUT)
    # a child the kill does not finish (here a grandchild keeps the pipe open, as a git held in the kernel's
    # access check keeps its call): the wait after the kill is bounded too, never subprocess.run's unbounded one
    _t0 = time.monotonic()
    _rc = _run(["sh", "-c", "sleep 4 & sleep 4"], timeout=0.3, kill_wait=0.5)[0]
    check("_run: the wait after a kill is bounded", (_rc, time.monotonic() - _t0 < 2.5), (RC_TIMEOUT, True))
    check("_run: a command that cannot start is RC_NOT_RUN",
          _run(["/no/such/binary-xyz"])[0], RC_NOT_RUN)
    # degenerate inputs never raise and are 'unknown'
    check("landed_state: missing repo -> unknown", landed_state("/no/such/repo", "feat"), "unknown")
    check("landed_state: falsy branch -> unknown", landed_state(os.getcwd(), ""), "unknown")

    # --- stranded-chain sweep (2026-09-23) -----------------------------------
    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _R(0, "# sweep: alpha: stranded marker -- firing one detached advance")
    check("sweep_slice_chains runs the slicer's --sweep (no plan argument)",
          sweep_slice_chains(run=_fake_run, slicer=__file__) == 0
          and calls and calls[0][-1] == "--sweep" and len(calls[0]) == 3, True)
    check("sweep_slice_chains passes the slicer's rc through",
          sweep_slice_chains(run=lambda *a, **k: _R(3, "", "boom"), slicer=__file__), 3)
    check("a missing slicer is a quiet no-op, never a crash",
          sweep_slice_chains(run=_fake_run, slicer="/no/such/ollama-dispatch-slice"), None)

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired("x", 120)
    check("a hung/broken sweep is contained (advisory: never breaks the ack pass)",
          sweep_slice_chains(run=_raise, slicer=__file__), None)
    src = Path(__file__).read_text()
    check("the launchd pass calls the sweep, the --quiet hook path and --dry-run do not",
          '"--quiet" not in args and "--dry-run" not in args' in src
          and "sweep_slice_chains()" in src, True)

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


SLICER = BIN / "ollama-dispatch-slice"


def sweep_slice_chains(run=subprocess.run, slicer=None):
    """Re-arm any slice chain whose completion event was stranded (2026-09-23).

    The slicer's chain is driven only by the job-completion hook. When the hook
    fires while a driver holds the plan lock it leaves a marker for that driver to
    consume on exit -- and a driver killed by a Bash-tool timeout / closed terminal
    never exits cleanly, so the marker sits there and the plan goes silently idle
    (arr-webhook-yearly-upgrade-batches, 2026-09-23: two `slice-autofeed (advance)`
    hook fires, zero advances, until a human ran --execute 40 min later). The
    operator levers had the same hole. `ollama-dispatch-slice --sweep` is the
    periodic consumer for those stranded events; this launchd job (StartInterval
    300) is the natural place to run it. Advisory: it must never break an ack pass,
    so it is contained end to end and prints only when something was done."""
    slicer = Path(slicer) if slicer else SLICER
    if not slicer.exists():
        return None
    try:
        r = run([sys.executable, str(slicer), "--sweep"],
                capture_output=True, text=True, timeout=120)
    except Exception as e:                      # noqa: BLE001 -- advisory
        print(f"[reconcile] slice sweep skipped: {type(e).__name__}: {str(e)[:160]}")
        return None
    out = (r.stdout or "").strip()
    if out:
        print(out)
    if r.returncode != 0:
        print(f"[reconcile] slice sweep rc={r.returncode}: {(r.stderr or '').strip()[-400:]}")
    return r.returncode


def auto_retire_pass(dry_run=False):
    """Best-effort `qctl auto-retire` (retire older bundle jobs superseded by a PASS).
    Errors are logged to stderr, never fatal."""
    try:
        cmd = ["python3", str(BIN / "qctl"), "auto-retire"] + (["--dry-run"] if dry_run else [])
        rc, out, err = _run(cmd)
        if out:
            print(out)
        if rc != 0:
            print(f"dispatch-ack-reconcile: auto-retire rc={rc}: {err[:300]}", file=sys.stderr)
    except Exception as e:  # never fatal
        print(f"dispatch-ack-reconcile: auto-retire failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(0 if _self_test() else 1)
    # --check-landed REPO BRANCH: print exactly one lowercase token (landed /
    # not-landed / unknown) from landed_state() and exit 0. Does NOT run the
    # reconcile loop.
    if "--check-landed" in args:
        i = args.index("--check-landed")
        rest = args[i + 1:]
        repo = rest[0] if len(rest) >= 1 else ""
        branch = rest[1] if len(rest) >= 2 else ""
        print(landed_state(repo, branch))
        sys.exit(0)
    # --quiet: only print actual acks (used from the post-commit hook, which
    # fires on every commit -- the idle/skip chatter would be noise there).
    reconcile(dry_run="--dry-run" in args, verbose="--quiet" not in args)
    # The launchd pass (no --quiet: the post-commit hook path stays cheap, and no
    # --dry-run: a dry run must not start drivers) also re-arms stranded chains.
    if "--quiet" not in args and "--dry-run" not in args and "--no-sweep" not in args:
        sweep_slice_chains()
    if "--quiet" not in args and "--no-sweep" not in args:
        auto_retire_pass(dry_run="--dry-run" in args)
    sys.exit(0)
