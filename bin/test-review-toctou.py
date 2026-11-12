#!/usr/bin/env python3
"""test-review-toctou.py -- code-review-agent with ~/.darkbloom/local.json EMPTIED MID-RUN must still send its
requests to the Darkbloom /v1/chat/completions (not fall back to /api/chat on a dead legacy host). Sandboxed HOME,
stub server, real code-review-agent.py. Prints REVIEW_TOCTOU_OK."""
import http.server, json, os, socketserver, subprocess, sys, tempfile, threading
from pathlib import Path
BIN = Path(os.environ.get("BIN", Path(__file__).resolve().parent)).resolve()
TMP = Path(tempfile.mkdtemp(prefix="review-toctou-"))
HOME = TMP / "home"; (HOME / ".darkbloom").mkdir(parents=True); (HOME / ".config" / "darkbloom").mkdir(parents=True)
LOCAL = HOME / ".darkbloom" / "local.json"
KEY = "SENTINEL-TOCTOU-KEY-cccccccccccccccc"
BARE = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
FAILS = []
def chk(n, c, d=""):
    if not c: FAILS.append(n)
    print(("ok   - " if c else "FAIL - ") + n + ("" if c else f": {str(d)[:400]}"))
class H(http.server.BaseHTTPRequestHandler):
    rec = []
    def log_message(self, *a): pass
    def _s(self, o, code=200):
        b = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._restart()
        H.rec.append(("GET", self.path, self.headers.get("Authorization")))
        self._s({"status": "ok"} if self.path.startswith("/health") else {"data": [{"id": BARE}], "models": [{"name": BARE, "model": BARE}]})
    def _restart(self):
        if not H.rec:
            LOCAL.write_text("")                       # the provider "restarts": local.json is empty from the first request on
    def do_POST(self):
        self._restart()
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        H.rec.append(("POST", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._s({"error": "unauthorized"}, 401)
        self._s({"model": BARE, "choices": [{"message": {"role": "assistant", "content": json.dumps({"findings": []})}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})
class TS(socketserver.ThreadingTCPServer):
    daemon_threads = True; allow_reuse_address = True
srv = TS(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{srv.server_address[1]}"
LOCAL.write_text(json.dumps({"api_key": KEY, "base_url": STUB + "/v1", "port": 8000, "pid": 4242, "version": "0.9.20"}))
(HOME / ".darkbloom" / "daemon-state.json").write_text(json.dumps({"schema": 1, "pid": 4242, "process_identity": {"pid": 4242}, "advertised_models": [BARE]}))
(HOME / ".config" / "darkbloom" / "provider.toml").write_text(f"[backend]\nenabled_models = [ '{BARE}' ]\n")
diff = TMP / "d.diff"
diff.write_text("".join(f"--- a/x{i}.py\n+++ b/x{i}.py\n@@ -1,2 +1,3 @@\n def f(a):\n-    return a\n+    return a + {i}\n+\n" for i in range(6)))
out = TMP / "out"; out.mkdir()
env = {"HOME": str(HOME), "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "SLOT_BUSY_WAIT_S": "1",
       **{k: v for k, v in os.environ.items() if k.startswith(("DARKBLOOM_", "LANE_PLAN"))}}
p = subprocess.run([sys.executable, str(BIN / "code-review-agent.py"), "--mode", "review", "--diff", str(diff), "--host", STUB,
                    "--model", BARE, "--no-verify", "--batch-chars", "120", "--out", str(out)], capture_output=True, text=True, timeout=240, env=env, cwd=str(TMP))
posts = [r for r in H.rec if r[0] == "POST"]
chk("agent made at least one request", len(posts) >= 1, (p.stdout + p.stderr)[-600:])
chk("every POST went to /v1/chat/completions (never /api/chat)", posts and all(r[1].startswith("/v1/chat/completions") for r in posts), posts)
chk("every POST carried the Bearer key even though local.json was emptied after the first", posts and all(r[2] == "Bearer " + KEY for r in posts), posts)
print("posts:", len(posts))
chk("local.json really was empty at the end (the premise held)", LOCAL.read_text() == "")
chk("key never in stdout/stderr", KEY not in p.stdout + p.stderr)
print("REVIEW_TOCTOU_OK" if not FAILS else f"REVIEW_TOCTOU_FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
