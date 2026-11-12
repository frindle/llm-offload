"""mini-swe-runner: a stalled git or disk during setup is capped, visible in the live log and graded INFRA (2026-11-02).

Live: job 50b475235c58's first `git rev-parse HEAD` blocked for good in open() of the worktree's gitdir file under
~/Desktop while a fresh git on the same repo answered at once. The live log stopped at the budgets line and the run
would have sat until its 40-minute alarm, then been graded a model timeout. Now each git call has a ceiling and
retries, setup prints a start line, a heartbeat and a done line, and a stall ends the run as `git-stall`/`disk-stall`
with class=INFRA (not charged to the model). A fake `git` on PATH stands in for the stall; no network, no Darkbloom.

    python3 -m pytest -q ~/bin/test_mini_git_stall.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REAL_GIT = shutil.which("git")


def load(name, fn):
    sp = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


R = load("mswe_git_stall", "mini-swe-runner.py")


@pytest.fixture(autouse=True)
def incidents(tmp_path, monkeypatch):
    """Every test writes incidents to its own file (never ~/.ollama-dispatch); returns a reader."""
    p = tmp_path / "incidents.jsonl"
    monkeypatch.setenv("INFRA_INCIDENTS_PATH", str(p))
    monkeypatch.setenv("INFRA_INCIDENTS_SCREEN_LOCKED", "0")

    def read(kind=None):
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        return [r for r in rows if kind is None or r["kind"] == kind]
    return read

FAKE_GIT = textwrap.dedent("""\
    #!/bin/sh
    # the first $STALL_N calls whose argv holds $STALL_MATCH hang (exec: the hung process IS this pid); the rest run git
    case " $* " in
      *" $STALL_MATCH "*)
        n=$(cat "$STALL_COUNT" 2>/dev/null || echo 0)
        n=$((n+1)); echo "$n" > "$STALL_COUNT"
        if [ "$n" -le "$STALL_N" ]; then exec sleep 30; fi
        if [ -n "$STALL_EMPTY" ]; then exit 0; fi;;
    esac
    exec "$REAL_GIT" "$@"
    """)


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """Returns set(n, match, empty=False): the first n git calls matching `match` hang; with empty, later matching
    calls answer nothing (exit 0). Each git call is capped at 1 s here."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    g = bindir / "git"
    g.write_text(FAKE_GIT)
    g.chmod(0o755)
    count = tmp_path / "stall-count"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REAL_GIT", REAL_GIT)
    monkeypatch.setenv("STALL_COUNT", str(count))
    monkeypatch.setattr(R, "GIT_TIMEOUT_S", 1)

    def set_(n, match, empty=False):
        monkeypatch.setenv("STALL_N", str(n))
        monkeypatch.setenv("STALL_MATCH", match)
        if empty:
            monkeypatch.setenv("STALL_EMPTY", "1")
        count.unlink(missing_ok=True)
    return set_


def repo(path):
    path.mkdir(parents=True)
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run([REAL_GIT, *c], cwd=path, check=True, capture_output=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run([REAL_GIT, "add", "a.py"], cwd=path, check=True, capture_output=True)
    subprocess.run([REAL_GIT, "commit", "-qm", "base"], cwd=path, check=True, capture_output=True)
    return path


def worktree(tmp_path):
    """A linked worktree, as dispatch makes them: its .git is a `gitdir:` file, so prepare() runs detach_git."""
    main = repo(tmp_path / "main")
    wt = tmp_path / "wt"
    subprocess.run([REAL_GIT, "worktree", "add", "-q", "--detach", str(wt)], cwd=main, check=True, capture_output=True)
    assert (wt / ".git").is_file()
    return main, wt


def test_a_git_that_does_not_answer_is_killed_and_run_again(tmp_path, fake_git, capsys, incidents):
    r = repo(tmp_path / "r")
    fake_git(1, "rev-parse HEAD")
    t0 = time.time()
    head = R.git(r, "rev-parse", "HEAD").strip()
    assert len(head) == 40 and time.time() - t0 < 10
    out = capsys.readouterr().out
    assert "did not answer in 1s (try 1/3); running it again" in out
    [rec] = incidents()
    assert rec["kind"] == "git-timeout" and rec["component"] == "runner" and rec["attempt"] == 1
    assert rec["cmd"] == "git rev-parse HEAD" and rec["path"] == str(r) and rec["screen_locked"] is False


def test_three_stalls_raise_git_stall(tmp_path, fake_git, capsys, incidents):
    r = repo(tmp_path / "r")
    fake_git(99, "rev-parse HEAD")
    t0 = time.time()
    with pytest.raises(R.GitStall, match=r"did not answer in 1s on 3 tries"):
        R.git(r, "rev-parse", "HEAD")
    assert time.time() - t0 < 15, "a killed stall is not waited on again"
    out = capsys.readouterr().out
    assert out.count("did not answer in 1s") == 3 and "(try 3/3)\n" in out
    assert [x["attempt"] for x in incidents("git-timeout")] == [1, 2, 3]


def test_check_raises_on_a_real_git_error_not_a_stall(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        R.run_git(tmp_path, "read-tree", "deadbeef" * 5, check=True)


def test_the_incident_one_stalled_rev_parse_then_the_work_dir_is_prepared(tmp_path, fake_git, capsys):
    _main, wt = worktree(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    fake_git(1, "rev-parse HEAD")
    R.prepare(cwd, {"baseline_from": str(wt)})
    assert (cwd / ".git").is_dir() and (cwd / "a.py").read_text() == "x = 1\n"
    assert subprocess.run([REAL_GIT, "status", "--short"], cwd=cwd, capture_output=True, text=True).stdout == ""
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("[mini-runner]")]
    assert lines[0] == f"[mini-runner] preparing the work dir from {wt}"
    assert any("did not answer in 1s (try 1/3)" in ln for ln in lines)
    assert lines[-1].startswith("[mini-runner] work dir ready (")


def test_an_empty_answer_stops_before_the_copy_loses_its_git_pointer(tmp_path, fake_git):
    _main, wt = worktree(tmp_path)
    cwd = tmp_path / "work"
    shutil.copytree(wt, cwd, symlinks=True)
    fake_git(0, "rev-parse HEAD", empty=True)
    with pytest.raises(R.GitStall, match=r"gave no answer \(exit 0"):
        R.detach_git(cwd, wt)
    assert (cwd / ".git").is_file(), "nothing in the work dir changed"


def test_main_reports_a_git_stall_as_infra(monkeypatch, capsys, incidents):
    def stall(argv=None):
        raise R.GitStall("`git rev-parse HEAD` in /x did not answer in 60s on 3 tries")
    monkeypatch.setattr(R, "_main", stall)
    assert R.main([]) == R.RC_FAIL
    out = capsys.readouterr().out
    assert "infrastructure, not the model" in out
    last = out.strip().splitlines()[-1]
    assert last.startswith("RESULT FAIL git-stall `git rev-parse HEAD`") and last.endswith(" class=INFRA")
    [rec] = incidents()
    assert rec["kind"] == "git-stall" and "did not answer in 60s on 3 tries" in rec["detail"]


def test_the_queue_does_not_charge_a_stall_to_the_model():
    oq = load("oq_git_stall", "ollama-queue.py")
    for why in ("git-stall `git rev-parse HEAD` in /x did not answer in 60s on 3 tries",
                "disk-stall preparing the work dir from /x did not finish in 900s"):
        line = "RESULT " + R.tag_result(f"FAIL {why}")
        assert R.fail_class(line) == "INFRA"
        verdict, cls, _reason = oq.parse_runner_result("[mini-runner] x\n" + line + "\n")
        assert cls == "infra" and verdict.startswith("FAIL (")


def test_setup_heartbeat_shows_progress(tmp_path, monkeypatch, capsys, incidents):
    monkeypatch.setattr(R, "SETUP_HEARTBEAT_S", 0.2)
    monkeypatch.setattr(R, "SETUP_SLOW_S", 0.5)
    monkeypatch.setattr(R, "_prepare", lambda cwd, spec: time.sleep(0.7))
    R.prepare(tmp_path, {"baseline_from": "/src"})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[mini-runner] preparing the work dir from /src"
    assert any(ln.startswith("[mini-runner] still preparing the work dir from /src (") for ln in out)
    assert out[-1].startswith("[mini-runner] work dir ready (")
    [rec] = incidents()
    assert rec["kind"] == "setup-slow" and rec["what"] == "preparing the work dir from /src" and rec["seconds"] >= 0


def test_a_quick_setup_records_nothing(tmp_path, monkeypatch, incidents):
    monkeypatch.setattr(R, "_prepare", lambda cwd, spec: None)
    R.prepare(tmp_path, {"baseline_from": "/src"})
    assert incidents() == []


DRIVER = textwrap.dedent("""\
    import importlib.util, sys, time
    sp = importlib.util.spec_from_file_location("m", sys.argv[1])
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    m.SETUP_HEARTBEAT_S, m.SETUP_LIMIT_S = 0.2, 1
    m._prepare = lambda cwd, spec: time.sleep(30)   # a copy stuck in the kernel: nothing reaches it
    m.prepare(sys.argv[2], {"baseline_from": "/stuck"})
    print("NOT REACHED", flush=True)
    """)


def test_a_setup_stuck_past_its_limit_ends_the_run_as_infra(tmp_path, incidents):
    drv = tmp_path / "drv.py"
    drv.write_text(DRIVER)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(drv), str(HERE / "mini-swe-runner.py"), str(tmp_path)],
                       capture_output=True, text=True, timeout=25, env=dict(os.environ))
    assert time.time() - t0 < 15
    assert r.returncode == R.RC_FAIL, r.stderr[-800:]
    lines = r.stdout.strip().splitlines()
    assert "NOT REACHED" not in r.stdout
    assert lines[-1] == "RESULT FAIL disk-stall preparing the work dir from /stuck did not finish in 1s class=INFRA"
    assert any("the disk is not answering (infrastructure, not the model)" in ln for ln in lines)
    [rec] = incidents("disk-stall")
    assert rec["what"] == "preparing the work dir from /stuck" and rec["seconds"] >= 1
