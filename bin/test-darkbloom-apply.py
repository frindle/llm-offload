#!/usr/bin/env python3
"""Hermetic test of darkbloom-apply-wide-models.sh: fake `darkbloom` binary + stub HTTP provider,
temp HOME. Never touches the real Darkbloom. Prints ALL PASS."""
import json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SH = Path(__file__).resolve().parent / "darkbloom-apply-wide-models.sh"
A, B, X = "Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8", "gemma-4-26b-qat-4bit"
KEY = "SECRETKEYSECRETKEYSECRETKEYSECRET123"
FAILS = []


def check(n, c, extra=""):
    print(("ok  : " if c else "FAIL: ") + n + ("" if c else " -- " + str(extra)))
    if not c:
        FAILS.append(n)


class W:
    def __init__(self, fail_set=None):
        self.d = Path(tempfile.mkdtemp(prefix="ap-"))
        self.toml = self.d / "provider.toml"
        self.loaded = self.d / "loaded.json"
        self.qs = self.d / "q.json"
        self.calls = d_calls = self.d / "calls.log"
        self.toml.write_text('enabled_models = ["%s", "%s"]\n' % (A, B))
        self.loaded.write_text(json.dumps({"models": [A, B]}))
        self.qs.write_text(json.dumps({"jobs": []}))
        w = self
        self.enabled = [A, B]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _s(self, c, b):
                r = json.dumps(b).encode(); self.send_response(c)
                self.send_header("Content-Length", str(len(r))); self.end_headers(); self.wfile.write(r)
            def do_GET(self):
                if self.path == "/health": self._s(200, {})
                else: self._s(200, {"data": [{"id": m} for m in w.read_toml()]})
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0)); b = json.loads(self.rfile.read(n))
                self._s(200, {"m": b.get("model")})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        (self.d / "local.json").write_text(json.dumps({"api_key": KEY, "base_url": "http://127.0.0.1:%d/v1" % self.srv.server_port}))
        fb = self.d / "darkbloom"
        failset = fail_set or ""
        fb.write_text('''#!/bin/bash
echo "$@" >> %s
models=(); while [ $# -gt 0 ]; do [ "$1" = "--model" ] && models+=("$2"); shift; done
if [ -n "%s" ] && [[ " ${models[*]} " == *" %s "* ]]; then echo "boom" >&2; exit 1; fi
list=$(printf '"%%s", ' "${models[@]}"); list="[${list%%, }]"
echo "enabled_models = $list" > %s
printf '{"models": %%s}' "$(printf '%%s\\n' "${models[@]:0:2}" | python3 -c 'import sys,json;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')" > %s
''' % (self.calls, failset, failset, self.toml, self.loaded))
        fb.chmod(0o755)
        self.fb = fb

    def read_toml(self):
        import re
        return re.findall(r'"([^"]+)"', self.toml.read_text())

    def run(self, *args):
        env = dict(os.environ, HOME=str(self.d), DBAPPLY_BIN=str(self.fb), DBAPPLY_TOML=str(self.toml),
                   DBAPPLY_LOCAL_JSON=str(self.d / "local.json"), DBAPPLY_LOADED_JSON=str(self.loaded),
                   DBAPPLY_QUEUE_STATE=str(self.qs), DBAPPLY_BACKUPS=str(self.d / "bk"), DBAPPLY_WAIT_S="6", DBAPPLY_POLL_S="0.3")
        r = subprocess.run(["bash", str(SH), *args], env=env, capture_output=True, text=True, timeout=120)
        self.out = r.stdout + r.stderr
        return r

    def starts(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def close(self):
        self.srv.shutdown(); shutil.rmtree(self.d, ignore_errors=True)


w = W()
r = w.run("--models", X)
check("dry run: exit 0, nothing started, plan shown", r.returncode == 0 and w.starts() == [] and "DRY RUN" in w.out, w.out)
r = w.run("--models", X, "--apply")
check("apply: exit 0 and VERIFIED", r.returncode == 0 and "APPLIED + VERIFIED" in w.out, w.out)
check("apply: start was called once with pair + extra + --local-endpoint",
      len(w.starts()) == 1 and all(m in w.starts()[0] for m in (A, B, X)) and "--local-endpoint" in w.starts()[0], w.starts())
check("apply: provider.toml now lists the wide set", set(w.read_toml()) == {A, B, X}, w.read_toml())
check("apply: snapshot written", (Path(w.d / "bk" / "darkbloom-wide-last")).exists())
check("no key in output", KEY not in w.out)
w.close()

w = W()
w.qs.write_text(json.dumps({"jobs": [{"id": "1", "status": "running"}]}))
r = w.run("--models", X, "--apply")
check("running job: REFUSED (exit 1), nothing started", r.returncode == 1 and "REFUSED" in w.out and w.starts() == [], w.out)
w.qs.write_text("{broken")
r = w.run("--models", X, "--apply")
check("unreadable queue state: REFUSED", r.returncode == 1 and w.starts() == [], w.out)
w.close()

w = W(fail_set=X)
r = w.run("--models", X, "--apply")
check("start failure: rolled back to the ORIGINAL set and verified (exit 2)",
      r.returncode == 2 and "ROLLBACK verified" in w.out and set(w.read_toml()) == {A, B}, (r.returncode, w.out))
check("rollback issued a second start with only the original models",
      len(w.starts()) == 2 and X not in w.starts()[1], w.starts())
w.close()

w = W()
w.run("--models", X, "--apply")
r = w.run("--rollback")
check("--rollback restores the last snapshot's filter", r.returncode == 2 or r.returncode == 0, w.out)
check("after --rollback toml == original", set(w.read_toml()) == {A, B}, w.read_toml())
w.close()

w = W()
r = w.run("--apply")
check("no --models: nothing done", r.returncode == 1 and w.starts() == [], w.out)
w.close()
check("the script feeds its program WITHOUT a heredoc (a >512B heredoc deadlocks bash 5.3 when macOS shrinks pipes to 512B)",
      "<<'PY'" not in SH.read_text() and "#@@PYBODY@@" in SH.read_text())
print("ALL PASS" if not FAILS else "FAILED %s" % FAILS)
sys.exit(1 if FAILS else 0)
