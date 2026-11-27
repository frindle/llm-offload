#!/usr/bin/env python3
"""/api/results must stay cheap with a big log dir, and slow reads must not starve cheap ones (2026-11 audit:
/api/results timed out at 60 s with limit=20; /api/triage, /api/hosts, /api/jobs/completed answered 503 "server busy").
Fixture: 6000 done sidecars. Proves: limit=20 parses ~tens of records, not 6000, and is fast; a repeat call parses
nothing new (memo); the newest records come back newest first; the heavy pool is separate; the reserve pool takes
overflow when the general pool is full; heavy reads cannot eat cheap slots."""
import importlib.util, json, os, sys, tempfile, threading, time
from pathlib import Path

import shutil

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "ollama-queue-api.py"
BIN = Path(os.environ["HOME"]) / "bin"
HOME = tempfile.mkdtemp(prefix="resbounded-")
shutil.copytree(BIN, Path(HOME) / "bin", symlinks=True, ignore=shutil.ignore_patterns(
    "ollama-queue-logs", "ollama-queue-livelogs", "__pycache__", "*.log"))
for _f in SRC.parent.glob("*.py"):          # overlay the dashboard modules beside the API file
    _d = Path(HOME) / "bin" / _f.name
    if _d.is_symlink() or _d.exists():
        _d.unlink()
    shutil.copy(_f, _d)
os.environ["HOME"] = HOME
sys.path.insert(0, str(Path(HOME) / "bin"))
spec = importlib.util.spec_from_file_location("oqapi", Path(HOME) / "bin" / "ollama-queue-api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

FAILS = []


def ok(name, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {extra}"))
    if not cond:
        FAILS.append(name)


ld = Path(HOME) / "logs"
ld.mkdir()
N = 6000
base = time.time() - N * 10
for i in range(N):
    jid = "%032x" % (i + 1)
    p = ld / f"{jid}.done.json"
    p.write_text(json.dumps({"label": f"job-{i}", "status": "done", "persisted_at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i * 10))}))
    os.utime(p, (base + i * 10, base + i * 10))
(ld / ("%032x" % 99999 + ".done.json")).write_text(json.dumps({"label": "gate-x", "status": "done"}))

calls = [0]
real = api.q._load_job_result


def counting(jid, d=None):
    calls[0] += 1
    return real(jid, d)


api.q._load_job_result = counting
api._RESULT_MEMO.clear()
t = time.time()
rows = api._results_bounded(None, None, 20, log_dir=ld)
dt = time.time() - t
print("A. bounded scan")
ok("limit=20 returns 20 rows", len(rows) == 20, len(rows))
ok("newest first", rows[0]["label"] == f"job-{N-1}" and rows[1]["label"] == f"job-{N-2}", rows[0]["label"])
ok("parsed only a handful of records (%d), not %d" % (calls[0], N), calls[0] <= 80)
ok("fast (%.2fs)" % dt, dt < 3)
c0 = calls[0]
api._results_bounded(None, None, 20, log_dir=ld)
ok("repeat call parses nothing new (memo)", calls[0] == c0)
rows = api._results_bounded(None, "job-5999", 0, log_dir=ld)
ok("a label filter still works over the whole dir", len(rows) == 1 and rows[0]["label"] == "job-5999")
t = time.time()
api._results_bounded(None, "job-1", 0, log_dir=ld)
ok("second full scan is served from the memo (<3s)", time.time() - t < 3)

print("B. slot pools")
held = [api._HEAVY_SLOTS.acquire(blocking=False) for _ in range(api.MAX_INFLIGHT_HEAVY)]
ok("heavy pool filled", all(held))
ok("a further heavy read is refused", api._pick_slot("/api/results?limit=20") is None)
s = api._pick_slot("/api/state")
ok("a cheap read is still admitted while the heavy pool is full", s is api._API_SLOTS)
s.release()
api._API_SLOTS = threading.BoundedSemaphore(1)
api._API_SLOTS.acquire()
s = api._pick_slot("/api/triage")
ok("general pool full: cheap read falls back to the reserve", s is api._RESERVE_SLOTS)
s.release()
api._RESERVE_SLOTS = threading.BoundedSemaphore(1)
api._RESERVE_SLOTS.acquire()
ok("general and reserve full: refused", api._pick_slot("/api/hosts") is None)
ok("/api/cpu/* keeps its own pool", api._pick_slot("/api/cpu/claim") is api._CPU_SLOTS)
api._CPU_SLOTS.release()
print("FAIL: " + ", ".join(FAILS) if FAILS else "PASS")
sys.exit(1 if FAILS else 0)
