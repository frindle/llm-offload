#!/usr/bin/env python3
"""The reaper's DEFAULT roots must include the NVMe dispatch-worktrees root, so a
worktree there is scanned (not skipped as 'not a dispatch worktree').
Stubbed root only (OLLAMA_DISPATCH_NVME_ROOT); nothing real is touched, nothing applied."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile

p = os.path.expanduser("~/bin/dispatch-worktree-reap")
if len(sys.argv) > 1:
    p = sys.argv[1]
ld = importlib.machinery.SourceFileLoader("reap", p)
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("reap", ld))
ld.exec_module(m)

with tempfile.TemporaryDirectory() as tmp:
    nvme = os.path.join(tmp, "nvme-wts")
    os.makedirs(nvme)
    os.environ["OLLAMA_DISPATCH_NVME_ROOT"] = nvme
    os.environ.pop("OLLAMA_DISPATCH_WORKTREES", None)
    src = m.Sources(home=os.path.join(tmp, "home"))
    fails = []
    if os.path.realpath(nvme) not in [os.path.realpath(r) for r in src.roots]:
        fails.append("default Sources().roots lacks the NVMe root")
    if getattr(m, "NVME_ROOT", None) != "/Volumes/NVMe-Models/dispatch-worktrees":
        fails.append("NVME_ROOT constant wrong")
    # behavioural: a worktree under the stubbed NVMe root is planned (dry-run only)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    def g(*a, cwd):
        subprocess.run(["git", "-c", "init.defaultBranch=main"] + list(a), cwd=cwd, env=env,
                       check=True, capture_output=True)
    repo = os.path.join(tmp, "repo"); os.makedirs(repo)
    g("init", "-q", cwd=repo)
    g("commit", "-q", "--allow-empty", "-m", "i", cwd=repo)
    g("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=repo)
    wt = os.path.join(nvme, "ollama", "stub-wt")
    g("worktree", "add", "-q", "-b", "task/stub", wt, cwd=repo)
    open(os.path.join(wt, "new.txt"), "w").write("unlanded\n")
    live = m.LiveRefs()
    recs = m.plan_repo(repo, src, live, fetch=False)
    if not any(os.path.realpath(r["path"]) == os.path.realpath(wt) for r in recs):
        fails.append("worktree under NVMe root was skipped by plan_repo")
    print("FAIL: " + "; ".join(fails) if fails else "PASS")
    sys.exit(1 if fails else 0)
