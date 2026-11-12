#!/usr/bin/env python3
"""Behavioural tests for the worker's zero-diff / vacuous-pass anti-fraud guard.

WHY THIS FILE EXISTS
--------------------
The guard's job is to catch a CODING dispatch that ends with a byte-for-byte
unchanged worktree and zero successful edits -- a job that claims success while
doing no work. Finding 3 (2026-09-19) carved out ONE exemption: an auto-author /
auto-refine dispatch whose deliverable is a STATE ("the harness discriminates and
is satisfiable") rather than a diff, for which changing nothing is correct.

Finding 3 shipped with 11 unit cases and 4 revert-tests that were run once and
then thrown away. Independent review flagged that across four pipeline-tooling
fixes NONE retained a test, so every proof was a one-time act rather than a
standing guard -- and then found a real hole in the exemption itself. This file
is the standing guard. Run it directly; it needs no queue, model, network or git.

The cases that actually matter are the ATTACK cases: an exemption on an anti-fraud
check is only as good as its narrowness.
"""
import importlib.util
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "ollama-worker.py"

_spec = importlib.util.spec_from_file_location("_ollama_worker_under_test", WORKER)
_w = importlib.util.module_from_spec(_spec)
sys.modules["_ollama_worker_under_test"] = _w
_spec.loader.exec_module(_w)

MARKER = _w.HARNESS_SELFCHECK_MARKER

# The verify command an authoring dispatch ACTUALLY runs, read out of
# ollama-dispatch-auto rather than retyped here. Independent review caught the
# first version of this file hardcoding "python3 auto-harness-check.py --strict",
# a string that exists nowhere in the pipeline. That is the dangerous kind of
# test: if ollama-dispatch-auto ever renames the script or changes verify_cmd,
# the worker silently stops exempting authoring self-checks -- reinstating the
# exact 10-wrongly-failed-jobs regression Finding 3 fixed -- while this suite
# stays green. Binding to the live string makes that rename fail HERE instead.
_DISPATCH_AUTO = Path(__file__).resolve().parent / "ollama-dispatch-auto"


def _live_harness_verify_cmd():
    """The literal verify_cmd ollama-dispatch-auto assigns for the harness check."""
    import re
    src = _DISPATCH_AUTO.read_text()
    m = re.search(r'verify_cmd\s*=\s*"([^"]*auto-harness-check\.py[^"]*)"', src)
    if not m:
        raise SystemExit(
            "FATAL: could not find the harness verify_cmd in ollama-dispatch-auto.\n"
            "Either the authoring dispatch no longer runs auto-harness-check.py, or it\n"
            "builds the command differently. Do NOT relax this test to make it pass --\n"
            "check that zero_diff_verdict still exempts the real authoring path first,\n"
            "because if it does not, every auto-author/auto-refine job with a correct\n"
            "zero diff is being failed again.")
    return m.group(1)


HARNESS_CMD = _live_harness_verify_cmd()
CODING_CMD = "bash verify.sh"

_fails = []
_n = 0


def check(name, got, want):
    global _n
    _n += 1
    if got == want:
        print(f"  [ok  ] {name}")
    else:
        print(f"  [FAIL] {name}   -> got {got!r}, want {want!r}")
        _fails.append(name)


def zdv(**kw):
    base = dict(task_kind="coding", stdout="", stderr="", tree_changed=False,
                files_modified_count=0, is_git_repo=True,
                run_bash_success_count=0, verify_cmd=CODING_CMD)
    base.update(kw)
    return _w.zero_diff_verdict(**base)


print("zero-diff verdict: core behaviour")
# The fraud case. This is the one the whole guard exists for: a coding dispatch,
# unchanged tree, no edits, no marker -> must be overridden to FAILED.
check("fraud: coding + unchanged tree + 0 edits -> fail", zdv(), "fail")
check("changed tree stands as a pass", zdv(tree_changed=True), "ok")
check("edits but no tree change -> warn (not silent)",
      zdv(files_modified_count=3), "warn")
check("research has no file deliverable -> ok",
      zdv(task_kind="research"), "ok")
check("degraded (non-git) + no edits + no run_bash -> fail",
      zdv(is_git_repo=False), "fail")
check("degraded + run_bash succeeded -> warn",
      zdv(is_git_repo=False, run_bash_success_count=4), "warn")
check("degraded + edits -> ok",
      zdv(is_git_repo=False, files_modified_count=2), "ok")

print("\nzero-diff verdict: the authoring self-check exemption")
check("exempt: harness check + marker alone on a line (stdout)",
      zdv(stdout=f"running...\n{MARKER}\n", verify_cmd=HARNESS_CMD), "exempt")
check("exempt: same via stderr",
      zdv(stderr=f"{MARKER}\n", verify_cmd=HARNESS_CMD), "exempt")
check("exempt: surrounding whitespace on the marker line is tolerated",
      zdv(stdout=f"  {MARKER}  \n", verify_cmd=HARNESS_CMD), "exempt")
# The exemption is tested before tree_changed, so a self-check that DID change the
# tree reports "exempt" rather than "ok". Both are pass outcomes -- they differ only
# in the log line -- so what is pinned here is the property that matters: an
# authoring self-check is never overridden to FAILED, whatever the tree did.
check("self-check with a changed tree is still not a failure",
      zdv(stdout=MARKER, verify_cmd=HARNESS_CMD, tree_changed=True) != "fail", True)

print("\nATTACK cases -- a coding dispatch must never reach the exemption")
# The hole independent review found. The marker is not secret: write_harness_check()
# writes auto-harness-check.py -- containing this exact literal -- INTO the worktree
# where a coding slice's verify.sh later runs. So any verify output that merely
# surfaces that file's text used to claim the exemption.
check("ATTACK: marker quoted inside a larger line (grep hit) is NOT a whole line",
      zdv(stdout=f"auto-harness-check.py:12:    print('{MARKER}')\n",
          verify_cmd=HARNESS_CMD), "fail")
check("ATTACK: marker inside a traceback line does not qualify",
      zdv(stdout=f'  File "x.py", line 3, in f: assert out == "{MARKER}"\n',
          verify_cmd=HARNESS_CMD), "fail")
check("ATTACK: marker with a prefix on the same line does not qualify",
      zdv(stdout=f"checking: {MARKER}\n", verify_cmd=HARNESS_CMD), "fail")
check("ATTACK: marker with a trailing suffix does not qualify",
      zdv(stdout=f"{MARKER} -- but I changed nothing\n",
          verify_cmd=HARNESS_CMD), "fail")
# The load-bearing condition: WHICH INSTRUMENT RAN.
check("ATTACK: perfect marker but the verify is a coding slice's verify.sh",
      zdv(stdout=f"{MARKER}\n", verify_cmd=CODING_CMD), "fail")
check("ATTACK: perfect marker but the instrument is unknown -> fail closed",
      zdv(stdout=f"{MARKER}\n", verify_cmd=None), "fail")
check("ATTACK: perfect marker, empty verify command -> fail closed",
      zdv(stdout=f"{MARKER}\n", verify_cmd=""), "fail")
check("ATTACK: a coding verify that cats the harness file gets nothing",
      zdv(stdout=f"$ cat auto-harness-check.py\n{MARKER}\n",
          verify_cmd=CODING_CMD), "fail")
# Near-miss markers.
check("ATTACK: truncated marker does not qualify",
      zdv(stdout="VERIFY_OK: harness discriminates\n",
          verify_cmd=HARNESS_CMD), "fail")
check("ATTACK: bare VERIFY_OK does not qualify",
      zdv(stdout="VERIFY_OK\n", verify_cmd=HARNESS_CMD), "fail")
check("ATTACK: case-altered marker does not qualify",
      zdv(stdout=MARKER.upper() + "\n", verify_cmd=HARNESS_CMD), "fail")

# The instrument token must stay NARROW, not just present. Review found that
# broadening HARNESS_SELFCHECK_SCRIPT to "harness", "check" or "auto-harness-check"
# all survived the original suite, because every negative case used "bash verify.sh"
# -- which shares no substring with any plausible loosening. "check" is exactly the
# kind of widening a future maintainer makes, and real coding verifies do contain it.
# These cases pin the narrowness by using coding verifies built from generic words.
for _generic in ("bash verify.sh && python3 check_literals.py",
                 "bash verify.sh && ./run-harness.sh",
                 "python3 -m pytest tests/test_harness_check.py",
                 "bash verify.sh # mirrors auto-harness-check logic",
                 "bash check.sh"):
    check(f"ATTACK: generic coding verify {_generic!r} is not the instrument",
          zdv(stdout=f"{MARKER}\n", verify_cmd=_generic), "fail")

print("\nexempt-branch TARGET residue revert: the decision")
# The hole THIS section exists for (2026-09-24, job 765661efeb0c /
# auto-refine-...-s1-fastapi-existing-route-p-r1). zero_diff_verdict returns
# "exempt" before it looks at tree_changed, so the exempt branch used to accept the
# run without asking what it had left in the tree. That run applied refimpl.py to
# the TARGET via its own run_bash (legitimate -- it is how a refine round proves the
# fixture discriminates) but outside auto-harness-check.py's try/finally revert(),
# so it completed with the reference solution sitting uncommitted in the target. The
# next stage's baseline-clean pre-flight NO-GO'd on `M server.py` and escalated the
# slice, which read as an unrelated "queue stuck" incident.


def rv(**kw):
    base = dict(verdict="exempt", is_git_repo=True, tree_changed=True,
                target="server.py", target_dirty_at_start=False,
                target_dirty_now=True)
    base.update(kw)
    return _w.harness_exempt_revert_decision(**base)


# THE LIVE CASE. If this one ever goes False the incident is back.
check("live case: exempt + target clean at start + dirty now -> REVERT",
      rv(), (True, "revert-target"))

# The case the exemption was ADDED for, which this must not break: a resumed
# authoring run / a refine round enqueued for a reason that turned out not to exist,
# where the model correctly changed nothing. Nothing to revert, and no noise.
check("no-op: exempt + unchanged tree (the 'nothing to do' case)",
      rv(tree_changed=False), (False, "tree-unchanged"))
check("no-op: exempt + changed tree but the target itself is clean",
      rv(target_dirty_now=False), (False, "target-already-clean"))

# ABSTAIN cases. Fail-safe direction is DO NOT REVERT: destroying state the guard
# cannot prove it created is strictly worse than leaving residue for the next
# pre-flight to catch loudly.
check("abstain: target was ALREADY dirty at dispatch start -> do not revert",
      rv(target_dirty_at_start=True), (False, "target-dirty-at-dispatch-start"))
check("abstain: target start state undetermined -> do not revert",
      rv(target_dirty_at_start=None), (False, "target-start-state-unknown"))
check("abstain: no declared target in .dispatch-harness.json",
      rv(target=None), (False, "no-declared-target"))
check("abstain: cwd is not a git repo",
      rv(is_git_repo=False), (False, "not-a-git-repo"))

# NARROWNESS. This must never fire for a non-exempt verdict -- a coding dispatch's
# diff IS its deliverable, and reverting the target would delete the work.
for _v in ("ok", "fail", "warn"):
    check(f"NARROWNESS: verdict {_v!r} never reverts the target",
          rv(verdict=_v), (False, "not-exempt"))

print("\nsnapshot_path_dirty: reading the start snapshot the guard already keeps")
_SNAP_CLEAN = "abc123\n---STATUS---\n\n---DIFF---\n"
_SNAP_DIRTY = "abc123\n---STATUS---\n M server.py\n?? notes.txt\n---DIFF---\ndiff --git a/x\n"
check("start snapshot with a clean tree -> target was not dirty",
      _w.snapshot_path_dirty(_SNAP_CLEAN, "server.py"), False)
check("start snapshot showing ` M server.py` -> target WAS dirty",
      _w.snapshot_path_dirty(_SNAP_DIRTY, "server.py"), True)
check("an untracked `?? notes.txt` counts as dirty for that path",
      _w.snapshot_path_dirty(_SNAP_DIRTY, "notes.txt"), True)
check("a path absent from the status block is clean",
      _w.snapshot_path_dirty(_SNAP_DIRTY, "app/main.py"), False)
# Substring accidents: 'server.py' must not match 'other_server.py' or a subpath.
check("NARROWNESS: a path that merely CONTAINS the target name is not the target",
      _w.snapshot_path_dirty("h\n---STATUS---\n M pkg/other_server.py\n---DIFF---\n",
                             "server.py"), False)
# Renames name two paths; either side is a path a revert would be reasoning about.
check("a rename's destination counts as dirty",
      _w.snapshot_path_dirty("h\n---STATUS---\nR  old.py -> server.py\n---DIFF---\n",
                             "server.py"), True)
check("a rename's source counts as dirty",
      _w.snapshot_path_dirty("h\n---STATUS---\nR  server.py -> new.py\n---DIFF---\n",
                             "server.py"), True)
# UNDETERMINED must stay distinguishable from "clean" -- it drives an abstain, and
# collapsing the two into False is what would make this guard delete real work.
check("no snapshot at all -> undetermined (None), NOT clean",
      _w.snapshot_path_dirty(None, "server.py"), None)
check("a snapshot missing the delimiters -> undetermined",
      _w.snapshot_path_dirty("some legacy string", "server.py"), None)
check("a git-QUOTED porcelain path we did not match -> undetermined, not clean",
      _w.snapshot_path_dirty('h\n---STATUS---\n M "caf\\303\\251.py"\n---DIFF---\n',
                             "server.py"), None)
check("no target -> undetermined",
      _w.snapshot_path_dirty(_SNAP_DIRTY, None), None)

print("\npredicate: is_harness_selfcheck_verify directly")
check("predicate: whole line + harness cmd",
      _w.is_harness_selfcheck_verify(MARKER, "", HARNESS_CMD), True)
check("predicate: substring of a line is rejected",
      _w.is_harness_selfcheck_verify(f"x {MARKER} y", "", HARNESS_CMD), False)
check("predicate: wrong instrument is rejected",
      _w.is_harness_selfcheck_verify(MARKER, "", CODING_CMD), False)
check("predicate: no instrument is rejected",
      _w.is_harness_selfcheck_verify(MARKER, "", None), False)

print("\nEND-TO-END in a real git repo: does the residue actually leave the tree?")
# The unit cases above prove the DECISION. They cannot prove the tree is really
# restored, which is the property the next stage's pre-flight actually measures --
# so this builds a throwaway git repo shaped like a dispatch worktree (committed
# target + .dispatch-harness.json), reproduces the live residue, and checks the
# bytes on disk. Composes exactly the calls the worker's exempt branch makes; the
# wiring assertion below pins that the branch really makes them.
import json
import os
import subprocess
import tempfile


def _git(wt, *a):
    return subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True)


def _mk_worktree(td, target="server.py", baseline="def route():\n    return 1\n"):
    wt = Path(td)
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    (wt / target).write_text(baseline)
    (wt / "test_fixture.py").write_text("# cases\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": target}))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "sealed baseline")
    return wt


def _exempt_branch(wt, start_snapshot):
    """Exactly what the worker's exempt branch does, in the same order."""
    ht = _w.declared_harness_target(wt)
    do, why = _w.harness_exempt_revert_decision(
        "exempt", True, _w._git_worktree_snapshot(wt) != start_snapshot, ht,
        target_dirty_at_start=_w.snapshot_path_dirty(start_snapshot, ht),
        target_dirty_now=_w._git_path_is_dirty(wt, ht) if ht else None)
    if do:
        _w._git_restore_path(wt, ht)
    return why


if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
    print("  [SKIP] git unavailable")
else:
    BASE = "def route():\n    return 1\n"
    REFIMPL_OUT = BASE + "\n# NEW: reference solution block\ndef extra():\n    return 2\n"

    # (1) THE LIVE CASE. Target clean at dispatch start; the model applies refimpl
    # to it mid-run via run_bash and it is never reverted; fixture edited too (that
    # is the round's real deliverable and MUST survive).
    with tempfile.TemporaryDirectory() as td:
        wt = _mk_worktree(td)
        snap0 = _w._git_worktree_snapshot(wt)
        (wt / "server.py").write_text(REFIMPL_OUT)             # refimpl residue
        (wt / "test_fixture.py").write_text("# cases\n# more cases\n")  # deliverable
        why = _exempt_branch(wt, snap0)
        check("E2E live case: decision was revert-target", why, "revert-target")
        check("E2E: refimpl residue is GONE from the target",
              (wt / "server.py").read_text(), BASE)
        check("E2E: `git status` is clean for the target (next pre-flight passes)",
              "server.py" in _git(wt, "status", "--porcelain").stdout, False)
        # THE THING A WHOLE-TREE `git checkout -- .` WOULD HAVE DESTROYED. This is
        # the bg-captcha-s3-vision regression; if it ever fails, the revert has been
        # widened past the declared target and is eating the round's deliverable.
        check("E2E: the fixture deliverable SURVIVED the revert",
              (wt / "test_fixture.py").read_text(), "# cases\n# more cases\n")

    # (2) The no-op case the exemption exists for: nothing changed at all.
    with tempfile.TemporaryDirectory() as td:
        wt = _mk_worktree(td)
        snap0 = _w._git_worktree_snapshot(wt)
        why = _exempt_branch(wt, snap0)
        check("E2E resumed-run no-op: nothing to do", why, "tree-unchanged")
        check("E2E no-op: target untouched", (wt / "server.py").read_text(), BASE)

    # (3) Tree changed, but only the fixture -- the target is clean, so this must
    # not fire (and must not touch the fixture).
    with tempfile.TemporaryDirectory() as td:
        wt = _mk_worktree(td)
        snap0 = _w._git_worktree_snapshot(wt)
        (wt / "test_fixture.py").write_text("# only the fixture moved\n")
        why = _exempt_branch(wt, snap0)
        check("E2E fixture-only round: target already clean -> no revert",
              why, "target-already-clean")
        check("E2E fixture-only: fixture edit survived",
              (wt / "test_fixture.py").read_text(), "# only the fixture moved\n")

    # (4) ABSTAIN: the target was ALREADY dirty when the dispatch started, so its
    # start content is not HEAD and `git checkout HEAD --` would overwrite work
    # that predates this run.
    with tempfile.TemporaryDirectory() as td:
        wt = _mk_worktree(td)
        (wt / "server.py").write_text(BASE + "# carried in from a previous round\n")
        snap0 = _w._git_worktree_snapshot(wt)
        (wt / "server.py").write_text(REFIMPL_OUT)
        why = _exempt_branch(wt, snap0)
        check("E2E carried-in dirty target: abstains", why,
              "target-dirty-at-dispatch-start")
        check("E2E carried-in: tree left exactly as found (nothing destroyed)",
              (wt / "server.py").read_text(), REFIMPL_OUT)

    # (5) No manifest -> no declared target -> abstain rather than guess.
    with tempfile.TemporaryDirectory() as td:
        wt = _mk_worktree(td)
        os.remove(wt / ".dispatch-harness.json")
        _git(wt, "commit", "-q", "-am", "drop manifest")
        snap0 = _w._git_worktree_snapshot(wt)
        (wt / "server.py").write_text(REFIMPL_OUT)
        why = _exempt_branch(wt, snap0)
        check("E2E no manifest: abstains", why, "no-declared-target")
        check("E2E no manifest: tree untouched",
              (wt / "server.py").read_text(), REFIMPL_OUT)

    # declared_harness_target must never hand git a path that escapes the worktree.
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        for bad in ("/etc/passwd", "../../outside.py", "", None):
            (wt / ".dispatch-harness.json").write_text(json.dumps({"target": bad}))
            check(f"manifest target {bad!r} is rejected",
                  _w.declared_harness_target(wt), None)
        (wt / ".dispatch-harness.json").write_text("{not json")
        check("unparseable manifest -> None, not an exception",
              _w.declared_harness_target(wt), None)

print("\nWIRING: the guard is reachable from the shipping exempt branch")
# The standing worry in this area, in the worker's own words: "a source grep cannot
# tell a live guard from a dead branch". The unit + E2E cases above prove the
# function; this proves the EXEMPT BRANCH still calls it, so the guard cannot be
# quietly orphaned while this suite stays green.
_src = WORKER.read_text()
_exempt_at = _src.find('if _zd == "exempt":')
_next_at = _src.find('elif _zd == "fail"', _exempt_at + 1)
check("exempt branch exists in ollama-worker.py", _exempt_at > 0, True)
check("...and the fail branch still follows it", _next_at > _exempt_at, True)
_branch_src = _src[_exempt_at:_next_at] if _exempt_at > 0 < _next_at else ""
for _needed in ("harness_exempt_revert_decision", "declared_harness_target",
                "snapshot_path_dirty", "_git_restore_path"):
    check(f"exempt branch calls {_needed}()", _needed in _branch_src, True)

print()
if _fails:
    print(f"zero-diff guard: {_n - len(_fails)}/{_n} passed -- FAILURES:")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"zero-diff guard: {_n}/{_n} passed")
print("ZERO_DIFF_GUARD_OK")
