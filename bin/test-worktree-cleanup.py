#!/usr/bin/env python3
"""Safety-rule tests for worktree_cleanup.py (temp repos only; never touches real worktrees)."""
import io, os, subprocess, sys, tempfile, time, unittest, importlib.util

BIN = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location("worktree_cleanup", os.path.join(BIN, "worktree_cleanup.py"))
wc = importlib.util.module_from_spec(spec); spec.loader.exec_module(wc)
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_NOSYSTEM="1",
           GIT_CONFIG_GLOBAL=os.devnull)


def g(cwd, *a):
    r = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *a],
                       cwd=cwd, capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout


def w(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)


def age(wt, hours):
    t = time.time() - hours * 3600
    gd = wc.gitdir_of(wt)[1]
    for p in [wt] + [os.path.join(wt, n) for n in os.listdir(wt) if n != ".git"] + \
            [os.path.join(gd, f) for f in ("HEAD", "index", "logs/HEAD")]:
        if os.path.exists(p):
            os.utime(p, (t, t))


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wtc-test-")
        origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "proj")
        self.root = os.path.join(self.tmp, "wts")
        os.makedirs(self.root)
        g(self.tmp, "init", "-q", "--bare", origin)
        g(self.tmp, "clone", "-q", origin, self.repo)
        w(f"{self.repo}/a.py", "1\n"); w(f"{self.repo}/.gitignore", "node_modules/\n")
        g(self.repo, "add", "-A"); g(self.repo, "commit", "-qm", "base"); g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "fetch", "-q", "origin")
        self.rp = wc._reaper()
        self.live = self.rp.LiveRefs()

    def mk(self, name, hours=48):
        wt = os.path.join(self.root, name)
        g(self.repo, "worktree", "add", "-q", "-b", f"dispatch/{name}", wt, "main")
        age(wt, hours)
        return wt

    def run_(self, wt, apply=True, **kw):
        out = io.StringIO()
        kw.setdefault("live", self.live); kw.setdefault("procs", set())
        r = wc.run([wt], apply=apply, roots=[self.root], log_path=os.path.join(self.tmp, "log.jsonl"),
                   out=out, **kw)
        return r[0], out.getvalue()

    def test_clean_landed_old_is_removed_with_branch(self):
        wt = self.mk("ok")
        w(f"{wt}/node_modules/pkg/i.js"); w(f"{wt}/TASK.md")  # build output + scaffold ignored
        age(wt, 48)
        r, _ = self.run_(wt)
        self.assertTrue(r["removed"], r)
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn("dispatch/ok", g(self.repo, "branch"))
        self.assertNotIn(wt, g(self.repo, "worktree", "list"))

    def test_dry_run_deletes_nothing_but_logs(self):
        wt = self.mk("dry")
        r, out = self.run_(wt, apply=False)
        self.assertEqual(r["action"], "remove"); self.assertFalse(r.get("removed"))
        self.assertTrue(os.path.isdir(wt)); self.assertIn("WOULD REMOVE", out)
        self.assertIn("dispatch/dry", g(self.repo, "branch"))
        self.assertTrue(os.path.getsize(os.path.join(self.tmp, "log.jsonl")) > 0)

    def test_dirty_tracked_kept(self):
        wt = self.mk("dirty"); w(f"{wt}/a.py", "changed\n"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("dirty", r["reason"])
        self.assertTrue(os.path.isdir(wt))

    def test_untracked_real_file_kept(self):
        wt = self.mk("untr"); w(f"{wt}/DIAGNOSIS.md"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("dirty", r["reason"])

    def test_unlanded_commit_kept(self):
        wt = self.mk("ahead"); w(f"{wt}/b.py"); g(wt, "add", "-A"); g(wt, "commit", "-qm", "new"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("unlanded", r["reason"])
        self.assertTrue(os.path.isdir(wt)); self.assertIn("dispatch/ahead", g(self.repo, "branch"))

    def test_commit_pushed_to_remote_counts_as_landed(self):
        wt = self.mk("pushed"); w(f"{wt}/b.py"); g(wt, "add", "-A"); g(wt, "commit", "-qm", "new")
        g(wt, "push", "-q", "origin", "HEAD:refs/heads/feature"); g(wt, "fetch", "-q", "origin"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "remove", r)

    def test_live_reference_kept(self):
        wt = self.mk("live"); self.live.add(wt, "queue", "running job abc")
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("live reference", r["reason"])
        self.assertTrue(os.path.isdir(wt))

    def test_running_process_kept(self):
        wt = self.mk("proc")
        r, _ = self.run_(wt, procs={os.path.realpath(wt) + "/sub"})
        self.assertEqual(r["action"], "keep"); self.assertIn("running process", r["reason"])

    def test_process_check_unavailable_kept(self):
        wt = self.mk("noproc"); r, _ = self.run_(wt, procs=None); self.assertEqual(r["action"], "keep")

    def test_grace_period_kept(self):
        wt = self.mk("fresh", hours=1)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("grace", r["reason"])
        r, _ = self.run_(wt, grace=0.5 * 3600); self.assertEqual(r["action"], "remove")  # constant is a parameter

    def test_grace_default_is_24h(self):
        self.assertEqual(wc.GRACE_SECONDS, 24 * 3600)

    def test_node_modules_cache_protected(self):
        d = os.path.join(self.root, "node_modules-cache"); os.makedirs(d)
        g(self.repo, "worktree", "add", "-q", "-b", "dispatch/nm", d, "main"); age(d, 48)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep"); self.assertIn("node_modules-cache", r["reason"])
        self.assertTrue(os.path.isdir(d))

    def test_locked_kept(self):
        wt = self.mk("locked"); g(self.repo, "worktree", "lock", wt); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep"); self.assertIn("locked", r["reason"])

    def test_outside_root_and_broken_gitdir_kept(self):
        r, _ = self.run_(self.repo); self.assertEqual(r["action"], "keep")
        bad = os.path.join(self.root, "broken"); os.makedirs(bad); w(f"{bad}/.git", "gitdir: /nonexistent/x\n")
        r, _ = self.run_(bad); self.assertEqual(r["action"], "keep"); self.assertIn("gitdir", r["reason"])
        self.assertTrue(os.path.isdir(bad))

    def test_standalone_clone_removed_when_clean_and_landed(self):
        d = os.path.join(self.root, "clone1")
        g(self.tmp, "clone", "-q", os.path.join(self.tmp, "origin.git"), d)
        t = time.time() - 48 * 3600
        for p in [d] + [os.path.join(d, n) for n in os.listdir(d)] + [os.path.join(d, ".git", f) for f in ("HEAD", "index", "logs/HEAD")]:
            os.utime(p, (t, t))
        r, _ = self.run_(d); self.assertEqual(r["action"], "remove", r); self.assertTrue(r["removed"], r); self.assertFalse(os.path.exists(d))

    def clone_(self, name, src=None):
        d = os.path.join(self.root, name)
        g(self.tmp, "clone", "-q", src or os.path.join(self.tmp, "origin.git"), d)
        return d

    def old(self, d):
        t = time.time() - 48 * 3600
        for p in [d] + [os.path.join(d, n) for n in os.listdir(d) if n != ".git"] + \
                [os.path.join(d, ".git", f) for f in ("HEAD", "index", "logs/HEAD")]:
            if os.path.exists(p):
                os.utime(p, (t, t))

    def side_commit(self, d, branch="side"):
        g(d, "checkout", "-q", "-b", branch); w(f"{d}/s.py", branch + "\n"); g(d, "add", "-A")
        g(d, "commit", "-qm", branch); g(d, "checkout", "-q", "main")

    def test_clone_with_unpushed_side_branch_kept(self):
        # HEAD (main) is landed, but a local branch is not: rmtree would lose it (the 11-15 sweep bug)
        d = self.clone_("side1"); self.side_commit(d); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep", r); self.assertIn("side", r["reason"])
        self.assertTrue(os.path.isdir(d))

    def test_clone_with_unpushed_annotated_tag_kept(self):
        d = self.clone_("tag1"); w(f"{d}/t.py"); g(d, "add", "-A"); g(d, "commit", "-qm", "t")
        g(d, "tag", "-a", "v9", "-m", "v9"); g(d, "reset", "-q", "--hard", "origin/main"); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep", r); self.assertIn("v9", r["reason"])

    def test_clone_with_stash_kept(self):
        d = self.clone_("stash1"); w(f"{d}/a.py", "2\n"); g(d, "stash", "-q"); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep", r); self.assertIn("stash", r["reason"])

    def test_clone_whose_local_remote_is_gone_kept(self):
        src = os.path.join(self.tmp, "gone.git"); g(self.tmp, "clone", "-q", "--bare", os.path.join(self.tmp, "origin.git"), src)
        d = self.clone_("gone1", src); import shutil; shutil.rmtree(src); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep", r); self.assertIn("no remote", r["reason"])

    def test_clone_whose_remote_is_under_the_root_kept(self):
        inner = self.clone_("inner-src")      # a clone under the managed root may be swept in the same run
        d = self.clone_("outer1", inner); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "keep", r); self.assertIn("no remote", r["reason"])

    def test_clone_branch_found_in_local_source_repo_removed(self):
        # tracking refs are stale, but the local source repo has the branch's commit on a ref: nothing is lost
        d = self.clone_("src1", self.repo); self.side_commit(d)
        g(self.repo, "fetch", "-q", d, "side:from-clone"); self.old(d)
        r, _ = self.run_(d); self.assertEqual(r["action"], "remove", r); self.assertFalse(os.path.exists(d))

    def test_feature_branch_in_primary_is_not_landed(self):
        g(self.repo, "checkout", "-q", "-b", "feature"); w(f"{self.repo}/f.py"); g(self.repo, "add", "-A")
        g(self.repo, "commit", "-qm", "f")
        wt = os.path.join(self.root, "onfeat"); g(self.repo, "worktree", "add", "-q", "--detach", wt, "feature"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r)

    def test_discover_skips_non_git_dirs_and_symlinks(self):
        self.mk("d1"); os.makedirs(os.path.join(self.root, "plain", "inner"))
        found = [os.path.basename(p) for p in wc.discover([self.root])]
        self.assertIn("d1", found)

    # --- 11-19: repo .claude/worktrees roots, squash-merged PRs, branch prune -------------

    def fake_prs(self, prs, slug="frindle/proj"):
        old = (wc.gh_slug, wc.repo_prs)
        wc.gh_slug = lambda wt: slug
        wc.repo_prs = lambda s: prs
        self.addCleanup(lambda: (setattr(wc, "gh_slug", old[0]), setattr(wc, "repo_prs", old[1])))

    def ahead(self, name):
        wt = self.mk(name); w(f"{wt}/b.py", name); g(wt, "add", "-A"); g(wt, "commit", "-qm", name); age(wt, 48)
        return wt, g(wt, "rev-parse", "HEAD").strip()

    def test_discover_dedupes_symlinked_roots(self):
        self.mk("d1"); link = os.path.join(self.tmp, "wts.nosync"); os.symlink(self.root, link)
        found = wc.discover([self.root, link])
        self.assertEqual([os.path.basename(p) for p in found], ["d1"])

    def test_squash_merged_pr_head_counts_as_landed(self):
        wt, sha = self.ahead("sq")
        self.fake_prs([{"number": 7, "state": "MERGED", "headRefName": "dispatch/sq", "headRefOid": sha}])
        r, _ = self.run_(wt); self.assertEqual(r["action"], "remove", r); self.assertIn("PR #7", r["reason"])
        self.assertNotIn("dispatch/sq", g(self.repo, "branch"))

    def test_closed_or_older_pr_head_is_not_landed(self):
        wt, sha = self.ahead("cl")
        self.fake_prs([{"number": 8, "state": "CLOSED", "headRefName": "dispatch/cl", "headRefOid": sha},
                       {"number": 9, "state": "MERGED", "headRefName": "dispatch/cl", "headRefOid": "0" * 40}])
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r); self.assertIn("unlanded", r["reason"])

    def test_pr_lookup_failure_keeps(self):
        wt, _ = self.ahead("nogh"); self.fake_prs(None)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r)

    def test_any_branch_name_deleted_with_its_worktree(self):
        wt = os.path.join(self.root, "agent-x")
        g(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-x", wt, "main"); age(wt, 48)
        r, _ = self.run_(wt); self.assertTrue(r["removed"], r)
        self.assertNotIn("worktree-agent-x", g(self.repo, "branch"))

    def test_dry_run_does_not_refresh_index(self):
        os.makedirs(f"{self.repo}/sub"); w(f"{self.repo}/sub/x.py"); g(self.repo, "add", "-A")
        g(self.repo, "commit", "-qm", "sub"); g(self.repo, "push", "-q", "origin", "main"); g(self.repo, "fetch", "-q")
        wt = self.mk("idx"); t = time.time() - 72 * 3600; os.utime(f"{wt}/sub/x.py", (t, t))  # stale stat
        for _ in range(2):
            r, _ = self.run_(wt, apply=False); self.assertEqual(r["action"], "remove", r)

    def test_default_roots_cover_project_worktrees_once(self):
        proj = os.path.join(self.tmp, "projects"); os.makedirs(proj)
        os.symlink(self.repo, os.path.join(proj, "proj")); os.symlink(self.repo, os.path.join(proj, "proj.nosync"))
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees"))
        extra = [r for r in wc.default_roots(proj) if r not in wc.ROOTS]
        self.assertEqual(extra, [os.path.join(os.path.realpath(self.repo), ".claude", "worktrees")])

    def prune(self, apply=True, later=48, **kw):
        out = io.StringIO()
        r = wc.prune_branches(self.repo, apply=apply, fetch=False, now=time.time() + later * 3600,
                              log_path=os.path.join(self.tmp, "log.jsonl"), out=out, **kw)
        return {(x["where"], x["branch"]): x for x in r}, out.getvalue()

    def test_prune_local_branches(self):
        g(self.repo, "branch", "merged-old", "main")
        g(self.repo, "checkout", "-q", "-b", "unmerged"); w(f"{self.repo}/u.py"); g(self.repo, "add", "-A")
        g(self.repo, "commit", "-qm", "u"); g(self.repo, "checkout", "-q", "main")
        self.mk("busy")  # dispatch/busy is checked out in a worktree: never touched
        self.fake_prs([])
        r, _ = self.prune()
        self.assertTrue(r[("local", "merged-old")]["removed"])
        self.assertEqual(r[("local", "unmerged")]["action"], "keep")
        self.assertNotIn(("local", "dispatch/busy"), r)
        self.assertNotIn(("local", "main"), r)
        br = g(self.repo, "branch")
        self.assertNotIn("merged-old", br); self.assertIn("unmerged", br); self.assertIn("dispatch/busy", br)

    def test_prune_keeps_recently_moved_branch_and_dry_run(self):
        g(self.repo, "branch", "fresh", "main"); self.fake_prs([])
        r, _ = self.prune(later=1); self.assertEqual(r[("local", "fresh")]["action"], "keep")
        r, out = self.prune(apply=False); self.assertIn("WOULD DELETE local proj:fresh", out)
        self.assertIn("fresh", g(self.repo, "branch"))

    def test_prune_squash_merged_branch(self):
        g(self.repo, "checkout", "-q", "-b", "sq"); w(f"{self.repo}/q.py"); g(self.repo, "add", "-A")
        g(self.repo, "commit", "-qm", "q"); sha = g(self.repo, "rev-parse", "HEAD").strip(); g(self.repo, "checkout", "-q", "main")
        self.fake_prs([{"number": 3, "state": "MERGED", "headRefName": "sq", "headRefOid": sha}])
        r, _ = self.prune(remote=False); self.assertTrue(r[("local", "sq")]["removed"], r)

    def test_prune_remote_branches(self):
        def push(br, commit):
            g(self.repo, "checkout", "-q", "-b", br, "main")
            if commit:
                w(f"{self.repo}/{br}.py"); g(self.repo, "add", "-A"); g(self.repo, "commit", "-qm", br)
            g(self.repo, "push", "-q", "origin", br); g(self.repo, "checkout", "-q", "main")
            g(self.repo, "branch", "-D", br)
            return g(self.repo, "rev-parse", f"origin/{br}").strip()
        push("r-merged", False); push("r-open", False); push("r-unlanded", True); sq = push("r-squash", True)
        g(self.repo, "remote", "set-head", "origin", "main")
        self.fake_prs([{"number": 1, "state": "OPEN", "headRefName": "r-open", "headRefOid": "x"},
                       {"number": 2, "state": "MERGED", "headRefName": "r-squash", "headRefOid": sq}])
        r, _ = self.prune()
        self.assertTrue(r[("remote", "r-merged")]["removed"]); self.assertTrue(r[("remote", "r-squash")]["removed"])
        self.assertEqual(r[("remote", "r-open")]["action"], "keep"); self.assertEqual(r[("remote", "r-unlanded")]["action"], "keep")
        heads = g(self.repo, "ls-remote", "--heads", "origin")
        self.assertNotIn("r-merged", heads); self.assertNotIn("r-squash", heads)
        self.assertIn("r-open", heads); self.assertIn("refs/heads/main", heads)

    def test_prune_remote_needs_owner_and_gh(self):
        g(self.repo, "push", "-q", "origin", "main:refs/heads/other"); g(self.repo, "fetch", "-q")
        self.fake_prs([], slug="someone/proj"); r, _ = self.prune(); self.assertNotIn(("remote", "other"), r)
        self.fake_prs(None); r, _ = self.prune(); self.assertNotIn(("remote", "other"), r)

    def test_prune_skips_pull_only_repo(self):
        d = os.path.join(self.tmp, "homelab-database"); os.symlink(self.repo, d)
        self.assertEqual(wc.prune_branches(d, apply=True, fetch=False, out=io.StringIO()), [])

    # --- stale dispatch leftovers: archive, then remove -----------------------------------

    def arch_setup(self):
        old = (wc.ARCHIVE_ROOTS, wc.ARCHIVE_DIR)
        wc.ARCHIVE_ROOTS = [self.root]; wc.ARCHIVE_DIR = os.path.join(self.tmp, "archive")
        self.addCleanup(lambda: (setattr(wc, "ARCHIVE_ROOTS", old[0]), setattr(wc, "ARCHIVE_DIR", old[1])))

    def members(self, path):
        import tarfile
        with tarfile.open(path) as tf:
            return {m.name: (tf.extractfile(m).read() if m.isfile() else b"") for m in tf.getmembers()}

    def test_stale_dirty_is_archived_then_removed(self):
        self.arch_setup()
        wt = self.mk("old-dirty"); w(f"{wt}/a.py", "edited\n"); w(f"{wt}/NOTES.md", "keep me\n")
        w(f"{wt}/node_modules/p/i.js"); age(wt, 15 * 24)
        r, out = self.run_(wt)
        self.assertEqual(r["action"], "archive", r); self.assertTrue(r["removed"], r); self.assertFalse(os.path.exists(wt))
        self.assertIn("ARCHIVED+REMOVED", out)
        m = self.members(r["archive"])
        self.assertIn(b"edited", m["changes.patch"]); self.assertEqual(m["untracked/NOTES.md"], b"keep me\n")
        self.assertFalse(any("node_modules" in k for k in m)); self.assertIn("meta.json", m)

    def test_stale_unlanded_commit_is_bundled_and_restorable(self):
        self.arch_setup()
        wt = self.mk("old-ahead"); w(f"{wt}/b.py", "work\n"); g(wt, "add", "-A"); g(wt, "commit", "-qm", "work")
        sha = g(wt, "rev-parse", "HEAD").strip(); age(wt, 15 * 24)
        r, _ = self.run_(wt); self.assertTrue(r["removed"], r)
        self.assertNotIn("dispatch/old-ahead", g(self.repo, "branch"))
        bdl = os.path.join(self.tmp, "b.bundle"); open(bdl, "wb").write(self.members(r["archive"])["commits.bundle"])
        g(self.repo, "fetch", "-q", bdl, "HEAD:refs/heads/restored")
        self.assertEqual(g(self.repo, "rev-parse", "restored").strip(), sha)

    def test_stale_clone_with_lost_branch_is_archived(self):
        self.arch_setup()
        d = self.clone_("old-clone"); self.side_commit(d)
        t = time.time() - 15 * 86400
        for p in [d] + [os.path.join(d, n) for n in os.listdir(d) if n != ".git"] + \
                [os.path.join(d, ".git", f) for f in ("HEAD", "index", "logs/HEAD")]:
            os.utime(p, (t, t))
        r, _ = self.run_(d); self.assertEqual(r["action"], "archive", r); self.assertFalse(os.path.exists(d))
        bdl = os.path.join(self.tmp, "c.bundle"); open(bdl, "wb").write(self.members(r["archive"])["commits.bundle"])
        self.assertIn("refs/heads/side", g(self.repo, "bundle", "list-heads", bdl))

    def test_not_stale_or_not_archive_root_is_kept(self):
        self.arch_setup()
        wt = self.mk("young"); w(f"{wt}/a.py", "e\n"); age(wt, 13 * 24)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r); self.assertTrue(os.path.isdir(wt))
        wc.ARCHIVE_ROOTS = [os.path.join(self.tmp, "elsewhere")]
        wt2 = self.mk("proj-wt"); w(f"{wt2}/a.py", "e\n"); age(wt2, 30 * 24)
        r, _ = self.run_(wt2); self.assertEqual(r["action"], "keep", r); self.assertTrue(os.path.isdir(wt2))

    def test_stale_but_live_or_locked_is_kept(self):
        self.arch_setup()
        wt = self.mk("old-live"); w(f"{wt}/a.py", "e\n"); age(wt, 30 * 24); self.live.add(wt, "queue", "job")
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r)
        wt = self.mk("old-lock"); w(f"{wt}/a.py", "e\n"); g(self.repo, "worktree", "lock", wt); age(wt, 30 * 24)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r)

    def test_archive_failure_keeps_worktree(self):
        self.arch_setup()
        wt = self.mk("old-fail"); w(f"{wt}/a.py", "e\n"); age(wt, 30 * 24)
        orig = wc.archive
        wc.archive = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.addCleanup(setattr, wc, "archive", orig)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r); self.assertIn("disk full", r["reason"])
        self.assertTrue(os.path.isdir(wt)); self.assertIn("dispatch/old-fail", g(self.repo, "branch"))

    def test_dry_run_archives_nothing(self):
        self.arch_setup()
        wt = self.mk("old-dry"); w(f"{wt}/a.py", "e\n"); age(wt, 30 * 24)
        r, out = self.run_(wt, apply=False); self.assertIn("WOULD ARCHIVE+REMOVE", out)
        self.assertTrue(os.path.isdir(wt)); self.assertFalse(os.path.exists(wc.ARCHIVE_DIR))

    def test_old_archives_pruned(self):
        self.arch_setup(); os.makedirs(wc.ARCHIVE_DIR)
        a, b = os.path.join(wc.ARCHIVE_DIR, "x.tar.gz"), os.path.join(wc.ARCHIVE_DIR, "y.tar.gz")
        w(a); w(b); t = time.time() - 91 * 86400; os.utime(a, (t, t))
        self.assertEqual(wc.prune_archives(out=io.StringIO()), 1)
        self.assertFalse(os.path.exists(a)); self.assertTrue(os.path.exists(b))


if __name__ == "__main__":
    unittest.main(verbosity=1)
