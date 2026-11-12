#!/usr/bin/env python3
"""Loopback OpenAI endpoint for the Goose chat front end. ONE backend, chosen explicitly,
hard-bound, no silent fallback.

    goose -> this proxy (127.0.0.1:8011) -> ONE of:
      A) "darkbloom"  http://127.0.0.1:8000/v1/chat/completions   Qwen3.5-9B   (default)
      B) "unraid"     http://198.51.100.145:11434/api/chat            qwen3:14b @ num_ctx 6144

    goose -> ~/bin/goose-queue-mcp.py (stdio MCP) -> ollama-queue.py enqueue -> studio-db

WHY A PROXY AT ALL, AND WHY CHAT IS NOT QUEUED (the owner 2026-10-01)
  Standing rule is that all GPU work goes through ~/bin/ollama-queue.py so the one
  studio-db lane is accounted for and visible. CHAT is the one deliberate exception:
  an earlier build (goose-queue-proxy.py.bak-unraidchat) made every chat turn a queue
  job, which meant the interactive front end queued behind whatever 10-minute coding
  dispatch held the lane. Chat is a human-latency path; it does not get a queue row.
  WORK is unchanged: every investigation, code change and research task still goes
  through the queue -- dispatched FROM the chat by the `queue_dispatch` MCP tool. That
  split is what makes a tiny chat context viable: the model talks and delegates, it does
  not read repos.

BACKEND SELECTION IS EXPLICIT AND FAILS CLOSED
  `GOOSE_CHAT_BACKEND` picks one of BACKENDS. An unset value takes DEFAULT_BACKEND; an
  UNKNOWN value is refused outright at startup and on every request. There is
  deliberately no "try A then fall back to B": a silent fallback is how you end up
  talking to a model at a window nobody measured, which on Unraid means a CUDA OOM on a
  host with no SSH. Each backend's endpoint is a CONSTANT in its class, not an env var,
  so "which host" cannot drift from a stray export -- the tests reach in and set the
  class attribute, which is honest about being a test.

THE GUARDS (revert-tested in test-goose-queue-proxy.py)
  1. TOOL ALLOWLIST (both backends). Goose re-sends every enabled tool's JSON schema on
     EVERY request. Measured on the owner's box: the default profile sent 29 tool definitions
     = 19,235 chars (~5.5k tokens) on top of a 10,306-char system prompt. The goose
     profile is trimmed to just the queue tools, but a profile is a config file that
     drifts, so the proxy enforces the budget independently: any tool whose name is not
     in TOOL_ALLOWLIST is DROPPED, loudly. Dropping beats refusing -- a dropped tool
     degrades to "the model does not offer that verb", a refused request breaks chat
     entirely over a config edit.
  2. CONTEXT CLAMP (unraid backend). Unraid is a 12GB 3080 with ~10.3GB usable and NO
     SSH access, so a bad num_ctx is not a slow turn, it is a CUDA OOM nobody can go and
     fix. num_ctx goes through ollama-worker.clamp_unraid_ctx() against
     UNRAID_CONFIRMED_SAFE_CTX -- the SAME function and table the pregate uses, IMPORTED
     rather than copied so the number cannot drift between the two callers. A model with
     no entry is REFUSED (nobody measured it; guessing is the exact mistake the table
     exists to prevent). If the import itself fails, the clamp drops to the table's floor,
     never to the caller's request. Pinning it also keeps Ollama from reloading the model
     every time chat and the pregate alternate, since a loaded model is keyed by options.
  3. PROMPT CEILING (both backends). A prompt past the backend's measured window is
     refused with a real error instead of being silently truncated FROM THE FRONT, which
     is how a server quietly deletes the system prompt and the tool definitions and
     leaves a model that has forgotten it can dispatch.

WHY NATIVE /api/chat FOR UNRAID BUT PASSTHROUGH FOR DARKBLOOM
  Darkbloom already speaks OpenAI, so its backend forwards the body (and relays SSE
  BYTE-FOR-BYTE -- re-serialising SSE is how passthrough proxies corrupt split tool-call
  arguments). Ollama's OpenAI shim maps only temperature/top_p/seed/stop/max_tokens and
  has NO way to pass num_ctx or keep_alive -- the two settings the Unraid design turns
  on -- so that backend translates to native /api/chat and back.

Run: python3 ~/bin/goose-queue-proxy.py [--port 8011] [--backend darkbloom|unraid]
Started/stopped automatically by ~/bin/goose-darkbloom.sh.
"""
import argparse
import errno
import importlib.util
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("GOOSE_QUEUE_HOME") or Path.home())
BIN = HOME / "bin"
WORKER_SRC = BIN / "ollama-worker.py"

DEFAULT_PORT = int(os.environ.get("GOOSE_QUEUE_PORT", "8011"))
DEFAULT_BACKEND = "darkbloom"

# Only these tools may reach the model (guard 1). Chat's job is to talk and to dispatch;
# everything else is a queue job.
TOOL_ALLOWLIST = {"queue_dispatch", "queue_status", "queue_result"}

MAX_BODY = int(os.environ.get("GOOSE_QUEUE_MAX_BODY", str(32 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("GOOSE_CHAT_TIMEOUT", "900"))
# Transient upstream failures, retried ONLY before the first relayed byte (see _open).
# 429/5xx = shared slots at the cap or a provider restart; a connection error covers
# the ~/.darkbloom/local.json rewrite window on a Darkbloom restart.
RETRY_CODES = (429, 500, 502, 503, 504)
RETRIES = int(os.environ.get("GOOSE_CHAT_RETRIES", "3"))
RETRY_BACKOFF = float(os.environ.get("GOOSE_CHAT_RETRY_BACKOFF", "2"))
_CHUNK = 8192


def log(msg):
    print(f"[goose-chat] {msg}", flush=True)


def _err_body(message, etype="upstream_error", code=None):
    return {"error": {"message": message, "type": etype, "param": None, "code": code}}


class Refused(Exception):
    """A request this proxy will not send upstream. Carries an HTTP status so the client
    sees a real error rather than a hang."""

    def __init__(self, message, status=503, etype="configuration_error"):
        super().__init__(message)
        self.status, self.etype = status, etype


# ---------------------------------------------------- guard 1: tool allowlist + text
def bare_tool_name(name):
    """Goose namespaces every MCP tool as '<extension>__<tool>' -- the slimmed profile's
    queue tools arrive as `queue__queue_dispatch`, NOT `queue_dispatch` (measured in the
    stub capture; matching the bare name alone silently dropped all three and left the
    chat with no way to dispatch at all). So the allowlist is matched against the LAST
    '__'-separated segment. Deliberately narrow: it does not also accept a bare suffix
    match or a prefix, so `developer__shell` and `queue_dispatch_v2` both still fail."""
    return str(name or "").rsplit("__", 1)[-1]


def filter_tools(tools):
    """(kept, dropped-names). See guard 1 in the module docstring."""
    kept, dropped = [], []
    for t in tools or []:
        name = ((t or {}).get("function") or {}).get("name") or (t or {}).get("name")
        if bare_tool_name(name) in TOOL_ALLOWLIST:
            kept.append(t)
        else:
            dropped.append(name or "<unnamed>")
    return kept, dropped


def _text(content):
    """OpenAI content (string, or a list of content parts) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in (None, "text"):
                out.append(p.get("text") or "")
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)
    return "" if content is None else str(content)


def prompt_chars(body):
    """Total characters this request will cost upstream: messages AND tool schemas.

    Tools are counted because they are the thing that actually blew the budget here --
    19,235 chars of them, measured. Counting only messages would have called the
    original profile's request 'small'."""
    n = sum(len(_text(m.get("content"))) + len(json.dumps(m.get("tool_calls") or ""))
            for m in (body.get("messages") or []) if isinstance(m, dict))
    return n + len(json.dumps(body.get("tools") or []))


# ----------------------------------------------- guard 2: Unraid ctx clamp (imported)
_worker = None
_worker_tried = False
# Floor used if ollama-worker.py cannot be imported: the smallest value in its table, so
# a broken import degrades to the SAFEST window rather than to the caller's request.
CLAMP_FALLBACK = 6144


def worker():
    """ollama-worker.py as a module, or None. Imported lazily and once: it is a large
    module (importing it at startup would slow every launch) but it is the single source
    of truth for UNRAID_CONFIRMED_SAFE_CTX and must not be duplicated here."""
    global _worker, _worker_tried
    if _worker_tried:
        return _worker
    _worker_tried = True
    try:
        spec = importlib.util.spec_from_file_location("_ollama_worker_for_chat", WORKER_SRC)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _worker = mod
    except Exception as e:
        log(f"WARNING: could not import {WORKER_SRC} ({type(e).__name__}: {e}); "
            f"clamping to the conservative floor {CLAMP_FALLBACK} instead")
        _worker = None
    return _worker


def safe_ctx_table():
    w = worker()
    return dict(getattr(w, "UNRAID_CONFIRMED_SAFE_CTX", {}) or {}) if w else {}


def resolve_unraid_ctx(url, model, requested):
    """The num_ctx to actually send to Unraid. Raises Refused for an unmeasured model."""
    w = worker()
    if w is None:
        return min(int(requested), CLAMP_FALLBACK)
    table = safe_ctx_table()
    if model not in table:
        raise Refused(f"{model!r} has no recorded confirmed-safe num_ctx for Unraid "
                      f"(UNRAID_CONFIRMED_SAFE_CTX: {sorted(table)}). Refusing rather "
                      f"than guessing a window on a 12GB card with no SSH access.")
    try:
        ctx = int(w.clamp_unraid_ctx(url, model, int(requested)))
    except Exception as e:
        log(f"clamp_unraid_ctx raised ({type(e).__name__}: {e}); using the floor")
        return min(int(requested), CLAMP_FALLBACK)
    # Belt and braces: clamp_unraid_ctx only clamps when `host` matches the CONFIGURED
    # Unraid URL. If that config drifts it returns the request untouched, so re-apply the
    # ceiling here -- this backend only ever talks to Unraid.
    return min(ctx, table[model])


# ============================================================ backend A: Darkbloom
class DarkbloomBackend:
    """Qwen3.5-9B on the local Darkbloom provider. OpenAI in, OpenAI out.

    Darkbloom keeps two models warm (the 35B the queue dispatches to, and this 9B), so
    chat gets a resident model without the Unraid backend's single-slot swap problem and
    without a 6144 ceiling. The Bearer key lives in ~/.darkbloom/local.json and ROTATES
    on every provider restart, so it is re-read PER REQUEST and never logged, printed or
    put in argv.

    The model id is the EXACT Darkbloom id, deliberately NOT run through
    darkbloom_chat.alias_model(): that helper rewrites any ':'-tagged id to the 35B
    default, which would quietly send chat to the big model."""

    name = "darkbloom"
    # Hard-bound: the local provider, nothing else. Taken from Darkbloom's own record so
    # a port change there is picked up, but pinned to loopback below.
    PINNED_HOST = "127.0.0.1"
    model = os.environ.get("GOOSE_CHAT_DARKBLOOM_MODEL", "Qwen3.5-9B")
    # MEASURED, 2026-10-01, via queued ctxprobe-9b-* jobs (needle-in-filler, needle at
    # the start / middle / end, scored on exact recall):
    #   * the provider ACCEPTS and processes at least 149,574 prompt tokens -- no error,
    #     no refusal, and prompt_eval_count scales linearly, so it does NOT truncate;
    #   * exact recall verified at 18,477 / 36,877 / 74,827 prompt tokens, at all three
    #     needle depths.
    #   (An earlier run appeared to fail above ~16k. That was the PROMPT's fault -- the
    #   needle sat on line 1 with the ask only at the very end, so the model answered
    #   with the dominant filler pattern. Restating the ask up front recalled a line-1
    #   needle at 18.5k. Recorded here because it is exactly the kind of result that
    #   gets mistaken for a context ceiling.)
    # So this ceiling is a BUDGET, not the model's limit: ~100k chars (~28.6k tokens)
    # matches GOOSE_CONTEXT_LIMIT in config.yaml and is well inside verified recall. A
    # chat that has grown past it wants restarting, or the long material dispatched.
    # Characters, not tokens: the proxy cannot tokenize, and chars/3.5 is the estimator
    # the rest of this toolchain uses.
    max_prompt_chars = int(os.environ.get("GOOSE_CHAT_MAX_PROMPT_CHARS", "100000"))
    # Thinking arrives INLINE in `content` on this provider (no separate reasoning
    # field) and eats the token budget; darkbloom_chat measured 0.4s vs 4s with it off.
    # An interactive chat wants the latency, not the monologue.
    enable_thinking = os.environ.get("GOOSE_CHAT_THINKING", "0") == "1"

    def __init__(self):
        self._dbk = self._load()

    @staticmethod
    def _load():
        spec = importlib.util.spec_from_file_location("_darkbloom_chat_for_proxy",
                                                      BIN / "darkbloom_chat.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def base_url(self):
        b = self._dbk.base_url()
        if not b:
            raise Refused("darkbloom endpoint not configured (~/.darkbloom/local.json)")
        if self.PINNED_HOST not in b:
            raise Refused(f"darkbloom base_url {b!r} is not on {self.PINNED_HOST}; "
                          f"refusing to send chat off-host")
        return b

    def describe(self):
        try:
            url = self.base_url()
        except Refused as e:
            url = f"UNAVAILABLE: {e}"
        return {"backend": self.name, "upstream": url, "model": self.model,
                "max_prompt_chars": self.max_prompt_chars,
                "enable_thinking": self.enable_thinking}

    def prepare(self, body):
        """(upstream_url, headers, payload, meta). The client's body MOSTLY VERBATIM:
        tools/tool_choice/stream/temperature/response_format must survive untouched or
        goose's tool loop breaks. The rewrites are the model (ours, not the client's) and
        the thinking switch."""
        tools, dropped = filter_tools(body.get("tools"))
        out = dict(body)
        if body.get("tools") is not None:
            out["tools"] = tools
            if not tools:
                out.pop("tools", None)
                out.pop("tool_choice", None)
        out["model"] = self.model
        if not self.enable_thinking:
            kw = dict(out.get("chat_template_kwargs") or {})
            kw["enable_thinking"] = False
            out["chat_template_kwargs"] = kw
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if out.get("stream") else "application/json",
                   # Re-read per request: the key rotates on every provider restart.
                   "Authorization": "Bearer " + str(self._dbk._record().get("api_key") or "")}
        return self.base_url() + "/v1/chat/completions", headers, out, {"dropped": dropped}

    # Darkbloom is already OpenAI-shaped, so both directions are passthrough. SSE is
    # relayed BYTE-FOR-BYTE: re-serialising it is how a proxy corrupts a tool call whose
    # `arguments` arrive a few characters at a time.
    passthrough = True


# =============================================================== backend B: Unraid
class UnraidBackend:
    """qwen3:14b on the Unraid Ollama, num_ctx pinned to the pregate's confirmed-safe
    value so the two never make Ollama reload the model.

    OpenAI in, native /api/chat out: Ollama's OpenAI shim cannot be given num_ctx or
    keep_alive, which are the two settings this backend exists for."""

    name = "unraid"
    # Hard-bound, deliberately NOT env-overridable: this backend exists to put chat on
    # Unraid at a VETTED window, and an env var is how that becomes "whatever host was
    # exported last", with the clamp table then matching the wrong GPU.
    URL = "http://198.51.100.145:11434"
    model = os.environ.get("GOOSE_UNRAID_MODEL", "qwen3:14b")
    # The ASK; resolve_unraid_ctx decides. Set to the pregate's value so the common case
    # is a no-op clamp.
    requested_num_ctx = int(os.environ.get("GOOSE_UNRAID_NUM_CTX", "6144"))
    # Keep the model resident between turns: a reload costs ~10s of a human's attention,
    # and the whole point of a dedicated chat host is that it is warm.
    keep_alive = os.environ.get("GOOSE_UNRAID_KEEP_ALIVE", "24h")
    # qwen3:14b is a thinking model and Ollama emits the monologue into `content`.
    # On a 6144-token window that is ruinous twice over: it eats the answer budget AND
    # the window. Observed in the live smoke 2026-10-01 -- a 24-token reply came back
    # EMPTY with finish_reason "length" because every token went into <think>. Ollama
    # 0.35's top-level `think` flag turns it off; parity with the darkbloom backend's
    # chat_template_kwargs.enable_thinking=false.
    think = os.environ.get("GOOSE_CHAT_THINKING", "0") == "1"
    passthrough = False

    @property
    def num_ctx(self):
        return resolve_unraid_ctx(self.URL, self.model, self.requested_num_ctx)

    @property
    def max_prompt_chars(self):
        # Leave a third of the window for the answer. chars/3.5 is this toolchain's
        # estimator, so the ceiling is (2/3 * num_ctx) tokens expressed in chars.
        return int(self.num_ctx * (2 / 3) * 3.5)

    def describe(self):
        try:
            ctx, why = self.num_ctx, None
        except Refused as e:
            ctx, why = None, str(e)
        return {"backend": self.name, "upstream": self.URL, "model": self.model,
                "num_ctx": ctx, "keep_alive": self.keep_alive, "refused": why,
                "max_prompt_chars": None if why else self.max_prompt_chars}

    # ---- OpenAI -> Ollama ----
    @staticmethod
    def to_ollama_messages(messages):
        """The one real translation is the tool-result role: OpenAI correlates a result
        to its call by `tool_call_id`, Ollama by `tool_name`, so the id->name map is
        built from the assistant turns as we walk forward. Getting this wrong is SILENT
        -- the model just stops being able to see what its tool returned."""
        id_to_name, out = {}, []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role") or "user"
            if role == "assistant":
                msg = {"role": "assistant", "content": _text(m.get("content"))}
                calls = []
                for c in m.get("tool_calls") or []:
                    fn = (c or {}).get("function") or {}
                    name = fn.get("name") or ""
                    if c.get("id"):
                        id_to_name[c["id"]] = name
                    raw = fn.get("arguments")
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw or "{}")
                        except Exception:
                            raw = {"_raw": raw}
                    calls.append({"function": {"name": name, "arguments": raw or {}}})
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool",
                            "tool_name": m.get("name")
                            or id_to_name.get(m.get("tool_call_id"), ""),
                            "content": _text(m.get("content"))})
            else:
                out.append({"role": role, "content": _text(m.get("content"))})
        return out

    def _options(self, body, num_ctx):
        """num_ctx is set LAST and unconditionally: a client-supplied option must never
        be able to widen the window past the clamp."""
        opts = {}
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("seed", "seed"), ("stop", "stop"), ("max_tokens", "num_predict"),
                         ("presence_penalty", "presence_penalty"),
                         ("frequency_penalty", "frequency_penalty")):
            if body.get(src) is not None:
                opts[dst] = body[src]
        opts["num_ctx"] = int(num_ctx)
        return opts

    def prepare(self, body):
        tools, dropped = filter_tools(body.get("tools"))
        ctx = self.num_ctx
        ob = {"model": self.model,
              "messages": self.to_ollama_messages(body.get("messages")),
              "stream": bool(body.get("stream")),
              "keep_alive": self.keep_alive,
              "think": self.think,
              "options": self._options(body, ctx)}
        if tools:
            ob["tools"] = tools
        rf = body.get("response_format") or {}
        if isinstance(rf, dict):
            if rf.get("type") == "json_object":
                ob["format"] = "json"
            elif rf.get("type") == "json_schema":
                schema = (rf.get("json_schema") or {}).get("schema")
                if schema:
                    ob["format"] = schema
        return (self.URL.rstrip("/") + "/api/chat", {"Content-Type": "application/json"},
                ob, {"dropped": dropped, "num_ctx": ctx})

    # ---- Ollama -> OpenAI ----
    @staticmethod
    def _openai_tool_calls(ollama_msg):
        calls = []
        for i, c in enumerate(ollama_msg.get("tool_calls") or []):
            fn = (c or {}).get("function") or {}
            args = fn.get("arguments")
            calls.append({"index": i,
                          "id": f"call_{i}_{int(time.time() * 1000) % 10 ** 9}",
                          "type": "function",
                          "function": {"name": fn.get("name") or "",
                                       # OpenAI's wire format is a STRING, always --
                                       # goose json.loads() it, so a dict breaks the loop.
                                       "arguments": args if isinstance(args, str)
                                       else json.dumps(args or {})}})
        return calls

    _FINISH = {"stop": "stop", "length": "length", "load": "stop", "unload": "stop"}

    @classmethod
    def _finish_reason(cls, rec, had_tool_calls):
        if had_tool_calls:
            return "tool_calls"
        return cls._FINISH.get(rec.get("done_reason") or "stop", "stop")

    def nonstream_response(self, raw):
        rec = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        msg = rec.get("message") or {}
        calls = self._openai_tool_calls(msg)
        out_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            out_msg["tool_calls"] = [{k: v for k, v in c.items() if k != "index"}
                                     for c in calls]
        pe, ec = rec.get("prompt_eval_count") or 0, rec.get("eval_count") or 0
        return {"id": f"chatcmpl-{int(time.time() * 1000)}", "object": "chat.completion",
                "created": int(time.time()), "model": self.model,
                "choices": [{"index": 0, "message": out_msg,
                             "finish_reason": self._finish_reason(rec, bool(calls))}],
                "usage": {"prompt_tokens": pe, "completion_tokens": ec,
                          "total_tokens": pe + ec}}

    @staticmethod
    def new_stream_state():
        return {"first": True, "saw_tool_calls": False}

    def sse_chunks(self, rec, cid, state):
        """Ollama NDJSON record -> the SSE `data:` lines OpenAI clients expect.

        Ollama streams WHOLE parsed tool calls; OpenAI streams `arguments` as text
        fragments. Emitting each Ollama tool call as ONE complete delta is valid OpenAI
        (a fragment may be the whole string) and keeps goose's accumulator correct.

        `state` spans the whole turn because the finish_reason needs it: Ollama puts the
        tool call in a record with done:false and then sends a SEPARATE done:true record
        with no tool_calls on it. Deciding finish_reason from that final record alone
        yields "stop", and an OpenAI client that sees finish_reason "stop" treats the
        turn as plain text and never executes the tool -- a silent dead end. So the flag
        is sticky for the turn."""
        out = []

        def chunk(delta, finish=None):
            return ("data: " + json.dumps(
                {"id": cid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": self.model,
                 "choices": [{"index": 0, "delta": delta,
                              "finish_reason": finish}]}) + "\n\n")

        msg = rec.get("message") or {}
        if state["first"]:
            out.append(chunk({"role": "assistant", "content": ""}))
            state["first"] = False
        calls = self._openai_tool_calls(msg)
        if calls:
            state["saw_tool_calls"] = True
            out.append(chunk({"tool_calls": calls}))
        if msg.get("content"):
            out.append(chunk({"content": msg["content"]}))
        if rec.get("done"):
            out.append(chunk({}, self._finish_reason(rec, state["saw_tool_calls"])))
            out.append("data: [DONE]\n\n")
        return out


BACKENDS = {DarkbloomBackend.name: DarkbloomBackend, UnraidBackend.name: UnraidBackend}


def make_backend(name=None):
    """The ONE backend, chosen explicitly. An unknown name is a hard error: see
    'BACKEND SELECTION IS EXPLICIT AND FAILS CLOSED' in the module docstring."""
    name = (name or os.environ.get("GOOSE_CHAT_BACKEND") or DEFAULT_BACKEND).strip()
    cls = BACKENDS.get(name)
    if cls is None:
        raise Refused(f"unknown chat backend {name!r}; set GOOSE_CHAT_BACKEND to one of "
                      f"{sorted(BACKENDS)}. Refusing to pick one for you.")
    return cls()


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "goose-chat-proxy"
    backend = None        # set by serve()
    backend_error = None  # set by serve() when selection itself failed

    def log_message(self, fmt, *a):
        log(f"{self.command} {self.path} -> {fmt % a}")

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _peer_gone(self):
        try:
            self.connection.setblocking(False)
            try:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            finally:
                self.connection.setblocking(True)
        except BlockingIOError:
            return False
        except OSError as e:
            return e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK)

    def do_GET(self):
        path = self.path.rstrip("/")
        if self.backend_error:
            self._json(503, _err_body(self.backend_error, "configuration_error"))
            return
        if path in ("/v1/models", "/models"):
            # Answered LOCALLY: one hard-bound model. A listing proxied upstream would
            # offer goose every model on the host, including ones with no measured window.
            self._json(200, {"object": "list", "data": [
                {"id": self.backend.model, "object": "model",
                 "owned_by": self.backend.name}]})
        elif path in ("/health", "/healthz"):
            self._json(200, {"ok": True, "tools": sorted(TOOL_ALLOWLIST),
                             **self.backend.describe()})
        else:
            self._json(404, _err_body(f"no route {self.path}", "invalid_request_error"))

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, _err_body(f"no route {self.path}", "invalid_request_error"))
            return
        if self.backend_error:
            self._json(503, _err_body(self.backend_error, "configuration_error"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, _err_body("empty request body", "invalid_request_error"))
            return
        if length > MAX_BODY:
            self._json(413, _err_body(f"request body {length} bytes exceeds the "
                                      f"{MAX_BODY}-byte cap", "invalid_request_error"))
            return
        raw = b""
        while len(raw) < length:
            chunk = self.rfile.read(min(65536, length - len(raw)))
            if not chunk:
                break
            raw += chunk
        try:
            body = json.loads(raw.decode())
            if not isinstance(body, dict):
                raise ValueError("body is not a JSON object")
        except Exception as e:
            self._json(400, _err_body(f"invalid JSON body: {e}", "invalid_request_error"))
            return
        try:
            self._serve(body)
        except Refused as e:
            log(f"REFUSED: {e}")
            self._json(e.status, _err_body(str(e), e.etype))

    def _serve(self, body):
        be = self.backend
        # Guard 3: refuse an over-long prompt rather than let the server truncate it
        # from the FRONT (which silently deletes the system prompt and the tools).
        ceiling = be.max_prompt_chars
        used = prompt_chars(body)
        if ceiling and used > ceiling:
            raise Refused(f"prompt is {used} chars, past this backend's measured ceiling "
                          f"of {ceiling} ({be.name}/{be.model}). Start a new chat or "
                          f"dispatch the long material to the queue instead -- a prompt "
                          f"this size would be truncated from the front, silently "
                          f"dropping the system prompt and the tool definitions.",
                          status=413, etype="invalid_request_error")
        url, headers, payload, meta = be.prepare(body)
        if meta.get("dropped"):
            log(f"TOOL ALLOWLIST dropped {len(meta['dropped'])} tool definition(s) not in "
                f"{sorted(TOOL_ALLOWLIST)}: {meta['dropped']}")
        log(f"turn backend={be.name} model={be.model} stream={bool(payload.get('stream'))} "
            f"prompt_chars={used} (~{int(used / 3.5)} tok) "
            f"tools={len(payload.get('tools') or [])} dropped={len(meta.get('dropped') or [])}"
            + (f" num_ctx={meta['num_ctx']}" if meta.get("num_ctx") else ""))
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers)
        if payload.get("stream"):
            self._stream(req, be)
        else:
            self._once(req, be)

    def _open(self, req):
        """Open the upstream, retrying only TRANSIENT failures and only before any byte
        has been relayed (this is called before the stream starts, so that holds).

        Not speculative hardening -- both of these were observed live on 2026-10-01
        while measuring the Darkbloom backend: a bare HTTP 500 from the provider, and
        `~/.darkbloom/local.json` momentarily unreadable because Darkbloom rewrites it
        (the API key rotates) on every restart. Each failed a whole chat turn that a
        two-second wait would have served. The request is REBUILT per attempt by the
        caller's backend only for the body; the Authorization header was prepared once,
        so a key rotated mid-retry is picked up on the NEXT turn, not this one -- which
        is the conservative direction (a stale key gets one clean 401, not a loop)."""
        last = None
        for attempt in range(RETRIES):
            try:
                return urllib.request.urlopen(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                last = Refused(f"{self.backend.name} HTTP {e.code}: {detail}",
                               status=e.code if 400 <= e.code < 600 else 502,
                               etype="upstream_error")
                if e.code not in RETRY_CODES:
                    raise last
            except Exception as e:
                last = Refused(f"{self.backend.name} unreachable: "
                               f"{type(e).__name__}: {e}",
                               status=502, etype="connection_error")
            if attempt < RETRIES - 1:
                log(f"transient upstream failure ({last}); retry "
                    f"{attempt + 1}/{RETRIES - 1}")
                time.sleep(RETRY_BACKOFF * (attempt + 1))
        raise last

    def _once(self, req, be):
        with self._open(req) as resp:
            raw = resp.read()
        data = raw if be.passthrough else json.dumps(be.nonstream_response(raw)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, req, be):
        resp = self._open(req)
        headers_sent = False
        cid = f"chatcmpl-{int(time.time() * 1000)}"
        state = (be.new_stream_state() if hasattr(be, "new_stream_state") else None)
        try:
            with resp:
                if be.passthrough:
                    # BYTE-FOR-BYTE. Re-serialising SSE is how a proxy corrupts a tool
                    # call whose `arguments` arrive a few characters at a time.
                    while True:
                        chunk = (resp.read1(_CHUNK) if hasattr(resp, "read1")
                                 else resp.read(_CHUNK))
                        if not chunk:
                            break
                        if not headers_sent:
                            self._sse_headers()
                            headers_sent = True
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line.decode())
                    except Exception:
                        continue
                    if rec.get("error"):
                        raise RuntimeError(str(rec["error"]))
                    out = be.sse_chunks(rec, cid, state)
                    if not out:
                        continue
                    if not headers_sent:
                        self._sse_headers()
                        headers_sent = True
                    self.wfile.write("".join(out).encode())
                    self.wfile.flush()
                    if self._peer_gone():
                        log("client disconnected mid-stream; abandoning the turn")
                        return
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")
        except Exception as e:
            msg = f"{be.name} stream failed: {type(e).__name__}: {e}"
            log(msg)
            if not headers_sent:
                self._json(502, _err_body(msg))
            else:
                # Mid-stream the only way to report a failure is in-band, and the client
                # MUST still see [DONE] or it waits out its whole request timeout.
                try:
                    self.wfile.write(b"data: " + json.dumps(_err_body(msg)).encode()
                                     + b"\n\ndata: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass


def serve(port=DEFAULT_PORT, backend=None, ready=None):
    class Bound(Handler):
        pass

    try:
        Bound.backend = make_backend(backend)
        Bound.backend_error = None
    except Refused as e:
        # Still bind the port and answer 503 with the reason: a dead port makes
        # goose-darkbloom.sh report "failed to start" with no explanation.
        Bound.backend, Bound.backend_error = None, str(e)
    # LOOPBACK ONLY, always: this proxy fronts a shared GPU and must never be reachable
    # off-host regardless of what a caller passes.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Bound)
    httpd.daemon_threads = True
    where = f"http://127.0.0.1:{httpd.server_address[1]}"
    if Bound.backend_error:
        log(f"listening on {where} but REFUSING every request: {Bound.backend_error}")
    else:
        log(f"listening on {where}  {Bound.backend.describe()}  "
            f"tools={sorted(TOOL_ALLOWLIST)}")
    if ready is not None:
        ready(httpd)
    httpd.serve_forever()
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--backend", default=None, choices=sorted(BACKENDS) + [None])
    ap.add_argument("--pidfile", default=None)
    args = ap.parse_args(argv)
    if args.pidfile:
        Path(args.pidfile).write_text(str(os.getpid()))
    try:
        serve(args.port, args.backend)
    except KeyboardInterrupt:
        log("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
