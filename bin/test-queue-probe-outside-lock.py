#!/usr/bin/env python3
"""test-queue-probe-outside-lock.py -- the Darkbloom /v1/models readiness probe (up to 3x10s + 2x3s =
36s when Darkbloom is down) must never run while the queue state flock is held.

WHY (adversarial review 2026-10-13, item 5): cmd_run's launch loop calls darkbloom_launch_wait ->
darkbloom_model_ready -> _darkbloom_healthy INSIDE `with _Locked()`, so a slow/down Darkbloom froze every
enqueue / status / dashboard poll for the probe's duration. The verdict is now taken just BEFORE the lock
(prime_darkbloom_ready) and the locked path only reads it (fail-closed: no fresh verdict = INFRA WAIT).

Usage: python3 ~/bin/test-queue-probe-outside-lock.py [--queue PATH]     Prints PROBE_OUTSIDE_LOCK_OK."""
import argparse, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
H = Path(tempfile.mkdtemp(prefix="probelock-"))
os.environ["HOME"] = str(H)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
(H / ".darkbloom").mkdir()
(H / "bin").mkdir()          # STATE_PATH / LOCK_PATH live under $HOME/bin
(H / ".darkbloom" / "local.json").write_text('{"api_key": "k", "base_url": "http://127.0.0.1:1"}')
sys.path.insert(0, str(Path(A.queue).resolve().parent))
q = SourceFileLoader("q_probelock", A.queue).load_module()
fails = 0


def chk(name, ok, detail=""):
    global fails
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": {detail}"))
    fails += 0 if ok else 1


calls = []
q._darkbloom_healthy = lambda model=None, **kw: (calls.append(model) or True)
q.bloom_idle_hold = lambda *a, **k: None
q.darkbloom_warm_hold = lambda *a, **k: None
M = "qwen3.6-35b-a3b-vl-mtp-mxfp8"

# 1. outside the lock the probe runs as before (up is re-probed every call)
q._DB_READY.clear()
chk("outside the lock: probes and caches the verdict", q.darkbloom_model_ready(M, 1000.0) is True and calls == [M], calls)

# 2. INSIDE the lock: NO probe, ever
calls.clear()
q._DB_READY.clear()
with q._Locked():
    chk("under the lock, no cached verdict: NOT ready and NO probe (fail-closed)",
        q.darkbloom_model_ready(M, 1000.0) is False and calls == [], calls)
    note = q.darkbloom_launch_wait(M, 1000.0)
    chk("...the launch path turns that into a named INFRA WAIT, not a launch / not a probe",
        note is not None and "infra" in str(note).lower() and calls == [], (note, calls))
q._DB_READY[M] = (True, 1000.0)
with q._Locked():
    chk("under the lock, a fresh cached UP verdict is honoured without probing",
        q.darkbloom_model_ready(M, 1000.0 + 30) is True and calls == [], calls)
    chk("under the lock, a STALE cached UP verdict (> DB_READY_LOCKED_TTL) is not trusted and not re-probed",
        q.darkbloom_model_ready(M, 1000.0 + q.DB_READY_LOCKED_TTL + 5) is False and calls == [], calls)
q._DB_READY[M] = (False, 1000.0)
with q._Locked():
    chk("under the lock, a cached DOWN verdict stays down", q.darkbloom_model_ready(M, 1000.0 + 31) is False and calls == [], calls)

# 3. prime_darkbloom_ready takes the verdict OUTSIDE the lock for the models pending jobs need
calls.clear()
q._DB_READY.clear()
jobs = [{"id": "a", "status": "pending", "model": M, "host_pref": "auto"},
        {"id": "b", "status": "pending", "model": "qwen3:14b", "host_pref": "unraid"},
        {"id": "c", "status": "done", "model": "other-bare", "host_pref": "auto"}]
got = q.prime_darkbloom_ready(1000.0, jobs=jobs)
chk("prime probes only the models a PENDING darkbloom-routed job needs", got == [M] and calls == [M], (got, calls))
with q._Locked():
    chk("...and the locked launch path then reads that verdict (ready, still no probe)",
        q.darkbloom_model_ready(M, 1000.0 + 5) is True and calls == [M], calls)
    chk("prime refuses to run under the lock", q.prime_darkbloom_ready(1000.0, jobs=jobs) == [], "")

# 4. kill switch restores the old (probe-under-lock) behaviour exactly
calls.clear()
q._DB_READY.clear()
q.DB_PROBE_OUTSIDE_LOCK = False
with q._Locked():
    chk("DB_PROBE_OUTSIDE_LOCK=0: probes under the lock again (old behaviour)",
        q.darkbloom_model_ready(M, 1000.0) is True and calls == [M], calls)
q.DB_PROBE_OUTSIDE_LOCK = True

# 5. wiring: the daemon primes BEFORE it takes the lock, in the poll loop
src = Path(A.queue).read_text()
i_prime = src.find("            prime_darkbloom_ready()\n")
i_lock = src.find("        with _Locked() as lock:\n            state = lock.load()\n            changed = False")
chk("cmd_run calls prime_darkbloom_ready() immediately before taking the state lock",
    0 < i_prime < i_lock and i_lock - i_prime < 400, (i_prime, i_lock))

print("PROBE_OUTSIDE_LOCK_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
