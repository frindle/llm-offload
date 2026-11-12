#!/usr/bin/env python3
"""Tests for mini-auto (new / land / show). Temp repos, a stub mini-submit and a stub queue; no real queue, model or Darkbloom.

  python3 test_mini_auto_cli.py [-k NAME]     prints ALL PASS / N FAILED (rc 0 / 1)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
T = Path(tempfile.mkdtemp(prefix="miniautocli-")).resolve()
FAILS, N = [], [0]
ONLY = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
MODEL = "qwen3.8:27b-q4_K_M"  # an alias the submit vet knows offline (test_mini_submit uses the same)

CALC = '''def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def bulk_total(qty, price):
    """Orders of 10 or more items get 5% off."""
    total = qty * price
    if qty > 10:
        total = total * 0.95
    return total
'''
FIXED = CALC.replace("qty > 10", "qty >= 10")
TEST = '''from calc import bulk_total


def test_ten_items_discounted():
    assert bulk_total(10, 2) == 19.0
'''
DESC = "Orders of exactly 10 items miss the 5% bulk discount: bulk_total(10, 2) returns 20 but should return 19.0."
OLD_TEST = '''from calc import bulk_total


def test_ten_items_full_price():
    assert bulk_total(10, 2) == 20
'''  # an existing test that asserts the old behaviour: the fix must update it (update_tests, gate update_flip)
UPDATED_TEST = OLD_TEST.replace("== 20", "== 19.0")


def chk(name, cond, detail=""):
    N[0] += 1
    if cond:
        print(f"ok   {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name}\n     {str(detail)[-1500:]}")


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                          capture_output=True, text=True, timeout=60)


def make_repo(d):
    d.mkdir(parents=True)
    (d / "calc.py").write_text(CALC)
    (d / "README.md").write_text("shop\n")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    return d


def env_for(tag, **kw):
    root = T / tag
    for x in ("runs", "wt", "qlogs", "home"):
        (root / x).mkdir(parents=True, exist_ok=True)
    stub = root / "stub-submit.py"
    stub.write_text("import json, os, sys\nopen(os.environ['STUB_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "rc = int(os.environ.get('STUB_RC', '0'))\n"
                    "print('mini-submit: C1 ok auto: whatever')\n"
                    "print('mini-submit: FAIL C3 must-not-offload: x' if rc else 'mini-submit: OK receipt /x/SUBMIT-OK.json')\n"
                    "print('  fix: fix it directly' if rc else 'job_id: feedbeef0001')\nsys.exit(rc)\n")
    e = {**os.environ, "MINI_RUNS": str(root / "runs"), "MINI_WT_ROOT": str(root / "wt"), "OFFLOAD_QUEUE_LOGS": str(root / "qlogs"),
         "OFFLOAD_HOME": str(root / "home"), "MINI_AUTO_SUBMIT": f"{sys.executable} {stub}", "STUB_LOG": str(root / "stub.log"),
         "MINI_SPEC_LIB": str(BIN / "mini-spec"), "PYTHONDONTWRITEBYTECODE": "1", **kw}
    return root, e


def auto(e, *args, stdin=None):
    p = subprocess.run([sys.executable, str(BIN / "mini-auto"), *map(str, args)], capture_output=True, text=True, env=e,
                       input=stdin, timeout=1800)
    return p.returncode, p.stdout, p.stderr


def want(name):
    return ONLY is None or ONLY in name


# ---------------------------------------------------------------------------------------------------- new
def t_new():
    root, e = env_for("new")
    repo = make_repo(root / "shop")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "Bad_Name", DESC)
    chk("new: a bad --name is refused (rc 2)", rc == 2 and "--name" in err, err)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-x")
    chk("new: no description is refused", rc == 2 and "no description" in err, err)
    rc, out, err = auto(e, "new", "--repo", repo, "--worktree", repo, "--name", "shop-x", DESC)
    chk("new: --repo with --worktree is refused", rc == 2 and "exactly one" in err, err)
    rc, out, err = auto(e, "new", "--repo", root / "nope", "--name", "shop-x", DESC)
    chk("new: a non-repo --repo is refused", rc == 2 and "not a git repository" in err, err)
    chk("new: ...and refusals leave no run dir", not (root / "runs" / "shop-x").exists())

    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-bulk", "--file", "calc.py", "--fn", "bulk_total", "--locate", "bulk_total",
                        "--diagnose", "always", "--timeout", "4000", DESC)
    run, wt = root / "runs" / "shop-bulk", root / "wt" / "shop-bulk"
    sp = json.loads((run / "spec.json").read_text()) if (run / "spec.json").is_file() else {}
    chk("new: makes a worktree of the repo on branch dispatch/<name>", rc == 0 and wt.is_dir()
        and git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "dispatch/shop-bulk", out + err)
    chk("new: TASK.md is the description", (run / "TASK.md").is_file() and (run / "TASK.md").read_text().strip() == DESC)
    chk("new: spec.json is an auto spec with the hints, an absolute task and the source repo",
        sp.get("mode") == "auto" and sp.get("allowed") == [] and sp.get("baseline_from") == str(wt) and sp.get("task") == str(run / "TASK.md")
        and sp.get("files") == ["calc.py"] and sp.get("fn") == "bulk_total" and sp.get("locate") == ["bulk_total"]
        and sp.get("diagnose") == "always" and sp.get("timeout") == 4000 and sp.get("name") == "shop-bulk"
        and sp.get("auto_repo") == os.path.realpath(repo), json.dumps(sp))
    chk("new: without --submit it prints the submit command", "submit: " in out and "--label shop-bulk" in out, out)
    chk("new: nothing was submitted", not (root / "stub.log").exists())
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-bulk", DESC)
    chk("new: an existing run dir is refused", rc == 2 and "exists" in err, err)

    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-min", DESC)
    sp = json.loads((root / "runs" / "shop-min" / "spec.json").read_text()) if rc == 0 else {}
    chk("new: no hints -> no files/fn/diagnose keys (the diagnosis locates the code)", rc == 0 and not ({"files", "fn", "diagnose", "locate"} & set(sp)), out + err)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-py311", "--remote", "--remote-python", "3.11", DESC)
    sp = json.loads((root / "runs" / "shop-py311" / "spec.json").read_text()) if rc == 0 else {}
    chk("new: --remote-python 3.11 (the CPU runner's Debian python3.11)", rc == 0 and sp.get("remote", {}).get("python") == "3.11", out + err)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-py310", "--remote", "--remote-python", "3.10", DESC)
    chk("new: --remote-python 3.10 is refused (rc 2) and leaves no run dir",
        rc == 2 and "--remote-python" in err and not (root / "runs" / "shop-py310").exists(), err)

    (root / "desc.txt").write_text(DESC + "\n\n")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-file", "--desc-file", root / "desc.txt")
    chk("new: --desc-file", rc == 0 and (root / "runs" / "shop-file" / "TASK.md").read_text().strip() == DESC, out + err)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-stdin", "--desc-file", "-", stdin=DESC)
    chk("new: --desc-file - reads stdin", rc == 0 and (root / "runs" / "shop-stdin" / "TASK.md").read_text().strip() == DESC, out + err)

    (repo / "calc.py").write_text(CALC + "# local edit\n")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-dirty", DESC)
    chk("new: a dirty --repo warns that the job starts from HEAD", rc == 0 and "uncommitted changes" in err, err)
    git(repo, "checkout", "--", "calc.py")
    (repo / ".." / "wt" / "shop-taken").mkdir(parents=True, exist_ok=True)
    (root / "wt" / "shop-taken" / "junk").write_text("x")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-taken", DESC)
    chk("new: a taken worktree path is refused (mini-spec Ambiguous) and leaves no run dir",
        rc == 1 and "worktree:" in err and not (root / "runs" / "shop-taken").exists(), out + err)

    wt2 = root / "wt" / "shop-bulk"
    rc, out, err = auto(e, "new", "--worktree", wt2, "--name", "shop-reuse", DESC)
    sp = json.loads((root / "runs" / "shop-reuse" / "spec.json").read_text()) if rc == 0 else {}
    chk("new: --worktree uses that clean worktree as the baseline", rc == 0 and sp.get("baseline_from") == str(wt2)
        and sp.get("auto_repo") == str(wt2), out + err)
    (wt2 / "calc.py").write_text(CALC + "# dirty\n")
    rc, out, err = auto(e, "new", "--worktree", wt2, "--name", "shop-reuse2", DESC)
    chk("new: a dirty --worktree is refused", rc == 2 and "uncommitted" in err, err)
    git(wt2, "checkout", "--", "calc.py")
    rc, out, err = auto(e, "new", "--worktree", repo / "..", "--name", "shop-reuse3", DESC)
    chk("new: a --worktree that is not a worktree root is refused", rc == 2 and "not the root" in err, err)


def t_submit_stub():
    root, e = env_for("sub")
    repo = make_repo(root / "shop")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-sub", "--file", "calc.py", DESC, "--submit", "--model", MODEL)
    log = [json.loads(x) for x in (root / "stub.log").read_text().splitlines()] if (root / "stub.log").is_file() else []
    chk("submit: runs mini-submit SPEC --label NAME --model M", rc == 0 and log and log[-1] == [
        str(root / "runs" / "shop-sub" / "spec.json"), "--label", "shop-sub", "--model", MODEL], str(log) + out + err)
    chk("submit: prints the job id and drops the per-check ok lines", "job_id: feedbeef0001" in out and "C1 ok" not in out, out)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-sub2", DESC, "--submit", "--label", "lbl-x", "--dry-run")
    log = [json.loads(x) for x in (root / "stub.log").read_text().splitlines()]
    chk("submit: --label and --dry-run pass through; the default model is the auto model",
        rc == 0 and log[-1][1:] == ["--label", "lbl-x", "--model", "qwen3.6-35b-a3b-vl-mtp-mxfp8", "--dry-run"], str(log[-1]))
    rc, out, err = auto({**e, "STUB_RC": "1"}, "new", "--repo", repo, "--name", "shop-sub3", DESC, "--submit")
    chk("submit: a refusal returns its rc, shows the FAIL + fix lines and the command to rerun",
        rc == 1 and "FAIL C3" in out and "fix: fix it directly" in out and "not submitted (rc 1)" in out and "mini-submit" in out, out)


def t_submit_real():
    """The spec mini-auto writes passes the real mini-submit (dry run and a stub-queue submit)."""
    root, e = env_for("real")
    repo = make_repo(root / "shop")
    MR = SourceFileLoader("mini_submit_receipt", str(BIN / "mini_submit_receipt.py")).load_module()
    h = root / "msh"
    (h / "mini-selftest").mkdir(parents=True)
    runner = BIN / "mini-swe-runner.py"
    (h / "mini-selftest" / MR.sha_file(runner)).write_text("seeded by test_mini_auto_cli\n")
    stubq = root / "stubq.py"
    stubq.write_text('import json, os, sys\nopen(os.environ["STUBQ_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                     'print(json.dumps({"id": "abc123def456"}))\n')
    (root / "livelogs").mkdir()
    e.pop("MINI_AUTO_SUBMIT")
    e.update(MINI_SUBMIT_HOME=str(h), MINI_RUNNER=str(runner), MINI_SUBMIT_QUEUE=str(stubq), STUBQ_LOG=str(root / "stubq.log"),
             FAILURE_LEDGER=str(root / "failures.jsonl"), MINI_LIVELOG_DIR=str(root / "livelogs"), MINI_GRAPH_LOOKUP="off", MINI_GRAPH_WARN="off")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-real", "--file", "calc.py", "--fn", "bulk_total", DESC,
                        "--submit", "--model", MODEL, "--dry-run")
    chk("real mini-submit: the mini-auto spec passes every check in a dry run", rc == 0 and "DRY-RUN ok" in out, out + err)
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-real2", DESC, "--submit", "--model", MODEL)
    log = [json.loads(x) for x in (root / "stubq.log").read_text().splitlines()] if (root / "stubq.log").is_file() else []
    argv = log[-1] if log else []
    chk("real mini-submit: a hint-less spec enqueues (stub queue) with the receipt in the run dir",
        rc == 0 and "job_id: abc123def456" in out and argv and argv[argv.index("--task-file") + 1] == str(root / "runs" / "shop-real2" / "spec.json")
        and (root / "runs" / "shop-real2" / "SUBMIT-OK.json").is_file(), out + err + str(argv))
    (root / "pw.txt").write_text("Users with an expired password cannot login: the session check rejects them.\n")
    rc, out, err = auto(e, "new", "--repo", repo, "--name", "shop-real3", "--desc-file", root / "pw.txt", "--submit", "--model", MODEL)
    chk("real mini-submit: a must-not-offload description is refused (C3) and not enqueued",
        rc == 1 and "FAIL C3" in out and "must-not-offload" in out and len((root / "stubq.log").read_text().splitlines()) == 1, out + err)


# ---------------------------------------------------------------------------------------------------- land / show
def fake_job(root, e, jid, repo, name, verdict="PASS", status="done", fixed=FIXED, test=TEST, fspec=True, mode="auto"):
    """A finished auto job as the queue and runner leave it: done.json, spec, run dir with work/ (own git, HEAD = the baseline)."""
    wt = root / "wt" / name
    if not wt.exists():
        git(repo, "worktree", "add", "-q", "-b", f"dispatch/{name}", str(wt), "HEAD")
    run_s = root / "runs" / name
    run_s.mkdir(parents=True, exist_ok=True)
    (run_s / "TASK.md").write_text(DESC + "\n")
    spec = {"mode": mode, "name": name, "allowed": [], "baseline_from": str(wt), "task": str(run_s / "TASK.md"), "auto_repo": os.path.realpath(repo)}
    (run_s / "spec.json").write_text(json.dumps(spec))
    rd = root / "mini-work" / f"{name}-20261103120000"
    work = rd / "work"
    shutil.copytree(wt, work)
    (work / ".git").unlink()  # the runner's detach_git: own repo, same HEAD, objects borrowed via alternates
    head = git(wt, "rev-parse", "HEAD").stdout.strip()
    common = Path(wt, git(wt, "rev-parse", "--git-common-dir").stdout.strip()).resolve()
    git(work, "init", "-q")
    (work / ".git/objects/info/alternates").write_text(f"{common / 'objects'}\n")
    (work / ".git/HEAD").write_text(head + "\n")
    git(work, "read-tree", head)
    (work / "calc.py").write_text(fixed)
    (work / "test_calc.py").write_text(test)
    if fspec:
        (rd / "spec.auto.json").write_text(json.dumps({"mode": "two-phase", "allowed": ["calc.py"], "test_file": "test_calc.py",
                                                       "test_cmd": "python3 -m pytest -q test_calc.py", "target_fn": "bulk_total"}))
    (rd / "REVIEW-AUTO.md").write_text(f"# AUTO review: {name}\n\nRESULT {verdict} mode=auto target=calc.py:bulk_total diag_s=1 fix_s=2\n")
    (Path(e["OFFLOAD_QUEUE_LOGS"]) / f"{jid}.done.json").write_text(json.dumps(
        {"id": jid, "status": status, "runner_verdict": verdict, "cwd": str(work), "task_file": str(run_s / "spec.json"), "label": name}))
    return rd, work


def review(e, jid, verdict):
    with open(Path(e["OFFLOAD_HOME"]) / "reviews.jsonl", "a") as f:
        f.write(json.dumps({"ts": "2026-11-03T12:00:00", "job": jid, "verdict": verdict, "kind": "auto", "family": "x", "note": ""}) + "\n")


def t_land():
    root, e = env_for("land")
    repo = make_repo(root / "shop")
    rd, work = fake_job(root, e, "j1", repo, "shop-l1")
    rc, out, err = auto(e, "land", "j1")
    chk("land: refused without a review verdict", rc == 1 and "no ok review verdict" in err and "review brief j1" in err, err)
    review(e, "j1", "reject")
    rc, out, err = auto(e, "land", "j1")
    chk("land: refused after a reject", rc == 1 and "latest: reject" in err, err)
    chk("land: ...and nothing was copied", (repo / "calc.py").read_text() == CALC and not (repo / "test_calc.py").exists())
    review(e, "j1", "ok")
    rc, out, err = auto(e, "land", "j1", "--dry-run")
    chk("land: --dry-run copies nothing", rc == 0 and "would copy: calc.py" in out and (repo / "calc.py").read_text() == CALC
        and not (repo / "test_calc.py").exists(), out + err)
    rc, out, err = auto(e, "land", "j1")
    chk("land: an ok verdict copies the fix and the new test into the source repo (uncommitted)",
        rc == 0 and (repo / "calc.py").read_text() == FIXED and (repo / "test_calc.py").read_text() == TEST
        and "M calc.py" in out and "?? test_calc.py" in out and "ledger add --job j1" in out
        and git(repo, "log", "--oneline").stdout.count("\n") == 1, out + err)
    rc, out, err = auto(e, "land", "j1")
    chk("land: landing again is a no-op", rc == 0 and out.count("already landed") == 2, out + err)

    rd2, _ = fake_job(root, e, "j2", repo, "shop-l2", fixed=CALC.replace("qty > 10", "qty >= 10 and qty"))
    review(e, "j2", "ok")
    git(repo, "checkout", "--", "calc.py")
    (repo / "test_calc.py").unlink()
    (repo / "calc.py").write_text(CALC.replace("return max", "return  max"))
    rc, out, err = auto(e, "land", "j2")
    chk("land: refused when the checkout changed the target since the job started; nothing copied (not even the test)",
        rc == 1 and "changed it since the job started" in err and "diff HEAD -- calc.py" in err and not (repo / "test_calc.py").exists(), err)
    git(repo, "checkout", "--", "calc.py")
    (repo / "test_calc.py").write_text("# someone else's test\n")
    rc, out, err = auto(e, "land", "j2")
    chk("land: refused when the test file exists with other content", rc == 1 and "test_calc.py" in err and "other content" in err, err)
    (repo / "test_calc.py").unlink()

    other = root / "other"
    shutil.copytree(repo, other, ignore=shutil.ignore_patterns(".git"))
    rc, out, err = auto(e, "land", "j2", "--into", other)
    chk("land: --into another checkout", rc == 0 and (other / "calc.py").read_text().endswith("return total\n")
        and "qty >= 10 and qty" in (other / "calc.py").read_text() and (other / "test_calc.py").is_file(), out + err)

    fake_job(root, e, "j3", repo, "shop-l3", verdict="FAIL mode=auto phase=fix test-red", status="done")
    review(e, "j3", "ok")
    rc, out, err = auto(e, "land", "j3")
    chk("land: a failed job is refused", rc == 1 and "did not pass" in err, err)
    fake_job(root, e, "j4", repo, "shop-l4", verdict="NEEDS-REVIEW (soft: mutation)")
    review(e, "j4", "ok")
    rc, out, err = auto(e, "land", "j4", "--dry-run")
    chk("land: a needs-review pass with an ok verdict can land", rc == 0 and "would copy: calc.py" in out, out + err)
    fake_job(root, e, "j5", repo, "shop-l5", fspec=False)
    review(e, "j5", "ok")
    rc, out, err = auto(e, "land", "j5")
    chk("land: no spec.auto.json (never reached the fix stage) is refused", rc == 1 and "spec.auto.json" in err, err)
    fake_job(root, e, "j6", repo, "shop-l6", mode="two-phase")
    review(e, "j6", "ok")
    rc, out, err = auto(e, "land", "j6")
    chk("land: a non-auto job is refused", rc == 1 and "not an auto job" in err, err)
    rc, out, err = auto(e, "land", "nope")
    chk("land: an unknown job is refused", rc == 1 and "no finished-job record" in err, err)
    rc, out, err = auto(e, "land", "../etc")
    chk("land: a path-like job id is refused", rc == 1 and "no finished-job record" in err, err)

    repo2 = make_repo(root / "shop2")
    rd7, work7 = fake_job(root, e, "j7", repo2, "shop-l7", verdict="FAIL mode=auto phase=fix phase-b RED test_green", status="failed")
    rc, out, err = auto(e, "land", "j7")
    chk("land: a failed auto job that was not finished points at mini-finish", rc == 1 and "did not pass" in err
        and "~/bin/mini-finish j7" in err, err)
    cand = rd7 / "finish" / "check-20261103T120000" / "cand"
    cand.mkdir(parents=True)
    for f in ("calc.py", "test_calc.py"):
        shutil.copyfile(work7 / f, cand / f)
    (rd7 / "finish" / "FINISHED.json").write_text(json.dumps({"job": "j7", "verdict": "PASS", "check_dir": str(cand.parent)}))
    rc, out, err = auto(e, "land", "j7", "--dry-run")
    chk("land: a job Claude finished (mini-finish PASS) lands without a review row", rc == 0 and "would copy: calc.py" in out
        and "would copy: test_calc.py" in out, out + err)
    (work7 / "calc.py").write_text(FIXED + "# edited after the check\n")
    rc, out, err = auto(e, "land", "j7")
    chk("land: ...but not when a file changed after the check", rc == 1 and "differ from what `mini-finish --check` passed" in err
        and (repo2 / "calc.py").read_text() == CALC, err)

    rc, out, err = auto(e, "show", "j1")
    chk("show: result line, target, test, diff stat and packet paths",
        rc == 0 and "PASS mode=auto target=calc.py:bulk_total" in out and "target ['calc.py'] fn bulk_total" in out
        and "calc.py" in out and "REVIEW-AUTO.md:" in out, out + err)


def t_land_update():
    """An existing test the fix updated (spec update_tests, gate update_flip) lands with the fix and the new test: left out, the
    checkout's suite still asserts the old behaviour against the new code (canvas r3, 2026-11-06)."""
    root, e = env_for("landupd")
    repo = make_repo(root / "shop")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text(OLD_TEST)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "old test")
    jobs = {}
    for jid, edit in (("u1", True), ("u2", False), ("u3", True)):
        rd, work = fake_job(root, e, jid, repo, f"shop-{jid}")
        fs = json.loads((rd / "spec.auto.json").read_text())
        (rd / "spec.auto.json").write_text(json.dumps({**fs, "update_tests": ["tests/test_old.py"], "update_tests_strict": True}))
        if edit:
            (work / "tests" / "test_old.py").write_text(UPDATED_TEST)
        jobs[jid] = (rd, work)
    review(e, "u1", "ok")
    review(e, "u2", "ok")
    (repo / "tests" / "test_old.py").write_text(OLD_TEST + "# a local edit\n")
    rc, out, err = auto(e, "land", "u1")
    chk("land: refused when the checkout changed an updated existing test since the job started; nothing copied",
        rc == 1 and "tests/test_old.py: " in err and "changed it since the job started" in err and "diff HEAD -- tests/test_old.py" in err
        and (repo / "calc.py").read_text() == CALC and not (repo / "test_calc.py").exists(), err)
    (repo / "tests" / "test_old.py").write_text(OLD_TEST)
    rc, out, err = auto(e, "land", "u1", "--dry-run")
    chk("land: --dry-run lists the updated existing test and copies nothing",
        rc == 0 and "would copy: tests/test_old.py" in out and "would copy: calc.py" in out
        and (repo / "tests" / "test_old.py").read_text() == OLD_TEST, out + err)
    rc, out, err = auto(e, "land", "u1")
    chk("land: the updated existing test lands with the fix and the new test (uncommitted)",
        rc == 0 and (repo / "tests" / "test_old.py").read_text() == UPDATED_TEST and (repo / "calc.py").read_text() == FIXED
        and (repo / "test_calc.py").read_text() == TEST and "M tests/test_old.py" in out and "M calc.py" in out
        and "?? test_calc.py" in out, out + err)
    rc, out, err = auto(e, "land", "u1")
    chk("land: landing again is a no-op for all three files", rc == 0 and out.count("already landed") == 3, out + err)
    rc, out, err = auto(e, "show", "u1")
    chk("show: names the tests the fix may update and counts the updated one in the diff stat",
        rc == 0 and "may update ['tests/test_old.py']" in out and "tests/test_old.py |" in out, out + err)

    other = root / "other"
    git(root, "clone", "-q", str(repo), str(other))
    rc, out, err = auto(e, "land", "u2", "--into", other)
    chk("land: an update_tests file the fix left alone is neither copied nor listed",
        rc == 0 and "test_old.py" not in out and (other / "calc.py").read_text() == FIXED
        and (other / "tests" / "test_old.py").read_text() == OLD_TEST, out + err)

    rd3, work3 = jobs["u3"]  # finished by Claude: mini-finish's check covers allowed + update_tests + the test
    git(repo, "checkout", "-q", "--", ".")
    (repo / "test_calc.py").unlink()
    rc, out, err = auto(e, "land", "u3")
    chk("land(finished): needs an ok verdict or a finish first", rc == 1 and "no ok review verdict" in err, err)
    cand = rd3 / "finish" / "check-20261106T120000" / "cand"
    for f in ("calc.py", "test_calc.py", "tests/test_old.py"):
        (cand / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work3 / f, cand / f)
    (rd3 / "finish" / "FINISHED.json").write_text(json.dumps({"job": "u3", "verdict": "PASS", "check_dir": str(cand.parent)}))
    rc, out, err = auto(e, "land", "u3", "--dry-run")
    chk("land(finished): the checked updated test lands too", rc == 0 and "would copy: tests/test_old.py" in out, out + err)
    (work3 / "tests" / "test_old.py").write_text(UPDATED_TEST + "# edited after the check\n")
    rc, out, err = auto(e, "land", "u3")
    chk("land(finished): ...but not when the updated test changed after the check",
        rc == 1 and "tests/test_old.py" in err and "differ from what `mini-finish --check` passed" in err
        and (repo / "tests" / "test_old.py").read_text() == OLD_TEST, err)


def rows(e):
    f = Path(e["OFFLOAD_HOME"]) / "reviews.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.is_file() else []


def t_review():
    root, e = env_for("review")
    repo = make_repo(root / "shop")
    fake_job(root, e, "r1", repo, "shop-r1")
    rc, out, err = auto(e, "review", "r1", "reject")
    chk("review: a reject needs a note; nothing recorded", rc == 1 and "--note" in err and rows(e) == [], err)
    rc, out, err = auto(e, "review", "r1", "reject", "--note", "test expects 90 but the Problem says 85")
    r = rows(e)
    chk("review: reject records one auto row with the note and lands nothing",
        rc == 0 and len(r) == 1 and r[0]["verdict"] == "reject" and r[0]["kind"] == "auto" and "85" in r[0]["note"]
        and "nothing landed" in out and "never land it as is" in out and (repo / "calc.py").read_text() == CALC
        and not (repo / "test_calc.py").exists(), out + err)
    rc, out, err = auto(e, "land", "r1")
    chk("review: land still refuses after the reject", rc == 1 and "latest: reject" in err, err)

    fake_job(root, e, "r2", repo, "shop-r2")
    rc, out, err = auto(e, "review", "r2", "ok", "--dry-run")
    chk("review: --dry-run records nothing and copies nothing, but shows the land plan",
        rc == 0 and "would record" in out and "would copy: calc.py" in out and len(rows(e)) == 1
        and (repo / "calc.py").read_text() == CALC, out + err)
    rc, out, err = auto(e, "review", "r2", "ok", "--note", "test + fix match the Problem")
    r = rows(e)
    chk("review: ok records the verdict and lands the fix + test in one step",
        rc == 0 and len(r) == 2 and r[-1]["verdict"] == "ok" and r[-1]["kind"] == "auto" and r[-1]["job"] == "r2"
        and (repo / "calc.py").read_text() == FIXED and (repo / "test_calc.py").read_text() == TEST
        and "ledger add --job r2" in out, out + err)
    rc, out, err = auto(e, "review", "r2", "ok")
    chk("review: repeating ok records no second row and the land is a no-op",
        rc == 0 and len(rows(e)) == 2 and "already recorded" in out and out.count("already landed") == 2, out + err)

    fake_job(root, e, "r3", repo, "shop-r3", verdict="FAIL mode=auto phase=fix test-red", status="failed")
    rc, out, err = auto(e, "review", "r3", "ok")
    chk("review: a failed job is refused before anything is recorded, pointing at mini-finish",
        rc == 1 and "did not pass" in err and "mini-finish r3" in err and len(rows(e)) == 2, err)
    fake_job(root, e, "r4", repo, "shop-r4", mode="two-phase")
    rc, out, err = auto(e, "review", "r4", "ok")
    chk("review: a non-auto job is refused", rc == 1 and "not an auto job" in err and len(rows(e)) == 2, err)

    other = root / "other"
    shutil.copytree(repo, other, ignore=shutil.ignore_patterns(".git"))
    git(repo, "checkout", "--", "calc.py")
    (repo / "test_calc.py").unlink()
    fake_job(root, e, "r5", repo, "shop-r5")
    (other / "test_calc.py").unlink()
    (other / "calc.py").write_text(CALC)
    rc, out, err = auto(e, "review", "r5", "ok", "--note", "fine", "--into", other)
    chk("review: --into lands into another checkout", rc == 0 and (other / "calc.py").read_text() == FIXED
        and (repo / "calc.py").read_text() == CALC, out + err)
    (repo / "calc.py").write_text(CALC.replace("return max", "return  max"))
    fake_job(root, e, "r6", repo, "shop-r6")
    rc, out, err = auto(e, "review", "r6", "ok", "--note", "fine")
    chk("review: ok is recorded even when the land is refused (the checkout moved); the refusal says what to do",
        rc == 1 and rows(e)[-1]["job"] == "r6" and rows(e)[-1]["verdict"] == "ok" and "changed it since the job started" in err, out + err)


def main():
    for name, fn in (("new", t_new), ("submit", t_submit_stub), ("real", t_submit_real), ("land", t_land), ("land_update", t_land_update),
                     ("review", t_review)):
        if want(name):
            fn()
    print(f"\n{N[0] - len(FAILS)}/{N[0]} passed" + ("" if not FAILS else "\nFAILED: " + "; ".join(FAILS)))
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    if not FAILS:
        shutil.rmtree(T, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
