"""dispatch-escalation-watcher: a git that does not answer is never read as a deleted branch or a divergence
(2026-11-02).

While the screen is locked, a launchd job's git can sit in the kernel on its first touch of ~/Desktop. `_git` used
to fold a timeout into rc 1, and `rev-parse --verify --quiet` / `merge-base --is-ancestor` say "no" with rc 1, so
the integration panel could call a live staged branch GONE or a fast-forward DIVERGED. A timeout is now its own
code (124), the panel says NOT CHECKED, and a hung git is killed without waiting on it again.

    python3 -m pytest -q ~/bin/test_watcher_git_timeout.py
"""
import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_sp = importlib.util.spec_from_file_location("dew_git_timeout", HERE / "dispatch-escalation-watcher.py")
W = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(W)

WAS, CUR, NEW = "a" * 40, "c" * 40, "b" * 40
BR = "integrate/p-x"
INTEG = {"onto": "main", "base_rev": WAS, "onto_tip": WAS, "integrate_branch": BR}


@pytest.fixture
def git_answers(monkeypatch):
    """Replace W._git with a table: refs answer rev-parse, ("anc", a, b) answers merge-base --is-ancestor a b."""
    table = {}

    def g(repo, *args, timeout=30):
        if args[0] == "rev-parse":
            return table[args[-1]]
        if args[0] == "merge-base":
            return table[("anc", args[-2], args[-1])]
        if args[0] == "rev-list":
            return 0, "3"
        if args[0] == "diff":
            return 0, ""
        pytest.fail("unexpected git call %r" % (args,))
    monkeypatch.setattr(W, "_git", g)
    return table


def text(lines):
    return "\n".join(lines)


def test_a_tip_that_did_not_answer_is_not_rechecked(git_answers):
    git_answers["refs/heads/main"] = (W.GIT_TIMEOUT_RC, "")
    lines, stale = W.integration_freshness(INTEG, "/r")
    assert "- main tip RIGHT NOW: `(git did not answer: not re-checked)`" in lines
    assert stale is None and "GONE" not in text(lines)


def test_a_staged_branch_lookup_that_did_not_answer_is_not_called_gone(git_answers):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (W.GIT_TIMEOUT_RC, "")})
    lines, stale = W.integration_freshness(INTEG, "/r")
    assert "- staged branch `%s`: NOT CHECKED (git did not answer)" % BR in lines
    assert "GONE" not in text(lines)
    assert stale == "main has advanced 3 commit(s) since this verdict was computed"


def test_a_missing_branch_is_still_gone(git_answers):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (1, "")})
    lines, _ = W.integration_freshness(INTEG, "/r")
    assert any("GONE (deleted, or landed and cleaned up)" in ln for ln in lines)


def test_a_git_error_is_not_gone_either(git_answers):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (128, "")})
    lines, _ = W.integration_freshness(INTEG, "/r")
    assert "- staged branch `%s`: NOT CHECKED (git exit 128)" % BR in lines and "GONE" not in text(lines)


@pytest.mark.parametrize("rca, rcb", [(W.GIT_TIMEOUT_RC, 1), (1, W.GIT_TIMEOUT_RC), (128, 1)])
def test_an_ancestry_check_that_did_not_answer_is_not_a_divergence(git_answers, rca, rcb):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (0, NEW),
                        ("anc", CUR, NEW): (rca, ""), ("anc", NEW, CUR): (rcb, "")})
    lines, stale = W.integration_freshness(INTEG, "/r")
    assert lines[-1].startswith("- is CURRENT main an ancestor of the stage? NOT CHECKED (git ")
    assert "DIVERGED" not in (stale or "") and "DIVERGED" not in text(lines)
    assert stale == "main has advanced 3 commit(s) since this verdict was computed"


def test_a_real_divergence_is_still_reported(git_answers):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (0, NEW),
                        ("anc", CUR, NEW): (1, ""), ("anc", NEW, CUR): (1, "")})
    lines, stale = W.integration_freshness(INTEG, "/r")
    assert "DIVERGED" in stale and "(i.e. still a fast-forward)? **NO**" in text(lines)


def test_a_fast_forward_still_says_yes(git_answers):
    git_answers.update({"refs/heads/main": (0, CUR), "refs/heads/" + BR: (0, NEW),
                        ("anc", CUR, NEW): (0, ""), ("anc", NEW, CUR): (1, "")})
    lines, _ = W.integration_freshness(INTEG, "/r")
    assert "(i.e. still a fast-forward)? **yes**" in text(lines)


def test_git_says_no_with_exit_1_on_a_real_repo(tmp_path):
    """The premise: on a real repo a missing ref is rc 1 (GONE), never the timeout code."""
    git = shutil.which("git")
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run([git, *c], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a").write_text("x")
    subprocess.run([git, "add", "a"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run([git, "commit", "-qm", "b"], cwd=tmp_path, check=True, capture_output=True)
    assert W._git(tmp_path, "rev-parse", "--verify", "--quiet", "refs/heads/nope") == (1, "")
    rc, head = W._git(tmp_path, "rev-parse", "--verify", "--quiet", "refs/heads/main")
    assert rc == 0 and len(head) == 40
    lines, _ = W.integration_freshness(dict(INTEG, onto_tip=head), str(tmp_path))
    assert any("GONE" in ln for ln in lines)


def test_a_hung_git_is_killed_and_reported_as_a_timeout(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (bindir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]))
    t0 = time.time()
    assert W._git(tmp_path, "rev-parse", "HEAD", timeout=1) == (W.GIT_TIMEOUT_RC, "")
    assert time.time() - t0 < 10
    assert W._git_why(W.GIT_TIMEOUT_RC) == "did not answer" and W._git_why(128) == "exit 128"


def test_no_repo_or_no_git_is_not_run(tmp_path, monkeypatch):
    assert W._git(None, "status") == (W.GIT_NOT_RUN_RC, "")
    monkeypatch.setenv("PATH", str(tmp_path))          # no git anywhere on PATH
    assert W._git(tmp_path, "status") == (W.GIT_NOT_RUN_RC, "")
