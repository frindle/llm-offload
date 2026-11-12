#!/usr/bin/env python3
"""Model-profile CONFORMANCE: the JSON body we actually put on the wire must equal the
model card's profile (model_profiles.yaml), for every served model x role x path.

  * A stub HTTP server captures real request bodies.
  * Senders exercised (the REAL code, not mocks of it):
      ollama-worker.py  call_ollama (openai + native), call_openai_streaming,
                        call_ollama_streaming, and a full `ollama-worker.py` subprocess
                        (proves the CLI defaults resolve from the profile, not constants)
      darkbloom_chat.py chat()
  * Expected values are read straight from the raw YAML (yaml.safe_load), NOT via
    model_profile.build_request_fields, so the builder is checked, not trusted.
  * Also asserts: explicit CLI/caller overrides win; the forbidden legacy values
    (temperature 0/0.15, max_tokens 8192, repetition_penalty 1.1) never leak into a
    profiled model; the qwen3.8 alias is declared, logged once, lane-scoped.
  * --drift: every profile cites a card that exists and whose sha256 matches.

Env: WORKER_SRC / DBC_SRC / MP_SRC / PROFILES_SRC point at alternative sources (used by
the canary's --prove red-on-revert mutations). Exit 0 = conformant.
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE_SRC") or Path(__file__).resolve().parent / "ollama-queue.py")
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
DBC = Path(os.environ.get("DBC_SRC") or HERE / "darkbloom_chat.py")
MP = Path(os.environ.get("MP_SRC") or HERE / "model_profile.py")
PROFILES = Path(os.environ.get("PROFILES_SRC") or HERE / "model_profiles.yaml")
FAILS = []
NCHECK = [0]

os.environ["MODEL_PROFILES_PATH"] = str(PROFILES)
import pwd
os.environ["MODEL_CARDS_ROOT"] = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Desktop/GitHub Projects/references")
os.environ["HOME"] = tempfile.mkdtemp(prefix="mpconf-home-")   # no real Darkbloom key
os.environ.pop("VAULT_TOKEN_FILE", None)
# NOTHINK_RUN is DEFAULT ON for the author role (2026-10-15), which forces enable_thinking=false and so changes the
# author body away from the card's thinking profile. Every section below asserts the PROFILE itself (card -> body), so it
# runs with the lever explicitly off; run_nothink_default() asserts the default-on behaviour and the off switch.
os.environ["NOTHINK_RUN"] = "0"

# Put the (possibly mutated) model_profile where the worker/dbc will import it from:
# they do sys.path.insert(0, <their own dir>), so for a mutated copy we pre-seed
# sys.modules instead.
sys.path.insert(0, str(HERE))


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mp = _load(MP, "model_profile")        # the worker's `import model_profile` reuses this
import yaml  # noqa: E402

RAW = yaml.safe_load(PROFILES.read_text())


def check(name, got, want):
    NCHECK[0] += 1
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"\n      got  {got!r}\n      want {want!r}"))


# ------------------------------------------------------------------ stub server
SENT = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json({"data": [{"id": m, "object": "model"} for m in RAW["models"]]})
        if self.path.startswith("/api/tags"):
            return self._json({"models": [{"name": m, "model": m, "size": 1} for m in RAW["models"]]})
        return self._json({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SENT.append({"path": self.path, "body": body})
        if self.path == "/v1/chat/completions":
            if body.get("stream"):
                data = (b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
                        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
                        b'data: [DONE]\n\n')
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json({"choices": [{"message": {"role": "assistant", "content": "hi"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        if self.path == "/api/chat":
            if body.get("stream"):
                data = (json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": False}) + "\n" +
                        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                    "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 1}) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json({"message": {"role": "assistant", "content": "hi"}, "done": True,
                               "prompt_eval_count": 3, "eval_count": 1})
        return self._json({})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOST = f"http://127.0.0.1:{srv.server_address[1]}"


def take():
    out = list(SENT)
    SENT.clear()
    return out


# ------------------------------------------------------------------ expectations
def raw_mode(model, role):
    ent = RAW["models"][model]
    return ent["modes"][RAW["roles"][role]]


def expect_openai(model, role, overrides=None, think=None):
    m = dict(raw_mode(model, role))
    for k, v in (overrides or {}).items():
        if v is not None:
            m[k] = v
    f = {}
    for k in ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"):
        if m.get(k) is not None:
            f[k] = m[k]
    if "repetition_penalty" in f:
        f["repeat_penalty"] = f["repetition_penalty"]
    if m.get("max_tokens") is not None:
        f["max_tokens"] = m["max_tokens"]
    if m.get("stop"):
        f["stop"] = m["stop"]
    th = think if think is not None else m.get("enable_thinking")
    ctk = {}
    if th is not None:
        ctk["enable_thinking"] = bool(th)
    if m.get("preserve_thinking") is not None and th:
        ctk["preserve_thinking"] = bool(m["preserve_thinking"])
    if ctk:
        f["chat_template_kwargs"] = ctk
    return f


def expect_ollama(model, role, overrides=None, think=None):
    m = dict(raw_mode(model, role))
    for k, v in (overrides or {}).items():
        if v is not None:
            m[k] = v
    o = {}
    for k in ("temperature", "top_p", "top_k", "min_p", "presence_penalty"):
        if m.get(k) is not None:
            o[k] = m[k]
    if m.get("repetition_penalty") is not None:
        o["repeat_penalty"] = m["repetition_penalty"]
    o["num_ctx"] = m["ctx"] if "num_ctx" not in (overrides or {}) else overrides["num_ctx"]
    if m.get("max_tokens") is not None:
        o["num_predict"] = m["max_tokens"]
    if m.get("stop"):
        o["stop"] = m["stop"]
    f = {"options": o}
    th = think if think is not None else m.get("enable_thinking")
    if th is not None:
        f["think"] = bool(th)
    return f


OPENAI_STRUCT = {"model", "messages", "stream", "tools", "stream_options"}
OLLAMA_STRUCT = {"model", "messages", "stream", "tools", "options", "think"}


def sampling_openai(body):
    return {k: v for k, v in body.items() if k not in OPENAI_STRUCT}


def sampling_ollama(body):
    return {k: v for k, v in body.items() if k in ("options", "think")}


MODELS = list(RAW["models"])
ROLES = ["author", "review", "non_thinking"]


def run_matrix(w, dbc):
    msgs = [{"role": "user", "content": "x"}]
    for model in MODELS:
        for role in ROLES:
            tag = f"{model}/{role}"
            # worker, OpenAI non-streaming
            take()
            w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role=role)
            b = take()[-1]["body"]
            check(f"worker openai   {tag}", sampling_openai(b), expect_openai(model, role))
            # worker, OpenAI streaming
            take()
            w.call_openai_streaming(HOST, model, msgs, None, None, tools=False, role=role)
            b = take()[-1]["body"]
            check(f"worker openai-sse {tag}", sampling_openai(b), expect_openai(model, role))
            # worker, native Ollama non-streaming
            take()
            w.call_ollama(HOST, model, msgs, None, None, api_style="ollama", tools=False, role=role)
            b = take()[-1]["body"]
            check(f"worker ollama   {tag}", sampling_ollama(b), expect_ollama(model, role))
            # worker, native Ollama streaming
            take()
            w.call_ollama_streaming(HOST, model, msgs, None, None, tools=False, role=role)
            b = take()[-1]["body"]
            check(f"worker ollama-stream {tag}", sampling_ollama(b), expect_ollama(model, role))
    # darkbloom_chat: review (default) + author(think=True) for the Darkbloom-served profiles
    dbc.base_url = lambda: HOST
    for model in [m for m in MODELS if RAW["models"][m].get("backend") == "darkbloom"]:
        take()
        dbc.chat(HOST, model, "sys", "user")
        b = take()[-1]["body"]
        check(f"darkbloom_chat review {model}", sampling_openai(b), expect_openai(model, "review"))
        take()
        dbc.chat(HOST, model, "sys", "user", think=True)
        b = take()[-1]["body"]
        check(f"darkbloom_chat think->author {model}", sampling_openai(b), expect_openai(model, "author", think=True))


def run_reasoning_parser(w):
    """REASONING_PARSER=qwen3 lever (default OFF): the wire body gains `reasoning_parser` ONLY for a model whose RAW profile
    declares it for a darkbloom backend, only on the openai-style path to a Darkbloom-lane host, only on a thinking turn.
    Expected values come from the raw YAML, not from model_profile. Unraid/Ollama paths and lever-off never carry it."""
    msgs = [{"role": "user", "content": "x"}]
    real_lane = w._is_darkbloom_lane
    w._is_darkbloom_lane = lambda h: str(h).rstrip("/") == HOST          # the stub plays the Darkbloom endpoint
    prev = os.environ.get("REASONING_PARSER")

    def declared(model):
        rp = RAW["models"][model].get("reasoning_parser")
        return (rp["name"] if isinstance(rp, dict) and "darkbloom" in (rp.get("backends") or [])
                and RAW["models"][model].get("backend") == "darkbloom" else None)
    try:
        for model in MODELS:
            for role in ROLES:
                for val, lever_on in ((None, False), ("0", False), ("1", False), ("deepseek_r1", False), ("qwen3", True)):
                    if val is None:
                        os.environ.pop("REASONING_PARSER", None)
                    else:
                        os.environ["REASONING_PARSER"] = val
                    want = dict(expect_openai(model, role))
                    thinking = want.get("chat_template_kwargs", {}).get("enable_thinking")
                    if lever_on and declared(model) and thinking is not False:
                        want["reasoning_parser"] = declared(model)
                    tag = f"{model}/{role} REASONING_PARSER={val}"
                    take()
                    w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role=role)
                    check(f"reasoning_parser openai      {tag}", sampling_openai(take()[-1]["body"]), want)
                    w.call_openai_streaming(HOST, model, msgs, None, None, tools=False, role=role)
                    check(f"reasoning_parser openai-sse  {tag}", sampling_openai(take()[-1]["body"]), want)
                    # Unraid / native Ollama: NEVER, lever or not
                    w.call_ollama(HOST, model, msgs, None, None, api_style="ollama", tools=False, role=role)
                    check(f"reasoning_parser ollama none {tag}", "reasoning_parser" in take()[-1]["body"], False)
                    w.call_ollama_streaming(HOST, model, msgs, None, None, tools=False, role=role)
                    check(f"reasoning_parser ollama-sse none {tag}", "reasoning_parser" in take()[-1]["body"], False)
        # a non-Darkbloom OpenAI host (llama-server, Strata, the Unraid box) never gets it even with the lever on
        os.environ["REASONING_PARSER"] = "qwen3"
        w._is_darkbloom_lane = real_lane
        take()
        w.call_ollama(HOST, "qwen3.6-35b-a3b-vl-mtp-mxfp8", msgs, None, None, api_style="openai", tools=False, role="author")
        w.call_openai_streaming(HOST, "qwen3.6-35b-a3b-vl-mtp-mxfp8", msgs, None, None, tools=False, role="author")
        check("reasoning_parser: non-Darkbloom openai host never gets the field (both paths)",
              ["reasoning_parser" in x["body"] for x in take()], [False, False])
        # thinking-off turn: no reasoning to split, body unchanged
        w._is_darkbloom_lane = lambda h: str(h).rstrip("/") == HOST
        w.call_ollama(HOST, "qwen3.6-35b-a3b-vl-mtp-mxfp8", msgs, None, None, api_style="openai", tools=False, role="author", think=False)
        check("reasoning_parser: thinking-off turn carries no field", "reasoning_parser" in take()[-1]["body"], False)
        # the builder itself: ollama api never emits it; undeclared parser name never emitted
        check("builder: api=ollama never emits reasoning_parser",
              "reasoning_parser" in mp.build_request_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "ollama", reasoning_parser="qwen3"), False)
        check("builder: a parser the profile does not declare is never emitted",
              "reasoning_parser" in mp.build_request_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "openai", reasoning_parser="deepseek_r1"), False)
        check("builder: default (no request) never emits it",
              "reasoning_parser" in mp.build_request_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "openai"), False)
    finally:
        w._is_darkbloom_lane = real_lane
        if prev is None:
            os.environ.pop("REASONING_PARSER", None)
        else:
            os.environ["REASONING_PARSER"] = prev


def run_overrides(w):
    msgs = [{"role": "user", "content": "x"}]
    pair = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    ov = {"temperature": 0.2, "top_k": 7, "max_tokens": 1234, "repetition_penalty": 1.25, "top_p": 0.5}
    take()
    w.call_ollama(HOST, pair, msgs, 0.2, None, api_style="openai", tools=False, top_p=0.5, top_k=7,
                  max_tokens=1234, repeat_penalty=1.25, role="author")
    check("explicit overrides beat the profile (openai)", sampling_openai(take()[-1]["body"]),
          expect_openai(pair, "author", ov))
    take()
    w.call_ollama(HOST, pair, msgs, None, None, api_style="openai", tools=False, role="author", think=False)
    check("explicit think=False beats the profile", take()[-1]["body"]["chat_template_kwargs"],
          {"enable_thinking": False})


def run_nothink_default(w, dbc):
    """NOTHINK_RUN (A/B arm C, DEFAULT ON since 2026-10-15): with the env var UNSET the author role sends
    enable_thinking=false on every worker path (and no preserve_thinking); review / non_thinking bodies are unchanged;
    NOTHINK_RUN=0/false/off restores the card's thinking profile byte-for-byte; darkbloom_chat is not a worker path."""
    msgs = [{"role": "user", "content": "x"}]
    prev = os.environ.get("NOTHINK_RUN")
    try:
        for state, val in (("unset", None), ("1", "1"), ("0", "0"), ("false", "false"), ("OFF", "OFF"), (" off ", " off ")):
            if val is None:
                os.environ.pop("NOTHINK_RUN", None)
            else:
                os.environ["NOTHINK_RUN"] = val
            on = state in ("unset", "1")
            for model in MODELS:
                for role in ROLES:
                    th = False if (on and role == "author") else None
                    tag = f"NOTHINK_RUN={state} {model}/{role}"
                    take()
                    w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role=role)
                    check(f"{tag} openai", sampling_openai(take()[-1]["body"]), expect_openai(model, role, think=th))
                    take()
                    w.call_openai_streaming(HOST, model, msgs, None, None, tools=False, role=role)
                    check(f"{tag} openai-sse", sampling_openai(take()[-1]["body"]), expect_openai(model, role, think=th))
                    take()
                    w.call_ollama(HOST, model, msgs, None, None, api_style="ollama", tools=False, role=role)
                    check(f"{tag} ollama", sampling_ollama(take()[-1]["body"]), expect_ollama(model, role, think=th))
                    take()
                    w.call_ollama_streaming(HOST, model, msgs, None, None, tools=False, role=role)
                    check(f"{tag} ollama-stream", sampling_ollama(take()[-1]["body"]), expect_ollama(model, role, think=th))
        # the default really turns thinking OFF for a thinking-profile author (not vacuous: the profile says on)
        os.environ.pop("NOTHINK_RUN", None)
        pair = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
        check("author profile is a thinking profile (so the default flip is observable)",
              expect_openai(pair, "author")["chat_template_kwargs"].get("enable_thinking"), True)
        take()
        w.call_ollama(HOST, pair, msgs, None, None, api_style="openai", tools=False, role="author")
        check("NOTHINK_RUN unset: author body enable_thinking=false and no preserve_thinking",
              take()[-1]["body"]["chat_template_kwargs"], {"enable_thinking": False})
        # darkbloom_chat (reviewer/gate transport) is not the worker author path
        dbc.base_url = lambda: HOST
        take()
        dbc.chat(HOST, pair, "sys", "user", think=True)
        check("NOTHINK_RUN unset: darkbloom_chat think=True still the author thinking profile",
              sampling_openai(take()[-1]["body"]), expect_openai(pair, "author", think=True))
    finally:
        if prev is None:
            os.environ.pop("NOTHINK_RUN", None)
        else:
            os.environ["NOTHINK_RUN"] = prev


def run_ladder(w):
    """SAMPLING ESCALATION LADDER (A/B arm, default OFF): the exact request body per step, expected
    values read from the RAW yaml (ladder table + the author mode), over every sender path.
    Arm off => step/seed are ignored and the body is byte-identical to the plain body."""
    msgs = [{"role": "user", "content": "x"}]
    model = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    lad = RAW["sampling_escalation"]["ladder"]
    check("shipped sampling arm is off", RAW["sampling_escalation"]["arm"] in ("off", False), True)
    base = raw_mode(model, "author")
    seed = 424242

    def want_mode(step, thinking_arm):
        st = lad[step]
        ov = {}
        if st.get("temperature_min") is not None:
            ov["temperature"] = max(base["temperature"], st["temperature_min"])
        if st.get("repetition_penalty_min") is not None:
            ov["repetition_penalty"] = max(base["repetition_penalty"], st["repetition_penalty_min"])
        if st.get("presence_penalty") is not None:
            ov["presence_penalty"] = max(base["presence_penalty"], st["presence_penalty"])
        th = None
        off = str(st.get("thinking")).lower() in ("off", "false")
        if off and st.get("requires_thinking_arm") in (None, thinking_arm):
            th = False
        return ov, th

    saved = {k: os.environ.get(k) for k in ("MODEL_SAMPLING_ARM", "MODEL_THINKING_ARM")}
    try:
        # arm OFF: step + seed must change nothing
        os.environ.pop("MODEL_SAMPLING_ARM", None)
        os.environ.pop("MODEL_THINKING_ARM", None)
        take()
        w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role="author",
                      sampling_step=1, seed=seed)
        b = take()[-1]["body"]
        check("ladder OFF: step 1 + seed leave the body untouched", sampling_openai(b), expect_openai(model, "author"))
        check("ladder OFF: no seed on the wire", "seed" in b, False)
        # arm ON
        os.environ["MODEL_SAMPLING_ARM"] = "ladder"
        for thinking_arm in (None, "hybrid"):
            if thinking_arm:
                os.environ["MODEL_THINKING_ARM"] = thinking_arm
            else:
                os.environ.pop("MODEL_THINKING_ARM", None)
            for step in (1, 2):
                ov, th = want_mode(step, thinking_arm)
                tag = f"ladder step {step} (thinking arm {thinking_arm or 'none'})"
                want_o = expect_openai(model, "author", ov, think=th)
                want_o["seed"] = seed
                take()
                w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role="author",
                              sampling_step=step, seed=seed)
                check(f"{tag} openai", sampling_openai(take()[-1]["body"]), want_o)
                take()
                w.call_openai_streaming(HOST, model, msgs, None, None, tools=False, role="author",
                                        sampling_step=step, seed=seed)
                check(f"{tag} openai-sse", sampling_openai(take()[-1]["body"]), want_o)
                want_n = expect_ollama(model, "author", ov, think=th)
                want_n["options"]["seed"] = seed
                take()
                w.call_ollama(HOST, model, msgs, None, None, api_style="ollama", tools=False, role="author",
                              sampling_step=step, seed=seed)
                check(f"{tag} ollama", sampling_ollama(take()[-1]["body"]), want_n)
                take()
                w.call_ollama_streaming(HOST, model, msgs, None, None, tools=False, role="author",
                                        sampling_step=step, seed=seed)
                check(f"{tag} ollama-stream", sampling_ollama(take()[-1]["body"]), want_n)
        # a role outside `roles:` (review) is never touched, even with the arm on
        take()
        w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role="review",
                      sampling_step=2, seed=seed)
        b = take()[-1]["body"]
        check("ladder ON: role outside sampling_escalation.roles untouched", sampling_openai(b),
              expect_openai(model, "review"))
        # step 0 (no abort pending) with the arm on is the plain body
        take()
        w.call_ollama(HOST, model, msgs, None, None, api_style="openai", tools=False, role="author",
                      sampling_step=0, seed=seed)
        check("ladder ON, step 0: plain body, no seed", sampling_openai(take()[-1]["body"]),
              expect_openai(model, "author", think=None))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_invariants():
    for model in MODELS:
        for role in ("author", "coding"):
            p = mp.get_profile(model, role)
            check(f"author max_tokens >= 32768 ({model}/{role})", (p["max_tokens"] or 0) >= 32768, True)
    for model in MODELS:
        o = mp.build_request_fields(model, "author", "openai")
        bad = {k: o[k] for k in ("temperature", "repetition_penalty", "max_tokens")
               if (k == "temperature" and o.get(k) in (0, 0.0, 0.15))
               or (k == "repetition_penalty" and o.get(k) == 1.1)
               or (k == "max_tokens" and o.get(k) == 8192)}
        check(f"no legacy hardcoded default leaks ({model})", bad, {})
    # DISPATCH_SAMPLING=legacy restores the pre-2026-10-08 values (A/B control + rollback); unset = card.
    _prev = os.environ.get("DISPATCH_SAMPLING")
    try:
        os.environ["DISPATCH_SAMPLING"] = "legacy"
        for role in ("author", "review"):
            o = mp.build_request_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", role, "openai")
            check(f"DISPATCH_SAMPLING=legacy restores old values ({role})",
                  (o.get("temperature"), o.get("repetition_penalty"), o.get("max_tokens"), "top_k" in o,
                   "chat_template_kwargs" in o), (0.15, 1.1, 8192, False, False))
    finally:
        if _prev is None:
            os.environ.pop("DISPATCH_SAMPLING", None)
        else:
            os.environ["DISPATCH_SAMPLING"] = _prev
    o = mp.build_request_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "openai")
    check("default (env unset) is the card profile", (o["temperature"], o["top_k"], o["max_tokens"]), (0.6, 20, 32768))
    # alias: declared, lane-scoped, logged, never silent
    got = mp.resolve_alias("qwen3.8:27b-q4_K_M", "darkbloom")
    check("qwen3.8 alias resolves on the darkbloom lane via the PROFILE",
          got, ("qwen3.6-35b-a3b-vl-mtp-mxfp8", "qwen3.8:27b-q4_K_M"))
    check("qwen3.8 alias does NOT apply without a lane (Ollama keeps its own profile)",
          mp.resolve_alias("qwen3.8:27b-q4_K_M", None), ("qwen3.8:27b-q4_K_M", None))
    # the alias must be LOGGED (once), never silent
    import contextlib
    import io
    mp._WARNED.clear()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        mp.resolve_alias("qwen3.8:27b-q4_K_M", "darkbloom")
        mp.resolve_alias("qwen3.8:27b-q4_K_M", "darkbloom")
    check("alias logs a warning exactly once", buf.getvalue().count("ALIAS qwen3.8:27b-q4_K_M"), 1)
    # unknown model: fallback, flagged loudly
    p = mp.get_profile("totally-unknown:7b", "author")
    check("unknown model -> fallback profile flagged", p["fallback"], True)


def run_cli_defaults(w):
    """The worker's argparse defaults must be None (profile-resolved), not constants."""
    src = WORKER.read_text()
    check("CLI --temperature default is None (profile owns it)",
          'ap.add_argument("--temperature", type=float, default=None' in src, True)
    check("CLI --max-tokens default is None", 'ap.add_argument("--max-tokens", type=int, default=None' in src, True)
    check("CLI --repeat-penalty default is None", 'ap.add_argument("--repeat-penalty", type=float, default=None' in src, True)
    check("CLI --num-ctx default is None", 'ap.add_argument("--num-ctx", type=int, default=None' in src, True)


def run_queue():
    """The queue's launch builder: no hardcoded temperature reaches the worker argv;
    an explicit enqueue --temperature does; legacy rows (temperature 0, not explicit)
    are NOT honored; the alias comes from the profile."""
    q = _load(QUEUE, "oq_conf")
    tf = Path(tempfile.mkdtemp(prefix="mpconf-q-")) / "task.md"
    tf.write_text("do a thing")
    job = {"id": "j1", "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "cwd": str(tf.parent), "task_file": str(tf),
           "num_ctx": 65536, "max_iters": None, "api": "openai", "temperature": 0, "task_kind": "coding"}
    cmd = q._build_cmd(dict(job), HOST)
    check("queue launch: legacy temperature=0 row is NOT forwarded", "--temperature" in cmd, False)
    cmd = q._build_cmd(dict(job, temperature=0.4, temperature_explicit=True), HOST)
    check("queue launch: explicit --temperature IS forwarded",
          cmd[cmd.index("--temperature") + 1] if "--temperature" in cmd else None, "0.4")
    cmd = q._build_cmd(dict(job, role="review"), HOST)
    check("queue launch: --role forwarded", cmd[cmd.index("--role") + 1] if "--role" in cmd else None, "review")
    qsrc = QUEUE.read_text()
    check("queue enqueue --temperature default is None",
          'e.add_argument("--temperature", type=float, default=None' in qsrc, True)
    check("queue alias qwen3.8 -> profile alias target",
          q._darkbloom_model("qwen3.8:27b-q4_K_M"), "qwen3.6-35b-a3b-vl-mtp-mxfp8")


def run_e2e_subprocess():
    """Full worker process: argparse -> run_task -> real request. Proves the CLI path
    (not just the call_* functions) sends the profile."""
    pair = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    cwd = tempfile.mkdtemp(prefix="mpconf-cwd-")
    for role, dflt in (("author", False), ("review", False), ("author", True)):
        take()
        env = {**os.environ, "WORKER_NO_WARMUP": "1"}
        if dflt:      # NOTHINK_RUN unset = the shipped default (ON): the author body is thinking-off
            env.pop("NOTHINK_RUN", None)
        r = subprocess.run([sys.executable, str(WORKER), "--model", pair, "--host", HOST, "--api", "openai",
                            "--task", "reply with the single word hi", "--cwd", cwd, "--max-iters", "1",
                            "--role", role, "--chat-timeout", "30", "--direct-ok"],
                           capture_output=True, text=True, timeout=180, env=env)
        allreq = [s for s in take() if s["path"] == "/v1/chat/completions"]
        probe = [s for s in allreq if s["body"]["messages"][0]["content"] == "ready"]
        sent = [s for s in allreq if s["body"]["messages"][0]["content"] != "ready"]
        if probe:
            check(f"e2e preflight probe uses the non-thinking profile ({role}{', NOTHINK_RUN default' if dflt else ''})",
                  sampling_openai(probe[0]["body"]),
                  expect_openai(pair, "review", {"max_tokens": 256}))
        if os.environ.get("MPCONF_DEBUG"):
            print("      sent:", json.dumps(sent)[:1500])
        check(f"e2e worker subprocess sent a chat request ({role}{', NOTHINK_RUN default' if dflt else ''})", bool(sent), True)
        if sent:
            check(f"e2e worker subprocess body == profile ({role}{', NOTHINK_RUN default: thinking off' if dflt else ''})",
                  sampling_openai(sent[0]["body"]), expect_openai(pair, role, think=False if dflt else None))
        elif r.returncode is not None:
            print("      worker stderr tail:", (r.stderr or r.stdout)[-400:])


def run_drift():
    probs = mp.check_profiles(PROFILES)
    check("model-card drift: every profile cites an existing, unchanged card", probs, [])
    # the detector itself must bite: a corrupted card_sha256 / an edited card number is reported
    first = next(iter(RAW["models"]))
    txt = PROFILES.read_text()
    bad_hash = txt.replace(RAW["models"][first]["card_sha256"], "0" * 64, 1)
    tmp = Path(tempfile.mkdtemp(prefix="mpconf-drift-")) / "model_profiles.yaml"
    tmp.write_text(bad_hash)
    check("drift detector flags a changed card (sha256 mismatch)",
          any("card changed" in x for x in mp.check_profiles(tmp)), True)
    tmp.write_text(txt.replace("temperature: 0.7, top_p: 0.8, top_k: 20", "temperature: 0.71, top_p: 0.8, top_k: 20", 1))
    check("drift detector flags a number the card does not state",
          any("do not match the card" in x for x in mp.check_profiles(tmp)), True)
    mp.load_profiles(PROFILES, refresh=True)
    for name, e in RAW["models"].items():
        check(f"has source citation ({name})", bool(str(e.get("source") or "").strip()), True)


def main():
    if "--drift" in sys.argv:
        run_drift()
    else:
        w = _load(WORKER, "ow_conf")
        dbc = _load(DBC, "dbc_conf")
        run_drift()
        run_invariants()
        run_cli_defaults(w)
        run_queue()
        run_matrix(w, dbc)
        run_nothink_default(w, dbc)
        run_reasoning_parser(w)
        run_overrides(w)
        run_ladder(w)
        run_e2e_subprocess()
    print(f"\n{NCHECK[0] - len(FAILS)}/{NCHECK[0]} passed")
    print("CONFORMANT" if not FAILS else f"NON-CONFORMANT: {len(FAILS)} FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
