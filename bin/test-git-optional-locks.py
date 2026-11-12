#!/usr/bin/env python3
"""Pipeline observers must not take .git/index.lock (2026-10-02, Rivian s5:
"index.lock: File exists" -> auto-harness-check "git is unusable"). Loads the
REAL worker and calls its worktree snapshot + dirty check on a repo whose tracked
file has a stale stat; the index must not be rewritten. Also checks every
pipeline tool sets the env. --revert-check removes the line (per file) -> RED."""
import importlib.util, os, subprocess, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = ["ollama-worker.py", "ollama-dispatch-slice", "ollama-dispatch-auto",
         "gate-on-complete.py", "ollama-queue.py"]
LINE = 'os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")'
FAILS = []


def check(name, ok):
    print(("ok  " if ok else "FAIL") + ": " + name)
    if not ok:
        FAILS.append(name)


def main():
    src_dir = Path(os.environ.get("TOOLS_DIR") or HERE)
    os.environ.pop("GIT_OPTIONAL_LOCKS", None)
    for t in TOOLS:
        check(f"{t} sets GIT_OPTIONAL_LOCKS=0", LINE in (src_dir / t).read_text())
    ld = SourceFileLoader("wk_gol", str(src_dir / "ollama-worker.py"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("wk_gol", ld))
    sys.argv = ["ollama-worker.py"]
    ld.exec_module(m)
    d = Path(tempfile.mkdtemp(prefix="gol-"))
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    g("init", "-q"); (d / "f").write_text("a\n"); g("add", "f")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i")
    time.sleep(1.1); os.utime(d / "f")
    before = (d / ".git/index").stat().st_mtime_ns
    m._git_worktree_snapshot(d)
    m._git_path_is_dirty(d, "f")
    check("worker snapshot + dirty check leave .git/index untouched",
          (d / ".git/index").stat().st_mtime_ns == before)
    (d / ".git/index.lock").write_text("")
    snap = m._git_worktree_snapshot(d)
    check("snapshot still reads status while index.lock is held",
          snap is not None and "---STATUS---" in snap)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    import shutil
    bad = 0
    for t in TOOLS:
        td = Path(tempfile.mkdtemp(prefix="golrc-"))
        for u in TOOLS:
            shutil.copy2(HERE / u, td / u)
        s = (td / t).read_text()
        assert s.count(LINE) == 1, t
        (td / t).write_text(s.replace(LINE, "pass"))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "TOOLS_DIR": str(td)},
                           capture_output=True, text=True, timeout=300)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": drop the env in {t} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    td = Path(tempfile.mkdtemp(prefix="golrc-"))
    for u in TOOLS:
        shutil.copy2(HERE / u, td / u)
    w = (td / "ollama-worker.py").read_text()
    PL = '["git", "diff-index", "-p", "HEAD", "--"]'
    assert w.count(PL) == 1
    (td / "ollama-worker.py").write_text(w.replace(PL, '["git", "diff", "HEAD"]'))
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "TOOLS_DIR": str(td)},
                       capture_output=True, text=True, timeout=300)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + f": worker snapshot back to porcelain `git diff HEAD` -> suite {'RED' if red else 'green'}")
    bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
