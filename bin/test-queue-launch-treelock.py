#!/usr/bin/env python3
"""Queue launch honours the worktree tree lock (ollama-queue.py tree_lock_try, 2026-10-06).

Found by the canary soak (seed 1): the auto self-check (and the gate's in-place
relevance step) hold <git-dir>/dispatch-tree.lock while they temporarily REWRITE the
target and restore it. The queue launch measured the launch baseline without that
lock, read the transient edit as dirt -> "launch baseline was dirty" -> UNTRUSTED ->
needs_opus -> bundle parked. Now the launch takes the lock (non-blocking) around
seal + measure + spawn and leaves the job pending while someone else holds it.

Hermetic: temp git repo, a child process holds the lock. QS_SRC points the suite at
another queue source (--revert-check mutates it and requires RED)."""
import importlib.util, os, re, subprocess, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("QS_SRC") or HERE / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="tl-home-")
    ld = SourceFileLoader("oq_tl", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_tl", ld))
    ld.exec_module(m)
    return m


HOLD = ("import fcntl,subprocess,sys,time\n"
        "g=subprocess.run(['git','rev-parse','--absolute-git-dir'],capture_output=True,"
        "text=True).stdout.strip()\n"
        "fh=open(g+'/dispatch-tree.lock','a+'); fcntl.flock(fh.fileno(),fcntl.LOCK_EX)\n"
        "print('held',flush=True); time.sleep(30)\n")


def main():
    m = load()
    T = Path(tempfile.mkdtemp(prefix="tl-"))
    r = T / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r)
    nongit = T / "plain"
    nongit.mkdir()

    fh, busy = m.tree_lock_try(r)
    check("free tree -> lock taken, not busy", (fh is not None, busy), (True, False))
    m.tree_lock_release(fh)
    check("not a git dir -> fail-open (None, False)", m.tree_lock_try(nongit), (None, False))

    holder = subprocess.Popen([sys.executable, "-c", HOLD], cwd=r, stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        fh, busy = m.tree_lock_try(r)
        check("lock held by a self-check/gate -> busy", (fh, busy), (None, True))
    finally:
        holder.kill()
        holder.wait()
    time.sleep(0.2)
    fh, busy = m.tree_lock_try(r)
    check("holder gone -> free again", busy, False)
    fh2, busy2 = m.tree_lock_try(r)
    check("while WE hold it, a second taker sees busy (exclusive)", busy2, True)
    m.tree_lock_release(fh)
    fh3, busy3 = m.tree_lock_try(r)
    check("release frees it", busy3, False)
    m.tree_lock_release(fh3)
    check("lock file lives in the git dir, never the work tree",
          (r / ".git" / "dispatch-tree.lock").exists() and not (r / "dispatch-tree.lock").exists(), True)

    # the launch site: lock BEFORE seal+measure, busy -> pending (continue), release after spawn
    src = SRC.read_text()
    i_try = src.find('_tl, _tbusy = tree_lock_try(job["cwd"])')
    i_seal = src.find("_sealed = seal_prev_round_baseline(job)")
    i_meas = src.find('apply_launch_baseline(job, measure_baseline(job["cwd"]))')
    i_pop = src.find("proc = subprocess.Popen(cmd, cwd=job[\"cwd\"]")
    check("launch takes the tree lock before sealing/measuring the baseline",
          0 < i_try < i_seal < i_meas < i_pop, True)
    blk = src[i_try:i_seal]
    check("a busy lock leaves the job pending (continue), not failed",
          bool(re.search(r"if _tbusy:.*?\n\s+continue\n", blk, re.S)) and "failed" not in blk, True)
    tail = src[i_pop:i_pop + 400]
    check("lock released after spawn and on launch failure",
          tail.count("tree_lock_release(_tl)") >= 2, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("launch ignores the lock", "                if _tbusy:\n", "                if False:\n"),
    ("lock probe always free", "        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n        return fh, False",
     "        return fh, False"),
    ("lock never released after spawn",
     "                                            stderr=subprocess.STDOUT, env=_env)\n                    tree_lock_release(_tl)\n",
     "                                            stderr=subprocess.STDOUT, env=_env)\n"),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "QS_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    bak = HERE / "ollama-queue.py.bak-20261006T091122Z-seam"
    if bak.exists():
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "QS_SRC": str(bak)},
                           capture_output=True, text=True, timeout=300)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": pre-fix backup -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
