#!/usr/bin/env python3
"""Tests for offload-repo-sync's queue-image handling (run on claude-sandbox; the test gate runs it).

Each case builds a fake HOME (a ~/bin git repo, a dashboard git repo, a public repo with MANIFEST.txt, private scrub
rules and patterns) in a temp dir and runs the sync with --dry-run or --no-push. Nothing real is read or pushed.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SYNC = Path(__file__).resolve().parent / "offload-repo-sync"
PASS = FAIL = 0


def check(got, want, what, out=""):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("ok   " + what)
    else:
        FAIL += 1
        print("FAIL %s (got %r want %r)\n%s" % (what, got, want, "\n".join(out.splitlines()[-4:])))


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null",
                    "-c", "init.defaultBranch=main", *a], cwd=cwd, check=True, capture_output=True)


def commit(cwd):
    git(cwd, "add", "-A")
    git(cwd, "commit", "-qm", "change")


class Env:
    def __init__(self, image=True):
        self.td = tempfile.TemporaryDirectory()
        t = Path(self.td.name)
        self.home, self.repo, self.priv = t / "home", t / "repo", t / "priv"
        self.bin, self.dash = self.home / "bin", self.home / "dash"
        for d in (self.bin / "queue-image", self.dash / "src", self.repo, self.priv):
            d.mkdir(parents=True)
        (self.priv / "scrub-rules.txt").write_text("zzzprivate\tPLACEHOLDER\n")
        (self.priv / "patterns.txt").write_text("zzzprivate\n")
        (self.dash / "src/api.py").write_text("print('api')\n")
        git(self.dash, "init", "-q")
        commit(self.dash)
        (self.bin / "a.py").write_text("print('a')\n")
        (self.bin / "excluded.py").write_text("print('ex')\n")
        (self.bin / "plain.py").write_text("print('plain')\n")
        (self.bin / "queue-image/entrypoint.sh").write_text("#!/bin/sh\necho hi\n")
        (self.bin / "api.py").symlink_to("../dash/src/api.py")
        if image:
            (self.bin / "queue-image/MANIFEST").write_text("# image files\na.py\nexcluded.py\napi.py   # dashboard\n")
        git(self.bin, "init", "-q")
        commit(self.bin)
        (self.repo / "MANIFEST.txt").write_text("[bin-exclude]\nexcluded.py\n\n[git-sources]\ndashboard|~/dash|HEAD\n\n"
                                                "[runtime]\n[hooks]\n[launchd]\n[config]\n[state-docs]\n")
        git(self.repo, "init", "-q")
        commit(self.repo)

    def add(self, rel, text, root=None):
        p = (root or self.bin) / rel
        with open(p, "a") as fh:
            fh.write(text)
        commit(root or self.bin)

    def sync(self, *args):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
        env.update(HOME=str(self.home), CLAUDE_OFFLOAD_REPO=str(self.repo), OFFLOAD_PRIVATE_DIR=str(self.priv),
                   OFFLOAD_PRIVATE_PATTERNS=str(self.priv / "patterns.txt"))
        r = subprocess.run([sys.executable, "-I", str(SYNC), *args], env=env, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def close(self):
        self.td.cleanup()


def main():
    e = Env()
    rc, out = e.sync("--no-push")
    check(rc, 0, "image: clean sync passes", out)
    check((e.repo / "bin/excluded.py").is_file(), True, "image: a bin-excluded MANIFEST file is published")
    check((e.repo / "bin/a.py").is_file() and (e.repo / "dashboard/src/api.py").is_file(), True,
          "image: the bin file and the dashboard file behind a listed symlink are published")
    check((e.repo / "bin/api.py").exists(), False, "image: the bin symlink itself is not copied")
    e.close()

    cases = [
        ("bin", "excluded.py", "HOST = '203.0.113.148'\n", "image: private IP in a bin-excluded image file stops the sync"),
        ("bin", "a.py", "x = 'zzzprivate'\n", "image: a private pattern in an image file stops the sync"),
        ("dash", "src/api.py", "HOST = '203.0.113.148'\n", "image: private IP in the dashboard file behind a listed symlink"),
        ("bin", "queue-image/entrypoint.sh", "echo 198.51.100.89\n", "image: private IP in queue-image/ stops the sync"),
    ]
    for root, rel, text, what in cases:
        e = Env()
        e.add(rel, text, e.bin if root == "bin" else e.dash)
        rc, out = e.sync("--dry-run")
        check((rc, "runtime check:" in out), (2, True), what, out)
        e.close()

    e = Env()
    e.add("plain.py", "HOST = '203.0.113.148'\n")
    rc, out = e.sync("--dry-run")
    check(rc, 0, "image: private IP in a file the image does not ship is scrubbed, not a stop", out)
    e.close()

    e = Env()
    (e.bin / "ghost.py").symlink_to("/nonexistent/ghost.py")
    e.add("queue-image/MANIFEST", "ghost.py\n")
    rc, out = e.sync("--dry-run")
    check((rc, "queue image:" in out), (2, True), "image: a listed symlink not backed by dashboard/src stops the sync", out)
    e.close()

    e = Env()
    e.add("queue-image/MANIFEST", "/etc/passwd\n")
    rc, out = e.sync("--dry-run")
    check(rc, 2, "image: an absolute path in the image MANIFEST stops the sync", out)
    e.close()

    e = Env(image=False)
    e.add("excluded.py", "HOST = '203.0.113.148'\n")
    rc, out = e.sync("--no-push")
    check(rc, 0, "no image: sync as before", out)
    check((e.repo / "bin/excluded.py").exists(), False, "no image: bin-exclude still applies")
    e.close()

    print("passed %d, failed %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
