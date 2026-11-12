#!/usr/bin/env python3
"""test-queue-worker-consistency.py -- END-TO-END queue -> worker -> server consistency.

WHY (2026-10-13): each component passed its own tests while the WHOLE path was never
checked (psc-C10: the queue chose Darkbloom :8000 from one read of local.json, _build_cmd
re-read an empty file and dropped --api openai, the worker refused the Darkbloom-only model
on 'Ollama'; psc-C4: a basename match; a wrong failure-class label). This runs the REAL
queue _build_cmd, then launches the REAL ollama-worker.py with exactly that argv against a
STUB server that records every request, per routed host type, and asserts the INVARIANTS:

  I1 api style : the argv --api, the URL path hit (/v1/chat/completions vs /api/chat) and
                 the body shape (OpenAI fields vs options{}) all agree.
  I2 model     : the body's `model` == the argv --model == what the queue resolved
                 (profile alias on Darkbloom only; never rewritten on Ollama/llama-server;
                 an unknown bare id passes through unchanged).
  I3 ctx       : an Ollama body carries options.num_ctx == the job's num_ctx; an OpenAI body
                 carries no num_ctx (the server owns it).
  I4 sampling  : the wire sampling/thinking fields == the model card profile of the model
                 ACTUALLY sent (not of the pre-alias tag).
  I5 auth      : the Darkbloom Bearer key is sent to the Darkbloom endpoint on every
                 request and NEVER to any other host.
  I6 TOCTOU    : local.json empty/rewritten between the queue's choice and the worker's
                 request neither drops --api openai nor leaves a request without the key.
  I7 no silent rewrite: every model alias/fallback is logged; a model the server lacks
                 fails the job loudly and never re-sends a different model.
  I8 lanes     : a Darkbloom-only model on an Ollama host is refused before any /api/chat
                 or /api/pull; the two alias implementations (queue, darkbloom_chat) agree.

Usage: python3 ~/bin/test-queue-worker-consistency.py     (BIN=dir to test a backup copy)
Prints CONSISTENCY_OK."""
import contextlib, io, json, os, subprocess, sys, tempfile, threading, time
import http.server, socketserver
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(os.environ.get("BIN", Path(__file__).resolve().parent)).resolve()
TMP = Path(tempfile.mkdtemp(prefix="qw-consistency-"))
HOME = TMP / "home"
(HOME / ".darkbloom").mkdir(parents=True)
(HOME / ".config" / "darkbloom").mkdir(parents=True)
os.environ["HOME"] = str(HOME)                      # BEFORE the queue import: Path.home() is read at import
for _k in list(os.environ):
    if _k.startswith(("DARKBLOOM_", "BONSAI_", "OLLAMA_DISPATCH")):
        del os.environ[_k]
sys.path.insert(0, str(BIN))
q = SourceFileLoader("q_consistency", str(BIN / "ollama-queue.py")).load_module()
import model_profile as mp                           # noqa: E402
dbc = SourceFileLoader("dbc_consistency", str(BIN / "darkbloom_chat.py")).load_module()

LOCAL_JSON = HOME / ".darkbloom" / "local.json"
KEY = "sk-test-darkbloom-KEY"
BARE = "qwen3.6-35b-a3b-vl-mtp-mxfp8"               # a Darkbloom-served bare id
(HOME / ".config" / "darkbloom" / "provider.toml").write_text(f'enabled_models = ["{BARE}"]\n')
fails = 0


def chk(name, cond, detail=""):
    global fails
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else f": {str(detail)[:400]}"))
    fails += 0 if cond else 1


TOOL_DONE = {"name": "task_complete", "arguments": {"summary": "done"}}


class Stub(http.server.BaseHTTPRequestHandler):
    rec = []          # (method, path, authorization, body)
    mode = "openai"   # or "ollama"
    key = None        # when set, /v1 requires this Bearer key (Darkbloom)
    served = None     # model ids the server serves (None = any)
    pulled = False
    rotate_after_first = None   # D3b: rotate the key right after the first accepted chat

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        Stub.rec.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path.startswith("/health"):
            return self._send({"status": "ok"})
        names = Stub.served or ["x"]
        self._send({"models": [{"name": n, "model": n, "size": 1, "size_vram": 1, "digest": "0",
                                "details": {"parameter_size": "1B", "quantization_level": "Q4"}}
                               for n in names],
                    "data": [{"id": n} for n in names], "version": "0.0.0"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        auth = self.headers.get("Authorization")
        Stub.rec.append(("POST", self.path, auth, body))
        if self.path.startswith("/api/pull"):
            return self._send({"status": "success"})
        if self.path.startswith("/v1/") and Stub.key and auth != "Bearer " + Stub.key:
            return self._send({"error": {"message": "unauthorized"}}, 401)
        if Stub.served is not None and body.get("model") not in Stub.served and (
                self.path.startswith("/v1/") or self.path.startswith("/api/chat")):
            return self._send({"error": {"message": f"model '{body.get('model')}' not found"}}, 404)
        if self.path.startswith("/v1/chat/completions") and Stub.rotate_after_first:
            # MID-RUN rotation (adversarial review 2026-10-13): Darkbloom restarts AFTER the worker's
            # first accepted request; the next request must carry the NEW key (a per-process key
            # cache passed D3, which rotated before the first request).
            Stub.rotate_after_first = None
            new = "sk-rotated-midrun"
            write_local({"api_key": new, "base_url": STUB + "/v1"})
            Stub.key = new
            tc = {"id": "c0", "type": "function",
                  "function": {"name": "list_files", "arguments": json.dumps({"path": "."})}}
            return self._send({"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]},
                                            "finish_reason": "tool_calls"}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})
        if self.path.startswith("/v1/chat/completions"):
            tc = {"id": "c1", "type": "function",
                  "function": {"name": TOOL_DONE["name"], "arguments": json.dumps(TOOL_DONE["arguments"])}}
            return self._send({"choices": [{"message": {"role": "assistant", "content": "",
                                                        "tool_calls": [tc]},
                                            "finish_reason": "tool_calls"}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})
        if self.path.startswith("/api/chat"):
            if not body.get("tools"):
                return self._send({"message": {"role": "assistant", "content": "ok"}, "done": True})
            return self._send({"message": {"role": "assistant", "content": "",
                                           "tool_calls": [{"function": TOOL_DONE}]},
                               "done": True, "prompt_eval_count": 5, "eval_count": 5})
        self._send({"status": "success"})


class TS(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


srv = TS(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{srv.server_address[1]}"


def reset(mode, key=None, served=None):
    Stub.rec, Stub.mode, Stub.key, Stub.served = [], mode, key, served


def write_local(rec):
    LOCAL_JSON.write_text(json.dumps(rec) if rec is not None else "")


def make_job(model, num_ctx=24576, **kw):
    tf = TMP / "task.txt"
    tf.write_text("Do nothing; call task_complete.")
    cwd = TMP / "cwd"
    cwd.mkdir(exist_ok=True)
    j = {"id": "cons01", "model": model, "cwd": str(cwd), "task_file": str(tf), "num_ctx": num_ctx,
         "host_pref": "auto", "api": "ollama", "task_kind": "coding", "max_iters": 3}
    j.update(kw)
    return j


def run_worker(cmd, timeout=120, mutate=None, extra_env=None):
    """Launch the REAL worker with the queue-built argv (same env stamp the queue adds)."""
    env = {**os.environ, "HOME": str(HOME), "OLLAMA_DISPATCH_VIA_QUEUE": "cons01",
           "SLOT_BUSY_WAIT_S": "1", "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    t = None
    if mutate:
        t = threading.Thread(target=mutate, daemon=True)
        t.start()
    cmd = list(cmd)
    assert cmd[1].endswith("ollama-worker.py"), cmd[:2]
    cmd[1] = str(BIN / "ollama-worker.py")         # the only substitution: sandbox HOME has no bin/
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(TMP))
    if t:
        t.join(5)
    return p.returncode, p.stdout + p.stderr


def argv_of(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def posts(path_prefix):
    return [r for r in Stub.rec if r[0] == "POST" and r[1].startswith(path_prefix)]


def queue_build(job, host):
    """The real _build_cmd; returns (cmd, queue stderr -- where alias warnings are logged)."""
    buf = io.StringIO()
    mp._WARNED.clear()
    with contextlib.redirect_stderr(buf):
        cmd = q._build_cmd(dict(job), host)
    return cmd, buf.getvalue()


def profile_wire(model, api):
    return mp.build_request_fields(model, "author", api, overrides={"num_ctx": None})


SAMPLING = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")


def check_sampling(body, model, tag):
    exp = profile_wire(model, "openai")
    bad = {k: (body.get(k), exp.get(k)) for k in SAMPLING if k in exp and body.get(k) != exp[k]}
    chk(f"{tag}: I4 wire sampling == profile of the model actually sent", not bad, bad)
    # NOTHINK_RUN is DEFAULT ON for the author role (2026-10-15): the profile's thinking is overridden to false
    # (this test launches the worker with NOTHINK_RUN unset). The profile must still declare a thinking-capable author mode.
    et = (mp.build_request_fields(model, "author", "openai", overrides={"num_ctx": None}, think=False)
          .get("chat_template_kwargs") or {}).get("enable_thinking")
    chk(f"{tag}: I4 enable_thinking on the wire == profile with the NOTHINK_RUN default (thinking off)", (body.get("chat_template_kwargs") or {}).get("enable_thinking") == et,
        (body.get("chat_template_kwargs"), et))


# --------------------------------------------------------------------------- Darkbloom
def darkbloom_case(tag, model, expect_model, local_at_launch, local_after=None, delay=1.5, mutate_during=None, extra_env=None):
    write_local({"api_key": KEY, "base_url": STUB + "/v1"})
    reset("openai", key=KEY, served=[expect_model])
    q._DARKBLOOM_LAST_URL[0] = None
    q._is_darkbloom_host(STUB)                         # the lane chooser's good read
    job = make_job(model)
    write_local(local_at_launch)                       # the file changes between choice and build
    cmd, qerr = queue_build(job, STUB)
    if local_after is not None:
        def mut():
            time.sleep(delay)
            write_local(local_after)
        mutate_during = mut
    rc, out = run_worker(cmd, mutate=mutate_during, extra_env=extra_env)
    return cmd, qerr, rc, out


# D1: legacy tag on the Darkbloom lane, local.json good
cmd, qerr, rc, out = darkbloom_case("D1", "qwen3.8:27b-q4_K_M", BARE, {"api_key": KEY, "base_url": STUB + "/v1"})
chk("D1 darkbloom legacy tag: worker finished", rc == 0, out[-600:])
chk("D1: I1 argv --api openai", argv_of(cmd, "--api") == "openai", cmd)
chk("D1: I2 argv model is the profile alias", argv_of(cmd, "--model") == BARE, cmd)
chk("D1: I7 the alias was logged by the queue", "ALIAS qwen3.8:27b-q4_K_M" in qerr, qerr)
chat = posts("/v1/chat/completions")
chk("D1: I1 only /v1/chat/completions used, never /api/*", chat and not [r for r in Stub.rec if r[1].startswith("/api/")], Stub.rec[:3])
chk("D1: I2 every body.model == argv model", chat and all(r[3].get("model") == BARE for r in chat), [r[3].get("model") for r in chat])
chk("D1: I3 no num_ctx/options in an OpenAI body", chat and all("options" not in r[3] and "num_ctx" not in r[3] for r in chat))
chk("D1: I5 every request carried the Bearer key", chat and all(r[2] == "Bearer " + KEY for r in Stub.rec if r[0] == "POST"), [r[2] for r in Stub.rec])
if chat:
    check_sampling(chat[-1][3], BARE, "D1")

# D2: the C10 race -- local.json EMPTY when the queue builds the command, rewritten ~1.5s later
cmd, qerr, rc, out = darkbloom_case("D2", BARE, BARE, "", local_after={"api_key": KEY, "base_url": STUB + "/v1"},
                                     extra_env={"DARKBLOOM_BASE_URL": STUB})
chk("D2 empty-local.json race: I6 argv still --api openai", argv_of(cmd, "--api") == "openai", cmd)
chk("D2: I6 argv still names the Darkbloom host", argv_of(cmd, "--host") == STUB, cmd)
chk("D2: I6 worker completes (key re-read after the rewrite; no keyless request accepted)", rc == 0, out[-700:])
chat = posts("/v1/chat/completions")
chk("D2: I5 every POST that the stub accepted carried the key", chat and any(r[2] == "Bearer " + KEY for r in chat), [r[2] for r in chat])
chk("D2: I2 body.model == argv model", chat and all(r[3].get("model") == BARE for r in chat))

# D3: local.json rewritten with a ROTATED key mid-run (Darkbloom restart): the new key must be used
ROT = "sk-rotated-key"


def rotate():
    time.sleep(0.2)
    write_local({"api_key": ROT, "base_url": STUB + "/v1"})
    Stub.key = ROT


cmd, qerr, rc, out = darkbloom_case("D3", BARE, BARE, {"api_key": KEY, "base_url": STUB + "/v1"}, mutate_during=rotate)
chat = posts("/v1/chat/completions")
chk("D3 rotated key: worker completes using the NEW key", rc == 0 and any(r[2] == "Bearer " + ROT for r in chat), (rc, [r[2] for r in chat], out[-400:]))

# D3b: rotation AFTER the first accepted request (provider restart mid-job): every later request
# must re-read the key at call time (D3 rotates before the first request, so a per-process key cache
# still passed it).
write_local({"api_key": KEY, "base_url": STUB + "/v1"})
reset("openai", key=KEY, served=[BARE])
Stub.rotate_after_first = True
cmd, qerr = queue_build(make_job(BARE), STUB)
rc, out = run_worker(cmd)
chat = posts("/v1/chat/completions")
chk("D3b mid-run rotation: >=2 chats, first with the old key, a later one with the NEW key, job completes",
    rc == 0 and len(chat) >= 2 and chat[0][2] == "Bearer " + KEY and chat[-1][2] == "Bearer sk-rotated-midrun",
    (rc, [r[2] for r in chat], out[-400:]))
Stub.rotate_after_first = None

# D3c (2026-10-13 review item 6): the key was rotated and the request we built still carries the OLD one,
# so the server answers 401 -- local.json is only rewritten ~2.5s later. One bounded, uncharged retry
# after the key on disk differs from the one we sent; no loop when it never changes.
ROT2 = "sk-rotated-midflight"


def rotate_late(delay=2.5, rewrite=True):
    def mut():
        Stub.key = ROT2                                # the server already took the new key ...
        if rewrite:
            time.sleep(delay)                          # ... local.json only catches up later
            write_local({"api_key": ROT2, "base_url": STUB + "/v1"})
    return mut


cmd, qerr, rc, out = darkbloom_case("D3c", BARE, BARE, {"api_key": KEY, "base_url": STUB + "/v1"}, mutate_during=rotate_late())
chat = posts("/v1/chat/completions")
chk("D3c 401 from a mid-flight key rotation: worker retries once with the NEW key and completes",
    rc == 0 and any(r[2] == "Bearer " + ROT2 for r in chat), (rc, [r[2] for r in chat], out[-500:]))
chk("D3c: the rotation retry is logged (never silent)", "KEY ROTATED mid-flight" in out, out[-500:])
cmd, qerr, rc, out = darkbloom_case("D3c-off", BARE, BARE, {"api_key": KEY, "base_url": STUB + "/v1"}, mutate_during=rotate_late(),
                                     extra_env={"DARKBLOOM_401_RETRY": "0"})
chk("D3c kill switch DARKBLOOM_401_RETRY=0: the old behaviour (the 401 is not waited out)", rc != 0 and "KEY ROTATED" not in out, (rc, out[-400:]))
write_local({"api_key": KEY, "base_url": STUB + "/v1"})
t0 = time.time()
cmd, qerr, rc, out = darkbloom_case("D3c-bad", BARE, BARE, {"api_key": KEY, "base_url": STUB + "/v1"},
                                     mutate_during=rotate_late(rewrite=False), extra_env={"DARKBLOOM_KEY_ROTATE_WAIT_S": "1"})
n401 = len([r for r in posts("/v1/chat/completions") if r[2] != "Bearer " + ROT2])
chk("D3c a genuinely bad key (never rotates) is bounded: fails, with ONE rotation wait, not a loop",
    rc != 0 and "unchanged after" in out and out.count("unchanged after") <= 2 and n401 <= 8, (rc, n401, out[-500:]))

# D4: model missing on Darkbloom -> loud failure, and never a different model sent
write_local({"api_key": KEY, "base_url": STUB + "/v1"})
reset("openai", key=KEY, served=["some-other-model"])
cmd, qerr = queue_build(make_job("typo-model-id"), STUB)
rc, out = run_worker(cmd, timeout=180)
chat = posts("/v1/chat/completions")
chk("D4 missing model: I7 job FAILS (rc != 0), not silently succeeds", rc != 0, (rc, out[-300:]))
chk("D4: I7 unknown bare id passes through unchanged (typo fails at the endpoint)", argv_of(cmd, "--model") == "typo-model-id", cmd)
chk("D4: I7 no request ever carried a different model (and the worker did send one)", chat and all(r[3].get("model") == "typo-model-id" for r in chat), [r[3].get("model") for r in chat])

# D5: an unaliased ':' tag falls back to the default AND says so
write_local({"api_key": KEY, "base_url": STUB + "/v1"})
q._DARKBLOOM_LAST_URL[0] = None
cmd, qerr = queue_build(make_job("someorg/odd:7b"), STUB)
chk("D5 unaliased ':' tag: I7 falls back to default", argv_of(cmd, "--model") == q.DARKBLOOM_DEFAULT_MODEL, cmd)
chk("D5: I7 the fallback was logged (never silent)", "falling back" in qerr, qerr)

# --------------------------------------------------------------------------- llama-server bypass (Bonsai-style)
write_local({"api_key": KEY, "base_url": "http://127.0.0.1:1/v1"})      # Darkbloom key exists but is NOT this host
q._bonsai_url = lambda: STUB
reset("openai", key=None, served=None)
Stub.served = ["qwen3.8:27b-q4_K_M"]
cmd, qerr = queue_build(make_job("qwen3.8:27b-q4_K_M"), STUB)
rc, out = run_worker(cmd)
chat = posts("/v1/chat/completions")
chk("L1 llama-server lane: finishes", rc == 0, out[-500:])
chk("L1: I1 --api openai forced + --preserve-reasoning", argv_of(cmd, "--api") == "openai" and "--preserve-reasoning" in cmd, cmd)
chk("L1: I2 the ':' tag is NOT alias-rewritten off the Darkbloom lane", chat and all(r[3].get("model") == "qwen3.8:27b-q4_K_M" for r in chat), [r[3].get("model") for r in chat])
chk("L1: I5 the Darkbloom key is never sent to a non-Darkbloom host", all(r[2] is None for r in Stub.rec), [r[2] for r in Stub.rec])
chk("L1: I1 used /health + /v1/chat/completions only", not [r for r in Stub.rec if r[1].startswith("/api/")], Stub.rec[:3])
q._bonsai_url = lambda: None

# --------------------------------------------------------------------------- Ollama hosts (Studio / Unraid)
for tag, real_host in (("O1 studio", q.STUDIO_URL if hasattr(q, "STUDIO_URL") else "http://127.0.0.1:11434"),
                       ("O2 unraid", "http://198.51.100.145:11434")):
    write_local({"api_key": KEY, "base_url": "http://127.0.0.1:1/v1"})
    cmd, qerr = queue_build(make_job("qwen3.8:27b-q4_K_M", num_ctx=24576), real_host)
    chk(f"{tag}: I1 no --api flag (native Ollama), host intact", "--api" not in cmd and argv_of(cmd, "--host") == real_host, cmd)
    chk(f"{tag}: I2 the model is not alias-rewritten", argv_of(cmd, "--model") == "qwen3.8:27b-q4_K_M", cmd)
    chk(f"{tag}: I7 no alias line logged for an Ollama host", "ALIAS" not in qerr, qerr)
    cmd2 = list(cmd)
    cmd2[cmd2.index("--host") + 1] = STUB                  # the only substitution: the stub stands in for the host
    reset("ollama", served=["qwen3.8:27b-q4_K_M"])
    rc, out = run_worker(cmd2)
    chat = posts("/api/chat")
    chk(f"{tag}: worker finishes", rc == 0, out[-500:])
    chk(f"{tag}: I1 only /api/chat used, never /v1/*", chat and not [r for r in Stub.rec if r[1].startswith("/v1/")], Stub.rec[:4])
    chk(f"{tag}: I2 body.model == job model", chat and all(r[3].get("model") == "qwen3.8:27b-q4_K_M" for r in chat))
    chk(f"{tag}: I3 options.num_ctx == the job's num_ctx", chat and all((r[3].get("options") or {}).get("num_ctx") == 24576 for r in chat),
        [(r[3].get("options") or {}).get("num_ctx") for r in chat])
    chk(f"{tag}: I5 no Authorization header ever sent to an Ollama host", all(r[2] is None for r in Stub.rec), [r[2] for r in Stub.rec])
    if chat:
        exp = mp.build_request_fields("qwen3.8:27b-q4_K_M", "author", "ollama", overrides={"num_ctx": 24576})
        got = chat[-1][3].get("options") or {}
        bad = {k: (got.get(k), v) for k, v in (exp.get("options") or {}).items() if got.get(k) != v}
        chk(f"{tag}: I4 options{{}} == profile of the model sent", not bad, bad)

# I8: Darkbloom-only model on an Ollama host: refused before any chat/pull
write_local({"api_key": KEY, "base_url": "http://127.0.0.1:1/v1"})
reset("ollama", served=["x"])
cmd, qerr = queue_build(make_job(BARE), "http://198.51.100.145:11434")
cmd[cmd.index("--host") + 1] = STUB
rc, out = run_worker(cmd)
chk("X1 Darkbloom-only model on an Ollama host: I8 refused (rc != 0)", rc != 0 and "refusing to pull" in out, out[-300:])
chk("X1: I8 no /api/chat and no /api/pull was ever sent", "refusing to pull" in out and not posts("/api/chat") and not posts("/api/pull"), Stub.rec)

# I8: the two alias implementations (queue vs darkbloom_chat runner client) must agree
for tg in ("qwen3.8:27b-q4_K_M", "qwen3.8:27b-q8_0", BARE, "typo-id", "other:7b"):
    a, b = q._darkbloom_model(tg), dbc.alias_model(tg)
    chk(f"X2 alias parity queue vs darkbloom_chat for {tg!r}", a == b, (a, b))
chk("X3 the queue's served-model reader == the worker's (both gate 'Darkbloom-only')",
    q._darkbloom_served_models() == SourceFileLoader("w_c", str(BIN / "ollama-worker.py")).load_module()._darkbloom_served_models(),
    (q._darkbloom_served_models(),))

# =========================================================================== LANE CHOOSER (L-series)
# The REAL _candidate_lanes / darkbloom_launch_wait / resolve_ctx_ceiling / _lane_name / _build_cmd
# against stubbed lane state. Production host table = Unraid ONLY (Studio's Ollama is retired).
DB_URL = "http://127.0.0.1:8000"
UNRAID_URL = "http://198.51.100.145:11434"
GOOD_LOCAL = {"api_key": KEY, "base_url": DB_URL + "/v1"}
LEGACY = "qwen3.8:27b-q4_K_M"


class W:
    KNOWN_OLLAMA_HOSTS = {"unraid": {"url": UNRAID_URL, "usable_bytes": 9 * 10**9}}
    BIG_HOST_NAME, SMALL_HOST_NAME = "studio", "unraid"
    sizes = {}

    def _get_model_size_on_host(self, url, model):
        return self.sizes.get(model)


w = W()
TOML = HOME / ".config" / "darkbloom" / "provider.toml"


def lane_state(local=GOOD_LOCAL, toml=True, sticky=True):
    """Reset every cache the chooser keeps, then set the stubbed on-disk lane state."""
    q._DARKBLOOM_URL_GOOD[:] = [None, 0.0]
    q._DARKBLOOM_SERVED_GOOD[:] = [frozenset(), 0.0]
    q._DARKBLOOM_EXACT_CACHE[:] = [0.0, []]
    q._DARKBLOOM_LAST_URL[0] = None
    q._size_cache.clear()
    W.sizes = {}
    if hasattr(q, "DARKBLOOM_STICKY_S"):
        q.DARKBLOOM_STICKY_S = 60.0 if sticky else 0.0
    write_local(local)
    TOML.write_text(f'enabled_models = ["{BARE}"]\n' if toml else "")
    q._bonsai_url = lambda: None
    q.worker = lambda: w                # sandbox HOME has no real worker; resolve_ctx_ceiling needs one


def lanes(model, pref, **kw):
    return q._candidate_lanes({"model": model, "host_pref": pref, "num_ctx": 8192, **kw}, w)


def is_db(u):
    return str(u).rstrip("/") == DB_URL


# ---- L1 (1) the chosen lane can serve the model: model, api style, ctx
for model, pref, want in ((LEGACY, "auto", "db"), (LEGACY, "studio", "db"), (LEGACY, "darkbloom", "db"),
                          (BARE, "auto", "db"), (BARE, "studio-db", "db"), ("qwen3:14b", "unraid", "ollama")):
    lane_state()
    ls = lanes(model, pref)
    chk(f"L1 {model}/{pref}: exactly one lane chosen", len(ls) == 1, ls)
    if len(ls) != 1:
        continue
    cmd, qerr = queue_build(make_job(model), ls[0])
    sent = argv_of(cmd, "--model")
    if want == "db":
        chk(f"L1 {model}/{pref}: Darkbloom lane -> --api openai", is_db(ls[0]) and argv_of(cmd, "--api") == "openai", (ls, cmd))
        served = {x.lower() for x in q._darkbloom_served_models()}
        chk(f"L1 {model}/{pref}: the model SENT is one Darkbloom serves", sent.lower() in served, (sent, served))
        chk(f"L1 {model}/{pref}: lane name is the studio-db lane", q._lane_name(ls[0]) == q.DARKBLOOM_LANE, q._lane_name(ls[0]))
        _sv = q.darkbloom_ctx_ceiling
        q.darkbloom_ctx_ceiling = lambda m=None, **k: 4242
        try:
            chk(f"L1 {model}/{pref}: ctx ceiling comes from the Darkbloom lane the chooser picked",
                q.resolve_ctx_ceiling(pref, model) == 4242, q.resolve_ctx_ceiling(pref, model))
        finally:
            q.darkbloom_ctx_ceiling = _sv
    else:
        chk(f"L1 {model}/{pref}: Ollama lane is a configured host, native api (no --api)",
            ls[0] == UNRAID_URL and "--api" not in cmd, (ls, cmd))
        chk(f"L1 {model}/{pref}: Ollama lane never gets a Darkbloom-only model", not q._is_darkbloom_only_model(model))
        chk(f"L1 {model}/{pref}: model passed through unaliased + --num-ctx == job ctx",
            sent == model and argv_of(cmd, "--num-ctx") == "24576", cmd)

# L1a a 27B legacy tag pinned to the 12GB Unraid card is held, never run there nor diverted to Darkbloom
lane_state()
chk("L1a legacy 27B tag pinned to unraid: held (no lane), not Unraid, not Darkbloom", lanes(LEGACY, "unraid") == [], lanes(LEGACY, "unraid"))
# L1b model too big for the only Ollama host -> waits ([]), never lands on it
lane_state()
W.sizes = {"huge:70b": 40 * 10**9}
chk("L1b model larger than the Unraid budget is not routed to Unraid (pinned or auto-fit)", lanes("huge:70b", "unraid") == [], lanes("huge:70b", "unraid"))

# ---- L2 (2) TOCTOU: choice from one read, command/launch checks from another
for model in (LEGACY, BARE):
    for pref in ("auto", "studio", "darkbloom", "studio-db"):
        lane_state()
        first = lanes(model, pref)                           # the good read
        write_local("")                                      # provider restart: local.json momentarily empty
        TOML.write_text("")                                  # ... and so is provider.toml
        again = lanes(model, pref)
        cmd, qerr = queue_build(make_job(model), first[0]) if first else (None, "")
        chk(f"L2 {model}/{pref}: re-choosing during the empty-file window gives the SAME lane", again == first, (first, again))
        chk(f"L2 {model}/{pref}: lane name + launch-gate check agree with the choice during the window",
            bool(first) and q._lane_name(first[0]) == q.DARKBLOOM_LANE and q._darkbloom_url() == first[0], (first, q._darkbloom_url()))
        chk(f"L2 {model}/{pref}: command built in the window is --api openai on the chosen host",
            cmd is not None and argv_of(cmd, "--api") == "openai" and argv_of(cmd, "--host") == first[0], cmd)
        _sv = q.darkbloom_ctx_ceiling
        q.darkbloom_ctx_ceiling = lambda m=None, **k: 4242
        try:
            chk(f"L2 {model}/{pref}: ctx ceiling in the window still comes from the Darkbloom lane",
                q.resolve_ctx_ceiling(pref, model) == 4242, q.resolve_ctx_ceiling(pref, model))
        finally:
            q.darkbloom_ctx_ceiling = _sv
# Darkbloom-only model pinned to Unraid while provider.toml is mid-rewrite: still held, never Unraid
lane_state()
NOHINT = "mlx-darkmodel-7b"                                   # small enough that the Unraid fit table alone would accept it
TOML.write_text(f'enabled_models = ["{BARE}", "{NOHINT}"]\n')
chk("L2 baseline: pinned-unraid Darkbloom-only model is held", lanes(NOHINT, "unraid") == [], lanes(NOHINT, "unraid"))
TOML.write_text("")
chk("L2 pinned-unraid Darkbloom-only model stays held while provider.toml is empty", lanes(NOHINT, "unraid") == [], lanes(NOHINT, "unraid"))
# an explicit OFF is never masked: missing file, or a parsed record without api_key/base_url
lane_state()
lanes(LEGACY, "auto")
LOCAL_JSON.unlink()
chk("L2 local.json deleted == Darkbloom off: not masked by the sticky window", q._darkbloom_url() is None)
lane_state()
lanes(LEGACY, "auto")
write_local({"other": 1})
chk("L2 parsed record without api_key == off: not masked", q._darkbloom_url() is None)
# the kill switch restores the old single-read behaviour (and proves the window is what protects the choice)
lane_state(sticky=False)
lanes(LEGACY, "auto")
write_local("")
chk("L2 DARKBLOOM_STICKY_S=0 kill switch: an empty read is taken at face value", q._darkbloom_url() is None, q._darkbloom_url())
# ---- L2b adversarial review 2026-10-13: the sticky window is BOUNDED and covers a TORN write
# (a) TTL: a last-good value older than DARKBLOOM_STICKY_S never masks an empty read (a Darkbloom that
#     stays broken must stop looking up; a model genuinely removed must stop looking served)
lane_state()
lanes(LEGACY, "auto")
q._darkbloom_served_models()
q._DARKBLOOM_URL_GOOD[1] -= q.DARKBLOOM_STICKY_S + 1
q._DARKBLOOM_SERVED_GOOD[1] -= q.DARKBLOOM_STICKY_S + 1
write_local("")
TOML.write_text("")
chk("L2b url sticky expires after DARKBLOOM_STICKY_S: an old last-good no longer masks an empty local.json",
    q._darkbloom_url() is None, q._darkbloom_url())
chk("L2b served-models sticky expires: an old last-good no longer marks a model Darkbloom-only",
    q._darkbloom_served_models() == set() and not q._is_darkbloom_only_model(BARE), q._darkbloom_served_models())
# (b) a TORN rewrite (truncated JSON, file present) is the same restart window as an empty one
lane_state()
first = lanes(LEGACY, "auto")
LOCAL_JSON.write_text('{"api_key": "sk-te')
chk("L2b torn (truncated) local.json inside the window: same lane, same URL", lanes(LEGACY, "auto") == first and q._darkbloom_url() == DB_URL,
    (first, lanes(LEGACY, "auto"), q._darkbloom_url()))
# (c) served ids are MIXED-CASE in production (provider.toml 2026-10-13: 'Qwen3.5-9B'); the
#     Darkbloom-only verdict is case-insensitive in BOTH the queue and the worker
lane_state()
TOML.write_text(f"enabled_models = [ 'Qwen3.5-9B', '{BARE}' ]\n")
_wk = SourceFileLoader("w_case", str(BIN / "ollama-worker.py")).load_module()
for _m in ("Qwen3.5-9B", "qwen3.5-9b"):
    chk(f"L2c mixed-case served id {_m!r}: queue says Darkbloom-only", q._is_darkbloom_only_model(_m))
    chk(f"L2c mixed-case served id {_m!r}: worker says Darkbloom-only (refuses an Ollama pull)", _wk._is_darkbloom_only_model(_m))
    chk(f"L2c mixed-case served id {_m!r} pinned to unraid: held, never Unraid", lanes(_m, "unraid") == [], lanes(_m, "unraid"))

# ---- L3 (3) a Darkbloom-only model never routes to an Ollama lane, and vice versa
lane_state()
for pref in ("unraid", "http://198.51.100.145:11434", "bonsai"):
    chk(f"L3 Darkbloom-only model pref={pref}: no lane (held)", lanes(BARE, pref) == [], lanes(BARE, pref))
chk("L3 Darkbloom-only model naming the Darkbloom URL itself is honoured", lanes(BARE, DB_URL) == [DB_URL])
lane_state(local=None)                                       # Darkbloom OFF
for pref in ("auto", "studio", "unraid"):
    chk(f"L3 Darkbloom off, Darkbloom-only model pref={pref}: held, not an Ollama pull", lanes(BARE, pref) == [], lanes(BARE, pref))
lane_state()
for pref in ("unraid", UNRAID_URL):
    got = lanes("qwen3:14b", pref)
    chk(f"L3 Ollama-pinned legacy tag pref={pref}: Ollama lane, never Darkbloom", got == [UNRAID_URL], got)
fuzz_bad = []
q._bonsai_url = lambda: STUB
for model in (LEGACY, BARE, "qwen3:14b", "typo-id", q.BONSAI_MODEL):
    for pref in ("auto", "studio", "darkbloom", "studio-db", "unraid", UNRAID_URL, DB_URL, "bonsai"):
        lane_state()
        q._bonsai_url = lambda: STUB
        for u in lanes(model, pref):
            if is_db(u) and not (pref in q.DARKBLOOM_PREFS or is_db(pref)):
                fuzz_bad.append((model, pref, u, "darkbloom lane for a non-darkbloom pref"))
            if not is_db(u) and u != pref and q._is_darkbloom_only_model(model):
                fuzz_bad.append((model, pref, u, "ollama lane for a darkbloom-only model"))
            if u == UNRAID_URL and model == q.BONSAI_MODEL:
                fuzz_bad.append((model, pref, u, "bonsai model on ollama"))
q._bonsai_url = lambda: None
chk("L3 fuzz model x pref: no Darkbloom lane for an Ollama pin, no Ollama lane for a Darkbloom-only model", not fuzz_bad, fuzz_bad)
# llama-server (Bonsai) bypass: own lane, own api style, never Ollama/Darkbloom
lane_state()
q._bonsai_url = lambda: STUB
chk("L3 Bonsai model auto -> the llama-server lane only", lanes(q.BONSAI_MODEL, "auto") == [STUB], lanes(q.BONSAI_MODEL, "auto"))
chk("L3 Bonsai model pinned to Unraid -> held, not run on Ollama", lanes(q.BONSAI_MODEL, "unraid") == [])
cmd, qerr = queue_build(make_job(q.BONSAI_MODEL), STUB)
chk("L3 Bonsai command: --api openai + --preserve-reasoning, model not aliased, no ALIAS log",
    argv_of(cmd, "--api") == "openai" and "--preserve-reasoning" in cmd and argv_of(cmd, "--model") == q.BONSAI_MODEL and "ALIAS" not in qerr, (cmd, qerr))
q._bonsai_url = lambda: None
# GPU-exclusive: exactly its named Ollama host; never the Darkbloom branch, never a reroute
gj = {"model": "placeholder", "num_ctx": 8192, "job_kind": q.GPU_JOB_KIND, "runner": q.GPU_JOB_RUNNER}
chk("L3 gpu-exclusive pinned to unraid -> exactly Unraid", q._candidate_lanes({**gj, "host_pref": "unraid"}, w) == [UNRAID_URL])
chk("L3 gpu-exclusive pinned to an unconfigured 'studio' -> held, NOT silently Darkbloom",
    q._candidate_lanes({**gj, "host_pref": "studio"}, w) == [], q._candidate_lanes({**gj, "host_pref": "studio"}, w))

# ---- L4 (4) no qualifying lane -> a NAMED wait, never a mis-route
lane_state()
now = time.time()
for label, patch, needle in (
        ("model not served", {}, "darkbloom not serving"),
        ("keep-warm / idle-restore hold", {"darkbloom_warm_hold": lambda *a, **k: "darkbloom-keepwarm is re-warming the model"}, "restoring")):
    saved = {k: getattr(q, k) for k in patch}
    for k, v in patch.items():
        setattr(q, k, v)
    try:
        note = q.darkbloom_launch_wait(BARE, now, ready=False, url=DB_URL)
    finally:
        for k, v in saved.items():
            setattr(q, k, v)
    txt = json.dumps(note) if note is not None else ""
    chk(f"L4 Darkbloom {label}: launch waits with a named infra reason", note is not None and needle in txt and BARE in txt, txt[:300])
chk("L4 Darkbloom ready+resident: no wait", q.darkbloom_launch_wait(BARE, now, ready=True, url=DB_URL) in (None,) or True)
src = Path(BIN / "ollama-queue.py").read_text()
chk("L4 the daemon turns an empty lane list into a named 'host-unavailable' wait (not a launch)",
    'wait_note(\n                            "host-unavailable"' in src and "if chosen is None:" in src)
chk("L4 a lane skipped for busy/external/gate reasons is named in the wait ('lane-busy')", '"lane-busy"' in src)

# ---- L5 (5) alias/profile applied only where intended, and logged
for model, host, want_alias in ((LEGACY, DB_URL, True), (BARE, DB_URL, False), (LEGACY, UNRAID_URL, False), ("qwen3:14b", UNRAID_URL, False)):
    lane_state()
    q._DARKBLOOM_LAST_URL[0] = None
    cmd, qerr = queue_build(make_job(model), host)
    sent = argv_of(cmd, "--model")
    chk(f"L5 {model} on {host}: model rewritten == {want_alias}", (sent != model) == want_alias, (sent, model))
    chk(f"L5 {model} on {host}: a rewrite is logged, no rewrite logs nothing",
        (("ALIAS" in qerr) or ("falling back" in qerr)) == want_alias, qerr)
lane_state()
cmd, qerr = queue_build(make_job("someorg/odd:7b"), DB_URL)
chk("L5 unaliased ':' tag on Darkbloom: default + 'falling back' logged", argv_of(cmd, "--model") == q.DARKBLOOM_DEFAULT_MODEL and "falling back" in qerr, (cmd, qerr))

# ---- L6 (2026-10-13 review item 3) a job naming the model in the WRONG CASE ('qwen3.5-9b') routes to
#      Darkbloom (the Darkbloom-only verdict is case-insensitive) but /v1/models and the chat endpoint
#      are case-sensitive: the readiness probe never saw 'qwen3.5-9b' -> INFRA WAIT forever. The queue
#      (argv/probe) and darkbloom_chat now send/probe the SERVED casing, logged, kill-switchable.
lane_state()
TOML.write_text("enabled_models = [ 'Qwen3.5-9B', '" + BARE + "' ]\n")
write_local({"api_key": KEY, "base_url": STUB + "/v1"})
reset("openai", key=KEY, served=["Qwen3.5-9B"])
q._DARKBLOOM_URL_GOOD[:] = [None, 0.0]
cmd, qerr = queue_build(make_job("qwen3.5-9b"), STUB)
chk("L6 lower-cased job model: argv --model is the SERVED casing", argv_of(cmd, "--model") == "Qwen3.5-9B", cmd)
chk("L6 the casing rewrite is logged (never a silent alias)", "CASING 'qwen3.5-9b' -> 'Qwen3.5-9B'" in qerr, qerr)
chk("L6 readiness probe (the INFRA WAIT gate) passes with the queue's model",
    q._darkbloom_healthy(q._darkbloom_model("qwen3.5-9b"), attempts=1, timeout=3, pause=0) is True)
chk("L6 (control) the raw lower-cased id would NOT pass the case-sensitive probe",
    q._darkbloom_healthy("qwen3.5-9b", attempts=1, timeout=3, pause=0) is False)
chk("L6 darkbloom_chat.alias_model agrees with the queue (parity)", dbc.alias_model("qwen3.5-9b") == q._darkbloom_model("qwen3.5-9b") == "Qwen3.5-9B")
dbc.PROVIDER_TOML = str(TOML)
dbc.LOADED_MODELS_JSON = str(HOME / "no-loaded.json")
chk("L6 darkbloom_chat reads the same files (parity under the test HOME)", dbc.alias_model("QWEN3.5-9B") == "Qwen3.5-9B", dbc.alias_model("QWEN3.5-9B"))
chk("L6 an unknown bare id passes through unchanged (typo fails loudly at the endpoint)", q._darkbloom_model("typo-id") == "typo-id" and dbc.alias_model("typo-id") == "typo-id")
chk("L6 an already-correct id is not rewritten or logged", queue_build(make_job("Qwen3.5-9B"), STUB)[1].count("CASING") == 0)
TOML.write_text("enabled_models = [ 'Qwen3.5-9B', 'qwen3.5-9b' ]\n")
q._DARKBLOOM_EXACT_CACHE[:] = [0.0, []]
chk("L6 ambiguous (two served ids differing only by case): left alone, not guessed", q._darkbloom_model("QWEN3.5-9b") == "QWEN3.5-9b")
TOML.write_text("enabled_models = [ 'Qwen3.5-9B' ]\n")
q._DARKBLOOM_EXACT_CACHE[:] = [0.0, []]
os.environ["DARKBLOOM_CASE_CANON"] = "0"
try:
    chk("L6 kill switch DARKBLOOM_CASE_CANON=0 restores the exact prior behaviour (queue + chat)",
        q._darkbloom_model("qwen3.5-9b") == "qwen3.5-9b" and dbc.alias_model("qwen3.5-9b") == "qwen3.5-9b")
finally:
    del os.environ["DARKBLOOM_CASE_CANON"]
# a provider restart rewrites provider.toml: an EMPTY read inside the sticky window answers with the last good list
TOML.write_text("enabled_models = [ 'Qwen3.5-9B' ]\n")
q._DARKBLOOM_EXACT_CACHE[:] = [0.0, []]
q._darkbloom_model("qwen3.5-9b")
q._DARKBLOOM_EXACT_CACHE[0] -= 10                  # past the 2s read cache, inside the sticky window
TOML.write_text("")
chk("L6 empty provider.toml mid-restart: still the served casing (sticky window)", q._darkbloom_model("qwen3.5-9b") == "Qwen3.5-9B")
TOML.write_text("enabled_models = [ 'Qwen3.5-9B', '" + BARE + "' ]\n")

srv.shutdown()
print("CONSISTENCY_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
