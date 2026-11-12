#!/usr/bin/env python3
"""Regate "worker produced no iterations (exit 1)" on the Darkbloom lane (2026-10-05).

ROOT CAUSE (from the livelogs of regate jobs 05cdd18d74e0, 2fe073f6041c, 954d2aeba164,
50e6b9e487b0): Darkbloom 422'd both schema rungs ("Internal inference failure"), the
unconstrained rung's answer was invalid JSON twice (mis-escaped / runaway code inside a
`quote` string), and darkbloom_chat.chat() raised RuntimeError("darkbloom: schema rungs
exhausted ..."). code-review-agent's Model.chat did not catch it -- on the Ollama path
the same situation is a parse failure (chat_json -> None) the stage handles -- so ONE
bad sample crashed the whole review: exit 1, no report.md, no run.json.

Pins (end to end, against a stub Darkbloom on 127.0.0.1 -- HOME is a temp dir, so the
real ~/.darkbloom/local.json is never read):
  A. multi-hunk batch answer unusable, per-hunk answers fine -> exit 0, report.md
     with VERDICT PASS, run.json written, the per-hunk retry happened.
  B. every answer unusable -> exit 0, report.md with VERDICT UNPROVEN (never PASS,
     never a crash); the gate's merge reads it as UNPROVEN -> 'concerns'.
  C. a real transport failure (HTTP 401) still raises / exits non-zero.
  D. _verdict(unreviewed>0) is UNPROVEN; a real defect still FAILs.

Run:    python3 ~/bin/test-review-darkbloom-schema-exhausted.py
Revert: CRA_SRC=~/bin/code-review-agent.py.bak-20261005T205908Z-harnessfix python3 this.py -> FAIL
"""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CRA_SRC") or HERE / "code-review-agent.py")
FAILS = []

DIFF = ("diff --git a/lib/x.py b/lib/x.py\n--- a/lib/x.py\n+++ b/lib/x.py\n"
        "@@ -1,3 +1,3 @@\n def a():\n-    return 1\n+    return 2\n \n"
        "@@ -20,3 +20,3 @@\n def b():\n-    return 3\n+    return 4\n \n")
# What the model produced in 05cdd18d74e0: an unescaped quote inside `quote`.
BAD = '{"findings": [{"quote": "    return "None"", "severity": "high", "claim": "c", "failure_scenario": "f"}]}'
GOOD = '{"findings": []}'


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def serve(mode):
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            user = body["messages"][-1]["content"]
            calls.append({"schema": "response_format" in body,
                          "hunks": user.count("HUNK:")})
            if mode == "auth":
                self.send_response(401); self.end_headers()
                self.wfile.write(b'{"error":"missing or invalid local API key"}')
                return
            if "response_format" in body:
                self.send_response(422); self.end_headers()
                self.wfile.write(b'{"error":{"message":"Internal inference failure."}}')
                return
            content = BAD if (mode == "allbad" or user.count("HUNK:") > 1) else GOOD
            out = json.dumps({"choices": [{"message": {"content": content},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 10}})
            self.send_response(200); self.end_headers()
            self.wfile.write(out.encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def run(mode):
    srv, calls = serve(mode)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        (home / ".darkbloom").mkdir(parents=True)
        (home / ".darkbloom" / "local.json").write_text(
            json.dumps({"api_key": "test-not-a-key", "base_url": base + "/v1"}))
        d = Path(td) / "x.diff"
        d.write_text(DIFF)
        out = Path(td) / "out"
        env = dict(os.environ, HOME=str(home), PYTHONPATH=str(HERE),
                   DARKBLOOM_BASE_URL=base, SLOT_BUSY_WAIT_S="0")
        r = subprocess.run([sys.executable, str(SRC), "--mode", "review", "--diff", str(d),
                            "--out", str(out), "--host", base,
                            "--model", "qwen3.6-35b-a3b-vl-mtp-mxfp8"],
                           capture_output=True, text=True, timeout=300, env=env)
        rep = (out / "report.md").read_text() if (out / "report.md").exists() else None
        rj = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else None
    srv.shutdown()
    m = re.search(r"^## VERDICT: (.+)$", rep or "", re.M)     # gate-on-complete's regex
    return r, (m.group(1).strip() if m else None), rj, calls


def main():
    r, v, rj, calls = run("splitok")
    check("A: one bad multi-hunk answer does not crash the review (exit 0)", r.returncode, 0)
    if r.returncode:
        print(r.stderr[-600:])
    check("A: report.md verdict is PASS", v, "PASS")
    check("A: run.json is written", rj is not None, True)
    check("A: the batch was re-asked one hunk at a time",
          any(c["hunks"] == 1 and not c["schema"] for c in calls), True)
    check("A: split retry recorded in stats",
          ((rj or {}).get("stats") or {}).get("review_split_retry"), 1)

    r, v, rj, calls = run("allbad")
    check("B: every answer unusable -> still exit 0 (not a harness crash)", r.returncode, 0)
    check("B: ...but the verdict is UNPROVEN, never PASS", v and v.startswith("UNPROVEN"), True)
    check("B: run.json counts the unreviewed calls",
          (((rj or {}).get("stats") or {}).get("review_parse_fail") or 0) > 0, True)

    r, v, rj, calls = run("auth")
    check("C: a transport/auth failure still exits non-zero", r.returncode != 0, True)
    check("C: ...and writes no report (no fake verdict)", v, None)

    sys.path.insert(0, str(HERE))
    cra = SourceFileLoader("cra_t", str(SRC)).load_module()
    try:
        got = cra._verdict([], [], unreviewed=2)[0]
    except TypeError:
        got = "no unreviewed parameter"
    check("D: unreviewed calls -> UNPROVEN", got, "UNPROVEN")
    try:
        got = cra._verdict([{"severity": "high"}], [], unreviewed=2)[0]
    except TypeError:
        got = "no unreviewed parameter"
    check("D: a real defect still FAILs with unreviewed calls", got, "FAIL")
    check("D: clean run is still PASS", cra._verdict([], [])[0], "PASS")

    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
