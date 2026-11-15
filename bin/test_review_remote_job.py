#!/usr/bin/env python3
"""A job that finished on the REMOTE queue (no <id>.done.json on this machine) can be briefed, reviewed and landed from the Mac
(2026-11-15: after the queue moved to Unraid, `mini-auto review` and `mini-scorecard review brief` died with "no finished-job
record" for every job). The run is fetched into the mini-finish mirror (mini_finish_remote.remote_record); the land goes into
the spec's auto_repo, never into the mirror. The queue is a fake: fetch_result copies a prepared run dir. No network.

  python3 test_review_remote_job.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
CLI = SourceFileLoader("tmac_helpers", str(BIN / "test_mini_auto_cli.py")).load_module()
FAILS, N = [], [0]
SRV = "/home/queue/.ollama-dispatch/mini-work"


def chk(name, cond, detail=""):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"\n     {str(detail)[-1500:]}"))
    if not cond:
        FAILS.append(name)


def server_run(root, e, jid, repo, name, auto_repo=True, finished=True, **kw):
    """The run dir as the queue's ?scope=run tar holds it: work/, spec.json (paths on the queue), spec.auto.json, REVIEW-AUTO.md,
    _queue/done.json. Returns (tree, server run dir)."""
    rd, work = CLI.fake_job(root, e, jid, repo, name, **kw)
    (Path(e["OFFLOAD_QUEUE_LOGS"]) / f"{jid}.done.json").unlink()   # the Mac has no record: it finished on the queue
    srv_run = f"{SRV}/upload-{name}"
    spec = json.loads((root / "runs" / name / "spec.json").read_text())
    spec.update(baseline_from=f"{srv_run}/trees/t0", task=f"{srv_run}/trees/t1/TASK.md")
    if not auto_repo:
        spec.pop("auto_repo")
    (rd / "spec.json").write_text(json.dumps(spec))
    if finished:
        (rd / "_queue").mkdir(exist_ok=True)
        (rd / "_queue" / "done.json").write_text(json.dumps(
            {"id": jid, "status": "done", "runner_verdict": "PASS", "cwd": f"{srv_run}/work", "task_file": f"{srv_run}/spec.json",
             "label": name, "runner": "/home/queue/bin/mini-swe-runner.py"}))
    return rd, srv_run


def run_main(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = mod.main(argv) or 0
        except SystemExit as x:
            rc = x.code if isinstance(x.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def main():
    root, e = CLI.env_for("remote")
    repo = CLI.make_repo(root / "shop")
    os.environ.update({k: v for k, v in e.items() if k in ("OFFLOAD_QUEUE_LOGS", "OFFLOAD_HOME", "MINI_RUNS", "MINI_WT_ROOT")})
    os.environ.update(QUEUE_API="http://q.invalid:1", QUEUE_API_TOKEN="fake", PYTHONDONTWRITEBYTECODE="1")
    import queue_config
    import mini_finish_remote as MFR
    trees, fetched = {}, []

    def fake_fetch(base, headers, jid, dest, timeout=300, scope=None):
        fetched.append((jid, scope))
        if jid not in trees:
            raise MFR.QR.RemoteError("queue answered 404 for the result of %s" % jid)
        shutil.copytree(trees[jid], dest, dirs_exist_ok=True)
        return Path(dest)

    MFR.QR.fetch_result = fake_fetch
    ma = SourceFileLoader("mini_auto_rr", str(BIN / "mini-auto")).load_module()
    sc = SourceFileLoader("mini_scorecard_rr", str(BIN / "mini-scorecard")).load_module()
    home = Path(e["OFFLOAD_HOME"])
    chk("the fake queue counts as remote", queue_config.queue_is_remote())

    jid = "abc123def456"
    rd, srv_run = server_run(root, e, jid, repo, "shop-rm")
    trees[jid] = rd
    rec, run, work, spec = ma.resolve(jid)
    mir = home / "mini-finish-remote" / jid
    chk("resolve: a job with no local record is fetched from the queue into the mirror",
        rec.get("_mirror") == str(mir) and run == mir and work == mir / "work" and fetched == [(jid, "run")], (rec, fetched))
    chk("resolve: the record is the queue's done.json", rec.get("status") == "done" and rec.get("runner_verdict") == "PASS", rec)
    chk("resolve: the spec's queue paths point into the mirror; auto_repo is still this machine's repo",
        spec.get("baseline_from") == f"{mir}/trees/t0" and spec.get("auto_repo") == os.path.realpath(repo), spec)
    chk("resolve: the mirror's git has no hooks (sanitized)", not (mir / "work" / ".git" / "hooks").exists())

    rc, out, err = run_main(sc, ["review", "brief", jid])
    chk("scorecard brief: works for the remote job and shows the mirror's packet",
        rc == 0 and f"packet: {mir / 'REVIEW-AUTO.md'}" in out and "AUTO job" in out and "RESULT PASS" in out, out + err)

    rc, out, err = run_main(ma, ["review", jid, "ok", "--note", "test + fix match the Problem"])
    rows = CLI.rows(e)
    chk("review ok: records the verdict and lands the fix + test into the source repo (not the mirror)",
        rc == 0 and rows and rows[-1]["job"] == jid and rows[-1]["verdict"] == "ok"
        and (repo / "calc.py").read_text() == CLI.FIXED and (repo / "test_calc.py").read_text() == CLI.TEST, out + err)
    chk("review ok: the mirror was fetched once", fetched == [(jid, "run")], fetched)

    j2 = "abc123def457"
    rd2, _ = server_run(root, e, j2, repo, "shop-rm2", finished=False)
    trees[j2] = rd2
    rc, out, err = run_main(ma, ["land", j2])
    chk("a run without done.json (not finished) is refused and its mirror is removed, so a later call fetches it again",
        rc == 1 and "no finished-job record" in err and not (home / "mini-finish-remote" / j2).exists(), err)

    j3 = "abc123def458"
    CLI.git(repo, "checkout", "--", "calc.py")
    (repo / "test_calc.py").unlink()
    rd3, _ = server_run(root, e, j3, repo, "shop-rm3", auto_repo=False)
    trees[j3] = rd3
    CLI.review(e, j3, "ok")
    rc, out, err = run_main(ma, ["land", j3])
    chk("a remote spec without auto_repo never lands into the mirror: --into is asked for",
        rc == 1 and "--into" in err and (repo / "calc.py").read_text() == CLI.CALC, err)
    rc, out, err = run_main(ma, ["land", j3, "--into", str(repo)])
    chk("...and --into lands it", rc == 0 and (repo / "calc.py").read_text() == CLI.FIXED, out + err)

    rc, out, err = run_main(ma, ["land", "ffffffffffff"])
    chk("a job the queue does not know: refused, the message says the queue had none either",
        rc == 1 and "none on the remote queue" in err, err)
    n = len(fetched)
    rc, out, err = run_main(ma, ["land", "../etc"])
    chk("a non-id job name is never fetched", rc == 1 and len(fetched) == n, fetched)

    os.environ["QUEUE_API"] = "http://127.0.0.1:7684"
    rc, out, err = run_main(ma, ["land", "abc123def499"])
    chk("a local queue never fetches", rc == 1 and len(fetched) == n and "remote queue" not in err, err)

    print(f"\n{N[0] - len(FAILS)}/{N[0]} passed")
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    if not FAILS:
        shutil.rmtree(CLI.T, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
