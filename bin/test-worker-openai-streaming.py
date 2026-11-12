#!/usr/bin/env python3
"""Tests for the Darkbloom / OpenAI-style streaming lane in ollama-worker.py
(call_openai_streaming + OpenAIStreamUnavailable), added 2026-10-01.

Context: every Studio dispatch now runs on local Darkbloom (--api openai), and
--live-log progress/tok-s only ever existed on the native Ollama /api/chat path
(call_ollama_streaming), so the dashboard's per-iteration token line went dead on
the lane that serves all real traffic.

NO network, NO GPU, NO Darkbloom: every check drives the real function against a
local python http.server stub that emits SSE chunks, including the cases that
actually break naive SSE parsers -- a tool call split across chunks (arguments a
few characters at a time), a mid-stream disconnect (truncated chunked body), a
stream that dies before the first delta, and a server that rejects
stream_options.include_usage.

Red-on-revert: drop the per-index tool_call accumulation and the split-tool-call
check goes red; make _fail() always raise RuntimeError and the
before-first-token fallback check goes red.

Run: python3 test-worker-openai-streaming.py
"""
import importlib.util
import json
import os
import tempfile
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "ollama-worker.py"
failures = []
MODE = ["happy"]
REQS = []
SLEEPS = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        failures.append(name)


def _load():
    # SANDBOX: the module computes LOG_DIR and friends from Path.home() at import
    # time and run_task appends to the real vault dispatch log. Point HOME at a
    # temp dir BEFORE import and drop the vault token so this test can never touch
    # the real ledgers (same guard as test-worker-think-cap-recovery.py).
    os.environ["HOME"] = tempfile.mkdtemp(prefix="sselane-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ollama_worker_sselane", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sse(*objs):
    out = []
    for o in objs:
        out.append("data: " + json.dumps(o) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _chunk(delta, finish=None):
    return {"id": "x", "object": "chat.completion.chunk", "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


HAPPY = _sse(
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "**Plan**\n"}),
    _chunk({"content": "line two\n"}),
    _chunk({"content": "line three\n"}, finish="stop"),
    {"choices": [], "usage": {"prompt_tokens": 111, "completion_tokens": 22,
                              "total_tokens": 133}},
)

# A tool call delivered the way real OpenAI-compatible servers deliver it: id and
# name in the first fragment, then `arguments` a few characters at a time, with
# only `index` to tie them together.
SPLIT_TOOL = _sse(
    _chunk({"content": ""}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_abc", "type": "function",
                            "function": {"name": "read_file", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"pa'}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a'}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '/b.py"}'}}]}, finish="tool_calls"),
    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}},
)

REASONING = _sse(
    _chunk({"reasoning_content": "I should check the file first. "}),
    _chunk({"reasoning_content": "Then edit it."}, finish="stop"),
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _stream(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _truncate(self, body, claim_extra=200):
        # Declare more bytes than we send, then slam the socket: the client sees a
        # truncated body (http.client.IncompleteRead), i.e. a real mid-stream
        # disconnect -- a provider restart or a killed slot.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body) + claim_extra))
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
            self.connection.close()
        except Exception:
            pass
        self.close_connection = True

    def _err(self, code, msg):
        body = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        REQS.append({"path": self.path, "auth": self.headers.get("Authorization"),
                     "accept": self.headers.get("Accept"), "payload": payload})
        mode = MODE[0]
        if mode == "happy":
            self._stream(HAPPY)
        elif mode == "split_tool":
            self._stream(SPLIT_TOOL)
        elif mode == "reasoning":
            self._stream(REASONING)
        elif mode == "mid_disconnect":
            # One COMPLETE chunk (so the turn has a first token and real content),
            # then a chunk cut off mid-JSON and the socket dropped -- no
            # finish_reason, no [DONE]. This is what a provider restart looks like.
            first = "data: " + json.dumps(_chunk({"content": "half an ans"})) + "\n\n"
            second = "data: " + json.dumps(_chunk({"content": "wer that must not be acc"}))[:40]
            self._truncate((first + second).encode())
        elif mode == "early_disconnect":
            self._truncate(b"")
        elif mode == "done_only":
            self._stream(b"data: [DONE]\n\n")
        elif mode == "no_stream_options":
            if "stream_options" in payload:
                self._err(400, "unknown field: stream_options")
            else:
                self._stream(_sse(_chunk({"content": "ok then\n"}, finish="stop")))
        elif mode == "always_500":
            self._err(500, "boom")
        elif mode == "always_503":
            self._err(503, "all slots busy")
        else:
            self._err(500, f"unknown mode {mode}")


def main():
    m = _load()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"

    live_path = Path(os.environ["HOME"]) / "live.log"
    live = m.LiveLog(live_path, "sse-test", "stub", host)
    msgs = [{"role": "user", "content": "do the thing"}]

    def call(**kw):
        kw.setdefault("live", live)
        return m.call_openai_streaming(host, "stub", msgs, 0.0, 8192, timeout=20, **kw)

    print("== happy path: content deltas + include_usage ==")
    MODE[0] = "happy"
    REQS.clear()
    r = call()
    ok("content accumulated in order",
       r["message"]["content"] == "**Plan**\nline two\nline three\n", r["message"])
    ok("role preserved", r["message"]["role"] == "assistant")
    ok("real usage passed through",
       r["usage"] == {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133},
       r["usage"])
    ok("finish_reason surfaced as done_reason", r["done_reason"] == "stop", r.get("done_reason"))
    ok("posts to /v1/chat/completions", REQS[-1]["path"] == "/v1/chat/completions")
    ok("stream:true sent", REQS[-1]["payload"].get("stream") is True)
    ok("stream_options.include_usage sent",
       REQS[-1]["payload"].get("stream_options") == {"include_usage": True})
    ok("tools sent by default (same as the non-streaming openai path)",
       isinstance(REQS[-1]["payload"].get("tools"), list) and REQS[-1]["payload"]["tools"])
    ok("max_tokens + repeat_penalty sent like the non-streaming path",
       REQS[-1]["payload"].get("max_tokens") == m.DEFAULT_MAX_TOKENS
       and REQS[-1]["payload"].get("repeat_penalty") == m.DEFAULT_REPEAT_PENALTY,
       REQS[-1]["payload"])
    call(tools=False)
    ok("tools=False omits tools (manual-tools parity)", "tools" not in REQS[-1]["payload"],
       list(REQS[-1]["payload"]))

    print("== live-log lines the dashboard/humans read ==")
    text = live_path.read_text()
    ok("writing phase line emitted", "writing..." in text)
    ok("done summary line emitted", "] done —" in text, text[-300:])
    ok("rate file written", (Path.home() / "qwen-rate-sse-test.txt").exists())

    print("== tool call split across chunks ==")
    MODE[0] = "split_tool"
    r = call()
    tcs = r["message"]["tool_calls"]
    ok("exactly one tool call", len(tcs) == 1, tcs)
    ok("name not duplicated/garbled", tcs[0]["function"]["name"] == "read_file", tcs[0])
    ok("id preserved", tcs[0]["id"] == "call_abc", tcs[0])
    ok("arguments reassembled into valid JSON",
       json.loads(tcs[0]["function"]["arguments"]) == {"path": "a/b.py"},
       tcs[0]["function"]["arguments"])
    ok("content empty on a pure tool turn", r["message"]["content"] == "")

    print("== reasoning_content / --preserve-reasoning ==")
    MODE[0] = "reasoning"
    r = call(preserve_reasoning=True)
    ok("reasoning captured as thinking",
       r["message"].get("thinking", "").startswith("I should check"), r["message"])
    ok("reasoning promoted to content on an otherwise-empty turn",
       r["message"]["content"].startswith("I should check"), r["message"])
    ok("usage estimated when the server sent none",
       r["usage"]["completion_tokens"] > 0 and r["usage"]["prompt_tokens"] > 0, r["usage"])
    r2 = call(preserve_reasoning=False)
    ok("without --preserve-reasoning the turn keeps no thinking key",
       "thinking" not in r2["message"], r2["message"])

    print("== mid-stream disconnect AFTER the first token: real failure, no fallback ==")
    MODE[0] = "mid_disconnect"
    try:
        call()
        ok("raises", False, "no exception")
    except m.OpenAIStreamUnavailable as e:
        ok("raises RuntimeError not OpenAIStreamUnavailable", False, repr(e))
    except RuntimeError as e:
        ok("raises RuntimeError (run_task's chat-failure path)", "mid-generation" in str(e), str(e))
        ok("a truncated body is not accepted as a short clean answer",
           "truncated body" in str(e), str(e))

    print("== stream dies BEFORE the first token: recoverable, falls back ==")
    MODE[0] = "early_disconnect"
    try:
        call()
        ok("raises", False, "no exception")
    except m.OpenAIStreamUnavailable as e:
        ok("raises OpenAIStreamUnavailable", "before first token" in str(e), str(e))
    except Exception as e:
        ok("raises OpenAIStreamUnavailable", False, repr(e))

    print("== [DONE] with no deltas at all: recoverable ==")
    MODE[0] = "done_only"
    try:
        call()
        ok("raises", False, "no exception")
    except m.OpenAIStreamUnavailable as e:
        ok("empty stream is recoverable, not an empty answer", "without producing" in str(e), str(e))
    except Exception as e:
        ok("empty stream is recoverable", False, repr(e))

    print("== server rejects stream_options.include_usage ==")
    MODE[0] = "no_stream_options"
    REQS.clear()
    r = call()
    ok("downgraded and retried without stream_options",
       len(REQS) == 2 and "stream_options" in REQS[0]["payload"]
       and "stream_options" not in REQS[1]["payload"], [list(q["payload"]) for q in REQS])
    ok("content still returned after the downgrade", r["message"]["content"] == "ok then\n")
    ok("counts estimated instead of reported", r["usage"]["completion_tokens"] > 0, r["usage"])

    print("== connect failure exhausts CHAT_RETRIES then falls back ==")
    MODE[0] = "always_500"
    REQS.clear()
    try:
        call()
        ok("raises", False, "no exception")
    except m.OpenAIStreamUnavailable as e:
        ok("OpenAIStreamUnavailable after retries", "connect failed after retries" in str(e), str(e))
    except Exception as e:
        ok("OpenAIStreamUnavailable after retries", False, repr(e))
    ok("attempted exactly CHAT_RETRIES+1 times", len(REQS) == m.CHAT_RETRIES + 1, len(REQS))

    print("== 503 backs off (shared Darkbloom slots) ==")
    MODE[0] = "always_503"
    REQS.clear()
    SLEEPS.clear()
    _real_sleep = _time.sleep
    _time.sleep = lambda s: SLEEPS.append(s)
    try:
        try:
            call()
        except Exception:
            pass
    finally:
        _time.sleep = _real_sleep
    ok("slept between 503 retries", len(SLEEPS) == m.CHAT_RETRIES and all(s > 0 for s in SLEEPS),
       SLEEPS)
    ok("the stream_options downgrade did not eat the retry budget (503 body has no such field)",
       len(REQS) == m.CHAT_RETRIES + 1, len(REQS))

    print("== Darkbloom key is read per attempt and only for the matching host ==")
    ok("no Authorization sent to a non-Darkbloom host", REQS[-1]["auth"] is None, REQS[-1]["auth"])
    db = Path.home() / ".darkbloom"
    db.mkdir(parents=True, exist_ok=True)
    (db / "local.json").write_text(json.dumps({"base_url": host + "/v1", "api_key": "k1"}))
    MODE[0] = "happy"
    REQS.clear()
    call()
    ok("Authorization: Bearer from ~/.darkbloom/local.json", REQS[-1]["auth"] == "Bearer k1",
       REQS[-1]["auth"])
    (db / "local.json").write_text(json.dumps({"base_url": host + "/v1", "api_key": "k2"}))
    REQS.clear()
    call()
    ok("rotated key picked up on the next call (read at call time, never cached)",
       REQS[-1]["auth"] == "Bearer k2", REQS[-1]["auth"])
    ok("Accept: text/event-stream sent", REQS[-1]["accept"] == "text/event-stream")

    print("== mid-generation SIGTERM pause aborts the stream ==")
    MODE[0] = "happy"
    m._sigterm_pause_requested = True
    try:
        call()
        ok("raises ChatAbortedForPause", False, "no exception")
    except m.ChatAbortedForPause:
        ok("raises ChatAbortedForPause", True)
    except Exception as e:
        ok("raises ChatAbortedForPause", False, repr(e))
    finally:
        m._sigterm_pause_requested = False

    live.close()
    srv.shutdown()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
