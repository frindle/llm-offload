#!/usr/bin/env python3
"""test-queue-remote-cli.py -- `ollama-queue.py` forwarding to a remote queue API (queue_remote_cli.py in --bin).
A stub HTTP server on this machine's non-loopback address records each request. Proves: with QUEUE_API on a non-loopback
host, resolve/clear-card/cancel/stop/hold/resume/promote/status/results/needs-opus hit the right method + path with the bearer token and never read a
local state file; the subcommands and flag forms without a route (focus, plan-add, resume --force, escalate, ...) and every
`qctl` verb go to POST /api/cli (escalate --gate-output FILE inline) and return the host's rc/stdout/stderr; host-only
subcommands (run, enqueue) exit non-zero with a message; a loopback QUEUE_API (or
OLLAMA_QUEUE_FORCE_LOCAL=1) leaves the local path alone (no request reaches the stub). Fake token only.
Run (sandbox only): python3 tests/test-queue-remote-cli.py [--bin PIPELINE_BIN]"""
import argparse
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
BIN = Path(ap.parse_args().bin)
SEEN = []
RESULT_ROWS = [
    {"id": "aaaa00000001", "label": "fix-one", "verdict": "pass", "status": "done", "model": "m", "host": "h",
     "timestamp": "2026-11-12T11:00:00", "changed_file_count": 1, "gate_json": "/queue-host/logs/aaaa00000001.gate.json"},
    {"id": "bbbb00000002", "label": "fix-two", "verdict": "fail", "status": "done", "model": "m", "host": "h",
     "timestamp": "2026-11-12T10:00:00", "changed_file_count": 2},
]
BODIES = []
CLI_REPLY = {"rc": 7, "stdout": "OUT-from-host\n", "stderr": "ERR-from-host\n", "truncated": False}


class H(http.server.BaseHTTPRequestHandler):
    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        BODIES.append(self.rfile.read(n))
        SEEN.append((self.command, self.path, self.headers.get("Authorization")))
        path = self.path.split("?", 1)[0]
        code = 200
        if self.path == "/api/jobs":
            obj = {"jobs": [{"id": "abc123def456", "status": "pending", "model": "m", "label": "lbl", "group_key": "grp-x"},
                            {"id": "fff000fff000", "status": "pending", "model": "m", "label": "l2", "group_key": "grp-y"}]}
        elif path == "/api/results":
            # unfiltered on purpose: the client applies the same filter again, so its output must still be right
            obj = {"results": RESULT_ROWS}
            if "label=boom" in self.path:
                code, obj = 500, {"error": "boom"}
        elif path == "/api/state":
            obj = {"jobs": [{"id": "abc123def456", "status": "pending", "label": "lbl"},
                            {"id": "0pus00000001", "status": "needs_opus", "label": "parked-one",
                             "escalation": {"category": "gate-split", "reason": "two gates disagree",
                                            "escalated_at": "2026-11-12T10:00:00", "recovery": {"worktree": "/w/x"}}}]}
        elif path == "/api/cli":
            obj = dict(CLI_REPLY)
        else:
            obj = {"ok": True}
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_DELETE = _do

    def log_message(self, *a):
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


IP = lan_ip()
srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
HOME = tempfile.mkdtemp(prefix="qrcli-home-")
FAILS = []


def ok(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        FAILS.append(name)


def run(args, api=None, extra_env=None):
    env = dict(os.environ, HOME=HOME, DISPATCH_VERIFY_SANDBOX="", OLLAMA_QUEUE_NO_NOTIFY="1",
               QUEUE_API=api or "http://%s:%d" % (IP, PORT), QUEUE_API_TOKEN="fake-test-token")
    env.pop("DISPATCH_VERIFY_SANDBOX")
    env.update(extra_env or {})
    del SEEN[:]
    del BODIES[:]
    p = subprocess.run([sys.executable, str(BIN / "ollama-queue.py")] + args, capture_output=True, text=True, env=env,
                       timeout=60, cwd=HOME)
    return p, list(SEEN)


if IP.startswith("127."):
    print("no non-loopback address on this machine; cannot run")
    sys.exit(2)
J = "abc123def456"
for args, want in [(["resolve", J], ("POST", "/api/jobs/%s/resolve" % J)),
                   (["clear-card", J], ("POST", "/api/runs/%s/clear" % J)),
                   (["cancel", J], ("DELETE", "/api/jobs/%s" % J)),
                   (["cancel", J, "--force"], ("DELETE", "/api/jobs/%s?force=1" % J)),
                   (["stop", J], ("POST", "/api/jobs/%s/kill" % J)),
                   (["hold", J], ("POST", "/api/jobs/%s/hold" % J)),
                   (["resume", J], ("POST", "/api/jobs/%s/resume" % J)),
                   (["promote", J], ("POST", "/api/jobs/%s/promote" % J))]:
    p, seen = run(args)
    ok("%s -> %s %s" % (" ".join(args[:1] + args[2:]), *want), p.returncode == 0 and len(seen) == 1
       and seen[0][:2] == want and seen[0][2] == "Bearer fake-test-token", (p.returncode, p.stderr[-200:], seen))
# bundle commands: a job id goes straight to its route; a group key is looked up in /api/jobs first
for args, want in [(["promote-group", J], ("POST", "/api/jobs/%s/promote-group" % J)),
                   (["run-now", J], ("POST", "/api/jobs/%s/run-now" % J))]:
    p, seen = run(args)
    ok("%s %s" % (args[0], "by job id"), p.returncode == 0 and len(seen) == 2 and seen[0][:2] == ("GET", "/api/jobs")
       and seen[1][:2] == want and all(x[2] == "Bearer fake-test-token" for x in seen), (p.returncode, p.stderr[-200:], seen))
p, seen = run(["promote-group", "grp-x"])
ok("promote-group by group key resolves to a member job id", p.returncode == 0 and seen[-1][:2] == (
    "POST", "/api/jobs/%s/promote-group" % J), (p.returncode, p.stderr[-200:], seen))
p, seen = run(["run-now", "grp-x"])
ok("run-now by group key", p.returncode == 0 and seen[-1][:2] == ("POST", "/api/jobs/%s/run-now" % J), seen)
p, seen = run(["move-group", "grp-x", "--before", "grp-y"])
ok("move-group posts id + before_id as JSON", p.returncode == 0 and seen[-1][:2] == ("POST", "/api/jobs/move-group")
   and json.loads(BODIES[-1]) == {"id": J, "before_id": "fff000fff000"}, (p.returncode, p.stderr[-200:], seen, BODIES[-1:]))
p, seen = run(["move-group", J])
ok("move-group without --before sends no before_id", p.returncode == 0 and json.loads(BODIES[-1]) == {"id": J}, BODIES[-1:])
p, seen = run(["promote-group", "nosuch"])
ok("an unknown key is passed on for the server to refuse", p.returncode == 0 and seen[-1][1] == "/api/jobs/nosuch/promote-group", seen)
ok("bundle commands never print the token", "fake-test-token" not in (p.stdout + p.stderr))
p, seen = run(["status"])
ok("status lists rows", p.returncode == 0 and "abc123def456" in p.stdout and seen and seen[0][:2] == ("GET", "/api/jobs"),
   (p.stdout, p.stderr[-200:]))
# results / needs-opus read the queue over its API (the log dir and state file are on the queue host)
p, seen = run(["results"])
ok("results -> GET /api/results?limit=40 with the bearer", p.returncode == 0 and len(seen) == 1
   and seen[0][:2] == ("GET", "/api/results?limit=40") and seen[0][2] == "Bearer fake-test-token", (p.returncode, p.stderr[-200:], seen))
ok("results prints the rows", "aaaa00000001" in p.stdout and "bbbb00000002" in p.stdout and "[PASS" in p.stdout, p.stdout[-300:])
p, seen = run(["results", "--verdict", "pass", "--label", "fix", "--limit", "5"])
ok("results sends verdict/label/limit", p.returncode == 0 and seen and seen[0][1] == "/api/results?verdict=pass&label=fix&limit=5", seen)
ok("results filters again on the client (a FAIL row the server sent is not shown)",
   "aaaa00000001" in p.stdout and "bbbb00000002" not in p.stdout, p.stdout[-300:])
p, seen = run(["results", "--json", "--limit", "0"])
try:
    js = json.loads(p.stdout)
except ValueError:
    js = None
ok("results --json --limit 0 -> no limit sent, the records as JSON", p.returncode == 0 and seen and seen[0][1] == "/api/results"
   and isinstance(js, list) and [r["id"] for r in js] == ["aaaa00000001", "bbbb00000002"], (seen, p.stdout[-200:]))
p, seen = run(["results", "-v"])
ok("results -v says the paths are on the queue host", "paths below are on the queue host" in p.stdout
   and "/queue-host/logs/aaaa00000001.gate.json" in p.stdout, p.stdout[-300:])
p, seen = run(["results", "--label", "boom"])
ok("results: an API error exits non-zero with the status, no token", p.returncode != 0 and "HTTP 500" in p.stderr
   and "fake-test-token" not in (p.stdout + p.stderr), (p.returncode, p.stderr[-200:]))
p, seen = run(["needs-opus"])
ok("needs-opus -> GET /api/state with the bearer, shows the parked job only", p.returncode == 0 and seen
   and seen[0][:2] == ("GET", "/api/state") and seen[0][2] == "Bearer fake-test-token" and "0pus00000001" in p.stdout
   and "abc123def456" not in p.stdout and "two gates disagree" in p.stdout, (p.returncode, p.stderr[-200:], p.stdout[-300:], seen))
p, seen = run(["needs-opus", "--json"])
try:
    js = json.loads(p.stdout)
except ValueError:
    js = None
ok("needs-opus --json", isinstance(js, list) and [r["id"] for r in js] == ["0pus00000001"] and js[0]["category"] == "gate-split",
   p.stdout[-200:])
p, seen = run(["needs-opus"], api="http://%s:%d" % (IP, 1))
ok("needs-opus: API unreachable -> non-zero with a message", p.returncode != 0 and "cannot read the queue state" in p.stderr,
   (p.returncode, p.stderr[-200:]))
os.makedirs(os.path.join(HOME, "bin"), exist_ok=True)   # the local path needs this machine's (empty) queue state dir
for args in (["results"], ["needs-opus"]):
    p, seen = run(args, api="http://127.0.0.1:%d" % PORT)
    ok("%s on a loopback API reads locally (no request)" % args[0], p.returncode == 0 and not seen, (p.returncode, p.stderr[-200:], seen))
    p, seen = run(args, extra_env={"OLLAMA_QUEUE_FORCE_LOCAL": "1"})
    ok("%s with OLLAMA_QUEUE_FORCE_LOCAL=1 reads locally (no request)" % args[0], p.returncode == 0 and not seen,
       (p.returncode, p.stderr[-200:], seen))
for args in (["run"], ["enqueue", "--help-x"], ["enqueue-gpu", "--help-x"]):
    p, seen = run(args)
    ok("refused: " + " ".join(args), p.returncode != 0 and not seen, (p.returncode, p.stderr[-150:], seen))
# everything else the routes cannot carry runs on the queue host: POST /api/cli {tool, argv}, rc/stdout/stderr come back
NOSTATE = os.path.join(HOME, "no-state-dir")
for args in (["focus", J], ["accept-bundle", "grp-x", "--job", J], ["plan-add", "--label", "step-1", "--cwd", "/w/x", "--note", "n; rm -rf /"],
             ["resume", J, "--force"], ["promote", J, "--preempt"], ["promote", J, "--take-focus"],
             ["cancel", J, "--plan"], ["cancel", J, "--automated"], ["stop", J, "--plan"], ["plan-clear"], ["unpin-group"],
             ["bundle", "t1", J, "--clear"], ["nsample", "status"], ["ttc", "status"], ["retag-bundles"]):
    p, seen = run(args, extra_env={"QUEUE_STATE_DIR": NOSTATE})
    body = json.loads(BODIES[0]) if len(BODIES) == 1 else None
    ok("passthrough: " + " ".join(args), p.returncode == 7 and len(seen) == 1 and seen[0][:2] == ("POST", "/api/cli")
       and seen[0][2] == "Bearer fake-test-token" and body == {"tool": "ollama-queue.py", "argv": args},
       (p.returncode, p.stderr[-150:], seen, BODIES))
    ok("passthrough returns the host's stdout/stderr on the matching streams: " + args[0],
       p.stdout == "OUT-from-host\n" and p.stderr == "ERR-from-host\n", (p.stdout, p.stderr[-150:]))
ok("passthrough never opened a local state file", not os.path.exists(NOSTATE))
CLI_REPLY.update(rc=0, stdout="", stderr="", truncated=True)
p, seen = run(["ttc", "status"])
ok("passthrough with rc 0 exits 0 and says the output was cut", p.returncode == 0 and "cut to its last 256 KB" in p.stderr, (p.returncode, p.stderr))
CLI_REPLY.update(rc=7, stdout="OUT-from-host\n", stderr="ERR-from-host\n", truncated=False)
# escalate: --gate-output FILE goes inline (last 4000 chars); a plain string and --cwd stay as typed
GF = os.path.join(HOME, "gate.txt")
with open(GF, "w") as f:
    f.write("HEAD-MARK" + "x" * 5000 + "TAIL-MARK")
for form in (lambda v: ["--gate-output", v], lambda v: ["--gate-output=" + v]):
    p, seen = run(["escalate", J, "--reason", "r", "--cwd", "/w/x"] + form(GF))
    argv = json.loads(BODIES[0])["argv"] if BODIES else []
    gate = [a for a in argv if "TAIL-MARK" in a]
    ok("escalate --gate-output FILE sends the file's last 4000 chars, not the path (%s)" % form("V")[0][:15],
       p.returncode == 7 and len(gate) == 1 and GF not in " ".join(argv) and "HEAD-MARK" not in gate[0]
       and len(gate[0].replace("--gate-output=", "")) == 4000 and "/w/x" in argv, (p.returncode, [a[:60] for a in argv], seen))
p, seen = run(["escalate", J, "--reason", "r", "--gate-output", "inline text, not a file"])
ok("escalate --gate-output with a string stays as typed", json.loads(BODIES[0])["argv"][-1] == "inline text, not a file", BODIES)
p, seen = run(["escalate", J, "--reason", "r", "--gate-output", os.path.join(HOME, "missing.txt")])
ok("escalate --gate-output with a missing path stays as typed", json.loads(BODIES[0])["argv"][-1].endswith("missing.txt"), BODIES)
# qctl: remote -> passthrough; QCTL_FORCE_LOCAL / OLLAMA_QUEUE_FORCE_LOCAL keep it local; a loopback API is local
def qrun(args, api=None, extra_env=None):
    env = dict(os.environ, HOME=HOME, OLLAMA_QUEUE_NO_NOTIFY="1", QUEUE_API=api or "http://%s:%d" % (IP, PORT),
               QUEUE_API_TOKEN="fake-test-token", QUEUE_STATE_DIR=NOSTATE)
    for k in ("QCTL_FORCE_LOCAL", "OLLAMA_QUEUE_FORCE_LOCAL", "QCTL_API", "QCTL_BIN"):
        env.pop(k, None)
    env.update(extra_env or {})
    del SEEN[:]
    del BODIES[:]
    p = subprocess.run([sys.executable, str(BIN / "qctl")] + args, capture_output=True, text=True, env=env, timeout=60, cwd=HOME)
    return p, list(SEEN)
p, seen = qrun(["status", "my-bundle"])
ok("qctl in remote mode -> POST /api/cli {qctl, argv} with the bearer, rc/stdout/stderr as returned",
   p.returncode == 7 and len(seen) == 1 and seen[0][:2] == ("POST", "/api/cli") and seen[0][2] == "Bearer fake-test-token"
   and json.loads(BODIES[0]) == {"tool": "qctl", "argv": ["status", "my-bundle"]}
   and p.stdout == "OUT-from-host\n" and p.stderr == "ERR-from-host\n", (p.returncode, p.stdout, p.stderr[-150:], seen, BODIES))
p, seen = qrun(["close", J, "--accept", "--reason", "done"])
ok("qctl close ... passes every flag through", seen and json.loads(BODIES[0])["argv"] == ["close", J, "--accept", "--reason", "done"], BODIES)
for env in ({"QCTL_FORCE_LOCAL": "1"}, {"OLLAMA_QUEUE_FORCE_LOCAL": "1"}):
    p, seen = qrun(["--help"], extra_env=env)
    ok("qctl with %s stays local (no request)" % list(env)[0], p.returncode == 0 and not seen and "usage" in p.stdout.lower(),
       (p.returncode, p.stderr[-150:], seen))
p, seen = qrun(["--help"], api="http://127.0.0.1:%d" % PORT)
ok("qctl on a loopback API stays local (no request)", p.returncode == 0 and not seen and "usage" in p.stdout.lower(), (p.returncode, seen))
ok("qctl never opened a local state file", not os.path.exists(NOSTATE))
ok("token never printed", "fake-test-token" not in (p.stdout + p.stderr))
p, seen = run(["resolve", J], api="http://127.0.0.1:%d" % PORT)
ok("loopback API keeps the local path (no request)", not seen, seen)
p, seen = run(["clear-card", J], api="http://127.0.0.1:%d" % PORT)
ok("clear-card on a loopback API posts there too (it is API-based in both modes)",
   p.returncode == 0 and seen and seen[0][:2] == ("POST", "/api/runs/%s/clear" % J), (p.returncode, p.stderr[-200:], seen))
p, seen = run(["resolve", J], extra_env={"OLLAMA_QUEUE_FORCE_LOCAL": "1"})
ok("OLLAMA_QUEUE_FORCE_LOCAL=1 keeps the local path", not seen, seen)
print("%d failed" % len(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
