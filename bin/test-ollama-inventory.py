#!/usr/bin/env python3
"""test-ollama-inventory.py -- the queue must not read an Ollama host's /api/tags once per job.

WHY (2026-11-06): on Ollama v0.40.15 every GET /api/tags runs GetModel() for EVERY pulled model, i.e. it parses
GGUF metadata off the array disks (~215 GB on the Unraid host). The queue daemon's pre-launch fit pass
(_apply_fit_routing -> _model_size_cached -> worker _get_model_size_on_host) sent one per newly queued job --
nearly all of them Darkbloom jobs whose routing never reads that size -- and ensure_model_ready sent another per
dispatch. The fix: (1) a Darkbloom-only / Bonsai model is answered WITHOUT touching any host, (2) the worker keeps
one shared, file-backed per-host model inventory (read at most once per OLLAMA_INVENTORY_TTL_S), (3) liveness
checks use GET /api/version.

Everything runs against FAKE Ollama servers on 127.0.0.1 that count requests per path. A socket guard fails any
attempt to reach a non-loopback address, and every worker gets a fresh OLLAMA_DISPATCH_HOME whose hosts.json
points at the fake -- the real hosts are never contacted.

Groups (a "KEEP" check pins behaviour that must NOT change; it passes on the unpatched code too):
  A  the queue's fit pass: 0 Ollama requests for a Darkbloom-only / Bonsai model, 1 read for N other jobs
  B  first lookup = 1 /api/tags; inside the TTL 0 more (also from a 2nd process); after the TTL 1 more; env knobs
  C  a missing model refreshes at most once per OLLAMA_INVENTORY_MISS_REFRESH_S
  D  invalidate_host_inventory -> the next lookup fetches (and reaches other processes)
  E  host down -> None; a second call inside 60 s makes no new connection attempt; a failed read keeps the list
  F  ensure_model_ready: fresh inventory + model present = 1 /api/version, 0 /api/tags (+ pull / LAN copy /
     stale-list / gate / ':latest' / spillover-exception behaviour)
  G  pick_host shares the inventory     H  studio-research.model_size_bytes shares it     I  file + robustness

Usage: python3 test-ollama-inventory.py          Prints OLLAMA_INVENTORY_OK (exit 0) or "<n> FAIL" (exit 1).
       QUEUE=... WORKER=... STUDIO=... override the files under test (default: next to this file)."""
import collections
import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE", HERE / "ollama-queue.py"))
WORKER = Path(os.environ.get("WORKER", HERE / "ollama-worker.py"))
STUDIO = Path(os.environ.get("STUDIO", HERE / "studio-research.py"))

TMP = Path(tempfile.mkdtemp(prefix="ollama-inventory-test-"))
HOME = TMP / "home"
for _sub in (".darkbloom", ".config/darkbloom", "bin"):
    (HOME / _sub).mkdir(parents=True)
for _k in list(os.environ):
    if _k.startswith(("OLLAMA_DISPATCH", "OLLAMA_INVENTORY", "DARKBLOOM_", "BONSAI_")) \
            or _k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
        del os.environ[_k]
os.environ.update(HOME=str(HOME), OLLAMA_QUEUE_NO_NOTIFY="1", PYTHONDONTWRITEBYTECODE="1",
                  NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

# Any connect() to a non-loopback address is refused (and recorded): the real Unraid Ollama must never be reached.
GUARD_SRC = '''
import socket as _s
_BLOCKED = []
_LOOP = ("127.0.0.1", "::1", "localhost")
_rc, _rce = _s.socket.connect, _s.socket.connect_ex
def _chk(addr):
    if isinstance(addr, tuple) and str(addr[0]) not in _LOOP:
        _BLOCKED.append(addr)
        raise OSError("NETGUARD: blocked non-loopback connect to %r" % (addr,))
def _c(self, addr, *a, **k):
    _chk(addr)
    return _rc(self, addr, *a, **k)
def _ce(self, addr, *a, **k):
    _chk(addr)
    return _rce(self, addr, *a, **k)
_s.socket.connect, _s.socket.connect_ex = _c, _ce
'''
_guard_ns = {}
exec(GUARD_SRC, _guard_ns)
_BLOCKED = _guard_ns["_BLOCKED"]

fails = 0


def chk(name, cond, detail=""):
    global fails
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else f": {str(detail)[:300]}"))
    fails += 0 if cond else 1


TAGS, VERSION, PULL, CHAT, PS = "GET /api/tags", "GET /api/version", "POST /api/pull", "POST /api/chat", "GET /api/ps"
USABLE = int(12 * 1024 ** 3 * 0.8)           # the Unraid card's configured usable_bytes (hosts.json)
GB = 1_000_000_000


def norm(m):
    n, _, t = m.partition(":")
    return f"{n}:{t or 'latest'}"


class Fake:
    """A fake Ollama on 127.0.0.1:<ephemeral> that counts every request per 'METHOD /path'."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = collections.Counter()
        self.reset()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                path = self.path.split("?")[0]
                with fake.lock:
                    fake.counts["GET " + path] += 1
                    models, tags_status, version_status = dict(fake.models), fake.tags_status, fake.version_status
                    delay, ps = fake.tags_delay, list(fake.ps_models)
                if path == "/api/tags":
                    if delay:
                        time.sleep(delay)
                    if tags_status != 200:
                        return self._reply(tags_status, {"error": "boom"})
                    return self._reply(200, {"models": [{"name": n, "model": n, "size": s, "digest": "d",
                                                         "details": {}} for n, s in models.items()]})
                if path == "/api/version":
                    if version_status != 200:
                        return self._reply(version_status, {"error": "boom"})
                    return self._reply(200, {"version": "0.40.15"})
                if path == "/api/ps":
                    return self._reply(200, {"models": ps})
                self._reply(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0]
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with fake.lock:
                    fake.counts["POST " + path] += 1
                    pull_status, chat_status = fake.pull_status, fake.chat_status
                    have = norm(str(body.get("model") or "")) in fake.models
                if path == "/api/pull":
                    if pull_status != 200:
                        return self._reply(pull_status, {"error": "pull failed"})
                    with fake.lock:
                        fake.models[norm(str(body.get("model")))] = fake.pull_size
                        fake.pulls.append(body.get("model"))
                    return self._reply(200, {"status": "success"})
                if path == "/api/chat":
                    if chat_status:
                        return self._reply(chat_status, {"error": "boom"})
                    if not have:
                        return self._reply(404, {"error": f"model '{body.get('model')}' not found"})
                    return self._reply(200, {"message": {"role": "assistant", "content": "ok"}, "done": True})
                self._reply(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def reset(self, models=None):
        with self.lock:
            self.counts.clear()
            self.models = {norm(k): v for k, v in (models or {}).items()}
            self.tags_status = self.version_status = self.pull_status = 200
            self.chat_status = None
            self.tags_delay = 0.0
            self.pull_size = 1_500_000_000
            self.ps_models, self.pulls = [], []

    def n(self, key):
        with self.lock:
            return self.counts[key]

    def total(self):
        with self.lock:
            return sum(self.counts.values())

    def zero(self):
        with self.lock:
            self.counts.clear()


class DropServer:
    """A 'down' host at the connection level: accepts every TCP connection and closes it at once; counts them."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.accepted = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                c.close()
            except OSError:
                pass


FAKE, FAKE2, DROP = Fake(), Fake(), DropServer()
_seq = [0]
ENV_KEYS = ("OLLAMA_DISPATCH_HOME", "OLLAMA_INVENTORY_TTL_S", "OLLAMA_INVENTORY_MISS_REFRESH_S")


class Clock:
    """Shifts the worker's clock hooks (_inv_now wall / _inv_mono monotonic). Harmless on code without the hooks."""

    def __init__(self, mod):
        self.wall = self.mono = 0.0
        mod._inv_now = lambda: time.time() + self.wall
        mod._inv_mono = lambda: time.monotonic() + self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def new_dispatch(hosts=None):
    _seq[0] += 1
    d = TMP / f"dispatch{_seq[0]}"
    d.mkdir()
    (d / "hosts.json").write_text(json.dumps(
        {"hosts": hosts or {"unraid": {"url": FAKE.url, "usable_bytes": USABLE}}}))
    return d


def load_module(path, name, dispatch, env=None):
    """Import `path` as a fresh module bound to OLLAMA_DISPATCH_HOME=`dispatch` (+ env knobs read at import)."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["OLLAMA_DISPATCH_HOME"] = str(dispatch)
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def new_worker(env=None, hosts=None):
    """-> (fresh ollama-worker.py module, its dispatch dir, a Clock on it). Own memo, own inventory file."""
    d = new_dispatch(hosts)
    w = load_module(WORKER, f"qinv_worker_{_seq[0]}", d, env)
    return w, d, Clock(w)


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def run_child(body, dispatch, env=None, timeout=180, wall_offset=0.0):
    """Run `body` in a SECOND python process that imported the same worker with the same OLLAMA_DISPATCH_HOME
    (its wall clock shifted by `wall_offset` seconds, to match a parent whose Clock was advanced).
    -> (returncode, the json after its 'RESULT ' line or None, output tail)."""
    src = GUARD_SRC + textwrap.dedent(f"""
        import importlib.util, json, sys, time
        sys.dont_write_bytecode = True
        sys.path.insert(0, {str(HERE)!r})
        _spec = importlib.util.spec_from_file_location("qinv_child_worker", {str(WORKER)!r})
        w = importlib.util.module_from_spec(_spec); sys.modules[_spec.name] = w; _spec.loader.exec_module(w)
        if hasattr(w, "_inv_now"):
            w._inv_now = lambda: time.time() + {float(wall_offset)!r}
    """) + textwrap.dedent(body) + "\nprint('BLOCKED', json.dumps(_BLOCKED))\n"
    e = {**os.environ, "OLLAMA_DISPATCH_HOME": str(dispatch), **(env or {})}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, timeout=timeout, env=e,
                       cwd=str(TMP))
    out = p.stdout + p.stderr
    res = blocked = None
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            res = json.loads(line[7:])
        elif line.startswith("BLOCKED "):
            blocked = json.loads(line[8:])
    if blocked:
        chk("child process never reached a non-loopback address", False, blocked)
    return p.returncode, res, out[-600:]


def inv_file(d):
    """The parsed ollama-inventory.json of a dispatch dir ({} when absent or unparsable)."""
    try:
        return json.loads((d / "ollama-inventory.json").read_text())
    except (OSError, ValueError):
        return {}


def inv_models(d, url):
    return (inv_file(d).get("hosts") or {}).get(url, {}).get("models")


_QUEUE = []


def load_queue():
    """The queue module (HOME is the temp dir, so ~/.darkbloom and ~/.config/darkbloom are the temp ones)."""
    if not _QUEUE:
        _QUEUE.append(load_module(QUEUE, "qinv_queue", new_dispatch()))
    return _QUEUE[0]


def run(fn):
    print(f"\n## {fn.__name__}: {(fn.__doc__ or '').strip().splitlines()[0]}")
    out, err = sys.stdout, sys.stderr
    try:
        fn()
    except Exception as e:  # an unpatched build lacks the new functions: that is a RED result, not a crashed run
        sys.stdout, sys.stderr = out, err
        chk(f"{fn.__name__} ran to the end", False,
            f"{type(e).__name__}: {e} @ {traceback.format_exc().strip().splitlines()[-3:-1]}")
    finally:
        sys.stdout, sys.stderr = out, err


# ====================================================================================================== A
DB_IDS = [f"db-model-{i}" for i in range(12)]
(HOME / ".config" / "darkbloom" / "provider.toml").write_text(
    "enabled_models = [" + ", ".join(f"'{m}'" for m in DB_IDS) + "]\n")


def group_a_queue_fit_pass():
    """A. the daemon's fit pass makes 0 Ollama requests for a Darkbloom-only / Bonsai model, and 1 read for N others."""
    q = load_queue()
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    q._size_cache.clear()
    chk("setup: the stubbed Darkbloom served-model list makes db-model-0 Darkbloom-only",
        q._is_darkbloom_only_model(DB_IDS[0]) is True and q._is_darkbloom_only_model("qwen3:14b") is False)
    decisions = {}
    for i, m in enumerate(DB_IDS):
        for pref in ("auto", "studio", "darkbloom", "unraid", "http://127.0.0.1:9"):
            job = {"id": f"j{i}", "model": m, "host_pref": pref}
            decisions[(m, pref)] = q._apply_fit_routing(job, w)
    chk("fit pass for 12 Darkbloom-only models x 5 host prefs: 0 requests to the Ollama host",
        FAKE.total() == 0, dict(FAKE.counts))
    chk("...every model size answered None", all(q._model_size_cached(w, m) is None for m in DB_IDS))
    want = {k: q._fit_route_decision(k[0], k[1], False, None, USABLE, 0, {}) for k in decisions}
    chk("KEEP: routing for a Darkbloom-only model is the known_size=None decision for every pref",
        decisions == want, [k for k in decisions if decisions[k] != want[k]][:3])
    q._size_cache.clear()
    chk("the Bonsai model: size None and 0 requests", q._model_size_cached(w, q.BONSAI_MODEL) is None
        and FAKE.total() == 0, dict(FAKE.counts))
    chk("the Bonsai model through the fit pass (host_pref bonsai): 0 requests",
        q._apply_fit_routing({"id": "b", "model": q.BONSAI_MODEL, "host_pref": "bonsai"}, w) is not None
        and FAKE.total() == 0, dict(FAKE.counts))
    # control: the fake IS wired -- a model that lives on the Ollama host is still sized from it
    q._size_cache.clear()
    chk("control: a model on the Ollama host is still sized (9.3 GB)",
        q._model_size_cached(w, "qwen3:14b") == 9_300_000_000)
    chk("...with exactly 1 GET /api/tags", FAKE.n(TAGS) == 1, dict(FAKE.counts))
    for i in range(20):
        q._apply_fit_routing({"id": f"n{i}", "model": f"legacy-model-{i}:7b", "host_pref": "unraid"}, w)
    chk("20 more newly queued non-Darkbloom jobs (20 distinct models): still exactly 1 GET /api/tags in total",
        FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    # the guard: if the Darkbloom check raises, fall back to the old host lookup (no exception, right answer)
    q._size_cache.clear()
    real = q._is_darkbloom_only_model
    q._is_darkbloom_only_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        got = q._model_size_cached(w, "qwen3:14b")
        got2 = q._model_size_cached(w, DB_IDS[0])
    finally:
        q._is_darkbloom_only_model = real
    chk("KEEP: a raising Darkbloom check falls back to the host lookup (size found, no exception)",
        got == 9_300_000_000 and got2 is None, (got, got2))


# ====================================================================================================== B
def group_b_ttl():
    """B. first lookup = 1 /api/tags; inside the TTL 0 more (also in a 2nd process); after the TTL 1 more."""
    w, d, clock = new_worker()
    ttl = getattr(w, "OLLAMA_INVENTORY_TTL_S", 4 * 3600)
    FAKE.reset({"qwen3:14b": 9_300_000_000, "m1:latest": 5_000_000_000})

    def size(m):
        with quiet():
            return w._get_model_size_on_host(FAKE.url, m)

    chk("first lookup returns the model's size", size("qwen3:14b") == 9_300_000_000)
    chk("first lookup = exactly 1 GET /api/tags", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    got = [size("qwen3:14b"), size("m1"), size("m1:latest"), size("qwen3:14b")]
    chk("more lookups inside the TTL return the right sizes (':latest' normalised)",
        got == [9_300_000_000, 5_000_000_000, 5_000_000_000, 9_300_000_000], got)
    chk("...and send 0 more GET /api/tags", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    body = f"print('RESULT', json.dumps([w._get_model_size_on_host({FAKE.url!r}, 'qwen3:14b'), " \
           f"w._get_model_size_on_host({FAKE.url!r}, 'm1')]))"
    rc, res, tail = run_child(body, d)
    chk("a second process (same OLLAMA_DISPATCH_HOME) gets the sizes", rc == 0 and res == [9_300_000_000, 5_000_000_000],
        (rc, res, tail))
    chk("...and sends 0 more GET /api/tags", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    clock.advance(ttl + 1)
    chk("after the TTL the next lookup still answers", size("qwen3:14b") == 9_300_000_000)
    chk("...with exactly 1 more GET /api/tags", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    size("m1")
    size("qwen3:14b")
    chk("...then 0 more again", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    qi = f"print('RESULT', json.dumps(w._get_model_size_on_host({FAKE.url!r}, 'qwen3:14b')))"
    rc2, res2, tail2 = run_child(qi, d, wall_offset=clock.wall)
    chk("a 2nd process right after the refresh: gets the size with 0 more reads (the refreshed entry is shared)",
        rc2 == 0 and res2 == 9_300_000_000 and FAKE.n(TAGS) == 2, (rc2, res2, FAKE.n(TAGS), tail2))

    # env knobs
    w7, _, _ = new_worker(env={"OLLAMA_INVENTORY_TTL_S": "7", "OLLAMA_INVENTORY_MISS_REFRESH_S": "3"})
    chk("OLLAMA_INVENTORY_TTL_S / OLLAMA_INVENTORY_MISS_REFRESH_S env override the defaults",
        (getattr(w7, "OLLAMA_INVENTORY_TTL_S", None), getattr(w7, "OLLAMA_INVENTORY_MISS_REFRESH_S", None)) == (7.0, 3.0))
    wd, _, _ = new_worker()
    chk("defaults: TTL 4 h, miss refresh 900 s", (getattr(wd, "OLLAMA_INVENTORY_TTL_S", None),
                                                  getattr(wd, "OLLAMA_INVENTORY_MISS_REFRESH_S", None)) == (14400.0, 900.0))
    for bad in ("junk", "-5", ""):
        wb, _, _ = new_worker(env={"OLLAMA_INVENTORY_TTL_S": bad})
        chk(f"an unusable OLLAMA_INVENTORY_TTL_S ({bad!r}) falls back to the default",
            getattr(wb, "OLLAMA_INVENTORY_TTL_S", None) == 14400.0, getattr(wb, "OLLAMA_INVENTORY_TTL_S", None))
    # TTL 0 = never cache: the rollback lever (the old read-every-time behaviour)
    w0, _, _ = new_worker(env={"OLLAMA_INVENTORY_TTL_S": "0"})
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        a, b = w0._get_model_size_on_host(FAKE.url, "qwen3:14b"), w0._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("KEEP: OLLAMA_INVENTORY_TTL_S=0 -> every lookup reads the host (rollback lever)",
        a == b == 9_300_000_000 and FAKE.n(TAGS) == 2, FAKE.n(TAGS))


# ====================================================================================================== C
def group_c_miss_refresh():
    """C. a missing model refreshes at most once per OLLAMA_INVENTORY_MISS_REFRESH_S."""
    w, d, clock = new_worker()
    miss = getattr(w, "OLLAMA_INVENTORY_MISS_REFRESH_S", 900)
    FAKE.reset({"m1:latest": 5_000_000_000})

    def size(m):
        with quiet():
            return w._get_model_size_on_host(FAKE.url, m)

    chk("a model the host lacks -> None", size("ghost:7b") is None)
    chk("...1 GET /api/tags (the first read)", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    for _ in range(5):
        size("ghost:7b")
    chk("repeated misses inside the interval: still 1 GET /api/tags (a miss does not re-read)",
        FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    clock.advance(miss + 1)                       # far inside the 4 h TTL
    FAKE.models["ghost:7b"] = 7_000_000_000       # pulled by hand, outside the queue
    chk("a miss older than the interval re-reads and finds the hand-pulled model", size("ghost:7b") == 7_000_000_000)
    chk("...with exactly 1 more GET /api/tags", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    for _ in range(5):
        size("still-missing:7b")
    chk("a model that stays missing, right after that refresh: no further read", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    clock.advance(miss + 1)
    size("still-missing:7b")
    size("still-missing:7b")
    size("m1")
    chk("next interval: exactly 1 more read for two more misses (and a present model never reads)",
        FAKE.n(TAGS) == 3, FAKE.n(TAGS))
    wl, _, cl = new_worker(env={"OLLAMA_INVENTORY_MISS_REFRESH_S": "30"})
    FAKE.reset({"m1:latest": 1})
    with quiet():
        wl._get_model_size_on_host(FAKE.url, "x:1")
        cl.advance(29)
        wl._get_model_size_on_host(FAKE.url, "x:1")
        n29 = FAKE.n(TAGS)
        cl.advance(2)
        wl._get_model_size_on_host(FAKE.url, "x:1")
    chk("a custom OLLAMA_INVENTORY_MISS_REFRESH_S=30: no refresh at 29 s, one at 31 s", (n29, FAKE.n(TAGS)) == (1, 2),
        (n29, FAKE.n(TAGS)))


# ====================================================================================================== D
def group_d_invalidate():
    """D. after invalidate_host_inventory (a pull / LAN copy) the next lookup fetches."""
    w, d, clock = new_worker(hosts={"unraid": {"url": FAKE.url, "usable_bytes": USABLE},
                                    "other": {"url": FAKE2.url, "usable_bytes": USABLE}})
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE2.reset({"qwen3:14b": 9_300_000_000})

    def size(url, m):
        with quiet():
            return w._get_model_size_on_host(url, m)

    for _ in range(2):
        size(FAKE.url, "qwen3:14b")
        size(FAKE2.url, "qwen3:14b")
    chk("warm: 1 read per host", (FAKE.n(TAGS), FAKE2.n(TAGS)) == (1, 1), (FAKE.n(TAGS), FAKE2.n(TAGS)))
    inv = getattr(w, "invalidate_host_inventory", None)
    chk("the worker defines invalidate_host_inventory(host_url)", callable(inv))
    FAKE.models["fresh-pull:7b"] = 4_000_000_000                 # what a pull adds
    if callable(inv):
        with quiet():
            inv(FAKE.url)
    chk("after invalidate the next lookup fetches and sees the new model", size(FAKE.url, "fresh-pull:7b") == 4_000_000_000)
    chk("...exactly 1 more read on that host", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    chk("...and none on the other host (its entry was kept)", FAKE2.n(TAGS) == 1 and size(FAKE2.url, "qwen3:14b") == 9_300_000_000
        and FAKE2.n(TAGS) == 1, FAKE2.n(TAGS))
    size(FAKE.url, "qwen3:14b")
    chk("...then cached again", FAKE.n(TAGS) == 2, FAKE.n(TAGS))
    if callable(inv):
        with quiet():
            try:
                inv("http://127.0.0.1:9")
                inv("")
                inv(None)
                ok = True
            except Exception:
                ok = False
        chk("invalidate of an unknown / empty host never raises", ok)
    # invalidation made by ANOTHER process is seen by this one (the file is the truth)
    FAKE.models["by-child:7b"] = 3_000_000_000
    rc, res, tail = run_child(f"w.invalidate_host_inventory({FAKE.url!r}); print('RESULT', json.dumps(True))", d)
    chk("a second process can invalidate the shared entry", rc == 0 and res is True, (rc, tail))
    chk("...and this process then re-reads", size(FAKE.url, "by-child:7b") == 3_000_000_000 and FAKE.n(TAGS) == 3, FAKE.n(TAGS))


# ====================================================================================================== E
def group_e_down():
    """E. host down -> None; a second call inside 60 s makes no new connection attempt; a failure keeps the list."""
    w, d, clock = new_worker()

    def size(url, m):
        with quiet():
            return w._get_model_size_on_host(url, m)

    chk("a host that drops connections -> None", size(DROP.url, "qwen3:14b") is None)
    chk("...1 connection attempt", DROP.accepted == 1, DROP.accepted)
    chk("a second call inside 60 s -> None", size(DROP.url, "qwen3:14b") is None)
    chk("...makes NO new connection attempt", DROP.accepted == 1, DROP.accepted)
    hmi = getattr(w, "host_model_inventory", None)
    with quiet():
        r = hmi(DROP.url) if callable(hmi) else "no host_model_inventory"
    chk("host_model_inventory(down host) is None, again without a new attempt", r is None and DROP.accepted == 1, (r, DROP.accepted))
    clock.advance(59)
    size(DROP.url, "x:1")
    chk("59 s after the failure: still no new attempt", DROP.accepted == 1, DROP.accepted)
    clock.advance(2)
    size(DROP.url, "x:1")
    chk("61 s after the failure: exactly one new attempt", DROP.accepted == 2, DROP.accepted)
    # the same through an HTTP-level failure
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE.tags_status = 500
    chk("an HTTP 500 from /api/tags -> None", size(FAKE.url, "qwen3:14b") is None)
    size(FAKE.url, "qwen3:14b")
    chk("...and the 2nd call inside 60 s does not ask again", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    # a failed refresh must not erase a good list
    w2, d2, clock2 = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w2._get_model_size_on_host(FAKE.url, "qwen3:14b")
    ttl = getattr(w2, "OLLAMA_INVENTORY_TTL_S", 4 * 3600)
    clock2.advance(ttl + 1)
    FAKE.tags_status = 500
    with quiet():
        r = w2._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("TTL expired + the host failing -> None (unreachable keeps its meaning)", r is None, r)
    chk("...and the failed refresh did not erase the stored list", inv_models(d2, FAKE.url) == {"qwen3:14b": 9_300_000_000},
        inv_models(d2, FAKE.url))
    FAKE.tags_status = 200
    n_before = FAKE.n(TAGS)
    with quiet():
        r = w2._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("host back, inside the 60 s memo: still None with no request", r is None and FAKE.n(TAGS) == n_before, (r, FAKE.n(TAGS)))
    clock2.advance(61)
    with quiet():
        r = w2._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("60 s later the host is read again (exactly 1 request) and the size is back",
        r == 9_300_000_000 and FAKE.n(TAGS) == n_before + 1, (r, FAKE.n(TAGS), n_before))


# ====================================================================================================== F
def ready(w, host, model, **kw):
    """ensure_model_ready as the dispatcher calls it; -> (None | the RuntimeError text)."""
    try:
        with quiet():
            w.ensure_model_ready(host, model, 0.2, 4096, **kw)
        return None
    except RuntimeError as e:
        return str(e)


def group_f_ensure_model_ready():
    """F. ensure_model_ready: liveness = /api/version; presence + base size from the inventory."""
    # ---- (f) the headline: a fresh inventory and the model present
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")                 # warms the shared inventory (the one read)
    FAKE.zero()
    err = ready(w, FAKE.url, "qwen3:14b")
    chk("fresh inventory + model present: ensure_model_ready returns", err is None, err)
    chk("...exactly 1 GET /api/version", FAKE.n(VERSION) == 1, dict(FAKE.counts))
    chk("...0 GET /api/tags", FAKE.n(TAGS) == 0, dict(FAKE.counts))
    chk("KEEP: ...no /api/pull, one warm-up /api/chat", FAKE.n(PULL) == 0 and FAKE.n(CHAT) == 1, dict(FAKE.counts))
    # ---- cold inventory: one read, and it is shared with the next dispatch
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    err = ready(w, FAKE.url, "qwen3:14b")
    chk("cold inventory: 1 /api/version + exactly 1 /api/tags", err is None and FAKE.n(VERSION) == 1 and FAKE.n(TAGS) == 1,
        (err, dict(FAKE.counts)))
    ready(w, FAKE.url, "qwen3:14b")
    ready(w, FAKE.url, "qwen3:14b")
    chk("two more dispatches of the same model: 0 more /api/tags", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    chk("KEEP: ':latest' normalisation -- a bare name matches name:latest, nothing is pulled",
        (FAKE.models.update({"agentic:latest": 2 * GB}) or True) and ready(w, FAKE.url, "agentic") is None
        and FAKE.n(PULL) == 0, dict(FAKE.counts))
    # ---- the list is older than the call and lacks the model, which was pulled by hand: ONE forced re-read, no pull
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    FAKE.models["by-hand:7b"] = 4 * GB
    clock.advance(5)
    FAKE.zero()
    err = ready(w, FAKE.url, "by-hand:7b")
    chk("a hand-pulled model the cached list lacks: found after ONE forced re-read, no pull",
        err is None and FAKE.n(TAGS) == 1 and FAKE.n(PULL) == 0, (err, dict(FAKE.counts)))
    # ---- really missing: read once (forced), then pull, then the list is invalidated
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    clock.advance(5)
    FAKE.zero()
    err = ready(w, FAKE.url, "ghost:7b")
    chk("a model missing everywhere is pulled (once)", err is None and FAKE.pulls == ["ghost:7b"], (err, FAKE.pulls))
    chk("...after exactly 1 forced re-read of the host list", FAKE.n(TAGS) == 1, dict(FAKE.counts))
    chk("...and the pull invalidated the shared list (no entry for the host)", inv_models(d, FAKE.url) is None,
        inv_models(d, FAKE.url))
    FAKE.zero()
    with quiet():
        sz = w._get_model_size_on_host(FAKE.url, "ghost:7b")
    chk("...so the next lookup re-reads and sees the pulled model", sz == FAKE.pull_size and FAKE.n(TAGS) == 1, (sz, dict(FAKE.counts)))
    # ---- cold + missing: the read made during the call is itself the re-read (no second read before the pull)
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    err = ready(w, FAKE.url, "ghost2:7b")
    chk("cold inventory + missing model: ONE /api/tags, then the pull", err is None and FAKE.n(TAGS) == 1 and FAKE.n(PULL) == 1,
        (err, dict(FAKE.counts)))
    # ---- LAN copy path also invalidates
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    clock.advance(5)

    def fake_run(cmd, **kw):
        FAKE.models[norm(cmd[-1])] = 2 * GB                      # the LAN copy puts the model on the host
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    w.LAN_MOUNT_ROOT = "/"                                        # os.path.ismount("/") is True
    w.subprocess = types.SimpleNamespace(run=fake_run)
    FAKE.zero()
    err = ready(w, FAKE.url, "lan-copied:7b")
    chk("LAN copy succeeds -> no /api/pull", err is None and FAKE.n(PULL) == 0, (err, dict(FAKE.counts)))
    chk("...and the LAN copy invalidated the shared list", inv_models(d, FAKE.url) is None, inv_models(d, FAKE.url))
    # ---- stale positive: cached list says present, the model was removed outside the queue
    w, d, clock = new_worker()
    FAKE.reset({"stale:7b": 4 * GB})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "stale:7b")
    FAKE.models.pop("stale:7b")                                   # `ollama rm` by hand
    clock.advance(5)
    FAKE.zero()
    err = ready(w, FAKE.url, "stale:7b")
    chk("KEEP: a model removed outside the queue (cached list stale) is recovered by a pull", err is None
        and FAKE.pulls == ["stale:7b"], (err, FAKE.pulls, dict(FAKE.counts)))
    chk("...with 1 list re-read and exactly one retry of the check (2 /api/version)", FAKE.n(TAGS) == 1 and FAKE.n(VERSION) == 2,
        dict(FAKE.counts))
    # ---- a non-404 warm-up failure is NOT retried and does not drop the list
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    FAKE.chat_status = 500
    FAKE.zero()
    err = ready(w, FAKE.url, "qwen3:14b")
    chk("a warm-up HTTP 500 propagates", err is not None and "Chat request failed" in err, err)
    chk("...without a re-read, a pull or a second check", FAKE.n(TAGS) == 0 and FAKE.n(PULL) == 0 and FAKE.n(VERSION) == 1,
        dict(FAKE.counts))
    # ---- host down: the same wording
    w, d, clock = new_worker()
    err = ready(w, DROP.url, "qwen3:14b")
    chk("KEEP: host down -> RuntimeError 'could not reach <host>...'", err is not None and f"could not reach {DROP.url}" in err, err)
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE.tags_status = 500
    err = ready(w, FAKE.url, "qwen3:14b")
    chk("KEEP: /api/version up but the model list unreadable -> 'could not reach <host>...'",
        err is not None and f"could not reach {FAKE.url}" in err, err)
    FAKE.tags_status = 200
    FAKE.version_status = 503
    w, d, clock = new_worker()
    FAKE.zero()
    err = ready(w, FAKE.url, "qwen3:14b")
    chk("/api/version failing is the liveness failure (no model-list read)", err is not None and f"could not reach {FAKE.url}" in err
        and FAKE.n(TAGS) == 0, (err, dict(FAKE.counts)))
    FAKE.version_status = 200
    # ---- the base-size gate and the spillover exceptions are untouched
    w, d, clock = new_worker()
    FAKE.reset({"huge:70b": 13_790_000_000, "qwen3:14b": 9_300_000_000})
    FAKE.zero()
    err = ready(w, FAKE.url, "huge:70b")
    chk("KEEP: base weights over the host's usable budget are refused", err is not None and "already exceeds" in err
        and "usable budget" in err, err)
    chk("KEEP: ...before any warm-up or pull", FAKE.n(CHAT) == 0 and FAKE.n(PULL) == 0, dict(FAKE.counts))
    exc = next(iter(w.UNRAID_SPILLOVER_EXCEPTIONS), None)
    if exc:
        FAKE.models[norm(exc)] = 13_790_000_000
        with quiet():
            w.invalidate_host_inventory(FAKE.url) if hasattr(w, "invalidate_host_inventory") else None
        err = ready(w, FAKE.url, exc)
        chk(f"KEEP: an UNRAID_SPILLOVER_EXCEPTIONS model ({exc}) skips the base-size gate", err is None, err)
    # ---- Darkbloom-only still refused before any network call
    w, d, clock = new_worker()
    w._darkbloom_served_models = lambda: {"db-model-0"}
    FAKE.reset({})
    err = ready(w, FAKE.url, "db-model-0")
    chk("KEEP: a Darkbloom-only model on an Ollama host is refused with no request at all", err is not None
        and err.startswith("refusing to pull") and FAKE.total() == 0, (err, dict(FAKE.counts)))


# ====================================================================================================== G
def group_g_pick_host():
    """G. pick_host reads each host's list through the shared inventory."""
    w, d, clock = new_worker(hosts={"studio": {"url": FAKE2.url, "usable_bytes": 44 * 1024 ** 3},
                                    "unraid": {"url": FAKE.url, "usable_bytes": USABLE}})
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE2.reset({})
    with quiet():
        picks = [w.pick_host("qwen3:14b") for _ in range(4)]
    chk("KEEP: Studio free -> Studio (same decision as before)", picks == [FAKE2.url] * 4, picks)
    chk("4 pick_host calls: exactly 1 GET /api/tags per host", (FAKE.n(TAGS), FAKE2.n(TAGS)) == (1, 1),
        (FAKE.n(TAGS), FAKE2.n(TAGS)))
    FAKE2.ps_models = [{"name": "busy:1b"}]
    with quiet():
        pick = w.pick_host("qwen3:14b")
    chk("KEEP: Studio busy + the model fits Unraid -> Unraid", pick == FAKE.url, pick)


# ====================================================================================================== H
def group_h_studio_research():
    """H. studio-research.model_size_bytes shares the worker's inventory (exact-name semantics unchanged)."""
    d = new_dispatch()
    sr = load_module(STUDIO, "qinv_studio_research", d)
    FAKE.reset({"qwen3:14b": 9_300_000_000, "m1:latest": 5_000_000_000})
    with quiet():
        sizes = [sr.model_size_bytes(FAKE.url, "qwen3:14b") for _ in range(4)]
    chk("model_size_bytes returns the size", sizes == [9_300_000_000] * 4, sizes)
    chk("4 calls: exactly 1 GET /api/tags", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    with quiet():
        exact = (sr.model_size_bytes(FAKE.url, "m1"), sr.model_size_bytes(FAKE.url, "m1:latest"))
        n_before_slash = FAKE.n(TAGS)
        slash = sr.model_size_bytes(FAKE.url + "/", "qwen3:14b")
        n_after_slash = FAKE.n(TAGS)
        unknown_down = (sr.model_size_bytes(FAKE.url, "nope:1"), sr.model_size_bytes(DROP.url, "qwen3:14b"))
    chk("KEEP: exact-name match as before ('m1' does not match 'm1:latest'; the full name does)",
        exact == (0, 5_000_000_000), exact)
    chk("KEEP: an unknown model -> 0, a host that is down -> 0", unknown_down == (0, 0), unknown_down)
    chk("a trailing slash on the host shares the same entry (no extra read)",
        slash == 9_300_000_000 and n_after_slash == n_before_slash == 1, (slash, n_before_slash, n_after_slash))
    real = sr._worker
    sr._worker = types.SimpleNamespace()                         # a worker module without the inventory
    n0 = FAKE.n(TAGS)
    try:
        with quiet():
            sz = sr.model_size_bytes(FAKE.url, "qwen3:14b")
    finally:
        sr._worker = real
    chk("KEEP: without a worker inventory it falls back to its own direct read", sz == 9_300_000_000 and FAKE.n(TAGS) == n0 + 1,
        (sz, FAKE.n(TAGS), n0))


# ====================================================================================================== I
def group_i_file_and_robustness():
    """I. the shared file: format, atomicity, corruption, clock skew, read-only home, thread coalescing."""
    w, d, clock = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000, "m1": 5_000_000_000})
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    ent = (inv_file(d).get("hosts") or {}).get(FAKE.url) or {}
    chk("ollama-inventory.json is keyed by host url: fetched_at + normalised {name:tag: size}",
        ent.get("models") == {"qwen3:14b": 9_300_000_000, "m1:latest": 5_000_000_000}
        and abs((ent.get("fetched_at") or 0) - time.time()) < 120, ent)
    chk("no temp file is left behind (atomic replace)", not [p.name for p in d.glob("ollama-inventory.json.*")],
        [p.name for p in d.iterdir()])
    with quiet():
        w._get_model_size_on_host(" " + FAKE.url + "/ ", "qwen3:14b")
    chk("a host url with a trailing slash / spaces shares the entry", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    # corrupt file -> treated as empty, re-read, rewritten valid
    (d / "ollama-inventory.json").write_text("{ this is not json")
    with quiet():
        sz = w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("a corrupt inventory file is ignored: size still answered, host re-read once",
        sz == 9_300_000_000 and FAKE.n(TAGS) == 2, (sz, FAKE.n(TAGS)))
    chk("...and the file is valid JSON again", inv_models(d, FAKE.url) is not None, inv_file(d))
    (d / "ollama-inventory.json").write_text(json.dumps({"hosts": {FAKE.url: {"fetched_at": "x", "models": []}, "z": 5}}))
    with quiet():
        sz = w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("a structurally wrong file is ignored the same way", sz == 9_300_000_000 and FAKE.n(TAGS) == 3, (sz, FAKE.n(TAGS)))
    # a fetched_at in the future (clock skew between machines/processes) is never trusted as 'fresh'
    f = json.loads((d / "ollama-inventory.json").read_text())
    f["hosts"][FAKE.url]["fetched_at"] = time.time() + 7200
    (d / "ollama-inventory.json").write_text(json.dumps(f))
    with quiet():
        w._get_model_size_on_host(FAKE.url, "qwen3:14b")
    chk("an entry stamped in the future is treated as stale (re-read once)", FAKE.n(TAGS) == 4, FAKE.n(TAGS))
    # the file cannot be written (its parent is a regular file): keep working from memory, one read
    w2, d2, clock2 = new_worker()
    blocker = TMP / "blocker"
    blocker.write_text("x")
    w2.OLLAMA_INVENTORY_FILE = blocker / "inv.json"
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    with quiet():
        got = [w2._get_model_size_on_host(FAKE.url, "qwen3:14b") for _ in range(3)]
    chk("an unwritable inventory file: sizes still answered", got == [9_300_000_000] * 3, got)
    chk("...from memory: exactly 1 GET /api/tags for 3 lookups", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    # threads on a cold inventory coalesce into ONE read
    w3, d3, clock3 = new_worker()
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE.tags_delay = 0.5
    out = []

    def worker_thread():
        out.append(w3._get_model_size_on_host(FAKE.url, "qwen3:14b"))

    ts = [threading.Thread(target=worker_thread) for _ in range(8)]
    with quiet():                       # redirect once, in the main thread (redirect_stdout is process-wide)
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
    chk("8 threads on a cold inventory: all get the size", out == [9_300_000_000] * 8, out)
    chk("...and send exactly 1 GET /api/tags between them", FAKE.n(TAGS) == 1, FAKE.n(TAGS))
    FAKE.tags_delay = 0.0
    # two hosts side by side in one file
    w4, d4, clock4 = new_worker(hosts={"a": {"url": FAKE.url, "usable_bytes": USABLE}, "b": {"url": FAKE2.url, "usable_bytes": USABLE}})
    FAKE.reset({"qwen3:14b": 9_300_000_000})
    FAKE2.reset({"other:1b": 1 * GB})
    with quiet():
        w4._get_model_size_on_host(FAKE.url, "qwen3:14b")
        w4._get_model_size_on_host(FAKE2.url, "other:1b")
    hosts = inv_file(d4).get("hosts") or {}
    chk("both hosts live in the one file, each with its own list",
        sorted(hosts) == sorted([FAKE.url, FAKE2.url]) and hosts[FAKE2.url]["models"] == {"other:1b": 1 * GB}, sorted(hosts))


for g in (group_a_queue_fit_pass, group_b_ttl, group_c_miss_refresh, group_d_invalidate, group_e_down,
          group_f_ensure_model_ready, group_g_pick_host, group_h_studio_research, group_i_file_and_robustness):
    run(g)

print()
chk("netguard: the test process never tried a non-loopback address", not _BLOCKED, _BLOCKED)
print("OLLAMA_INVENTORY_OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
