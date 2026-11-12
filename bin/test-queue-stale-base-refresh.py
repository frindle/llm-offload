#!/usr/bin/env python3
"""STALE-BASE REFRESH regressions (stale-base audit 2026-10-09).

The 21 daemon-log `class=stale-base` failures were ~9 incidents with ONE mechanism: a dispatch
worktree is cut at enqueue time, its HEAD never moves, commits that touch the task's own files land
on the base, and every round (author, -c1, -c2, -r1, -r2 ...) re-launches in the SAME worktree on the
SAME old HEAD -- 12-24 iterations burned per round against a tree lacking those commits, then reported
(never fixed). refresh_stale_base() (ollama-queue.py) now brings the worktree up to its base BEFORE
the launch baseline is stamped: fast-forward, or rebase of earlier rounds' sealed commits, only when
the drift touches the task's declared files, fail-open (tree untouched) when git refuses.

Real git, temp repo. Usage: python3 test-queue-stale-base-refresh.py [--queue PATH]    Marker: ALL PASS."""
import argparse, importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="stalebase-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ld = SourceFileLoader("queue_sb", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_sb", ld))
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
    except Exception as e:      # an API the reverted code lacks is a FAIL, not a crash
        return f"EXC {type(e).__name__}: {e}"


def git(cwd, *args):
    cp = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert cp.returncode == 0, (args, cp.stderr)
    return cp.stdout.strip()


def make_repo(name):
    r = T / name
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    git(r, "config", "user.email", "t@t"); git(r, "config", "user.name", "t")
    (r / "lib").mkdir()
    (r / "lib" / "a.ts").write_text("export const a = 1;\n")
    (r / "README.md").write_text("r\n")
    git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    return r


def make_wt(repo, name):
    wt = T / name
    git(repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
    (wt / ".dispatch-harness.json").write_text('{"target": "lib/a.ts"}')     # untracked scaffold
    return wt


def land(repo, path, text, msg="land"):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", msg)


def job(wt, **kw):
    j = {"id": "j1", "label": "auto-author-x", "cwd": str(wt), "verify": "python3 auto-harness-check.py"}
    j.update(kw)
    return j


# 1. drift touching the task's file: fast-forwarded, baseline would now be measured on the new base
repo = make_repo("r1"); wt = make_wt(repo, "wt1")
land(repo, "lib/a.ts", "export const a = 2;\n"); land(repo, "lib/b.ts", "export const b = 1;\n")
old = git(wt, "rev-parse", "HEAD")
j = job(wt)
res = safe(q.refresh_stale_base, j)
chk("touching drift -> refreshed by fast-forward", (res or {}).get("how") if isinstance(res, dict) else res, "fast-forward")
chk("...HEAD is now the base tip", git(wt, "rev-parse", "HEAD"), git(repo, "rev-parse", "main"))
chk("...the incoming file is present in the worktree", (wt / "lib" / "b.ts").is_file(), True)
chk("...the untracked scaffold survives", (wt / ".dispatch-harness.json").is_file(), True)
chk("...the refresh is recorded on the job", (j.get("base_refreshed") or {}).get("from"), old[:12])
chk("...a second call is a no-op (not behind any more)", safe(q.refresh_stale_base, job(wt)), None)

# 2. drift touching NONE of the task's files: left alone (same evidence rule as the classifier)
repo = make_repo("r2"); wt = make_wt(repo, "wt2")
land(repo, "README.md", "changed\n")
h = git(wt, "rev-parse", "HEAD")
chk("unrelated drift -> not refreshed", safe(q.refresh_stale_base, job(wt)), None)
chk("...HEAD unchanged", git(wt, "rev-parse", "HEAD"), h)

# 3. jobs that must not be touched
repo = make_repo("r3"); wt = make_wt(repo, "wt3")
land(repo, "lib/a.ts", "export const a = 3;\n")
h = git(wt, "rev-parse", "HEAD")
chk("a non-authoring job is skipped",
    safe(q.refresh_stale_base, job(wt, label="plain-coding", verify="bash verify.sh")), None)
chk("an in-place resume (own transcript) is skipped", safe(q.refresh_stale_base, job(wt, resume_transcript="/x")), None)
chk("a progress continuation is skipped", safe(q.refresh_stale_base, job(wt, continuation_active=True)), None)
chk("...none of them moved HEAD", git(wt, "rev-parse", "HEAD"), h)
chk("no cwd / missing dir never raises", (safe(q.refresh_stale_base, {"label": "auto-author-x"}),
                                         safe(q.refresh_stale_base, job("/nonexistent-dir-xyz"))), (None, None))

# 4. earlier rounds' SEALED commit on HEAD (c1/c2/r1 continuations): rebased on the new base
repo = make_repo("r4"); wt = make_wt(repo, "wt4")
(wt / "refimpl.py").write_text("# sealed harness\n"); git(wt, "add", "refimpl.py"); git(wt, "commit", "-qm", "seal")
land(repo, "lib/a.ts", "export const a = 4;\n")
res = safe(q.refresh_stale_base, job(wt))
chk("sealed commit + touching drift -> rebased", (res or {}).get("how") if isinstance(res, dict) else res, "rebase")
chk("...base tip is an ancestor of the new HEAD",
    subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", "main", "HEAD"]).returncode, 0)
chk("...the sealed file survived the rebase", (wt / "refimpl.py").read_text(), "# sealed harness\n")

# 5. git refuses (overlapping tracked edit): fail-open, tree exactly as found
repo = make_repo("r5"); wt = make_wt(repo, "wt5")
(wt / "lib" / "a.ts").write_text("export const a = 'model wip';\n")
land(repo, "lib/a.ts", "export const a = 5;\n")
h = git(wt, "rev-parse", "HEAD")
res = safe(q.refresh_stale_base, job(wt))
chk("overlapping tracked edit -> skipped, not forced", bool(isinstance(res, dict) and res.get("skipped")), True)
chk("...HEAD unchanged", git(wt, "rev-parse", "HEAD"), h)
chk("...the model's edit is intact", (wt / "lib" / "a.ts").read_text(), "export const a = 'model wip';\n")

# 6. rebase conflict is ABORTED (no half-rebased worktree)
repo = make_repo("r6"); wt = make_wt(repo, "wt6")
(wt / "lib" / "a.ts").write_text("export const a = 'sealed';\n"); git(wt, "add", "-A"); git(wt, "commit", "-qm", "seal")
land(repo, "lib/a.ts", "export const a = 6;\n")
h = git(wt, "rev-parse", "HEAD")
res = safe(q.refresh_stale_base, job(wt))
chk("rebase conflict -> skipped", bool(isinstance(res, dict) and res.get("skipped")), True)
chk("...HEAD unchanged and no rebase in progress",
    (git(wt, "rev-parse", "HEAD"), bool(git(wt, "status", "--porcelain", "--untracked-files=no")),
     (Path(git(wt, "rev-parse", "--git-dir")) / "rebase-merge").exists()), (h, False, False))

# 7. wired into the LAUNCH path before the baseline is stamped
src = Path(A.queue).read_text()
i_ref = src.find("_rb = refresh_stale_base(job)")
i_base = src.find("apply_launch_baseline(job, measure_baseline(job[\"cwd\"]))")
chk("launch path calls refresh_stale_base before stamping the launch baseline", 0 < i_ref < i_base, True)

print()
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
