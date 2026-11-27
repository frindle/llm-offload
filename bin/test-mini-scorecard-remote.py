#!/usr/bin/env python3
"""mini-scorecard measures pass/reject rates from the remote API, else from queue-events.jsonl, when no local done.json exists."""
import http.server, importlib.machinery, importlib.util, json, os, sys, tempfile, threading, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="screm-"))
for d in ("home", "q", "runs"): (tmp / d).mkdir()
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
RES = [{"id": "r1", "label": "famA-1", "runner": "/x/mini-swe-runner.py", "status": "done", "timestamp": now},
       {"id": "r2", "label": "famA-2", "runner": "/x/mini-swe-runner.py", "status": "done", "needs_review": True, "timestamp": now},
       {"id": "r3", "label": "famA-3", "runner": "/x/mini-swe-runner.py", "status": "failed", "terminal_reason": "x", "timestamp": now},
       {"id": "r4", "label": "other", "runner": "/x/other.py", "status": "done", "timestamp": now}]
seen = {}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen["auth"] = self.headers.get("Authorization")
        b = json.dumps({"results": RES} if self.path.startswith("/api/results") else {}).encode() if not os.environ.get("DOWN") else b"x"
        self.send_response(200 if not os.environ.get("DOWN") else 500); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(OFFLOAD_HOME=str(tmp / "home"), OFFLOAD_QUEUE_LOGS=str(tmp / "q"), OFFLOAD_RUNS=str(tmp / "runs"),
                  QUEUE_CONFIG=str(tmp / "none.toml"), QUEUE_API=f"http://localhost:{srv.server_address[1]}", QUEUE_API_TOKEN="tk")
ldr = importlib.machinery.SourceFileLoader("sc", str(HERE / "mini-scorecard"))
S = importlib.util.module_from_spec(importlib.util.spec_from_loader("sc", ldr)); ldr.exec_module(S)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
# local queue (loopback): no done.json at all -> still measured through the API fallback
jobs = S.load_jobs(14)
chk("3 mini-swe jobs from the API (non-mini ignored)", len(jobs) == 3)
chk("2 ok (one needs-review), 1 failed", sum(j["ok"] for j in jobs) == 2 and sum(j["nr"] for j in jobs) == 1 and sum(j["failed"] for j in jobs) == 1)
chk("token sent", seen.get("auth") == "Bearer tk")
# API down -> events file
os.environ["DOWN"] = "1"
with open(tmp / "home" / "queue-events.jsonl", "w") as f:
    for i, st in enumerate(("passed", "needs-review", "failed", "done")):
        f.write(json.dumps({"seq": i, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": f"e{i}", "label": f"famB-{i}", "state": st, "runner": "mini-swe-runner.py"}) + "\n")
    f.write(json.dumps({"seq": 9, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": "e9", "label": "z", "state": "passed", "runner": "x.py"}) + "\n")
jobs = S.load_jobs(14)
chk("events fallback: passed, needs-review, failed counted", len(jobs) == 3 and sum(j["ok"] for j in jobs) == 2 and sum(j["failed"] for j in jobs) == 1)
print("FAIL: " + ", ".join(fails) if fails else "SCORECARD_REMOTE_OK")
sys.exit(1 if fails else 0)
