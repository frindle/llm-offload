#!/usr/bin/env python3
"""worktree_cleanup -- remove dispatch/mini-swe/qagent worktrees once their work is landed.

Complements dispatch-worktree-reap (content-vs-origin/main, 5 repos, daily). This tool is
repo-agnostic (it follows each worktree's own .git pointer) and judges by GIT HISTORY, not
by file content. A worktree/clone is removed ONLY when ALL hold:

  1. it lives under a managed root (ROOTS) and is not node_modules-cache / the root itself
  2. older than GRACE_SECONDS (24h) -- newest of dir/top-level files/HEAD/index mtimes
  3. not referenced by a live/queued job, slice run, open run-status row, handoff row
     (reuses dispatch-worktree-reap.collect_live) and no running process has it as cwd
  4. not locked, not a primary checkout, branch not main/master
  5. clean: no tracked changes, no untracked files other than build output / pipeline scaffold
  6. no commit missing from landed work: HEAD is an ancestor of a landed ref (origin/HEAD,
     origin/main|master, main|master, the primary checkout's branch) OR contained in a
     remote-tracking branch
Anything it cannot prove (unreadable gitdir, git error) is KEPT, with the reason logged.

  worktree_cleanup.py sweep [--apply] [--grace-hours H] [--root DIR]...   # dry-run default
  worktree_cleanup.py job --job-id ID | --bundle B | --path P [--apply]   # terminal-state hook
  worktree_cleanup.py status                                              # enabled? last run

Automatic runs (qctl close/retire, escalation-watcher daily sweep) only APPLY when the
enable flag exists: ~/.ollama-dispatch/worktree-cleanup.enabled. Without it they dry-run
and log "would remove". Log: ~/.ollama-dispatch/worktree-cleanup.log.jsonl
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import argparse
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BIN = os.path.dirname(os.path.realpath(__file__))
GRACE_SECONDS = 24 * 3600
ROOTS = [
    os.path.join(HOME, ".ollama-dispatch", "worktrees"),
    os.path.join(HOME, ".ollama-dispatch", "worktrees.moved"),
    os.path.join(HOME, ".ollama-dispatch", "qagent-worktrees"),
    os.path.join(HOME, "dispatch-worktrees"),
    os.environ.get("OLLAMA_DISPATCH_NVME_ROOT", "/Volumes/NVMe-Models/dispatch-worktrees"),
]
PROTECTED_NAMES = {"node_modules-cache"}
PROTECTED_BRANCHES = ("main", "master")
BRANCH_PREFIXES = ("dispatch/", "slice/", "fix/", "wt-", "draft/", "refine/", "auto/")
ENABLE_FLAG = os.path.join(HOME, ".ollama-dispatch", "worktree-cleanup.enabled")
LOG_PATH = os.path.join(HOME, ".ollama-dispatch", "worktree-cleanup.log.jsonl")
QUEUE_STATE = os.path.join(str(queue_config.state_dir()), "ollama-queue-state.json")
QUEUE_LOGS = os.path.join(str(queue_config.state_dir()), "ollama-queue-logs")
MAX_DEPTH = 2  # root/<x> or root/<group>/<x>


def _reaper():
    """Load dispatch-worktree-reap (no .py suffix) for its artefact/scaffold lists and
    live-reference collector."""
    path = os.path.join(BIN, "dispatch-worktree-reap")
    loader = importlib.machinery.SourceFileLoader("dispatch_worktree_reap", path)
    spec = importlib.util.spec_from_loader("dispatch_worktree_reap", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def real(p):
    return os.path.realpath(os.path.expanduser(p))


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


# --- discovery ---------------------------------------------------------------------

def discover(roots):
    """Candidate dirs: those containing a .git entry, at most MAX_DEPTH below a root.
    Dirs without .git are reported by the caller as not-a-worktree (listed, not removed)."""
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue

        def walk(d, depth):
            try:
                names = sorted(os.listdir(d))
            except OSError:
                return
            for n in names:
                p = os.path.join(d, n)
                if os.path.islink(p) or not os.path.isdir(p):
                    continue
                if os.path.lexists(os.path.join(p, ".git")) or depth >= MAX_DEPTH:
                    out.append(p)
                else:
                    walk(p, depth + 1)
        walk(root, 1)
    return out


# --- git facts ---------------------------------------------------------------------

def gitdir_of(wt):
    """-> (kind, gitdir, common_dir). kind: 'linked' | 'clone' | 'broken' | 'none'."""
    g = os.path.join(wt, ".git")
    if os.path.isdir(g):
        return "clone", g, g
    if os.path.isfile(g):
        try:
            txt = open(g).read().strip()
        except OSError:
            return "broken", None, None
        if not txt.startswith("gitdir:"):
            return "broken", None, None
        gd = txt[7:].strip()
        if not os.path.isabs(gd):
            gd = os.path.join(wt, gd)
        if not os.path.isdir(gd):
            return "broken", gd, None
        cd = gd
        try:
            c = open(os.path.join(gd, "commondir")).read().strip()
            cd = c if os.path.isabs(c) else os.path.normpath(os.path.join(gd, c))
        except OSError:
            pass
        return "linked", gd, cd
    return "none", None, None


def newest_mtime(wt, gitdir, artefact):
    ts = [os.path.getmtime(wt)]
    try:
        for n in os.listdir(wt):
            if n == ".git" or artefact(n):
                continue
            try:
                ts.append(os.lstat(os.path.join(wt, n)).st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    for f in ("HEAD", "index", "logs/HEAD"):
        try:
            ts.append(os.path.getmtime(os.path.join(gitdir, f)))
        except (OSError, TypeError):
            pass
    return max(ts)


def landed_refs(wt, common):
    """Existing refs counted as 'landed': origin/HEAD, origin/main|master, main|master; else the
    branch checked out in the primary checkout."""
    cand = []
    r = git(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], wt)
    if r.returncode == 0 and r.stdout.strip():
        cand.append(r.stdout.strip())
    cand += ["origin/main", "origin/master", "main", "master"]
    seen, out = set(), []
    for c in cand:
        if c not in seen:
            seen.add(c)
            if git(["rev-parse", "-q", "--verify", c + "^{commit}"], wt).returncode == 0:
                out.append(c)
    # No main/master anywhere (e.g. ~/bin: local-only branch, archived origin): the primary
    # checkout's branch is the integration line. Otherwise a feature branch checked out in the
    # primary must NOT count as landed.
    if not out and common and os.path.basename(common) == ".git":
        pr = git(["symbolic-ref", "-q", "--short", "HEAD"], os.path.dirname(common))
        if pr.returncode == 0 and pr.stdout.strip():
            out.append(pr.stdout.strip())
    return out


# --- decision ----------------------------------------------------------------------

def running_cwds():
    """Set of realpaths that are the cwd of a running process (lsof -d cwd). None on failure."""
    try:
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=60)
    except Exception:  # noqa: BLE001
        return None
    return {real(l[1:]) for l in r.stdout.splitlines() if l.startswith("n/")}


def evaluate(wt, *, now, grace, live, procs, rp, roots=None):
    """-> (action, reason, info) with action 'remove' or 'keep'. Never raises."""
    try:
        return _evaluate(wt, now, grace, live, procs, rp, roots or ROOTS)
    except Exception as e:  # noqa: BLE001
        return "keep", f"error evaluating: {e}", {}


def _evaluate(wt, now, grace, live, procs, rp, roots):
    w = real(wt)
    if os.path.basename(w) in PROTECTED_NAMES or any(p in PROTECTED_NAMES for p in w.split("/")):
        return "keep", "protected (node_modules-cache)", {}
    if not any(under(w, real(r)) and w != real(r) for r in roots):
        return "keep", "outside managed roots", {}
    kind, gitdir, common = gitdir_of(wt)
    if kind == "none":
        return "keep", "not a git worktree/clone (no .git)", {}
    if kind == "broken":
        return "keep", f"gitdir missing/unreadable ({gitdir}); cannot verify commits", {}
    # grace period
    age = now - newest_mtime(wt, gitdir, rp.is_artefact)
    if age < grace:
        return "keep", f"within grace period ({age / 3600:.1f}h < {grace / 3600:.0f}h)", {}
    # live references
    hits = live.match(wt) if live is not None else [("unknown", "live refs unavailable")]
    if hits:
        return "keep", "live reference: " + "; ".join(sorted({f"{h[0]}: {h[1]}" for h in hits}))[:300], {}
    if procs is None:
        return "keep", "running-process check unavailable", {}
    for p in procs:
        if under(p, w):
            return "keep", f"running process has cwd {p}", {}
    if kind == "linked" and os.path.exists(os.path.join(gitdir, "locked")):
        return "keep", "worktree is locked", {}
    br = git(["symbolic-ref", "-q", "--short", "HEAD"], wt)
    branch = br.stdout.strip() if br.returncode == 0 else ""
    if kind == "linked" and branch in PROTECTED_BRANCHES:
        return "keep", f"on protected branch {branch}", {}
    if kind == "linked" and common and os.path.dirname(common) == w:
        return "keep", "primary checkout", {}
    # clean?
    st = git(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], wt)
    if st.returncode != 0:
        return "keep", f"git status failed: {st.stderr.strip()[:120]}", {}
    dirty = []
    for ent in filter(None, st.stdout.split("\0")):
        code, path = ent[:2], ent[3:]
        if code == "??":
            p = path.rstrip("/")
            if rp.is_artefact(p) or rp.is_scaffold_name(p):
                continue
            dirty.append(path)
        elif code[0] in "RC":  # rename/copy entry is followed by an origin path token
            dirty.append(path)
        else:
            dirty.append(path)
    # a rename's second token shows up as its own bare entry; any non-?? is dirty anyway
    if dirty:
        return "keep", f"dirty: {len(dirty)} path(s), e.g. {dirty[0]}", {}
    # landed?
    head = git(["rev-parse", "-q", "--verify", "HEAD"], wt)
    if head.returncode != 0:
        return "keep", "cannot resolve HEAD", {}
    sha = head.stdout.strip()
    landed = None
    for ref in landed_refs(wt, common):
        if git(["merge-base", "--is-ancestor", sha, ref], wt).returncode == 0:
            landed = ref
            break
    if landed is None:
        rb = git(["for-each-ref", "--contains", sha, "--format=%(refname:short)", "refs/remotes"], wt)
        remotes = [l for l in rb.stdout.split() if not l.endswith("/HEAD")]
        if remotes:
            landed = remotes[0]
    if landed is None:
        n = git(["rev-list", "--count", sha, "--not", "--remotes"] +
                [f"--branches=main", "--branches=master"], wt)
        return "keep", f"unlanded commits: HEAD {sha[:10]} not on any landed ref or remote" \
                       f" ({n.stdout.strip() or '?'} unique)", {}
    info = {"branch": branch, "landed_on": landed, "kind": kind, "head": sha[:10]}
    return "remove", f"clean, HEAD {sha[:10]} landed on {landed}, idle {age / 3600:.0f}h", info


# --- apply -------------------------------------------------------------------------

def _log(entry, log_path):
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def remove(wt, info, *, apply):
    """Remove one worktree/clone already judged removable. -> (ok, detail)."""
    if not apply:
        return True, "dry-run"
    kind, gitdir, common = gitdir_of(wt)
    if kind == "linked":
        owner = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        r = git(["worktree", "remove", "--force", wt], owner)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200]
        detail = "worktree removed"
        br = info.get("branch") or ""
        if br.startswith(BRANCH_PREFIXES) and br not in PROTECTED_BRANCHES:
            tip = git(["rev-parse", "-q", "--verify", f"refs/heads/{br}"], owner).stdout.strip()
            if tip.startswith(info.get("head", "?")):  # branch still at the verified HEAD
                d = git(["branch", "-D", br], owner)
                detail += "; branch deleted" if d.returncode == 0 else "; branch kept"
        git(["worktree", "prune"], owner)
        if os.path.isdir(wt):  # leftover ignored build output
            shutil.rmtree(wt, ignore_errors=True)
        return True, detail
    if kind == "clone":
        shutil.rmtree(wt, ignore_errors=False)
        return True, "clone removed"
    return False, f"unexpected kind {kind}"


def automatic_apply():
    return os.path.exists(ENABLE_FLAG)


def run(paths, *, apply, grace=GRACE_SECONDS, roots=None, now=None, live=None, procs="auto",
        log_path=LOG_PATH, out=sys.stdout, tag="sweep"):
    """Evaluate (and optionally remove) `paths`. Returns list of result dicts."""
    rp = _reaper()
    now = time.time() if now is None else now
    roots = roots or ROOTS
    if live is None:
        live = rp.collect_live(rp.Sources())
        hard = [e for e in live.errors if "API unreachable" not in e]
        if hard and apply:
            print("REFUSING apply, live-reference sources broken: " + "; ".join(hard), file=out)
            apply = False
    if procs == "auto":
        procs = running_cwds()
    results = []
    for wt in paths:
        action, reason, info = evaluate(wt, now=now, grace=grace, live=live, procs=procs,
                                        rp=rp, roots=roots)
        res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "path": wt,
               "action": action, "reason": reason, "mode": "apply" if apply else "dry-run",
               "trigger": tag}
        if action == "remove":
            try:
                ok, detail = remove(wt, info, apply=apply)
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)[:200]
            res["removed"] = bool(ok and apply)
            res["detail"] = detail
            if not ok:
                res["action"] = "keep"
                res["reason"] = f"remove failed: {detail}"
        print(("WOULD REMOVE " if res["action"] == "remove" and not apply else
               "REMOVED " if res.get("removed") else "SKIP ") + f"{wt}: {res['reason']}", file=out)
        _log(res, log_path)
        results.append(res)
    return results


def paths_for_job(job_id=None, bundle=None):
    """Worktree paths recorded for a job/bundle: queue rows + done/gate sidecars."""
    paths = set()
    rows = []
    try:
        rows = json.load(open(QUEUE_STATE)).get("jobs", [])
    except Exception:  # noqa: BLE001
        pass
    try:
        for f in os.listdir(QUEUE_LOGS):
            if f.endswith(".done.json"):
                try:
                    rows.append(json.load(open(os.path.join(QUEUE_LOGS, f))))
                except Exception:  # noqa: BLE001
                    pass
    except OSError:
        pass
    for j in rows:
        if (job_id and j.get("id") == job_id) or (bundle and (j.get("bundle") == bundle
                                                              or j.get("label") == bundle)):
            for k in ("cwd", "worktree"):
                if j.get(k):
                    paths.add(j[k])
    out = set()
    for p in paths:  # a cwd may be a subdir/work dir of the worktree: climb to the .git owner
        q = real(p)
        while q != "/" and not os.path.lexists(os.path.join(q, ".git")):
            q = os.path.dirname(q)
        if q != "/":
            out.add(q)
    return sorted(out)


def hook_terminal(job_id=None, bundle=None):
    """Best-effort call for qctl close/retire. Never raises; applies only when enabled."""
    try:
        ps = paths_for_job(job_id, bundle)
        if ps:
            run(ps, apply=automatic_apply(), tag="terminal:" + (job_id or bundle or ""))
    except Exception as e:  # noqa: BLE001
        print(f"worktree-cleanup hook: {e}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--auto", action="store_true", help="apply only if the enable flag exists")
    s.add_argument("--grace-hours", type=float, default=GRACE_SECONDS / 3600)
    s.add_argument("--root", action="append")
    j = sub.add_parser("job")
    j.add_argument("--job-id")
    j.add_argument("--bundle")
    j.add_argument("--path", action="append")
    j.add_argument("--apply", action="store_true")
    j.add_argument("--auto", action="store_true")
    j.add_argument("--grace-hours", type=float, default=GRACE_SECONDS / 3600)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(f"enable flag {ENABLE_FLAG}: {'PRESENT (automatic runs apply)' if automatic_apply() else 'absent (automatic runs dry-run)'}")
        print(f"log {LOG_PATH}")
        return 0
    apply = a.apply or (a.auto and automatic_apply())
    grace = a.grace_hours * 3600
    if a.cmd == "sweep":
        roots = a.root or ROOTS
        run(discover(roots), apply=apply, grace=grace, roots=roots, tag="sweep")
    else:
        ps = list(a.path or []) + paths_for_job(a.job_id, a.bundle)
        if not ps:
            print("no worktree paths found for that job/bundle")
            return 0
        run(ps, apply=apply, grace=grace, tag="job")
    if not apply:
        print("(dry-run; nothing changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
