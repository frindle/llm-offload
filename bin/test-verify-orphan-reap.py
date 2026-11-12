#!/usr/bin/env python3
"""Real-process test for verify-orphan-reap.py (the launchd janitor that kills
verify trees orphaned to launchd -- Rivian s3 2026-10-02, five at 100% CPU).

Spawns REAL orphans (ppid 1) and checks the safety rules:
  * an orphaned `bash verify.sh` tree (incl. its child) under the worktree root is
    reaped with --apply, and NOT touched by a dry run;
  * an orphan OUTSIDE the worktree root is never touched;
  * an orphan whose command is a pipeline driver (ollama-worker...) is never touched;
  * an orphan younger than --min-age is never touched;
  * an apply that killed something alerts ONCE (deduped), a dry run never alerts.
Run: python3 test-verify-orphan-reap.py [--revert-check]
"""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("REAP_SRC") or HERE / "verify-orphan-reap.py")
FAILS = []
SPAWNED = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(st.strip()) and not st.strip().startswith("Z")


def orphan(cwd, script_name, body="while :; do sleep 1; done"):
    """Start `bash <script_name>` in cwd, orphaned (its launching shell exits)."""
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / script_name).write_text(f"sleep 600 &\n{body}\n")
    pf = cwd / "pid"
    subprocess.run(["sh", "-c", f"cd '{cwd}' && (bash {script_name} >/dev/null 2>&1 & echo $! > pid) &"],
                   check=True)
    for _ in range(50):
        if pf.exists() and pf.read_text().strip():
            break
        time.sleep(0.1)
    pid = int(pf.read_text())
    for _ in range(50):
        pp = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if pp == "1":
            break
        time.sleep(0.1)
    SPAWNED.append(pid)
    return pid


def kids(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def main():
    loader = SourceFileLoader("reap_t", str(SRC))
    spec = importlib.util.spec_from_loader("reap_t", loader)
    rp = importlib.util.module_from_spec(spec)
    loader.exec_module(rp)
    root = Path(tempfile.mkdtemp(prefix="reap-")).resolve()
    wts = root / "worktrees"
    # --- PURE rules on synthetic rows (no process is touched) ---------------------
    W = str(wts)
    rows = [{"pid": 10, "ppid": 1, "age": 999, "cmd": "npm exec tsx ./verify_impl.mts"},
            {"pid": 11, "ppid": 10, "age": 999, "cmd": "node tsx ./verify_impl.mts"},
            {"pid": 20, "ppid": 1, "age": 999, "cmd": "npm exec tsx ./verify_impl.mts"},
            {"pid": 30, "ppid": 5, "age": 999, "cmd": "bash verify.sh"},
            {"pid": 40, "ppid": 1, "age": 999, "cmd": "python3 /Users/user/bin/ollama-worker.py --verify 'bash verify.sh'"}]
    cwds = {10: W + "/wt-a", 20: "/Users/user/elsewhere", 30: W + "/wt-b", 40: W + "/wt-c"}
    got = rp.find_orphans(rows, cwds, wts, min_age=0, self_pid=1)
    check("pure: only the in-worktree orphan is a root",
          [r["pid"] for r, _ in got], [10])
    check("pure: its descendants are killed first", got[0][1] if got else None, [11, 10])
    if os.environ.get("REAP_PURE_ONLY"):
        print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
        return 0 if not FAILS else 1
    target = orphan(wts / "wt-slice-x", "verify.sh")
    time.sleep(0.5)
    tkids = kids(target)
    outside = orphan(root / "elsewhere", "verify.sh")
    driver = orphan(wts / "wt-slice-y", "ollama-worker-verify.sh")
    try:
        check("fixture: the target is a real orphan (ppid 1)",
              subprocess.run(["ps", "-o", "ppid=", "-p", str(target)], capture_output=True,
                             text=True).stdout.strip(), "1")
        check("fixture: the target has a child", len(tkids) >= 1, True)
        rep = rp.reap(apply=False, worktrees=wts, min_age=0, log=lambda m: None)
        check("dry run finds exactly the in-worktree verify orphan",
              [e["root"] for e in rep], [target])
        check("dry run kills nothing", alive(target), True)
        rep = rp.reap(apply=True, worktrees=wts, min_age=3600, log=lambda m: None)
        check("a young orphan (< min-age) is never touched", (rep, alive(target)), ([], True))
        sent = []
        rep = rp.reap(apply=True, worktrees=wts, min_age=0, log=lambda m: None)
        time.sleep(0.5)
        check("--apply reaps the orphan", alive(target), False)
        check("...and its child", any(alive(k) for k in tkids), False)
        check("an orphan OUTSIDE the worktree root is never touched", alive(outside), True)
        check("a pipeline-driver orphan is never touched", alive(driver), True)
        logp = root / "log.jsonl"
        ch = rp.record_and_alert(rep, True, log_path=logp,
                                 notifier=lambda t, m, **k: sent.append((t, k)) or "stub")
        check("an apply that killed something alerts once", (ch, len(sent)), ("stub", 1))
        check("...with a dedupe key naming the roots",
              sent[0][1].get("dedupe_key"), f"verify-orphan-reap:{target}")
        check("...and is logged", logp.exists() and str(target) in logp.read_text(), True)
        sent.clear()
        rp.record_and_alert(rep, False, log_path=logp,
                            notifier=lambda t, m, **k: sent.append(t) or "stub")
        check("a dry run never alerts", sent, [])
        check("nothing found -> no log, no alert",
              rp.record_and_alert([], True, log_path=root / "none.jsonl",
                                  notifier=lambda *a, **k: sent.append(1)), None)
    finally:
        for p in SPAWNED:
            for k in kids(p):
                try:
                    os.kill(k, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


# (name, old, new, pure_only): pure_only mutants could kill REAL orphans outside the
# fixture if their suite ran --apply, so they are judged on the pure checks only.
MUTATIONS = [
    ("no ppid==1 rule", '        if r["ppid"] != 1 or r["pid"] == self_pid', '        if r["pid"] == self_pid', True),
    ("no worktree-cwd rule", '        if not (c + "/").startswith(wt):\n            continue\n', "", True),
    ("no driver exclusion", "        if DRIVER_RE.search(r[\"cmd\"]) or not VERIFY_RE", "        if not VERIFY_RE", False),
    ("no descendants", "        out.append((r, list(reversed(tree)) + [r[\"pid\"]]))",
     "        out.append((r, [r[\"pid\"]]))", False),
    ("alert on dry run too", "    if not applied:\n        return None\n", "", False),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new, pure_only in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = Path(tempfile.mkdtemp(prefix="reapmut-"))
        dst = d / "verify-orphan-reap.py"
        dst.write_text(src.replace(old, new))
        (d / "notify-owner.py").write_text((HERE / "notify-owner.py").read_text())
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "REAP_SRC": str(dst),
                                                     **({"REAP_PURE_ONLY": "1"} if pure_only else {})},
                           capture_output=True, text=True, timeout=180)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
