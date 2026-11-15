#!/usr/bin/env python3
"""The container reads Darkbloom's model configs through the Mac status service (2026-11-13).

Back-test B1 on the standby copy found the container could not see Darkbloom's HF cache (it is on the Mac): every
Darkbloom window fell back to 64K (the Mac derives 98K for the 35B) and the second-slot memory check never had a KV
size, so it always serialized. In the container (QUEUE_AGENT_URL set) _darkbloom_model_config now looks the model up
in the map from GET /v1/darkbloom-model-configs, cached, and never fetched while the state flock is held.
Run on claude-sandbox:  python3 test-darkbloom-model-configs-remote.py"""
import importlib.util
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
FAILS = []
CFG = {"num_hidden_layers": 8, "num_attention_heads": 16, "num_key_value_heads": 2, "head_dim": 128,
       "max_position_embeddings": 262144}          # 8192 KV bytes per token


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def main():
    os.environ.pop("QUEUE_AGENT_URL", None)
    os.environ.pop("DARKBLOOM_CTX", None)
    q = load(QUEUE, "q_dbcfg")
    calls = []

    def get(path, timeout=None):
        calls.append(path)
        return answers.pop(0) if answers else None

    # --- the cache: fetch, keep, retry after a failure, keep the last good map, nothing under the lock
    cache = {"at": 0.0, "ok": False, "configs": {}}
    answers = [{"configs": {"models--Org--M": CFG, "models--bad": [1]}}]
    got = q._darkbloom_model_configs_remote(now=1000.0, get=get, cache=cache)
    check("first call fetches; keys lowercased; non-object configs dropped", got, {"models--org--m": CFG})
    check("asks the model-configs route", calls, ["/v1/darkbloom-model-configs"])
    q._darkbloom_model_configs_remote(now=1000.0 + q.DARKBLOOM_CFG_REMOTE_TTL - 1, get=get, cache=cache)
    check("inside the hour: no second fetch", len(calls), 1)
    answers = [None]
    got = q._darkbloom_model_configs_remote(now=1000.0 + q.DARKBLOOM_CFG_REMOTE_TTL + 1, get=get, cache=cache)
    check("refresh fails: last good map kept", (len(calls), got), (2, {"models--org--m": CFG}))
    q._darkbloom_model_configs_remote(now=1000.0 + q.DARKBLOOM_CFG_REMOTE_TTL + 30, get=get, cache=cache)
    check("after a failure: no retry inside a minute", len(calls), 2)
    answers = [{"configs": {}}]
    q._darkbloom_model_configs_remote(now=1000.0 + q.DARKBLOOM_CFG_REMOTE_TTL + 62, get=get, cache=cache)
    check("after a failure: retried after a minute", len(calls), 3)
    cold = {"at": 0.0, "ok": False, "configs": {}}
    q._STATE_LOCK_DEPTH = 1
    try:
        got = q._darkbloom_model_configs_remote(now=5.0, get=get, cache=cold)
    finally:
        q._STATE_LOCK_DEPTH = 0
    check("state flock held: cache only, no network", (len(calls), got), (3, {}))
    boom = {"at": 0.0, "ok": False, "configs": {}}

    def raises(path, timeout=None):
        raise OSError("down")
    check("a raising client is 'no answer'", q._darkbloom_model_configs_remote(now=5.0, get=raises, cache=boom), {})

    # --- _darkbloom_model_config: the map in the container, the HF cache on the Mac
    q._DARKBLOOM_CFG_REMOTE.update(at=1e12, ok=True, configs={"models--org--m": CFG})
    os.environ["QUEUE_AGENT_URL"] = "http://127.0.0.1:9"
    try:
        check("container: model found in the map (case-insensitive)", q._darkbloom_model_config("ORG--m"), CFG)
        check("container: unknown model -> None", q._darkbloom_model_config("other"), None)
        check("container: no model -> None", q._darkbloom_model_config(None), None)
        check("container: opt-in max window from the same config", q._darkbloom_model_config_ctx("org--m"), 262144)
        # the real point: the derived window comes from the Mac's config, not the 64K fallback
        q.darkbloom_status = lambda *a, **k: {"usable_gb": 8.0, "concurrency": {}, "default_cap": 4}
        q._darkbloom_model = lambda m: m
        got = q.darkbloom_ctx_ceiling("org--m", now=1.0, probe=lambda m: None, cache={})
        check("container: Darkbloom window derived from the Mac's config (not the fallback)",
              (got, got != q.DARKBLOOM_CTX_FALLBACK), (131072, True))
    finally:
        os.environ.pop("QUEUE_AGENT_URL", None)
    root = Path(tempfile.mkdtemp(prefix="dbcfg-hf-"))
    snap = root / "models--Org--M" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"num_hidden_layers": 3}')
    q._darkbloom_cache_dir = lambda: root
    check("Mac (no QUEUE_AGENT_URL): reads the HF cache, the map is not used",
          q._darkbloom_model_config("org--m"), {"num_hidden_layers": 3})
    q._darkbloom_cache_dir = lambda: None
    check("Mac: no cache configured -> None", q._darkbloom_model_config("org--m"), None)

    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failing")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
