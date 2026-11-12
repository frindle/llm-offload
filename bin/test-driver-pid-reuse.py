#!/usr/bin/env python3
"""Driver liveness survives pid reuse (dispatch-self-heal.py _driver_alive /
auto_driver_live / stale_drivers, ollama-queue.py _driver_pid_alive /
chain_run_progress, 2026-10-06).

Live: the chain record of aw-sched-routes-s15 (driver gone 2026-09-25, no `ended`
written) pointed at pid 1174, reused by macOS's diagnostics_agent; a bare kill(0)
probe read it as a live driver forever (resume refused, stale-driver report wrong).
Hermetic: spawns two throwaway `sleep` processes (one whose argv names
ollama-dispatch-auto), temp runs dir. --revert-check mutates the sources -> RED."""
import importlib.util, json, os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SH = Path(os.environ.get("SELF_HEAL_SRC") or HERE / "dispatch-self-heal.py")
QU = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    s = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def main():
    T = Path(tempfile.mkdtemp(prefix="pidreuse-"))
    os.environ["HOME"] = str(T)            # the queue module reads paths off HOME
    other = subprocess.Popen(["sleep", "60"])
    drv = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                            "/x/bin/ollama-dispatch-auto", "--label", "x"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    time.sleep(0.3)
    try:
        sh = load(SH, "dsh_pid")
        check("self-heal: a reused pid (not a driver) is NOT a live driver",
              sh._driver_alive(other.pid), False)
        check("self-heal: a real driver pid is live", sh._driver_alive(drv.pid), True)
        check("self-heal: a dead pid is not live", sh._driver_alive(dead.pid), False)
        rd = T / "runs"
        rd.mkdir()
        (rd / "b.json").write_text(json.dumps({"runs": {
            "s15": {"label": "s15", "pid": other.pid, "phase": "waiting",
                    "started_at": "2026-09-25T17:14:15Z"},
            "s16": {"label": "s16", "pid": drv.pid, "phase": "waiting",
                    "started_at": "2026-09-25T17:14:15Z"}}}))
        check("auto_driver_live: a record pointing at a reused pid -> not live",
              sh.auto_driver_live("s15", "b", runs_dir=rd), False)
        check("auto_driver_live: a real driver -> live", sh.auto_driver_live("s16", "b", runs_dir=rd),
              True)
        auto = T / "ollama-dispatch-auto"
        auto.write_text("# x\n")
        check("stale_drivers reports only the real (old) driver",
              [l for l, _p, _w in sh.stale_drivers(runs_dir=rd, auto=auto)], ["s16"])
        q = load(QU, "oq_pid")
        check("queue: a reused pid is NOT a live driver", q._driver_pid_alive(other.pid), False)
        check("queue: a real driver pid is live", q._driver_pid_alive(drv.pid), True)
        cr = T / "chain"
        cr.mkdir()
        rec = {"key": "b", "label": "b", "pid": other.pid, "phase": "advancing",
               "phase_since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        (cr / "b.json").write_text(json.dumps(rec))
        check("queue chain_run_progress: a reused pid is not a live driver",
              q.chain_run_progress("b", runs_dir=cr)["driver_live"], False)
        rec["pid"] = drv.pid
        (cr / "b.json").write_text(json.dumps(rec))
        check("queue chain_run_progress: a real driver is live",
              q.chain_run_progress("b", runs_dir=cr)["driver_live"], True)
    finally:
        other.kill()
        drv.kill()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    (SH, "SELF_HEAL_SRC", "self-heal liveness is pid-only",
     '        return "ollama-dispatch-auto" in (r.stdout or "")', "        return True"),
    (SH, "SELF_HEAL_SRC", "auto_driver_live uses the bare probe",
     "    shared chain record (runs[label]) and the legacy single-run shape.\"\"\"\n    alive = alive or _driver_alive",
     "    shared chain record (runs[label]) and the legacy single-run shape.\"\"\"\n    alive = alive or _pid_alive"),
    (QU, "QUEUE_SRC", "queue liveness is pid-only",
     '    return (not cmd) or ("ollama-dispatch" in cmd) or (int(pid) == os.getpid())',
     "    return True"),
]


def revert_check():
    bad = 0
    for path, env, name, old, new in MUTATIONS:
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
