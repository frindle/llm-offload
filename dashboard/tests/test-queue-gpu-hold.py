#!/usr/bin/env python3
"""test-queue-gpu-hold.py -- GET /api/gpu-hold of the queue API (plex-automation's LLM_UNRAID_HOLD_URL), with the real
Handler on 127.0.0.1 port 0 (a subclass reports a LAN peer so the bearer token is enforced).
Proves: the pure answer holds only for a RUNNING enqueue-gpu row on the asked lane (pending, other lane, ordinary job,
wrong runner: no hold; a runner path from another host's home and a lane given as the Ollama URL still hold); over HTTP a
stub exclusive job flips the answer to {"hold": true, "cause": "gpu-exclusive"} and back to false once it is done; 401
without / with a wrong token, 403 with no token configured; unreadable state and standby answer 503 (the caller fails
open); with --plex, plex's own llm_advisor.hold_active reads (True, "gpu-exclusive") while held and (False, None) after.
Sandboxed: temp HOME with a copy of --bin (the pipeline bin) with the dashboard src overlaid. Fake token only.
Run (sandbox only): python3 tests/test-queue-gpu-hold.py [--bin PIPELINE_BIN] [--plex PLEX_AUTOMATION_CHECKOUT]"""
import argparse
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
ap.add_argument("--plex", default="")
ARGS = ap.parse_args()
SRC = HERE.parent / "src"
HOME = tempfile.mkdtemp(prefix="qgpuhold-home-")
for k in [k for k in os.environ if k.startswith("QUEUE_")]:
    del os.environ[k]
os.environ.update(HOME=HOME, DISPATCH_VERIFY_SANDBOX="1", OLLAMA_QUEUE_NO_NOTIFY="1", QUEUE_API_TOKEN="fake-test-token")
B = Path(HOME) / "bin"
shutil.copytree(ARGS.bin, B, symlinks=True, ignore=shutil.ignore_patterns(
    "ollama-queue-logs", "ollama-queue-livelogs", "__pycache__", "*.log", ".git"))
for f in SRC.glob("*.py"):
    d = B / f.name
    if d.is_symlink() or d.exists():
        d.unlink()
    shutil.copy(f, d)
sys.path.insert(0, str(B))
spec = importlib.util.spec_from_file_location("oqapi_gpuhold", B / "ollama-queue-api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
api.q.STATE_PATH = Path(HOME) / "t-state.json"
api.q.LOCK_PATH = Path(HOME) / "t-state.lock"
UNRAID_URL = "http://198.51.100.145:11434"
# Lane naming without the worker module or a Darkbloom/Bonsai config: Unraid's URL names the "unraid" lane.
api.q.worker = lambda: types.SimpleNamespace(KNOWN_OLLAMA_HOSTS={"unraid": {"url": UNRAID_URL}})
api.q._darkbloom_url = lambda: None
api.q._bonsai_url = lambda: None

FAILS = []


def ok(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        FAILS.append(name)


def gpu_row(jid, status="running", host="unraid", lane="unraid", runner=None):
    return {"id": jid, "label": "gpu-trial-" + jid, "status": status, "job_kind": api.q.GPU_JOB_KIND,
            "model": api.q.GPU_JOB_MODEL, "runner": runner or api.q.GPU_JOB_RUNNER, "host_pref": host,
            "lane": lane if status == "running" else None}


def plain_row(jid, status="running", lane="unraid"):
    return {"id": jid, "label": "fix-" + jid, "status": status, "model": "qwen3:8b", "host_pref": lane, "lane": lane}


# ---- pure answer
H = api._gpu_hold
ok("pure: no jobs -> no hold", H([]) == {"hold": False, "lane": "unraid"}, H([]))
r = H([gpu_row("g1")])
ok("pure: a running gpu-exclusive row on unraid holds it", r.get("hold") is True and r.get("cause") == "gpu-exclusive"
   and r.get("job") == "g1" and r.get("lane") == "unraid", r)
ok("pure: a PENDING gpu-exclusive row does not hold (no GPU yet)", H([gpu_row("g2", status="pending")])["hold"] is False)
ok("pure: a gpu-exclusive row on another lane does not hold unraid",
   H([gpu_row("g3", host="bonsai", lane="bonsai")])["hold"] is False)
ok("pure: ... but holds the lane it runs on", H([gpu_row("g3", host="bonsai", lane="bonsai")], "bonsai")["hold"] is True)
ok("pure: an ordinary running job on unraid does not hold", H([plain_row("p1")])["hold"] is False)
ok("pure: gpu kind with a different runner does not hold",
   H([gpu_row("g4", runner="/home/x/bin/bakeoff-runner.py")])["hold"] is False)
ok("pure: a runner path from another host's home still holds (state imported across the queue move)",
   H([gpu_row("g5", runner="/Users/someone/bin/gpu-exclusive-runner.py")])["hold"] is True)
ok("pure: a row known only by its lane URL (no host_pref) holds",
   H([gpu_row("g6", host=None, lane=UNRAID_URL)])["hold"] is True)
ok("pure: done / failed gpu rows do not hold",
   H([gpu_row("g7", status="done"), gpu_row("g8", status="failed")])["hold"] is False)
ok("pure: junk rows are skipped", H([None, "x", 3, {"status": "running"}])["hold"] is False)


# ---- over HTTP
def serve(peer):
    class Hd(api.Handler):
        def __init__(self, request, client_address, server):
            super().__init__(request, (peer, client_address[1]), server)
    srv = api.ThreadingServer(("127.0.0.1", 0), Hd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SRV = serve("192.0.2.7")
PORT = SRV.server_address[1]
GOOD = {"Authorization": "Bearer fake-test-token"}
URL = "http://127.0.0.1:%d/api/gpu-hold" % PORT


def get(path, headers):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body)
        except ValueError:
            return r.status, body
    finally:
        c.close()


def write_state(jobs):
    tmp = api.q.STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"jobs": jobs}))
    os.replace(tmp, api.q.STATE_PATH)


ok("http: no state file yet -> 200 no hold", get("/api/gpu-hold", GOOD) == (200, {"hold": False, "lane": "unraid"}),
   get("/api/gpu-hold", GOOD))
write_state([plain_row("p1"), gpu_row("g9", status="pending")])
st, body = get("/api/gpu-hold", GOOD)
ok("http: queue busy but no exclusive job running -> no hold", st == 200 and body.get("hold") is False, (st, body))
write_state([plain_row("p1", status="done"), gpu_row("g9")])
st, body = get("/api/gpu-hold", GOOD)
ok("http: the stub exclusive job running flips it to hold", st == 200 and body.get("hold") is True
   and body.get("cause") == "gpu-exclusive" and body.get("job") == "g9", (st, body))
st, body = get("/api/gpu-hold?lane=bonsai", GOOD)
ok("http: ?lane= asks about another lane", st == 200 and body == {"hold": False, "lane": "bonsai"}, (st, body))
ok("http: no bearer -> 401", get("/api/gpu-hold", {})[0] == 401)
ok("http: wrong bearer -> 401", get("/api/gpu-hold", {"Authorization": "Bearer nope"})[0] == 401)
del os.environ["QUEUE_API_TOKEN"]
ok("http: no token configured, LAN peer -> 403", get("/api/gpu-hold", {})[0] == 403)
os.environ["QUEUE_API_TOKEN"] = "fake-test-token"
os.environ["QUEUE_STANDBY"] = "1"
st, body = get("/api/gpu-hold", GOOD)
ok("http: standby -> 503 (a copied state says nothing about this host's GPU)", st == 503 and body == {"error": "standby"},
   (st, body))
del os.environ["QUEUE_STANDBY"]
api.q.STATE_PATH.write_text("{not json")
ok("http: unreadable state -> 503", get("/api/gpu-hold", GOOD)[0] == 503)
write_state([gpu_row("g9", status="done")])
st, body = get("/api/gpu-hold", GOOD)
ok("http: the job done -> no hold again", st == 200 and body.get("hold") is False, (st, body))

# ---- plex's own reader (optional: needs a plex-automation checkout)
if ARGS.plex:
    sys.path.insert(0, ARGS.plex)
    import llm_advisor  # noqa: E402

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return json.loads(self._b)

    class Http:
        def get(self, url, headers=None, timeout=None):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
                    return Resp(r.status, r.read())
            except urllib.error.HTTPError as e:
                return Resp(e.code, e.read())

    env = {"LLM_UNRAID_HOLD_URL": URL, "LLM_UNRAID_HOLD_TOKEN": "fake-test-token"}
    write_state([gpu_row("ga")])
    got = llm_advisor.hold_active(env, http=Http(), prefix="LLM_UNRAID_HOLD")
    ok("plex: hold_active reads (True, 'gpu-exclusive') while the job runs", got == (True, "gpu-exclusive"), got)
    write_state([gpu_row("ga", status="done")])
    got = llm_advisor.hold_active(env, http=Http(), prefix="LLM_UNRAID_HOLD")
    ok("plex: hold_active reads (False, None) once it is done", got == (False, None), got)
    write_state([gpu_row("ga")])
    got = llm_advisor.hold_active(dict(env, LLM_UNRAID_HOLD_TOKEN="nope"), http=Http(), prefix="LLM_UNRAID_HOLD")
    ok("plex: a wrong token fails open (401 is not a hold)", got == (False, None), got)
else:
    print("  skip plex hold_active checks (no --plex)")

SRV.shutdown()
shutil.rmtree(HOME, ignore_errors=True)
print("FAIL: %d" % len(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
