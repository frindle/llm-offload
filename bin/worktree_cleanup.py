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
  7. a plain clone (deleted whole) also needs: no stash, and EVERY local branch and tag tip (not
     just HEAD) on a remote that outlives it -- a network remote's tracking refs, or a local
     source repo that still exists outside the managed roots and has the commit on one of its refs
     (2026-11-15: 76 of 97 clones in a sweep held local-branch commits that HEAD alone hid)
Anything it cannot prove (unreadable gitdir, git error) is KEPT, with the reason logged.

  worktree_cleanup.py sweep [--apply] [--grace-hours H] [--root DIR]...   # dry-run default
  worktree_cleanup.py job --job-id ID | --bundle B | --path P [--apply]   # terminal-state hook
  worktree_cleanup.py branches [--apply|--auto] [--no-remote] [--repo DIR]... # merged branches
  worktree_cleanup.py status                                              # enabled? last run

Default sweep roots also cover each project repo's .claude/worktrees (one per real path: the
.nosync names are symlinks to the same repos). A squash/rebase-merged PR whose final head is the
worktree's HEAD counts as landed (GitHub deletes the branch, so no remote ref holds it).
`branches` deletes local branches (and, for REMOTE_OWNERS repos, origin branches with no open PR)
whose tip is in the default branch or is a merged PR's final head, untouched for GRACE_SECONDS.
`sweep --branches` runs it after the worktree pass (the daily automatic run does).

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
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
PROJECTS_DIR = os.environ.get("WORKTREE_CLEANUP_PROJECTS",
                              os.path.join(HOME, "Desktop", "GitHub Projects.nosync"))
SKIP_REPOS = {"homelab-database"}  # pull-only repo: its branches are never touched
REMOTE_OWNERS = ("frindle",)       # origin branches are deleted only in these owners' repos
_GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
# Stale leftovers in the dispatch roots (never the project repos' own .claude/worktrees) that are
# dirty or hold unlanded commits are archived, then removed: changes.patch (tracked edits),
# untracked/ (non-build files up to ARCHIVE_MAX_FILE), commits.bundle (commits no remote or landed
# ref has) and meta.json, in ARCHIVE_DIR/<name>-<ts>.tar.gz, kept ARCHIVE_KEEP_SECONDS.
ARCHIVE_AFTER_SECONDS = 14 * 86400
ARCHIVE_KEEP_SECONDS = 90 * 86400
ARCHIVE_MAX_FILE = 20 * 1024 * 1024
ARCHIVE_ROOTS = list(ROOTS)
ARCHIVE_DIR = os.environ.get("WORKTREE_ARCHIVE_DIR") or (
    "/Volumes/NVMe-Models/worktree-archive" if os.path.isdir("/Volumes/NVMe-Models")
    else os.path.join(HOME, ".ollama-dispatch", "worktree-archive"))  # a dry run must not refresh the index (resets grace)
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
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=_GIT_ENV)


def real(p):
    return os.path.realpath(os.path.expanduser(p))


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


# --- discovery ---------------------------------------------------------------------

def discover(roots):
    """Candidate dirs: those containing a .git entry, at most MAX_DEPTH below a root.
    Dirs without .git are reported by the caller as not-a-worktree (listed, not removed)."""
    out, seen = [], set()
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
                    if real(p) not in seen:
                        seen.add(real(p))
                        out.append(p)
                else:
                    walk(p, depth + 1)
        walk(root, 1)
    return out


def project_repos(projects_dir=None):
    """Primary checkouts directly under the projects dir, one per real path."""
    d = projects_dir or PROJECTS_DIR
    out, seen = [], set()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        r = real(os.path.join(d, n))
        if r not in seen and os.path.isdir(os.path.join(r, ".git")):
            seen.add(r)
            out.append(r)
    return out


def default_roots(projects_dir=None):
    return ROOTS + [os.path.join(r, ".claude", "worktrees") for r in project_repos(projects_dir)
                    if os.path.isdir(os.path.join(r, ".claude", "worktrees"))]


# --- GitHub PRs (squash merges leave HEAD off main's history) ----------------------

_PR_CACHE = {}


def gh_slug(wt):
    u = git(["remote", "get-url", "origin"], wt).stdout.strip()
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/(.+?)(?:\.git)?/?$", u)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_prs(slug):
    """slug -> [{number, state, headRefName, headRefOid}], or None when gh cannot answer."""
    if slug not in _PR_CACHE:
        try:
            r = subprocess.run(["gh", "pr", "list", "--repo", slug, "--state", "all", "--limit", "1000",
                                "--json", "number,state,headRefName,headRefOid"],
                               capture_output=True, text=True, timeout=90)
            _PR_CACHE[slug] = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            _PR_CACHE[slug] = None
    return _PR_CACHE[slug]


def merged_pr_for(wt, sha):
    """Number of a merged PR whose final head is exactly `sha`, else None."""
    slug = gh_slug(wt)
    for p in (repo_prs(slug) if slug else None) or []:
        if p.get("state") == "MERGED" and p.get("headRefOid") == sha:
            return p.get("number")
    return None


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


def _surviving_remotes(wt, roots):
    """A plain clone's remotes that outlive it -> [(name, local_repo_path or None for network)]. A local-path
    remote that is gone, or that sits under a managed root (it may be swept in the same run), does not count."""
    out = []
    for name in git(["remote"], wt).stdout.split():
        url = git(["remote", "get-url", name], wt).stdout.strip()
        if not url:
            continue
        path = None
        if url.startswith("file://"):
            path = url[len("file://"):]
        elif url.startswith(("/", ".", "~")) or not re.match(r"^[^/]*:", url):   # not scheme:// or scp-like host:
            path = url
        if path is not None:
            path = os.path.expanduser(path)
            if not os.path.isabs(path):
                path = os.path.join(wt, path)
            path = real(path)
            if not os.path.isdir(path) or any(under(path, real(r)) for r in roots):
                continue
        out.append((name, path))
    return out


def clone_loses(wt, roots):
    """A plain clone is deleted whole, so HEAD landing is not enough. -> why it must stay, or '' when every
    local branch and tag tip (and HEAD) is on a surviving remote and there is no stash."""
    if git(["rev-parse", "-q", "--verify", "refs/stash"], wt).returncode == 0:
        return "clone has a stash"
    keepers = _surviving_remotes(wt, roots)
    if not keepers:
        return "clone has no remote that outlives it (none, gone, or under a managed root)"
    r = git(["for-each-ref", "--format=%(objectname) %(*objectname) %(refname:short)", "refs/heads", "refs/tags"], wt)
    if r.returncode != 0:
        return "cannot list the clone's branches and tags"
    tips = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            tips.setdefault(parts[0], parts[1])
        elif len(parts) >= 3:                       # annotated tag: the peeled commit
            tips.setdefault(parts[1], parts[2])
    h = git(["rev-parse", "-q", "--verify", "HEAD"], wt)
    if h.returncode == 0:
        tips.setdefault(h.stdout.strip(), "HEAD")
    if not tips:
        return ""
    rl = git(["rev-list", *sorted(tips), "--not", *[f"--remotes={n}" for n, _ in keepers]], wt)
    if rl.returncode != 0:
        return f"cannot verify the clone's branches: {rl.stderr.strip()[:120]}"
    missing = [s for s in sorted(tips) if s in set(rl.stdout.split())]
    local = [p for _, p in keepers if p]
    if missing and local and len(missing) <= 50:   # tracking refs can be stale: ask the local source repo itself
        missing = [s for s in missing if not any(
            (c := git(["for-each-ref", "--contains", s, "--count=1", "--format=%(refname)"], p)).returncode == 0
            and c.stdout.strip() for p in local)]
    if missing:
        return (f"clone has {len(missing)} branch/tag tip(s) on no surviving remote, e.g. {tips[missing[0]]}"
                f" ({missing[0][:10]})")
    return ""


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
    stale = age >= ARCHIVE_AFTER_SECONDS and any(under(w, real(r)) and w != real(r) for r in ARCHIVE_ROOTS)
    if dirty:
        if stale:
            return "archive", f"stale ({age / 86400:.0f}d) and dirty: {len(dirty)} path(s)", \
                {"branch": branch, "kind": kind, "head": _head(wt), "dirty": True}
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
        n = merged_pr_for(wt, sha)
        if n:
            landed = f"merged PR #{n}"
    if landed is None:
        n = git(["rev-list", "--count", sha, "--not", "--remotes"] +
                [f"--branches=main", "--branches=master"], wt)
        if stale:
            return "archive", f"stale ({age / 86400:.0f}d) with {n.stdout.strip() or '?'} unlanded commit(s)", \
                {"branch": branch, "kind": kind, "head": sha[:10]}
        return "keep", f"unlanded commits: HEAD {sha[:10]} not on any landed ref or remote" \
                       f" ({n.stdout.strip() or '?'} unique)", {}
    if kind == "clone":
        lost = clone_loses(wt, roots)
        if lost:
            if stale:
                return "archive", f"stale ({age / 86400:.0f}d) clone: {lost}"[:300], \
                    {"branch": branch, "kind": kind, "head": sha[:10]}
            return "keep", lost, {}
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


def _head(wt):
    return git(["rev-parse", "-q", "--verify", "HEAD"], wt).stdout.strip()[:10]


def archive(wt, info, *, rp, reason=""):
    """Write ARCHIVE_DIR/<name>-<ts>.tar.gz holding everything removal would lose. -> its path.
    Raises on any failure, so the caller keeps the worktree."""
    kind, gitdir, common = gitdir_of(wt)
    tmp = tempfile.mkdtemp(prefix="wtc-arch-")
    dest = None
    try:
        d = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=wt, capture_output=True, env=_GIT_ENV)
        if d.returncode != 0:
            raise RuntimeError("git diff failed: " + d.stderr.decode(errors="replace")[:120])
        with open(os.path.join(tmp, "changes.patch"), "wb") as fh:
            fh.write(d.stdout)
        refs = ["--all"] if kind == "clone" else ["HEAD"]
        excl = ["--not", "--remotes"] + landed_refs(wt, common)
        bundle = os.path.join(tmp, "commits.bundle")
        b = git(["bundle", "create", "-q", bundle] + refs + excl, wt)
        if b.returncode != 0 and "empty bundle" not in b.stderr.lower():
            raise RuntimeError("git bundle failed: " + b.stderr.strip()[:120])
        if os.path.exists(bundle) and git(["bundle", "verify", "-q", bundle], wt).returncode != 0:
            raise RuntimeError("bundle does not verify")
        skipped = []
        unt = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"], cwd=wt,
                             capture_output=True, env=_GIT_ENV)
        if unt.returncode != 0:
            raise RuntimeError("git ls-files failed")
        files = []
        for rel in filter(None, unt.stdout.decode(errors="surrogateescape").split("\0")):
            full = os.path.join(wt, rel)
            if rp.is_artefact(rel) or os.path.islink(full) or not os.path.isfile(full):
                continue
            if os.path.getsize(full) > ARCHIVE_MAX_FILE:
                skipped.append(rel)
                continue
            files.append(rel)
        meta = {"path": wt, "kind": kind, "branch": info.get("branch"), "head": _head(wt),
                "origin": git(["remote", "get-url", "origin"], wt).stdout.strip(), "reason": reason,
                "archived": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "untracked_skipped_over_limit": skipped,
                "restore": "git worktree add <dir> <head> (or git fetch commits.bundle), git apply changes.patch, copy untracked/"}
        with open(os.path.join(tmp, "meta.json"), "w") as fh:
            json.dump(meta, fh, indent=1)
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(wt.rstrip("/")))[:80]
        dest = os.path.join(ARCHIVE_DIR, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        with tarfile.open(dest + ".part", "w:gz") as tf:
            for n in sorted(os.listdir(tmp)):
                tf.add(os.path.join(tmp, n), arcname=n)
            for rel in files:
                tf.add(os.path.join(wt, rel), arcname="untracked/" + rel, recursive=False)
        with tarfile.open(dest + ".part", "r:gz") as tf:  # readable end to end before anything goes
            names = set(tf.getnames())
        if "changes.patch" not in names or "meta.json" not in names:
            raise RuntimeError("archive incomplete")
        os.replace(dest + ".part", dest)
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if dest and os.path.exists(dest + ".part"):
            os.remove(dest + ".part")


def prune_archives(now=None, out=sys.stdout):
    now = time.time() if now is None else now
    try:
        names = os.listdir(ARCHIVE_DIR)
    except OSError:
        return 0
    n = 0
    for f in names:
        p = os.path.join(ARCHIVE_DIR, f)
        try:
            if f.endswith(".tar.gz") and now - os.path.getmtime(p) > ARCHIVE_KEEP_SECONDS:
                os.remove(p)
                n += 1
        except OSError:
            pass
    if n:
        print(f"pruned {n} archive(s) older than {ARCHIVE_KEEP_SECONDS // 86400}d", file=out)
    return n


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
        if br and br not in PROTECTED_BRANCHES:
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
    roots = roots or default_roots()
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
        if action == "archive":
            try:
                if apply:
                    res["archive"] = archive(wt, info, rp=rp, reason=reason)
                ok, detail = remove(wt, info, apply=apply)
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)[:200]
            res["removed"] = bool(ok and apply)
            res["detail"] = detail
            if not ok:
                res["action"] = "keep"
                res["reason"] = f"archive/remove failed: {detail}"
        elif action == "remove":
            try:
                ok, detail = remove(wt, info, apply=apply)
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)[:200]
            res["removed"] = bool(ok and apply)
            res["detail"] = detail
            if not ok:
                res["action"] = "keep"
                res["reason"] = f"remove failed: {detail}"
        verb = "ARCHIVE+REMOVE" if res["action"] == "archive" else "REMOVE"
        print((f"WOULD {verb} " if res["action"] in ("remove", "archive") and not apply else
               ("ARCHIVED+REMOVED " if res["action"] == "archive" else "REMOVED ") if res.get("removed")
               else "SKIP ") + f"{wt}: {res['reason']}", file=out)
        _log(res, log_path)
        results.append(res)
    return results


def _ref_age(repo, ref, tip, now):
    """Seconds since the ref last moved (newest reflog entry), else since the tip's commit date."""
    r = git(["reflog", "show", "-n1", "--date=unix", "--format=%gd", ref], repo)
    m = re.search(r"@\{(\d+)\}", r.stdout) if r.returncode == 0 else None
    ts = m.group(1) if m else git(["show", "-s", "--format=%ct", tip], repo).stdout.strip()
    try:
        return now - int(ts)
    except ValueError:
        return 0  # unknown age: treat as fresh (kept)


def _commit_age(repo, tip, now):
    try:
        return now - int(git(["show", "-s", "--format=%ct", tip], repo).stdout.strip())
    except ValueError:
        return 0


def prune_branches(repo, *, apply, remote=True, fetch=True, grace=GRACE_SECONDS, now=None,
                   log_path=LOG_PATH, out=sys.stdout):
    """Delete branches whose work is already in the default branch or a merged PR. -> results.
    Local: not main/master, not checked out in any worktree, tip an ancestor of a landed ref or a
    merged PR's final head, ref unmoved for `grace`. Remote (REMOTE_OWNERS repos only, gh must
    answer): same, against origin's default branch only, and no open PR uses the branch."""
    now = time.time() if now is None else now
    results = []
    if os.path.basename(repo) in SKIP_REPOS:
        return results
    has_origin = git(["remote", "get-url", "origin"], repo).returncode == 0
    if fetch and has_origin:
        git(["fetch", "--prune", "--quiet", "origin"], repo)
    refs = landed_refs(repo, os.path.join(repo, ".git"))
    busy = {l[len("branch refs/heads/"):] for l in git(["worktree", "list", "--porcelain"], repo).stdout.splitlines()
            if l.startswith("branch refs/heads/")}
    slug = gh_slug(repo) if has_origin else None
    prs = repo_prs(slug) if slug else None
    merged = {p["headRefOid"]: p["number"] for p in prs or [] if p.get("state") == "MERGED"}
    open_heads = {p["headRefName"] for p in prs or [] if p.get("state") == "OPEN"}

    def landed_in(tip, among):
        for ref in among:
            if git(["merge-base", "--is-ancestor", tip, ref], repo).returncode == 0:
                return ref
        return f"merged PR #{merged[tip]}" if tip in merged else None

    def record(where, br, action, reason):
        res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "path": repo, "branch": br,
               "where": where, "action": action, "reason": reason, "mode": "apply" if apply else "dry-run",
               "trigger": "branches"}
        if action == "delete" and apply:
            d = git(["branch", "-D", br], repo) if where == "local" else \
                git(["push", "--quiet", "origin", "--delete", br], repo)
            res["removed"] = d.returncode == 0
            if not res["removed"]:
                res["action"], res["reason"] = "keep", f"delete failed: {d.stderr.strip()[:160]}"
        tag = ("WOULD DELETE " if res["action"] == "delete" and not apply else
               "DELETED " if res.get("removed") else "KEEP ")
        print(f"{tag}{where} {os.path.basename(repo)}:{br}: {res['reason']}", file=out)
        _log(res, log_path)
        results.append(res)

    for line in git(["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"], repo).stdout.splitlines():
        br, tip = line.rsplit(" ", 1)
        if br in PROTECTED_BRANCHES or br in busy:
            continue
        where = landed_in(tip, refs)
        if not where:
            record("local", br, "keep", "unlanded")
        elif _ref_age(repo, f"refs/heads/{br}", tip, now) < grace:
            record("local", br, "keep", f"landed on {where}, but moved within grace")
        else:
            record("local", br, "delete", f"landed on {where}")

    if not (remote and slug and prs is not None and slug.split("/")[0] in REMOTE_OWNERS):
        return results
    head = git(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], repo).stdout.strip()
    base = [r for r in [head, "origin/main", "origin/master"] if r and
            git(["rev-parse", "-q", "--verify", r + "^{commit}"], repo).returncode == 0][:1]
    if not base:
        return results
    for line in git(["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes/origin"],
                    repo).stdout.splitlines():
        ref, tip = line.rsplit(" ", 1)
        br = ref[len("origin/"):]
        if not ref.startswith("origin/") or br in ("HEAD", "") or ref == base[0] or br in PROTECTED_BRANCHES:
            continue
        if br in open_heads:
            record("remote", br, "keep", "open PR")
            continue
        where = landed_in(tip, base)
        if not where:
            record("remote", br, "keep", "unlanded")
        elif _commit_age(repo, tip, now) < grace:
            record("remote", br, "keep", f"landed on {where}, but new within grace")
        else:
            record("remote", br, "delete", f"landed on {where}")
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
    s.add_argument("--branches", action="store_true", help="then prune merged branches in project repos")
    b = sub.add_parser("branches")
    b.add_argument("--apply", action="store_true")
    b.add_argument("--auto", action="store_true")
    b.add_argument("--no-remote", action="store_true")
    b.add_argument("--repo", action="append")
    b.add_argument("--grace-hours", type=float, default=GRACE_SECONDS / 3600)
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
        roots = a.root or default_roots()
        run(discover(roots), apply=apply, grace=grace, roots=roots, tag="sweep")
        if apply:
            prune_archives()
        if a.branches:
            for repo in project_repos():
                prune_branches(repo, apply=apply, grace=grace)
    elif a.cmd == "branches":
        for repo in [real(r) for r in a.repo] if a.repo else project_repos():
            prune_branches(repo, apply=apply, remote=not a.no_remote, grace=grace)
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
