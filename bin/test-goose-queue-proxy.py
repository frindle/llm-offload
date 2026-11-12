#!/usr/bin/env python3
"""Tests for the Goose chat front end:
  ~/bin/goose-queue-proxy.py   the switchable, hard-bound chat proxy (darkbloom|unraid)
  ~/bin/goose-queue-mcp.py     the 3-tool queue MCP server (the chat's whole tool surface)
  ~/bin/goose-chat-runner.py   the queue runner that serves one chat completion on a lane

NO network beyond loopback, NO GPU, NO real Darkbloom, NO real Ollama, NO real queue:
  * the RUNNER is driven against a local http.server stub standing in for Darkbloom's
    /v1/chat/completions -- including the cases that break naive passthrough proxies
    (a tool call split across several SSE writes, a non-stream response, a hard HTTP
    error, a key that rotates between calls);
  * the PROXY is driven over real loopback HTTP, once per backend, with that backend's
    upstream pointed at a stub (Darkbloom's OpenAI shape, or Ollama's NDJSON shape);
  * the MCP server is driven both in-process (with `_run` recording the argv it would
    have handed ollama-queue.py) and once as a real subprocess over stdio, so the
    JSON-RPC framing itself is covered.

GUARDS, each with a revert-proof in REVERTS below (`--selfcheck-reverts` patches the
proxy, re-runs this file against the patched copy, and insists the named check goes RED):
  GUARD 1  tool allowlist      -- drop every tool definition that is not a queue tool
  GUARD 2  unraid ctx clamp    -- never send a num_ctx above UNRAID_CONFIRMED_SAFE_CTX
  GUARD 3  unmeasured model    -- refuse a model with no recorded safe window on Unraid
  GUARD 4  prompt ceiling      -- refuse an over-long prompt instead of being truncated
  GUARD 5  backend selection   -- refuse an unknown backend; never silently fall back

Run: python3 test-goose-queue-proxy.py
     python3 test-goose-queue-proxy.py --selfcheck-reverts   (proves the guards bite)
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIN = Path(__file__).resolve().parent
RUNNER_SRC = BIN / "goose-chat-runner.py"
PROXY_SRC = Path(os.environ.get("GOOSE_QUEUE_PROXY_MODULE") or (BIN / "goose-queue-proxy.py"))
MCP_SRC = BIN / "goose-queue-mcp.py"

failures = []
UPSTREAM = {"mode": "stream", "reqs": []}


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        failures.append(name)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ stub payloads
# A tool-call stream, deliberately chopped so `arguments` arrives a few characters at a
# time across SSE events: the shape that a proxy which re-serialises SSE corrupts.
TOOL_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "queue__queue_dispatch", "arguments": ""}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{\"ta"}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "sk\": \"look\"}"}}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
]
TEXT_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
    {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
    {"choices": [{"index": 0, "delta": {"content": " world"}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]
NONSTREAM = {"id": "cmpl-1", "object": "chat.completion",
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "hi"}}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}

# Ollama native /api/chat shapes (what the unraid backend has to translate).
OLLAMA_NDJSON = [
    {"model": "qwen3:14b", "message": {"role": "assistant", "content": "hel"}, "done": False},
    {"model": "qwen3:14b", "message": {"role": "assistant", "content": "lo"}, "done": False},
    {"model": "qwen3:14b", "message": {"role": "assistant", "content": ""},
     "done": True, "done_reason": "stop", "prompt_eval_count": 11, "eval_count": 2},
]
OLLAMA_NDJSON_TOOLS = [
    {"model": "qwen3:14b", "message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "queue__queue_dispatch",
                      "arguments": {"task": "look at the thing", "label": "thing"}}}]},
     "done": False},
    {"model": "qwen3:14b", "message": {"role": "assistant", "content": ""},
     "done": True, "done_reason": "stop", "prompt_eval_count": 40, "eval_count": 9},
]
OLLAMA_ONCE = {"model": "qwen3:14b", "done": True, "done_reason": "stop",
               "prompt_eval_count": 7, "eval_count": 2,
               "message": {"role": "assistant", "content": "hi there"}}


def sse_bytes(chunks):
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) \
        + b"data: [DONE]\n\n"


class Upstream(BaseHTTPRequestHandler):
    """Stands in for BOTH upstreams; UPSTREAM['mode'] picks the shape."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _chunked(self, payloads):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for raw in payloads:
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()
            time.sleep(0.01)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _json(self, obj, status=200):
        msg = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        UPSTREAM["reqs"].append({"body": body, "auth": self.headers.get("Authorization"),
                                 "accept": self.headers.get("Accept"), "path": self.path})
        mode = UPSTREAM["mode"]
        if mode == "http400":
            self._json({"error": {"message": "bad things"}}, 400)
        elif mode == "nonstream":
            self._json(NONSTREAM)
        elif mode == "ollama_once":
            self._json(OLLAMA_ONCE)
        elif mode in ("ollama_stream", "ollama_stream_tools"):
            recs = OLLAMA_NDJSON_TOOLS if mode.endswith("tools") else OLLAMA_NDJSON
            self._chunked([json.dumps(r).encode() + b"\n" for r in recs])
        else:
            chunks = TOOL_CHUNKS if mode == "tools" else TEXT_CHUNKS
            self._chunked([b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks]
                          + [b"data: [DONE]\n\n"])


def start_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# --------------------------------------------------------------------------- runner
def test_runner(tmp):
    print("== runner: forwards the body verbatim and streams to disk ==")
    srv, base = start_upstream()

    m = _load(RUNNER_SRC, "goose_chat_runner_t")
    keys = ["k1"]
    m._dbk.base_url = lambda: base
    m._dbk._record = lambda: {"api_key": keys[0], "base_url": base}

    def call(body, mode):
        UPSTREAM["mode"] = mode
        d = Path(tempfile.mkdtemp(dir=tmp))
        (d / "request.json").write_text(json.dumps(body))
        rc = m.main(["--model", "qwen3.8:27b-q4_K_M", "--host", base,
                     "--num-ctx", "32768", "--cwd", str(d),
                     "--task-file", str(d / "request.json")])
        return rc, d

    req = {"model": "qwen3.8:27b-q4_K_M", "stream": True, "temperature": 0.2,
           "messages": [{"role": "user", "content": "hi there"}],
           "tools": [{"type": "function", "function": {"name": "shell",
                                                       "parameters": {"type": "object"}}}],
           "tool_choice": "auto"}
    rc, d = call(req, "stream")
    sent = UPSTREAM["reqs"][-1]["body"]
    ok("runner stream: rc=0", rc == 0, rc)
    ok("runner stream: stream.sse holds the upstream bytes verbatim",
       (d / m.STREAM_FILE).read_bytes() == sse_bytes(TEXT_CHUNKS))
    done = json.loads((d / m.DONE_FILE).read_text())
    ok("runner stream: done.json marks ok + stream",
       done.get("ok") and done.get("stream") is True, done)
    ok("runner body forwarded verbatim: tools/tool_choice/stream/temperature survive",
       (sent.get("tools") == req["tools"] and sent.get("tool_choice") == "auto"
        and sent.get("stream") is True and sent.get("temperature") == 0.2), sent)
    ok("runner: legacy ollama tag is aliased to the darkbloom model id",
       ":" not in sent.get("model", ":"), sent.get("model"))
    ok("runner: Accept: text/event-stream on a streaming request",
       UPSTREAM["reqs"][-1]["accept"] == "text/event-stream")
    ok("runner: auth is a Bearer key read from the darkbloom record",
       UPSTREAM["reqs"][-1]["auth"] == "Bearer k1", UPSTREAM["reqs"][-1]["auth"])

    print("== runner: tool-call chunks pass through byte-for-byte ==")
    rc, d = call(dict(req), "tools")
    ok("runner tool-call stream: rc=0", rc == 0, rc)
    ok("runner tool-call stream: split `arguments` deltas are byte-identical",
       (d / m.STREAM_FILE).read_bytes() == sse_bytes(TOOL_CHUNKS))

    print("== runner: non-stream relays the JSON ==")
    rc, d = call({"model": "x", "messages": [{"role": "user", "content": "hi"}]}, "nonstream")
    ok("runner non-stream: rc=0", rc == 0, rc)
    ok("runner non-stream: response.json is the upstream body",
       json.loads((d / m.RESPONSE_FILE).read_text()) == NONSTREAM)
    ok("runner non-stream: no stream.sse written", not (d / m.STREAM_FILE).exists())

    print("== runner: rotating provider key is re-read per request ==")
    keys[0] = "k2"
    rc, d = call({"model": "x", "messages": []}, "nonstream")
    ok("runner: rotated key picked up on the next request (never cached)",
       UPSTREAM["reqs"][-1]["auth"] == "Bearer k2", UPSTREAM["reqs"][-1]["auth"])

    print("== runner: upstream failure becomes an OpenAI-shaped error, not a hang ==")
    rc, d = call({"model": "x", "messages": []}, "http400")
    ok("runner error: rc=1", rc == 1, rc)
    err = json.loads((d / m.ERROR_FILE).read_text())
    ok("runner error: OpenAI-shaped {error:{message,type}}",
       isinstance(err.get("error"), dict) and err["error"].get("message")
       and err["error"].get("type"), err)
    ok("runner error: carries the upstream status for the proxy to pass on",
       err.get("_status") == 400, err.get("_status"))
    ok("runner error: done.json written with ok=false (so nothing waits forever)",
       json.loads((d / m.DONE_FILE).read_text()).get("ok") is False)

    print("== runner: unreadable request body fails closed ==")
    d = Path(tempfile.mkdtemp(dir=tmp))
    (d / "request.json").write_text("not json")
    rc = m.main(["--cwd", str(d), "--task-file", str(d / "request.json")])
    ok("runner bad body: rc=1 + error.json + done.json", rc == 1
       and (d / m.ERROR_FILE).is_file() and (d / m.DONE_FILE).is_file())
    srv.shutdown()


# ---------------------------------------------------------------------- proxy rig
class ProxyFixture:
    """The proxy serving ONE backend on a real loopback port, with that backend's
    upstream repointed at the stub."""

    def __init__(self, backend, upstream_base, **patch):
        self.m = _load(PROXY_SRC, f"goose_chat_proxy_{backend}_{os.getpid()}_"
                                  f"{int(time.time() * 1000) % 10 ** 6}")
        self.logs = []
        self.m.log = lambda msg: self.logs.append(str(msg))
        ready = threading.Event()

        def _ready(httpd):
            self.httpd = httpd
            ready.set()

        threading.Thread(target=lambda: self.m.serve(0, backend, _ready),
                         daemon=True).start()
        ready.wait(60)
        self.handler = self.httpd.RequestHandlerClass
        self.backend = self.handler.backend
        if backend == "unraid":
            # Repoint the hard-bound URL at the stub. Deliberately a MODULE/instance
            # attribute, not an env var: see the proxy's docstring.
            self.backend.URL = upstream_base
        else:
            self.backend._dbk.base_url = lambda: upstream_base
            self.backend._dbk._record = lambda: {"api_key": "stubkey",
                                                 "base_url": upstream_base}
            # PINNED_HOST must still match, and the stub IS on loopback.
        for k, v in patch.items():
            setattr(self.backend, k, v)
        self.port = self.httpd.server_address[1]

    def post(self, body, headers=None):
        payload = json.dumps(body).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=60)
        head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n")
        for k, v in (headers or {}).items():
            head += f"{k}: {v}\r\n"
        s.sendall(head.encode() + b"\r\n" + payload)
        return s

    def get(self, path):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        buf = _read_until(s, lambda b: b"\r\n\r\n" in b and b.count(b"}") > 0)
        s.close()
        return buf

    def stop(self):
        self.httpd.shutdown()


def _read_until(sock, pred, deadline=30):
    buf = b""
    end = time.time() + deadline
    sock.settimeout(1.0)
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            if pred(buf):
                return buf
            continue
        if not chunk:
            break
        buf += chunk
        if pred(buf):
            return buf
    return buf


def _body_of(buf):
    return buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""


# Goose namespaces MCP tools as '<extension>__<tool>' -- the allowlist must match THAT.
QUEUE_TOOLS = [{"type": "function", "function": {
    "name": f"queue__{n}", "parameters": {"type": "object"}}}
    for n in ("queue_dispatch", "queue_status", "queue_result")]
FAT_TOOLS = [{"type": "function", "function": {
    "name": n, "parameters": {"type": "object", "description": "x" * 400}}}
    for n in ("shell", "write", "edit", "developer__shell", "queue_dispatch_v2",
              "megamemory-vault__understand")]


# ------------------------------------------------------- proxy: darkbloom backend
def test_proxy_darkbloom():
    print("== proxy/darkbloom: selection, passthrough, allowlist, ceiling ==")
    srv, base = start_upstream()
    f = ProxyFixture("darkbloom", base)

    ok("default backend is darkbloom (chosen, not guessed)",
       f.m.DEFAULT_BACKEND == "darkbloom" and f.backend.name == "darkbloom",
       f.backend.name)

    UPSTREAM["mode"] = "stream"
    UPSTREAM["reqs"].clear()
    s = f.post({"model": "whatever-goose-thinks", "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
                "tools": QUEUE_TOOLS + FAT_TOOLS, "tool_choice": "auto"})
    buf = _read_until(s, lambda b: b"[DONE]" in b)
    s.close()
    sent = UPSTREAM["reqs"][-1]["body"]
    ok("darkbloom: 200 text/event-stream",
       b"200" in buf.split(b"\r\n")[0] and b"text/event-stream" in buf, buf[:120])
    ok("darkbloom: SSE relayed BYTE-FOR-BYTE (split tool args cannot be corrupted)",
       sse_bytes(TEXT_CHUNKS) in _body_of(buf), _body_of(buf)[:200])
    # ---- GUARD 1
    names = [t["function"]["name"] for t in sent.get("tools") or []]
    ok("GUARD 1 non-allowlisted tool definitions are dropped before the model sees them",
       sorted(names) == sorted(t["function"]["name"] for t in QUEUE_TOOLS), names)
    ok("GUARD 1 goose's '<extension>__<tool>' namespacing still matches the allowlist",
       all(n.startswith("queue__") for n in names) and len(names) == 3, names)
    ok("GUARD 1 the drop is LOGGED, not silent",
       any("TOOL ALLOWLIST dropped" in ln for ln in f.logs), f.logs[-3:])
    ok("darkbloom: the model id is OURS, not the client's",
       sent.get("model") == f.backend.model, sent.get("model"))
    ok("darkbloom: thinking disabled by default (it is inline and eats the budget)",
       (sent.get("chat_template_kwargs") or {}).get("enable_thinking") is False,
       sent.get("chat_template_kwargs"))
    ok("darkbloom: auth is a Bearer key from the rotating provider record",
       UPSTREAM["reqs"][-1]["auth"] == "Bearer stubkey", UPSTREAM["reqs"][-1]["auth"])
    ok("darkbloom: the provider key is never written to the proxy log",
       not any("stubkey" in ln for ln in f.logs), [ln for ln in f.logs if "stubkey" in ln])

    print("== proxy/darkbloom: every tool dropped => tools/tool_choice removed ==")
    UPSTREAM["reqs"].clear()
    s = f.post({"stream": True, "messages": [{"role": "user", "content": "hi"}],
                "tools": FAT_TOOLS, "tool_choice": "auto"})
    _read_until(s, lambda b: b"[DONE]" in b)
    s.close()
    sent = UPSTREAM["reqs"][-1]["body"]
    ok("an empty tool list is omitted entirely (not sent as [])",
       "tools" not in sent and "tool_choice" not in sent, sent.keys())

    print("== proxy/darkbloom: non-stream passthrough ==")
    UPSTREAM["mode"] = "nonstream"
    s = f.post({"messages": [{"role": "user", "content": "hi"}]})
    buf = _read_until(s, lambda b: b"finish_reason" in b)
    s.close()
    ok("non-stream: the upstream completion is relayed unchanged",
       json.loads(_body_of(buf).decode()) == NONSTREAM, _body_of(buf)[:200])

    print("== proxy/darkbloom: GUARD 4 prompt ceiling ==")
    f.backend.max_prompt_chars = 500
    s = f.post({"messages": [{"role": "user", "content": "x" * 2000}]})
    # Wait for the BODY, not just the status line: `b"413" in b` matches the status line
    # on the first packet and would assert against headers that have no body yet.
    buf = _read_until(s, lambda b: b"ceiling" in b)
    s.close()
    ok("GUARD 4 an over-long prompt is refused with 413, not silently truncated",
       b"413" in buf.split(b"\r\n")[0] and b"ceiling" in buf, buf[:200])
    print("   (the ceiling counts TOOL SCHEMAS too -- they were the thing that blew it)")
    ok("GUARD 4 prompt_chars() counts tool schemas, not just messages",
       f.m.prompt_chars({"messages": [{"role": "user", "content": "hi"}],
                         "tools": FAT_TOOLS}) > 2000,
       f.m.prompt_chars({"messages": [], "tools": FAT_TOOLS}))
    f.backend.max_prompt_chars = 100000

    print("== proxy/darkbloom: metadata answered locally, bad routes refused ==")
    buf = f.get("/v1/models")
    ok("/v1/models is answered LOCALLY with the one hard-bound model",
       json.loads(_body_of(buf).decode())["data"][0]["id"] == f.backend.model,
       _body_of(buf)[:200])
    buf = f.get("/health")
    h = json.loads(_body_of(buf).decode())
    ok("/health names the backend, the upstream and the tool allowlist",
       h.get("backend") == "darkbloom" and h.get("upstream") == base
       and sorted(h.get("tools") or []) == sorted(f.m.TOOL_ALLOWLIST), h)
    buf = f.get("/v1/embeddings")
    ok("an unknown route is a 404, not a proxied request",
       b"404" in buf.split(b"\r\n")[0], buf[:80])

    print("== proxy/darkbloom: upstream failure is an error, never a hang ==")
    UPSTREAM["mode"] = "http400"
    s = f.post({"messages": []})
    # Body, not status line: `b"400" in b` would match the status line on packet one.
    buf = _read_until(s, lambda b: b"bad things" in b)
    s.close()
    ok("upstream 400 -> a real HTTP 400 with the upstream detail",
       b"400" in buf.split(b"\r\n")[0] and b"bad things" in buf, buf[:200])
    f.stop()
    srv.shutdown()


# ---------------------------------------------------------- proxy: unraid backend
def test_proxy_unraid():
    print("== proxy/unraid: ctx clamp, keep_alive, OpenAI<->Ollama translation ==")
    srv, base = start_upstream()
    f = ProxyFixture("unraid", base)
    real_url = f.m.UnraidBackend.URL  # before the fixture repointed the instance

    # ---- GUARD 2 / GUARD 3, against the REAL Unraid URL and the REAL imported table
    table = f.m.safe_ctx_table()
    ok("the clamp table comes from ollama-worker.py (imported, not copied)",
       table.get("qwen3:14b") == 6144 and table.get("qwen3.5:9b") == 12288, table)
    ok("GUARD 2 a huge request is clamped to the model's confirmed-safe ceiling",
       f.m.resolve_unraid_ctx(real_url, "qwen3:14b", 65536) == 6144,
       f.m.resolve_unraid_ctx(real_url, "qwen3:14b", 65536))
    ok("GUARD 2 a request BELOW the ceiling is left alone (a clamp, not a floor)",
       f.m.resolve_unraid_ctx(real_url, "qwen3:14b", 4096) == 4096)
    refused = None
    try:
        f.m.resolve_unraid_ctx(real_url, "gpt-oss:20b", 8192)
    except f.m.Refused as e:
        refused = str(e)
    ok("GUARD 3 a model with no recorded safe window is REFUSED, not guessed at",
       refused and "no recorded confirmed-safe" in refused, refused)

    UPSTREAM["mode"] = "ollama_stream"
    UPSTREAM["reqs"].clear()
    s = f.post({"model": "whatever", "stream": True, "temperature": 0.3,
                "max_tokens": 128, "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": [{"type": "text", "text": "hi "},
                                                 {"type": "text", "text": "there"}]},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "queue__queue_status", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "pending=0"}],
                "tools": QUEUE_TOOLS + FAT_TOOLS})
    buf = _read_until(s, lambda b: b"[DONE]" in b)
    s.close()
    sent = UPSTREAM["reqs"][-1]["body"]
    ok("unraid: native /api/chat is used (the OpenAI shim cannot carry num_ctx)",
       UPSTREAM["reqs"][-1]["path"].endswith("/api/chat"), UPSTREAM["reqs"][-1]["path"])
    ok("GUARD 2 num_ctx on the wire is the pinned, clamped value",
       (sent.get("options") or {}).get("num_ctx") == 6144, sent.get("options"))
    ok("unraid: keep_alive is long, so the model stays resident between turns",
       sent.get("keep_alive") == f.backend.keep_alive, sent.get("keep_alive"))
    # Caught in the live smoke: with thinking ON, a 24-token reply came back EMPTY
    # (finish_reason "length") because every token went into <think>.
    ok("unraid: thinking is off by default (it burns both the answer and the window)",
       sent.get("think") is False, sent.get("think"))
    ok("unraid: openai params are mapped into options (max_tokens -> num_predict)",
       (sent["options"].get("num_predict") == 128
        and sent["options"].get("temperature") == 0.3), sent["options"])
    ok("GUARD 1 non-allowlisted tools are dropped on this backend too",
       [t["function"]["name"] for t in sent.get("tools") or []]
       == [t["function"]["name"] for t in QUEUE_TOOLS],
       [t["function"]["name"] for t in sent.get("tools") or []])
    msgs = sent.get("messages") or []
    ok("unraid: content PARTS are flattened to a string",
       msgs[1]["content"] == "hi there", msgs[1])
    ok("unraid: a tool result is correlated by tool_name (OpenAI uses tool_call_id)",
       msgs[3]["role"] == "tool" and msgs[3]["tool_name"] == "queue__queue_status", msgs[3])
    ok("unraid: assistant tool_calls get PARSED arguments (Ollama wants an object)",
       msgs[2]["tool_calls"][0]["function"]["arguments"] == {}, msgs[2])
    body = _body_of(buf)
    ok("unraid: Ollama NDJSON is translated into OpenAI SSE chunks",
       b'"chat.completion.chunk"' in body and b'"content": "hel"' in body
       and b"data: [DONE]" in body, body[:300])
    ok("unraid: a client-supplied num_ctx cannot widen the window past the clamp",
       (f.backend.prepare({"messages": [], "options": {"num_ctx": 99999},
                           "num_ctx": 99999})[2]["options"]["num_ctx"]) == 6144)

    print("== proxy/unraid: tool calls become OpenAI deltas with STRING arguments ==")
    UPSTREAM["mode"] = "ollama_stream_tools"
    s = f.post({"stream": True, "messages": [{"role": "user", "content": "go"}],
                "tools": QUEUE_TOOLS})
    buf = _read_until(s, lambda b: b"[DONE]" in b)
    s.close()
    deltas = [json.loads(ln[len("data: "):])
              for ln in _body_of(buf).decode().splitlines()
              if ln.startswith("data: ") and ln != "data: [DONE]"]
    calls = [c for d in deltas for c in (d["choices"][0]["delta"].get("tool_calls") or [])]
    ok("a tool call survives the translation", len(calls) == 1, calls)
    args = calls[0]["function"]["arguments"] if calls else None
    ok("`arguments` is a JSON STRING on the wire (goose json.loads() it)",
       isinstance(args, str) and json.loads(args).get("task") == "look at the thing", args)
    ok("the tool call carries an id and type=function",
       calls and calls[0].get("id") and calls[0].get("type") == "function", calls)
    fins = [d["choices"][0].get("finish_reason") for d in deltas]
    # Ollama puts the tool call in a done:false record and the finish in a SEPARATE
    # done:true record with no tool_calls on it. Deciding from the final record alone
    # gives "stop", and a client that sees "stop" never executes the tool. Caught live
    # by this check on 2026-10-01.
    ok("finish_reason is tool_calls even though the final Ollama record has none",
       "tool_calls" in fins and "stop" not in fins, fins)
    ok("the assistant role opener is emitted exactly once for the whole stream",
       sum(1 for d in deltas if (d["choices"][0]["delta"] or {}).get("role")) == 1,
       [d["choices"][0]["delta"] for d in deltas])

    print("== proxy/unraid: non-stream translation ==")
    UPSTREAM["mode"] = "ollama_once"
    s = f.post({"messages": [{"role": "user", "content": "hi"}]})
    buf = _read_until(s, lambda b: b"finish_reason" in b)
    s.close()
    out = json.loads(_body_of(buf).decode())
    ok("non-stream: an OpenAI chat.completion is built from Ollama's object",
       (out["object"] == "chat.completion"
        and out["choices"][0]["message"]["content"] == "hi there"
        and out["choices"][0]["finish_reason"] == "stop"), out)
    ok("non-stream: usage is carried over from prompt_eval_count/eval_count",
       out["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
       out["usage"])

    print("== proxy/unraid: the window is the ceiling for the prompt too ==")
    ok("GUARD 4 max_prompt_chars is derived from the pinned window, not a guess",
       0 < f.backend.max_prompt_chars < 6144 * 3.5, f.backend.max_prompt_chars)
    f.stop()
    srv.shutdown()


# ------------------------------------------------------------- backend selection
def test_backend_selection():
    print("== GUARD 5 backend selection is explicit and fails closed ==")
    m = _load(PROXY_SRC, f"goose_chat_proxy_sel_{int(time.time() * 1000) % 10 ** 6}")
    ok("both backends are offered", sorted(m.BACKENDS) == ["darkbloom", "unraid"],
       sorted(m.BACKENDS))
    refused = None
    try:
        m.make_backend("studio")
    except m.Refused as e:
        refused = str(e)
    ok("GUARD 5 an unknown backend is REFUSED (never a silent fallback)",
       refused and "unknown chat backend" in refused, refused)
    ok("an explicit choice is honoured", m.make_backend("unraid").name == "unraid")
    os.environ["GOOSE_CHAT_BACKEND"] = "unraid"
    try:
        ok("GOOSE_CHAT_BACKEND selects the backend when no flag is passed",
           m.make_backend().name == "unraid")
    finally:
        os.environ.pop("GOOSE_CHAT_BACKEND", None)
    ok("no flag and no env -> the documented default, not a guess",
       m.make_backend().name == m.DEFAULT_BACKEND)
    ok("each backend's endpoint is a CONSTANT, not read from the environment",
       m.UnraidBackend.URL == "http://198.51.100.145:11434", m.UnraidBackend.URL)
    # A bad selection must still bind a port and explain itself, or goose-darkbloom.sh
    # just reports "failed to start" with no reason.
    ready = threading.Event()
    box = {}

    def _ready(httpd):
        box["httpd"] = httpd
        ready.set()

    threading.Thread(target=lambda: m.serve(0, "nope", _ready), daemon=True).start()
    ready.wait(30)
    h = box["httpd"]
    s = socket.create_connection(("127.0.0.1", h.server_address[1]), timeout=10)
    s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    buf = _read_until(s, lambda b: b"unknown chat backend" in b, deadline=10)
    s.close()
    ok("GUARD 5 a bad selection serves 503 WITH THE REASON (not a dead port)",
       b"503" in buf.split(b"\r\n")[0] and b"unknown chat backend" in buf, buf[:200])
    h.shutdown()


# ---------------------------------------------------------------- the MCP server
def test_mcp(tmp):
    print("== mcp: the chat's entire tool surface ==")
    m = _load(MCP_SRC, "goose_queue_mcp_t")
    m.SPOOL = Path(tempfile.mkdtemp(dir=tmp, prefix="spool-"))
    calls = []
    reply = {"rc": 0, "out": "enqueued abc123def456  goose-x  model=m  host=studio-db\n",
             "err": ""}

    def fake_run(args):
        calls.append(list(args))
        return reply["rc"], reply["out"], reply["err"]

    m._run = fake_run

    ok("exactly three tools, no more", [t["name"] for t in m.TOOLS]
       == ["queue_dispatch", "queue_status", "queue_result"], [t["name"] for t in m.TOOLS])
    init = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}})
    ok("initialize echoes the CLIENT's protocol version (forward/backward compatible)",
       init["result"]["protocolVersion"] == "2024-11-05", init)
    ok("a notification (no id) gets NO reply",
       m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
    ok("tools/list returns the three schemas",
       len(m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
           ["result"]["tools"]) == 3)

    def call(name, args):
        return m.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})["result"]

    r = call("queue_dispatch", {"task": "find out why X fails", "label": "why x"})
    argv = calls[-1]
    ok("queue_dispatch enqueues on the studio-db lane as task-kind research",
       ("--host" in argv and argv[argv.index("--host") + 1] == "studio-db"
        and argv[argv.index("--task-kind") + 1] == "research"), argv)
    ok("queue_dispatch writes the task to a FILE (never argv)",
       Path(argv[argv.index("--task-file") + 1]).read_text() == "find out why X fails")
    ok("queue_dispatch reports the job id back, short",
       "abc123def456" in r["content"][0]["text"] and not r["isError"], r)
    ok("a dispatch with no repo is unisolated-by-admission",
       "--allow-unisolated" in argv and "--cwd" in argv, argv)
    call("queue_dispatch", {"task": "fix the thing", "repo": "/Users/user/x"})
    argv = calls[-1]
    ok("a dispatch WITH a repo uses --repo (the queue's enforced worktree isolation)",
       "--repo" in argv and "--cwd" not in argv and "--allow-unisolated" not in argv, argv)
    r = call("queue_dispatch", {"task": "   "})
    ok("an empty task is refused as an isError result, not enqueued",
       r["isError"] and "refused" in r["content"][0]["text"], r)

    reply["out"] = ""
    reply["rc"] = 1
    reply["err"] = "REFUSING enqueue: nope"
    r = call("queue_dispatch", {"task": "x"})
    ok("an enqueue refusal is surfaced as isError with the queue's reason",
       r["isError"] and "nope" in r["content"][0]["text"], r)

    reply["rc"] = 0
    reply["err"] = ""
    reply["out"] = ("[running ] aaaaaaaaaaaa  live-one   model=m\n"
                    "[pending ] bbbbbbbbbbbb  next-one   model=m\n"
                    + "".join(f"[done    ] cccccccccc{i:02d}  old-{i}  model=m\n"
                              for i in range(300)))
    r = call("queue_status", {})
    text = r["content"][0]["text"]
    ok("queue_status shows the LIVE rows only (not 300 retained done rows)",
       "live-one" in text and "next-one" in text and "old-0" not in text, text[:200])
    ok("queue_status counts the rest instead of listing it",
       "done=300" in text, text[:120])
    ok("every reply is clipped to the chat's tiny budget",
       len(text) <= m.MAX_REPLY + 60, len(text))

    reply["out"] = json.dumps([
        {"id": "abc123def456", "label": "goose-why-x", "status": "done", "verdict": "PASS",
         "exit_code": 0, "answer": "Because the nudge test asserts on a stale fixture."},
        {"id": "999999999999", "label": "other", "status": "failed", "exit_code": 1,
         "failure_detail": "boom"}])
    r = call("queue_result", {"job": "abc123"})
    ok("queue_result finds a job by id prefix and reports its answer",
       "stale fixture" in r["content"][0]["text"], r["content"][0]["text"][:200])
    r = call("queue_result", {"job": "other"})
    ok("queue_result also matches on a label substring",
       "boom" in r["content"][0]["text"], r["content"][0]["text"][:200])
    r = call("queue_result", {"job": "nothing-like-this"})
    ok("an unmatched job says so (and is NOT an error -- it may still be running)",
       not r["isError"] and "no finished job" in r["content"][0]["text"], r)

    long = "y" * 20000
    reply["out"] = json.dumps([{"id": "abc123def456", "label": "l", "status": "done",
                               "exit_code": 0, "answer": long}])
    t = call("queue_result", {"job": "abc123"})["content"][0]["text"]
    ok("a huge answer is clipped AND says it was clipped (never silently truncated)",
       len(t) <= m.MAX_REPLY + 60 and "clipped" in t, len(t))

    err = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "shell", "arguments": {}}})
    ok("an unknown tool is a JSON-RPC error (the server offers nothing else)",
       err.get("error", {}).get("code") == -32602, err)

    def boom(_a):
        raise RuntimeError("kaboom")

    m.HANDLERS["queue_status"] = boom
    r = call("queue_status", {})
    ok("a tool crash is an isError RESULT, not a dead MCP server",
       r["isError"] and "kaboom" in r["content"][0]["text"], r)

    print("== mcp: real stdio round-trip (JSON-RPC framing) ==")
    p = subprocess.run([sys.executable, str(MCP_SRC)], input=(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    ), capture_output=True, text=True, timeout=120,
        env=dict(os.environ, GOOSE_QUEUE_SPOOL=str(m.SPOOL)))
    lines = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    ok("stdio: one JSON object per line, and nothing for the notification",
       [r.get("id") for r in lines] == [1, 2], p.stdout[:300])
    ok("stdio: logs go to stderr, never polluting the JSON-RPC channel",
       "goose-queue-mcp" in p.stderr and "goose-queue-mcp" not in p.stdout)
    ok("stdio: tools/list over the wire returns the three tools",
       len(lines[1]["result"]["tools"]) == 3, lines[1])


# ------------------------------------------------------------------ token budget
def test_tool_budget():
    print("== budget: the whole point of the slim profile ==")
    m = _load(MCP_SRC, "goose_queue_mcp_budget")
    # On the wire goose wraps each tool as {"type":"function","function":{...}} and
    # namespaces the name, so measure THAT, not the bare TOOLS list.
    wire = [{"type": "function", "function": {
        "name": f"queue__{t['name']}", "description": t["description"],
        "parameters": t["inputSchema"]}} for t in m.TOOLS]
    chars = len(json.dumps(wire))
    print(f"   3 queue tools on the wire: {chars} chars (~{chars / 3.5:.0f} tok)")
    print("   measured baselines (stub capture, goose 1.50.0 on this box):")
    print("     default profile: 28,784 chars total / 19,235 of tool schemas (29 tools)")
    print("     slim profile:     3,138 chars total /  1,062 of tool schemas  (3 tools)")
    # A ceiling, not an equality: descriptions may be reworded, but this budget is paid
    # on EVERY turn, so growth has to be deliberate enough to come and change this number.
    ok("the queue tool schemas stay under 2,000 chars (~570 tok) on the wire",
       chars < 2000, chars)
    ok("every allowlisted tool actually exists in the MCP server (no dead entries)",
       {t["name"] for t in m.TOOLS} == set(
           _load(PROXY_SRC, "goose_chat_proxy_budget").TOOL_ALLOWLIST),
       {t["name"] for t in m.TOOLS})


# ------------------------------------------------------------------- revert proofs
REVERTS = [
    ("GUARD 1 tool allowlist",
     r"if bare_tool_name\(name\) in TOOL_ALLOWLIST:",
     "if True:  # GUARD 1 REVERTED",
     "non-allowlisted tool definitions are dropped before the model sees them"),
    ("GUARD 2 unraid ctx clamp",
     r"return min\(ctx, table\[model\]\)",
     "return int(requested)  # GUARD 2 REVERTED",
     "a huge request is clamped to the model's confirmed-safe ceiling"),
    ("GUARD 3 unmeasured model refused",
     r"raise Refused\(f\"\{model!r\} has no recorded confirmed-safe num_ctx",
     "pass  # GUARD 3 REVERTED\n        raise Refused(f\"unused {model!r}",
     "a model with no recorded safe window is REFUSED"),
    ("GUARD 4 prompt ceiling",
     r"if ceiling and used > ceiling:",
     "if False:  # GUARD 4 REVERTED",
     "an over-long prompt is refused with 413"),
    ("GUARD 5 backend selection fails closed",
     r"raise Refused\(f\"unknown chat backend",
     "cls = BACKENDS[DEFAULT_BACKEND]  # GUARD 5 REVERTED\n        return cls()\n        raise Refused(f\"unknown chat backend",
     "an unknown backend is REFUSED"),
]


def selfcheck_reverts():
    src = PROXY_SRC.read_text()
    rc_all = 0
    for name, pat, repl, expect in REVERTS:
        patched, n = re.subn(pat, repl, src, count=1)
        if n != 1:
            print(f"REVERT SETUP FAILED for {name}: pattern not found")
            rc_all = 1
            continue
        tmpf = Path(tempfile.mkdtemp()) / "goose-queue-proxy.py"
        tmpf.write_text(patched)
        env = dict(os.environ, GOOSE_QUEUE_PROXY_MODULE=str(tmpf))
        p = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                           capture_output=True, text=True, env=env, timeout=1200)
        red = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("FAIL")]
        hit = any(expect in ln for ln in red)
        print(f"  {'ok  ' if hit else 'FAIL'} revert {name}: expected check goes red "
              f"({len(red)} red)")
        if not hit:
            print("    red checks were:", red or "(none)")
            rc_all = 1
    return rc_all


def main():
    if "--selfcheck-reverts" in sys.argv:
        raise SystemExit(selfcheck_reverts())
    tmp = tempfile.mkdtemp(prefix="goose-chat-test-")
    try:
        test_runner(tmp)
        print()
        test_proxy_darkbloom()
        print()
        test_proxy_unraid()
        print()
        test_backend_selection()
        print()
        test_mcp(tmp)
        print()
        test_tool_budget()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
