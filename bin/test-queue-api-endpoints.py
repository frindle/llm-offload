#!/usr/bin/env python3
"""Mac-side tools take the queue API from queue_config, not a hard-coded 127.0.0.1:7684.
dispatch-worktree-reap: remote queue -> local state file ignored, bearer token sent, API down -> --apply refuses."""
import http.server, importlib.machinery, importlib.util, json, os, sys, tempfile, threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(name, fn):
    ld = importlib.machinery.SourceFileLoader(name, os.path.join(HERE, fn))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


fails = []
tmp = tempfile.mkdtemp()
os.environ["QUEUE_CONFIG"] = os.path.join(tmp, "none.toml")
os.environ["QUEUE_API"] = "http://queue.invalid:7684"
os.environ["QUEUE_API_TOKEN"] = "tok-xyz"
os.environ.pop("MINI_FINISH_API", None)
reap = load("reap", "dispatch-worktree-reap")
mf = load("mini_finish", "mini-finish")

if mf.API() != "http://queue.invalid:7684":
    fails.append("mini-finish API() ignores queue_config")
if mf.API_HEADERS().get("Authorization") != "Bearer tok-xyz":
    fails.append("mini-finish sends no token")
os.environ["MINI_FINISH_API"] = "http://127.0.0.1:1"
if mf.API() != "http://127.0.0.1:1" or mf.API_HEADERS():
    fails.append("MINI_FINISH_API override broken")
os.environ.pop("MINI_FINISH_API")

home = os.path.join(tmp, "home")
os.makedirs(os.path.join(home, "bin"))
src = reap.Sources(home=home)
if src.runs_api != "http://queue.invalid:7684/api/runs":
    fails.append("reap runs_api: " + src.runs_api)
if not src.queue_remote:
    fails.append("reap should see remote queue")
json.dump({"jobs": [{"id": "x1", "status": "running", "cwd": "/tmp/stale-cwd"}]}, open(src.queue_state, "w"))
live = reap.collect_live(src)
if any(r == "queue" for _, r, _ in live.paths):
    fails.append("stale local state was used")
if not any("API unreachable" in e for e in live.errors):
    fails.append("API-down not reported")

seen = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen["auth"] = self.headers.get("Authorization")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
reap._fetch_json(f"http://127.0.0.1:{srv.server_address[1]}/api/runs", src.runs_headers)
if seen.get("auth") != "Bearer tok-xyz":
    fails.append("reaper sent no token")

sys.argv = ["reap", "--apply", "--repo", tmp, "--no-fetch"]
reap.Sources.__init__.__defaults__ = (home,)
if reap.main() != 3:
    fails.append("--apply did not refuse with remote API down")
ls = load("loc_submit", "localize/loc_submit.py")
os.environ.pop("CPU_RUNNER_API", None)
if ls._queue_api() != "http://queue.invalid:7684":
    fails.append("loc_submit ignores queue_config")
print("FAIL: " + "; ".join(fails) if fails else "PASS")
sys.exit(1 if fails else 0)
