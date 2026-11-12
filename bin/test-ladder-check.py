#!/usr/bin/env python3
"""ladder-check: argument parsing and the rsync/ssh command lines (no network), plus a live run (--live-only).

Run on claude-sandbox:  python3 test-ladder-check.py            -> prints LADDER_CHECK_OK
Run from the Mac:       python3 test-ladder-check.py --live-only -> rsync/ssh only, prints LADDER_CHECK_LIVE_OK
"""
import argparse
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

BIN = Path(__file__).resolve().parent
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(src):
    ld = importlib.machinery.SourceFileLoader("ladder_check", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("ladder_check", ld))
    ld.exec_module(m)
    return m


def unit(lc):
    a = lc.parse_args(["/x/proj", "--", "pytest", "-q"])
    check("defaults", (a.dir, a.timeout, a.tail, a.base, a.overlay, a.cmd), ("/x/proj", 900, 60, None, False, ["pytest", "-q"]))
    a = lc.parse_args(["/x/proj", "--timeout", "30", "--tail", "5", "--base", "abc123", "--overlay", "--", "make test"])
    check("options", (a.timeout, a.tail, a.base, a.overlay, a.cmd), (30, 5, "abc123", True, ["make test"]))
    for bad in (["/x"], ["/x", "--"]):
        try:
            lc.parse_args(bad)
            got = "no error"
        except SystemExit:
            got = "exit"
        check(f"rejects {bad}", got, "exit")
    check("remote dir", lc.remote_dir("/a/b/proj/"), "work/ladder/proj")
    check("remote base dir", lc.remote_dir("/a/b/proj", "-base"), "work/ladder/proj-base")
    check("rsync cmd", lc.rsync_cmd("/a/b/proj", "work/ladder/proj"),
          ["rsync", "-az", "--delete", "-e", "ssh", "--exclude", ".venv", "--exclude", "node_modules",
           "--exclude", "__pycache__", "/a/b/proj/", "claude-sandbox:work/ladder/proj/"])
    check("git not excluded", ".git" in lc.EXCLUDES, False)
    try:
        lc.remote_dir("/")
        refused = False
    except SystemExit:
        refused = True
    check("root dir refused (no usable basename)", refused, True)
    check("shell string single", lc.shell_string(["make test && echo hi"]), "make test && echo hi")
    check("shell string quoted", lc.shell_string(["python3", "-c", "print('a b')"]), "python3 -c 'print('\"'\"'a b'\"'\"')'")
    rc = lc.run_cmd("work/ladder/proj", ["pytest", "-q"], 30)
    check("run cmd head", rc[:2], ["ssh", "claude-sandbox"])
    check("run cmd body", "cd work/ladder/proj && timeout 30 bash -c" in rc[2] and "pytest -q" in rc[2], True)
    arch, ext = lc.archive_cmds("/a/b/proj", "HEAD~1", "work/ladder/proj-base", clean=True)
    check("archive cmd", arch, ["git", "-C", "/a/b/proj", "archive", "HEAD~1"])
    check("extract clean", ext, ["ssh", "claude-sandbox", "rm -rf work/ladder/proj-base; mkdir -p work/ladder/proj-base && tar -x -C work/ladder/proj-base"])
    _, ext = lc.archive_cmds("/a/b/proj", "HEAD~1", "work/ladder/proj-base", clean=False)
    check("extract overlay", ext[2].startswith("mkdir -p"), True)


def live(lc):
    def git(d, *args):
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)

    base = tempfile.mkdtemp(prefix="ladder-live-")
    d = os.path.join(base, f"lc{os.getpid()}")
    os.mkdir(d)
    git(d, "init", "-q")
    Path(d, "f.txt").write_text("broken\n")
    Path(d, "junk.pyc").write_text("x")
    os.makedirs(os.path.join(d, "node_modules"))
    Path(d, "node_modules", "m.txt").write_text("m")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    Path(d, "f.txt").write_text("fixed\n")
    Path(d, "new_test.sh").write_text("grep -q fixed f.txt\n")  # untracked: only in the fix tree
    git(d, "add", "-A")
    git(d, "commit", "-qm", "fix")

    def run(*args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lc.main(list(args))
        return rc, buf.getvalue()

    rc, out = run(d, "--tail", "3", "--", "grep -q fixed f.txt && echo GREEN")
    check("live rc 0", rc, 0)
    check("live output", "GREEN" in out, True)
    rc, out = run(d, "--", "grep -q broken f.txt; echo ran")
    check("live shell string rc is last cmd", rc, 0)
    rc, out = run(d, "--", "sh", "-c", "echo boom; exit 3")
    check("live rc 3 passes through", rc, 3)
    check("live tail has output", "boom" in out, True)
    rc, out = run(d, "--tail", "2", "--", "sh", "-c", "for i in 1 2 3 4 5; do echo line$i; done")
    check("live tail limit", out.strip().splitlines(), ["line4", "line5"])
    rc, out = run(d, "--", "test ! -e node_modules/m.txt")
    check("live excludes node_modules", rc, 0)
    rc, out = run(d, "--timeout", "2", "--", "sleep 20")
    check("live timeout rc 124", rc, 124)
    rc, out = run(d, "--base", "HEAD~1", "--", "grep -q fixed f.txt")
    check("live base: fix rc 0", rc, 0)
    check("live base markers", "BASE rc=1" in out and "FIX rc=0" in out, True)
    rc, out = run(d, "--base", "HEAD~1", "--", "sh", "new_test.sh")
    check("live clean base lacks new test file (rc 1 anyway)", "BASE rc=2" in out or "BASE rc=1" in out, True)
    rc, out = run(d, "--base", "HEAD~1", "--overlay", "--", "sh", "new_test.sh")
    check("live overlay: red on base, green on fix", "BASE rc=1" in out and "FIX rc=0" in out, True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(BIN / "ladder-check"))
    ap.add_argument("--live-only", action="store_true")
    a = ap.parse_args()
    lc = load(a.src)
    if a.live_only:
        live(lc)
        mark = "LADDER_CHECK_LIVE_OK"
    else:
        unit(lc)
        mark = "LADDER_CHECK_OK"
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        sys.exit(1)
    print(mark)


if __name__ == "__main__":
    main()
