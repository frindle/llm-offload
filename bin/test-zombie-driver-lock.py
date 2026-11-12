#!/usr/bin/env python3
"""test-zombie-driver-lock.py -- a SIGKILLed-but-unreaped (ZOMBIE) slicer driver does
not keep the per-plan advance lock alive (soak seed 31, 2026-10-06).

Seed 31: a driver --execute was SIGKILLed while its parent never wait()ed it. signal 0
still reached the zombie pid, so lock_is_stale() said "live owner": every later
--execute / completion hook abstained ("another --execute is already driving this
plan (pid N)") and the chain sat idle ~20 min until the run timed out.
ollama-dispatch-auto's _slice_lock_owner_alive() had the same blind spot.

Checks (a REAL zombie: a child this test SIGKILLs and deliberately does not reap):
  1. slicer _pid_alive(zombie) is False; lock_is_stale({pid: zombie}) is True
  2. acquire_advance_lock reclaims a lock naming the zombie
  3. a live pid is still alive / its lock is NOT stale / not reclaimed (guard intact)
  4. auto _slice_lock_owner_alive -> False for the zombie, True for a live owner

  --revert-check SLICER_BAK AUTO_BAK : the pre-fix copies must go RED.
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path.home() / "bin"


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m)
    return m


def make_zombie():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    os.kill(p.pid, signal.SIGKILL)
    for _ in range(50):
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(p.pid)],
                            capture_output=True, text=True).stdout.strip()
        if st.startswith("Z"):
            return p
        time.sleep(0.1)
    raise SystemExit("could not produce a zombie")


def checks(slicer_path, auto_path):
    home = tempfile.mkdtemp(prefix="zl-home-")
    os.environ["HOME"] = home
    sl = load(slicer_path, "_zl_slice")
    sl.STATE_ROOT = os.path.join(home, "slice-runs")
    au = load(auto_path, "_zl_auto")
    z = make_zombie()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    now = datetime.now(timezone.utc).isoformat()
    res = []
    try:
        res.append(("slicer: zombie pid is NOT alive", sl._pid_alive(z.pid) is False, ""))
        res.append(("slicer: lock naming a zombie is stale",
                    sl.lock_is_stale({"pid": z.pid, "started_at": now}) is True, ""))
        os.makedirs(sl.STATE_ROOT, exist_ok=True)
        lp = sl.lock_path("zp")
        Path(lp).write_text(json.dumps({"pid": z.pid, "started_at": now}))
        got = sl.acquire_advance_lock("zp")
        res.append(("slicer: acquire reclaims the zombie's lock", got is not None, str(got)))
        res.append(("slicer: live pid still alive", sl._pid_alive(live.pid) is True, ""))
        Path(lp).write_text(json.dumps({"pid": live.pid, "started_at": now}))
        res.append(("slicer: live owner's lock NOT reclaimed", sl.acquire_advance_lock("zp") is None, ""))
        al = Path(home, ".ollama-dispatch", "slice-runs")
        al.mkdir(parents=True, exist_ok=True)
        (al / "zq.advance.lock").write_text(json.dumps({"pid": z.pid}))
        res.append(("auto: zombie slicer owner is not alive",
                    au._slice_lock_owner_alive("zq") is False, ""))
        (al / "zq.advance.lock").write_text(json.dumps({"pid": live.pid}))
        res.append(("auto: live slicer owner is alive", au._slice_lock_owner_alive("zq") is True, ""))
    finally:
        live.kill(); live.wait(); z.wait()
    return res


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--revert-check":
        bad = []
        for sp, ap in ((sys.argv[2], BIN / "ollama-dispatch-auto"),
                       (BIN / "ollama-dispatch-slice", sys.argv[3])):
            r = subprocess.run([sys.executable, __file__, "--paths", str(sp), str(ap)],
                               capture_output=True, text=True)
            print(r.stdout[-800:])
            bad.append(r.returncode != 0)
        if all(bad):
            print("REVERT-CHECK OK: each pre-fix copy goes RED")
            return 0
        print("REVERT-CHECK FAILED: a pre-fix copy passes -- inert test")
        return 1
    sp, ap = BIN / "ollama-dispatch-slice", BIN / "ollama-dispatch-auto"
    if len(sys.argv) == 4 and sys.argv[1] == "--paths":
        sp, ap = Path(sys.argv[2]), Path(sys.argv[3])
    n_bad = 0
    for n, ok, why in checks(sp, ap):
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        n_bad += not ok
    print("ALL PASS" if not n_bad else f"{n_bad} FAILED")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
