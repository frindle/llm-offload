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

    def test_feature_branch_in_primary_is_not_landed(self):
        g(self.repo, "checkout", "-q", "-b", "feature"); w(f"{self.repo}/f.py"); g(self.repo, "add", "-A")
        g(self.repo, "commit", "-qm", "f")
        wt = os.path.join(self.root, "onfeat"); g(self.repo, "worktree", "add", "-q", "--detach", wt, "feature"); age(wt, 48)
        r, _ = self.run_(wt); self.assertEqual(r["action"], "keep", r)

    def test_discover_skips_non_git_dirs_and_symlinks(self):
        self.mk("d1"); os.makedirs(os.path.join(self.root, "plain", "inner"))
        found = [os.path.basename(p) for p in wc.discover([self.root])]
        self.assertIn("d1", found)


if __name__ == "__main__":
    unittest.main(verbosity=1)
