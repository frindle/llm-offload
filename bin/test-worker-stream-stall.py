#!/usr/bin/env python3
"""Streaming STALL guard (ollama-worker.py, 2026-10-10).

Live: Costco job 341135975678 iteration 5 -- a Darkbloom streaming request returned headers
then sat 14+ min with zero chunks; CHAT_TIMEOUT_S (1200s) was the only bound. Now the stream
read has a first-chunk limit (WORKER_STREAM_FIRST_S, default 600) and an inter-chunk idle limit
(WORKER_STREAM_IDLE_S, default 240); a stall closes the connection, logs one
'[worker] STREAM STALL' line and raises the same TimeoutError path (-> OpenAIStreamUnavailable
before the first delta, RuntimeError after) so run_task's existing handling applies.
Any chunk (data / keepalive comment) resets the clock. Local stub only. --revert-check: WORKER_SRC."""
import importlib.util, json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
FAILS = []
MODE = ["stall"]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "stub",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _w(self, text):
        b = text.encode()
        self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = MODE[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        try:
            if mode == "stall":                      # headers, then silence forever
                time.sleep(30)
            elif mode == "midstall":                 # one delta, then silence
                self._w(chunk({"content": "hi"}))
                time.sleep(30)
            elif mode == "slow":                     # a chunk every 1s -- longer than IDLE overall
                for i in range(5):
                    self._w(chunk({"content": f"w{i} "}))
                    time.sleep(1.0)
                self._w(chunk({}, "stop"))
                self._w("data: [DONE]\n\n")
            elif mode == "keepalive":                # only comments/empty deltas for a while, then content
                for _ in range(4):
                    self._w(": keepalive\n\n")
                    time.sleep(1.0)
                self._w(chunk({"content": "ok"}))
                self._w(chunk({}, "stop"))
                self._w("data: [DONE]\n\n")
            elif mode == "slowfirst":                # first chunk later than IDLE but inside FIRST
                time.sleep(2.5)
                self._w(chunk({"content": "ok"}))
                self._w(chunk({}, "stop"))
                self._w("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True


def main():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="stall-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ow_stall", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    logs = []
    m.log = lambda s, *a, **k: logs.append(str(s))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    msgs = [{"role": "user", "content": "go"}]

    def go(mode, idle, first):
        MODE[0] = mode
        os.environ["WORKER_STREAM_IDLE_S"] = str(idle)
        os.environ["WORKER_STREAM_FIRST_S"] = str(first)
        logs.clear()
        t0 = time.time()
        try:
            r = m.call_openai_streaming(host, "stub", msgs, 0.0, 4096, timeout=60, live=None, max_tokens=64)
            exc = None
        except Exception as e:
            r, exc = None, e
        return r, exc, time.time() - t0

    # (a) headers then stall, FIRST=IDLE=2 -> aborted ~2s, pre-first-delta exception type
    r, exc, dt = go("stall", 2, 2)
    check("a: stall aborted within ~limit (not the 60s request timeout)", 1.5 <= dt < 6, True)
    check("a: raises OpenAIStreamUnavailable (run_task falls back non-streaming)",
          type(exc).__name__, "OpenAIStreamUnavailable")
    check("a: one STREAM STALL log line, first_chunk=True",
          [l for l in logs if "STREAM STALL" in l],
          ["[worker] STREAM STALL: no chunk for 2s (first_chunk=True) -- aborting request"])

    # (a2) stall after a delta -> IDLE applies, mid-generation RuntimeError
    r, exc, dt = go("midstall", 2, 30)
    check("a2: mid-stream stall aborted at IDLE (FIRST=30 not used)", 1.5 <= dt < 6, True)
    check("a2: raises RuntimeError mid-generation", type(exc).__name__, "RuntimeError")
    check("a2: STREAM STALL first_chunk=False", any("(first_chunk=False)" in l for l in logs), True)

    # (b) chunk every 1s for 5s with IDLE=2 -> total > IDLE but never idle that long
    r, exc, dt = go("slow", 2, 2)
    check("b: slow stream not aborted", (exc, "STREAM STALL" in "".join(logs)), (None, False))
    check("b: slow stream content complete", (r or {}).get("message", {}).get("content", "").strip(), "w0 w1 w2 w3 w4")
    r, exc, dt = go("keepalive", 2, 2)
    check("b2: keepalive comments reset the clock", (exc, "STREAM STALL" in "".join(logs)), (None, False))

    # (c) first-chunk uses FIRST_S, not IDLE_S
    r, exc, dt = go("slowfirst", 1, 5)
    check("c: first chunk at 2.5s with IDLE=1 FIRST=5 is not aborted", (exc, "STREAM STALL" in "".join(logs)), (None, False))
    r, exc, dt = go("stall", 1, 3)
    check("c: silent first chunk aborts at FIRST (3s), not IDLE (1s)", 2.5 <= dt < 6, True)
    check("c: log names FIRST value", any("no chunk for 3s (first_chunk=True)" in l for l in logs), True)

    # defaults
    for k in ("WORKER_STREAM_IDLE_S", "WORKER_STREAM_FIRST_S"):
        os.environ.pop(k, None)
    check("defaults: FIRST 600 / IDLE 240", m._stream_idle_limits(), (600.0, 240.0))

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
