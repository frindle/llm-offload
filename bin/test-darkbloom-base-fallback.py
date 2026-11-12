#!/usr/bin/env python3
"""Darkbloom-only jobs must never land on an Ollama lane (2026-10-04).

ROOT CAUSE (reproduced): Darkbloom 0.9.17 rewrites ~/.darkbloom/local.json with ONLY
`api_key` -- no `base_url`. queue._darkbloom_url() then returned None, every
DARKBLOOM_PREFS branch was skipped, and host_pref 'studio' (not in the Ollama host
table any more) fell to the auto path whose only host is Unraid. 27de99a9d14c,
1977f33a6919, 759f2ebc2ea1 (and edf9e823be47) launched on unraid and crashed in the
worker's Ollama /api/pull (HTTP 500). The worker also sent NO Bearer key to
Darkbloom (base unknown) -- the 401 'missing or invalid local API key'.

Pins:
  * all three readers (queue, worker, darkbloom_chat) treat an api_key-only record
    as the local endpoint at http://127.0.0.1:8000
  * queue: a Darkbloom-only model holds (no lane) instead of overflowing to Ollama,
    for any pref and even with Darkbloom unconfigured; Ollama-tag models unaffected
  * worker: Bearer header sent for the api_key-only record; ensure_model_ready
    refuses to pull a Darkbloom-only model on an Ollama host BEFORE any network call

Usage:  python3 ~/bin/test-darkbloom-base-fallback.py
Revert: QUEUE=~/bin/ollama-queue.py.bak-dbbase WORKER=~/bin/ollama-worker.py.bak-dbbase \
        DBCHAT=~/bin/darkbloom_chat.py.bak-dbbase python3 this.py   -> FAIL
"""
import os
import sys
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE", HERE / "ollama-queue.py"))
WORKER = Path(os.environ.get("WORKER", HERE / "ollama-worker.py"))
DBCHAT = Path(os.environ.get("DBCHAT", HERE / "darkbloom_chat.py"))
DB_MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
SERVED = {DB_MODEL, "qwen3.5-9b"}
UNRAID = "http://198.51.100.145:11434"
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name
          + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


os.environ.pop("DARKBLOOM_BASE_URL", None)
q = SourceFileLoader("q_under_test", str(QUEUE)).load_module()
w = SourceFileLoader("w_under_test", str(WORKER)).load_module()
d = SourceFileLoader("d_under_test", str(DBCHAT)).load_module()
FAKE_KEY = "test-not-a-real-key"
KEY_ONLY = {"api_key": FAKE_KEY}

# ---- 1. base resolution in all three readers ----
q._darkbloom_record = lambda: dict(KEY_ONLY)
chk("queue: api_key-only local.json -> local endpoint", q._darkbloom_url(), "http://127.0.0.1:8000")
q._darkbloom_record = lambda: {"base_url": "http://127.0.0.1:8000/v1", "api_key": FAKE_KEY}
chk("queue: explicit base_url still wins (/v1 stripped)", q._darkbloom_url(), "http://127.0.0.1:8000")
q._darkbloom_record = lambda: {}
chk("queue: no record -> None (Darkbloom off)", q._darkbloom_url(), None)
d._record = lambda: dict(KEY_ONLY)
chk("darkbloom_chat: api_key-only -> local endpoint", d.base_url(), "http://127.0.0.1:8000")
d._record = lambda: {}
chk("darkbloom_chat: no record -> None", d.base_url(), None)
wb = getattr(w, "_darkbloom_base_from_record", None)
chk("worker: api_key-only -> local endpoint", wb and wb(dict(KEY_ONLY)), "http://127.0.0.1:8000")

# ---- 2. worker auth: the Bearer key is sent to the local endpoint (the 401s) ----
w._darkbloom_local_record = lambda: dict(KEY_ONLY)
if not hasattr(w, "_darkbloom_local_record") or "_darkbloom_local_record" not in WORKER.read_text():
    chk("worker reads local.json through _darkbloom_local_record", False, True)
else:
    chk("worker: Bearer header for the api_key-only record",
        w._darkbloom_auth_headers("http://127.0.0.1:8000"), {"Authorization": "Bearer " + FAKE_KEY})
    chk("worker: never sends the key to an Ollama host", w._darkbloom_auth_headers(UNRAID), {})

# ---- 3. queue routing ----
class FakeW:  # offline host table: Studio's Ollama retired, Unraid the only Ollama host
    KNOWN_OLLAMA_HOSTS = {"unraid": UNRAID}
    BIG_HOST_NAME = "studio"
    SMALL_HOST_NAME = "unraid"


q._apply_fit_routing = lambda job, ww: ("unraid", "fits unraid (stub)", False)
q._host_url_for = lambda ww, n: ww.KNOWN_OLLAMA_HOSTS.get(n)
q._model_size_cached = lambda ww, m: 10 * 1024 ** 3
q._host_budget_or_zero = lambda ww, n: 40 * 1024 ** 3
q._darkbloom_served_models = lambda: set(SERVED)
q._darkbloom_record = lambda: dict(KEY_ONLY)
for pref in ("studio", "auto", "darkbloom"):
    chk(f"api_key-only record: Darkbloom model, pref {pref!r} -> Darkbloom lane",
        q._candidate_lanes({"id": "x", "model": DB_MODEL, "host_pref": pref}, FakeW),
        ["http://127.0.0.1:8000"])
chk("Darkbloom-only model with pref 'unraid' HOLDS (no Ollama lane)",
    q._candidate_lanes({"id": "x", "model": DB_MODEL, "host_pref": "unraid"}, FakeW), [])
chk("Darkbloom-only model with an explicit Ollama URL HOLDS",
    q._candidate_lanes({"id": "x", "model": DB_MODEL, "host_pref": UNRAID}, FakeW), [])
chk("an Ollama-tag model on 'unraid' is unaffected",
    q._candidate_lanes({"id": "x", "model": "qwen3:14b", "host_pref": "unraid"}, FakeW), [UNRAID])
q._darkbloom_record = lambda: {}
chk("Darkbloom unconfigured: Darkbloom-only model on 'studio' HOLDS, never Unraid",
    q._candidate_lanes({"id": "x", "model": DB_MODEL, "host_pref": "studio"}, FakeW), [])
chk("Darkbloom unconfigured: an Ollama-tag model on auto still uses Ollama",
    q._candidate_lanes({"id": "x", "model": "qwen3:14b", "host_pref": "auto"}, FakeW), [UNRAID])
isdb = getattr(q, "_is_darkbloom_only_model", lambda m, served=None: None)
chk("':tag' names are never Darkbloom-only", isdb("qwen3.8:27b-q4_K_M", SERVED), False)
chk("an unknown bare id is not Darkbloom-only", isdb("llama3", SERVED), False)

# ---- 4. darkbloom_chat served-model parsing (provider.toml shape) ----
sm = getattr(d, "served_models", None)
toml = ("[backend]\nenabled_models = [ 'qwen3.6-35b-a3b-vl-mtp-mxfp8', 'Qwen3.5-9B' ]\n"
        "preload_models = [ 'gemma-x' ]\nport = 8100\n")
chk("served_models parses enabled + preload + loaded",
    sm and sm(toml_text=toml, loaded=["Extra-Model"]),
    {"qwen3.6-35b-a3b-vl-mtp-mxfp8", "qwen3.5-9b", "gemma-x", "extra-model"})

# ---- 5. worker refuses the pull BEFORE any network call ----
w._darkbloom_served_models = lambda: set(SERVED)
calls = []


def _no_net(*a, **k):
    calls.append(a)
    raise AssertionError("network call made")


_real = urllib.request.urlopen
urllib.request.urlopen = _no_net
try:
    w.ensure_model_ready(UNRAID, DB_MODEL, 0.2, 8192)
    msg = "(returned)"
except RuntimeError as e:
    msg = str(e)
except AssertionError as e:
    msg = "network: " + str(e)
except Exception as e:  # noqa: BLE001
    msg = f"{type(e).__name__}: {e}"
finally:
    urllib.request.urlopen = _real
chk("ensure_model_ready refuses a Darkbloom-only model on an Ollama host",
    msg.startswith("refusing to pull"), True)
chk("...with zero network calls (no /api/tags, no LAN copy, no /api/pull)", calls, [])

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
