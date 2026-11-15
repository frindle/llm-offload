#!/usr/bin/env python3
"""Tests repo-toolchain-check against made-up local fixture repos (no network, fake token)."""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
bad = 0


def sh(*a, cwd=None):
    subprocess.run(a, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mkrepo(root, name, files):
    d = os.path.join(root, name)
    os.makedirs(d)
    for p, c in files.items():
        fp = os.path.join(d, p)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        open(fp, "w").write(c)
    sh("git", "init", "-q", cwd=d)
    sh("git", "add", ".", cwd=d)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x", cwd=d)
    return d


def check(label, ok):
    global bad
    print(("ok   " if ok else "FAIL ") + label)
    bad += 0 if ok else 1


def run(repos):
    env = dict(os.environ, QUEUE_TOOLCHAIN_REPOS=",".join(repos), GITHUB_TOKEN="fake-token-123")
    p = subprocess.run([sys.executable, os.path.join(HERE, "repo-toolchain-check")], env=env,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


with tempfile.TemporaryDirectory() as t:
    good = mkrepo(t, "goodpy", {"mod.py": "def f():\n    return 1\n", "test_mod.py": "import mod\ndef test_f():\n    assert mod.f() == 1\n"})
    badt = mkrepo(t, "badpy", {"mod.py": "def f():\n    return 1\n", "test_mod.py": "import mod\ndef test_f():\n    assert mod.f() == 2\n"})
    empty = mkrepo(t, "empty", {"README.txt": "hi\n"})
    import shutil
    if shutil.which("npm"):
        lock = '{"name":"x","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{"":{"name":"x","version":"1.0.0"}}}'
        nok = mkrepo(t, "goodnode", {"package.json": '{"name":"x","version":"1.0.0","scripts":{"test":"node -e \\"process.exit(0)\\""}}', "package-lock.json": lock})
        nbad = mkrepo(t, "badnode", {"package.json": '{"name":"x","version":"1.0.0","scripts":{"test":"node -e \\"process.exit(1)\\""}}', "package-lock.json": lock})
        rc, out = run([nok])
        check("good node repo passes", rc == 0 and "PASS goodnode" in out)
        rc, out = run([nbad])
        check("planted failing npm test is caught", rc == 1 and "step failed: npm test" in out)
    rc, out = run([good])
    check("good python repo passes", rc == 0 and "PASS goodpy" in out)
    check("token never printed", "fake-token-123" not in out)
    rc, out = run([badt])
    check("planted failing test is caught", rc == 1 and "FAIL badpy - step failed: pytest" in out)
    rc, out = run([empty])
    check("repo with no toolchain is SKIP, not PASS", rc == 0 and "SKIP empty" in out)
    rc, out = run([good, badt])
    check("mixed list exits non-zero, one line each", rc == 1 and "PASS goodpy" in out and "FAIL badpy" in out)
    rc, out = run([os.path.join(t, "missing")])
    check("clone failure is FAIL", rc == 1 and "clone failed" in out)
    env = {k: v for k, v in os.environ.items() if k != "QUEUE_TOOLCHAIN_REPOS"}
    env["QUEUE_CONFIG"] = os.path.join(t, "none.toml")
    p = subprocess.run([sys.executable, os.path.join(HERE, "repo-toolchain-check")], env=env, capture_output=True, text=True)
    check("no repos configured fails", p.returncode == 1 and "no repos configured" in p.stdout)
print("%d failed" % bad)
sys.exit(1 if bad else 0)
