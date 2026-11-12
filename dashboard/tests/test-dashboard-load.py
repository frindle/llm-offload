#!/usr/bin/env python3
"""Dashboard API load hardening (2026-10-10 pile-up: every poll rebuilt ~6s of payload; ~170 stacked
request threads drove load average to ~80 on the Studio). Fires ~50 CONCURRENT requests at the real
Handler (ThreadingServer on an ephemeral port, slow stand-ins for the expensive builders) and proves:
  A. every GET endpoint the handler routes is covered by this test (a new un-timed endpoint fails);
  B. 50 concurrent hits on each heavy endpoint run the builder ONCE (single-flight cache), and every
     response lands inside the latency budget; a second warm round is fast on every endpoint;
  C. stale-while-revalidate: an expired entry is served at once while ONE background refresh runs;
  D. the concurrency cap exists: over the cap -> fast 503 + Retry-After (never a pile-up), slots are
     returned, and /api/cpu/* (the Unraid runner) keeps its OWN headroom while the dashboard pool is full;
  E. _livelog_path never globs the ~14k-file log dirs per request (indexed; sees new/removed files);
  F. the Unraid runner's constant /api/cpu/* POSTs do not wipe the response cache (they did: every heavy
     endpoint stayed permanently cold); G. startup priming warms the heavy endpoints one at a time.
Sandboxed: temp HOME with a copy of the pipeline bin; nothing live is read or written.
Run: python3 test-dashboard-load.py [--src PATH/ollama-queue-api.py] [--bin PIPELINE_BIN]"""
import argparse
import http.client
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--src", default=str(HERE.parent / "src" / "ollama-queue-api.py"))
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
ARGS = ap.parse_args()
SRC = Path(ARGS.src).resolve()
HOME = tempfile.mkdtemp(prefix="dashload-home-")
os.environ["HOME"] = HOME
shutil.copytree(ARGS.bin, Path(HOME) / "bin", symlinks=True, ignore=shutil.ignore_patterns(
    "ollama-queue-logs", "ollama-queue-livelogs", "__pycache__", "*.log"))
for _f in SRC.parent.glob("*.py"):          # overlay the dashboard modules beside the API file
    if _f.resolve() == SRC:
        continue
    _d = Path(HOME) / "bin" / _f.name
    if _d.is_symlink() or _d.exists():
        _d.unlink()
    shutil.copy(_f, _d)
_api_copy = Path(HOME) / "bin" / "ollama-queue-api.py"
if _api_copy.is_symlink() or _api_copy.exists():
    _api_copy.unlink()
shutil.copy(SRC, _api_copy)
sys.path.insert(0, str(Path(HOME) / "bin"))
spec = importlib.util.spec_from_file_location("oqapi", _api_copy)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

FAILS = []


def ok(name, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {extra}"))
    if not cond:
        FAILS.append(name)


# ---- slow stand-ins for the expensive builders: serialize on one lock (the GIL-bound reality: N
# concurrent rebuilds cost N x, not 1 x) and count calls.
COST = 0.25
CALLS = {}
_CPU = threading.Lock()


def slow(name, value):
    def fn(*a, **k):
        CALLS[name] = CALLS.get(name, 0) + 1
        with _CPU:
            time.sleep(COST)
        return value
    fn.__name__ = name
    return fn


# (GET path, name the stand-in counts under) -- the table the coverage check (A) holds the handler to
HEAVY = [
    ("/api/triage", "_triage_summary"),
    ("/api/bundle-views", "_bundle_views"),
    ("/api/jobs/completed", "_completed_jobs"),
    ("/api/runs", "_run_status_jobs"),
    ("/api/runs/retention", "_retention_preview"),
    ("/api/hosts", "_hosts_summary"),
    ("/api/settings/hosts", "_hosts_settings"),
    ("/api/web-search-usage", "_web_search_usage"),
]
for _p, _a in HEAVY:
    setattr(api, _a, slow(_a, {"views": {}, "ok": 1} if _a == "_bundle_views" else []))
api.Handler._build_jobs_payload = lambda self: slow("jobs", [])()      # /api/jobs
api._build_run_tree = lambda rows: slow("tree", {"tree": []})()         # /api/runs/tree
api._finished_bundle_views = lambda **a: slow("bhist", {"bundles": []})()
HEAVY += [("/api/jobs", "jobs"), ("/api/runs/tree", "tree"), ("/api/bundle-history", "bhist")]
# cheap/other routes: timed warm but not single-flight-asserted
CHEAP = ["/", "/api/queue-wait", "/api/bundle-verdicts", "/api/cpu-lane", "/api/jobs/abcdef123456/gate", "/api/jobs/abcdef123456/livelog"]
# routes that shell out to a child / external system: timed by their own tests, deliberately not here
EXEMPT = {"/api/handoff"}

api.Handler.log_message = lambda self, *a, **k: None
api.Handler._cpu_lane = lambda self, method: self._text("lane-ok", 200)
_real_gate = api.q._load_job_result
srv = api.ThreadingServer(("127.0.0.1", 0), api.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path, timeout=30):
    t0 = time.monotonic()
    try:
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        out = (r.status, r.getheader("Retry-After"), time.monotonic() - t0)
        c.close()
        return out
    except Exception as e:  # noqa: BLE001
        return (type(e).__name__, None, time.monotonic() - t0)


def storm(paths):
    """Fire every path in `paths` at once (barrier); returns [(status, retry_after, seconds)]."""
    res = [None] * len(paths)
    bar = threading.Barrier(len(paths))

    def one(i, p):
        bar.wait()
        res[i] = get(p)
    th = [threading.Thread(target=one, args=(i, p)) for i, p in enumerate(paths)]
    [t.start() for t in th]
    [t.join(90) for t in th]
    return res


def reset():
    api._invalidate_response_cache()
    CALLS.clear()
    if hasattr(api, "_API_SLOTS"):
        api._API_SLOTS = threading.BoundedSemaphore(500)    # B/C measure the cache, not the cap


print("A. every routed GET endpoint is covered")
src = _api_copy.read_text()
body = src[src.index("    def do_GET(self):"):src.index("    def do_POST(self):")]
routed = set(re.findall(r'self\.path == "(/[^"]*)"', body))
routed |= set(re.findall(r'self\.path\.startswith\("(/api/bundle-history)\?"\)', body))
known = {p for p, _ in HEAVY} | set(CHEAP) | EXEMPT | {"/index.html", "/chat"}
ok("no routed GET endpoint is missing from the load test", routed <= known, str(sorted(routed - known)))

print("B. 50 concurrent requests per heavy endpoint: one build, inside the latency budget")
BUDGET = 3.0          # 50 serialized rebuilds would be 50 x COST = 12.5s
for path, attr in HEAVY:
    reset()
    res = storm([path] * 50)
    bad = [r for r in res if r[0] != 200]
    worst = max(r[2] for r in res)
    calls = CALLS.get(attr, 0)
    ok(f"{path}: 50 concurrent -> 50x200", not bad, str(bad[:3]))
    ok(f"{path}: builder ran <=2 times for 50 requests (ran {calls})", 1 <= calls <= 2)
    ok(f"{path}: slowest of 50 {worst:.2f}s within {BUDGET}s budget", worst < BUDGET)
reset()
mix = [p for p, _ in HEAVY] * 4 + CHEAP * 2                 # ~54 mixed requests
res = storm(mix)
badmix = [r for r in res if r[0] not in (200, 404)]
ok("mixed storm of %d: all answered 200/404" % len(mix), not badmix, str(badmix[:3]))
ok("mixed storm: slowest %.2fs within %.1fs budget" % (max(r[2] for r in res), BUDGET * 2),
   max(r[2] for r in res) < BUDGET * 2)
warm = {p: get(p)[2] for p in [p for p, _ in HEAVY] + CHEAP}
slow_warm = {p: round(t, 3) for p, t in warm.items() if t > 0.3}
ok("every endpoint answers a warm request in <=0.3s", not slow_warm, str(slow_warm))

print("C. stale-while-revalidate: expired heavy entry is served at once, rebuilt once in the background")
reset()
get("/api/runs")
ts, val = api._RESP_CACHE["runs"]
api._RESP_CACHE["runs"] = (ts - 30, val)               # past ttl (10s), inside the stale window
CALLS.clear()
res = storm(["/api/runs"] * 20)
ok("expired entry: all 20 served without waiting on the rebuild",
   max(r[2] for r in res) < COST * 0.8, f"{max(r[2] for r in res):.2f}s")
time.sleep(COST * 3)
ok("expired entry: exactly one background rebuild ran (ran %d)" % CALLS.get("_run_status_jobs", 0),
   CALLS.get("_run_status_jobs", 0) == 1)
ok("expired entry: the refreshed entry replaced the stale one", api._RESP_CACHE["runs"][0] > ts)

print("D. concurrency cap: fast 503 + Retry-After over the cap; /api/cpu/* keeps its own headroom")
has_cap = hasattr(api, "_API_SLOTS") and hasattr(api, "_CPU_SLOTS")
ok("the server defines an API slot pool and a separate CPU-lane pool", has_cap)
if has_cap:
    api._API_SLOTS = threading.BoundedSemaphore(4)
    api._CPU_SLOTS = threading.BoundedSemaphore(8)
    GATE_DELAY = 0.8
    api.q._load_job_result = lambda jid: (time.sleep(GATE_DELAY), None)[1]      # uncached slow route
    res = storm(["/api/jobs/abcdef123456/gate"] * 14 + ["/api/cpu/claim"] * 6)
    gate, lane = res[:14], res[14:]
    busy = [r for r in gate if r[0] == 503]
    served = [r for r in gate if r[0] != 503]
    ok("over the cap: excess requests are refused with 503 (%d of 14)" % len(busy), len(busy) >= 8,
       str([r[0] for r in gate]))
    ok("503s carry Retry-After", bool(busy) and all(r[1] for r in busy))
    ok("503s are FAST (<0.4s, not queued behind the slow work)",
       bool(busy) and all(r[2] < 0.4 for r in busy), str([round(r[2], 2) for r in busy]))
    ok("requests within the cap are still served (%d)" % len(served), 1 <= len(served) <= 5)
    ok("/api/cpu/* is never refused while the dashboard pool is full", all(r[0] == 200 for r in lane), str(lane))
    ok("/api/cpu/* answers fast while the dashboard pool is saturated", all(r[2] < 0.4 for r in lane))
    time.sleep(GATE_DELAY + 0.3)
    free = sum(1 for _ in range(4) if api._API_SLOTS.acquire(blocking=False))
    ok("every slot is returned after the burst (%d/4 free)" % free, free == 4)
    api._API_SLOTS = threading.BoundedSemaphore(500)
    api.q._load_job_result = _real_gate

print("F. the runner's /api/cpu/* writes do NOT wipe the response cache; any other write does")


def post(path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


reset()
get("/api/runs")
ok("cache is warm before the writes", "runs" in api._RESP_CACHE)
for _ in range(5):
    post("/api/cpu/claim")
ok("5 runner POST /api/cpu/claim leave the cache intact", "runs" in api._RESP_CACHE)
post("/api/not-a-real-route")
ok("any other POST drops the cache (an action is visible on the next poll)", "runs" not in api._RESP_CACHE)

print("G. startup priming warms the heavy endpoints serially")
reset()
api._prime_caches(PORT, paths=[p for p, _ in HEAVY if p != "/api/bundle-history"])
ok("after _prime_caches every heavy builder has run (once; /api/runs + /api/runs/tree share one)",
   all(1 <= CALLS.get(a, 0) <= 2 for p, a in HEAVY if p != "/api/bundle-history"), str(CALLS))
ok("the server entrypoint starts the priming thread", "target=_prime_caches" in src[src.index('if __name__ == "__main__":'):])
t0 = time.monotonic()
get("/api/jobs")
ok("first dashboard poll after priming is a cache hit", time.monotonic() - t0 < 0.2)

print("E. _livelog_path: no per-request directory glob over the log dirs")
ld, ll = Path(HOME) / "logs", Path(HOME) / "live"
(ld / "archive").mkdir(parents=True)
ll.mkdir()
for i in range(3000):
    (ld / f"{i:012x}-job.log").write_text("x")
(ld / "abcabcabcabc-label.log").write_text("t")
_orig_glob = Path.glob
Path.glob = lambda self, pat: (_ for _ in ()).throw(AssertionError("directory glob on a request path"))
try:
    t0 = time.monotonic()
    got = [api._livelog_path(None, "abcabcabcabc", live_dir=ll, log_dir=ld) for _ in range(300)]
    dt = time.monotonic() - t0
    ok("lookup finds the log without globbing", got[0] == str(ld / "abcabcabcabc-label.log"), str(got[0]))
    ok("300 lookups over a 3000-file dir are cheap (%.2fs)" % dt, dt < 1.0)
    (ll / "abcabcabcabc-label.livelog").write_text("live")
    ok("a file created later is seen (index follows the dir mtime)",
       api._livelog_path(None, "abcabcabcabc", live_dir=ll, log_dir=ld) == str(ll / "abcabcabcabc-label.livelog"))
    (ll / "abcabcabcabc-label.livelog").unlink()
    ok("a removed file falls back to the next source",
       api._livelog_path(None, "abcabcabcabc", live_dir=ll, log_dir=ld) == str(ld / "abcabcabcabc-label.log"))
    ok("an unknown id is None", api._livelog_path(None, "ffffffffffff", live_dir=ll, log_dir=ld) is None)
    ok("a non-hex id is None", api._livelog_path(None, "../../etc", live_dir=ll, log_dir=ld) is None)
except AssertionError as e:
    ok("no directory glob on the request path", False, str(e))
finally:
    Path.glob = _orig_glob

srv.shutdown()
shutil.rmtree(HOME, ignore_errors=True)
print("ALL PASSED" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
