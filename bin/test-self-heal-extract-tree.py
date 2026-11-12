#!/usr/bin/env python3
"""Guards the fix of 2026-10-02 (test-auto-skip.py: 8 "could not extract the tip
tree: " failures). dispatch-self-heal._extract_tree piped `git archive | tar -x`;
macOS bsdtar stops reading at the end-of-archive marker, git then died of SIGPIPE
(rc -13, empty stderr) and a fully extracted tree was reported as a failure, so
every auto-skip proof was refused. Now: git archive --output <file>, tar -xf.

Deterministic reproduction: a fake `tar` on PATH that never reads stdin (a reader
that stops early, taken to the limit) plus a >pipe-buffer archive. The pipe code
gets SIGPIPE every time; the file code does not care how tar reads.

Run: python3 test-self-heal-extract-tree.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HEAL = Path(os.environ.get("HEAL_SRC") or Path(__file__).resolve().parent / "dispatch-self-heal.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = importlib.machinery.SourceFileLoader("heal_x", str(HEAL))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("heal_x", ld))
    ld.exec_module(m)
    return m


def main():
    m = load()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "r"
        repo.mkdir()
        (repo / "small.txt").write_text("hi\n")
        (repo / "big.bin").write_bytes(os.urandom(256 * 1024))
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
        g("init", "-q")
        g("add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null",
          "commit", "-qm", "x")
        d1 = td / "d1"
        d1.mkdir()
        ok, why = m._extract_tree(repo, "HEAD", d1, timeout=60)
        check("real tar: extraction reports success", (ok, why), (True, ""))
        check("real tar: tree is extracted", sorted(p.name for p in d1.iterdir()), ["big.bin", "small.txt"])
        d2 = td / "d2"
        d2.mkdir()
        ok, why = m._extract_tree(repo, "nosuchrev", d2, timeout=60)
        check("bad rev: refused", ok, False)
        check("bad rev: reason is non-empty and names git", "git archive" in why and "nosuchrev" in why, True)
        # early-exit reader: fake tar that never reads stdin
        fb = td / "fakebin"
        fb.mkdir()
        (fb / "tar").write_text("#!/bin/sh\nexit 0\n")
        (fb / "tar").chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fb}:{old}"
        try:
            d3 = td / "d3"
            d3.mkdir()
            ok, why = m._extract_tree(repo, "HEAD", d3, timeout=60)
        finally:
            os.environ["PATH"] = old
        check("tar that stops reading early does not turn into a git SIGPIPE failure", (ok, why), (True, ""))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    bak = HEAL.parent / "dispatch-self-heal.py.bak-sigpipe"
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "HEAL_SRC": str(bak)},
                       capture_output=True, text=True, timeout=300)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": pre-fix pipe version (.bak-sigpipe) -> suite "
          + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
