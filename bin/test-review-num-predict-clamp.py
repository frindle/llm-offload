#!/usr/bin/env python3
"""code-review-agent must never ask Ollama for more output than the window has left.

Root cause (2026-10-02, gate patch-B measurements): at the pregate tier (Unraid
qwen3:14b, num_ctx 6144) every reviewer call sends a ~3k-token prompt with
num_predict 8000 (review/removal) or 4000 (verify). prompt + num_predict can never
fit, so a long answer makes Ollama context-shift and silently drop the start of
the prompt. The fix clamps num_predict on the Ollama path to
num_ctx - estimated_prompt - safety, floored at MIN_NUM_PREDICT.

Checks run against a stub /api/chat server that records the request body.
Usage: CRA=<code-review-agent.py> python3 this.py      (--revert-check: mutants must go RED)
"""
import http.server, importlib.util, json, os, subprocess, sys, tempfile, threading
from importlib.machinery import SourceFileLoader
from pathlib import Path

CRA = Path(os.environ.get("CRA") or Path(__file__).resolve().parent / "code-review-agent.py")
sys.path.insert(0, str(Path.home() / "bin"))
FAILS = []
SEEN = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        out = json.dumps({"message": {"content": "{}"}, "done_reason": "stop",
                          "prompt_eval_count": 10, "eval_count": 5}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def load():
    loader = SourceFileLoader("cra_npc", str(CRA))
    spec = importlib.util.spec_from_loader("cra_npc", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    cra = load()
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    sys_p = "S" * 2000
    user = "U" * 10500          # ~3.6k tokens at 3.5 chars/token, like the real pregate prompts

    def sent(num_ctx, num_predict, schema=None, u=user):
        SEEN.clear()
        cra.Model(host, "stub", num_ctx).chat(sys_p, u, schema=schema, num_predict=num_predict)
        return SEEN[-1]["options"]["num_predict"]

    est = (len(sys_p) + len(user)) / 4.07          # the MEASURED Unraid chars/token
    np6144 = sent(6144, 8000)
    chk("pregate 6144, review 8000: prompt + num_predict fits the window",
        est + np6144 <= 6144, True)
    chk("pregate 6144, review 8000: still leaves a real answer budget (>= 1500)", np6144 >= 1500, True)
    chk("pregate 6144, verify 4000: clamped below 4000", sent(6144, 4000) < 4000, True)
    chk("regate 32768: the full 8000 is sent unchanged", sent(32768, 8000), 8000)
    chk("small request under the room is never raised", sent(6144, 300), 300)
    big_schema = {"type": "object", "properties": {f"k{i}": {"type": "string"} for i in range(200)}}
    chk("the JSON schema Ollama injects counts against the window",
        sent(6144, 8000, schema=big_schema) < np6144, True)
    chk("an already-overflowing prompt floors at MIN_NUM_PREDICT, never 0/negative",
        sent(6144, 8000, u="U" * 40000), cra.MIN_NUM_PREDICT)
    chk("num_ctx missing -> request passed through", cra.clamp_num_predict(8000, 0, "a", "b"), 8000)
    usage = cra.Model(host, "stub", 6144)
    usage.chat(sys_p, user, num_predict=8000)
    chk("call_usage records the CLAMPED num_predict (what was actually asked)",
        usage.usage[-1]["num_predict"] < 8000, True)
    srv.shutdown()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("no clamp", "        num_predict = clamp_num_predict(num_predict, self.num_ctx, system, user, schema)\n",
     ""),
    ("schema ignored", "        chars += len(json.dumps(schema))\n", "        pass\n"),
    ("no floor", "    return max(min(int(num_predict), room), min(int(num_predict), MIN_NUM_PREDICT))",
     "    return min(int(num_predict), room)"),
    ("no safety margin", "    room = int(num_ctx) - est - CTX_SAFETY_TOKENS", "    room = int(num_ctx) - est // 2"),
]


def revert_check():
    src, bad = CRA.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "CRA": f.name},
                           capture_output=True, text=True, timeout=120)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
