#!/usr/bin/env python3
"""ollama-worker.py tool-calling preflight timeout (2026-10-08, Strata arm).

A slow-prefill server (Strata expert-offload) needs minutes for the tools-bearing
first request, so WORKER_PREFLIGHT_TIMEOUT_S overrides the 30s default. Asserted:
  1. unset / empty            -> 30
  2. valid integer            -> that value
  3. invalid / zero / negative-> 30 (never raises)
  4. _tool_calling_preflight passes the resolved value to urlopen(timeout=...)
Red-on-revert: hard-code 30 in _preflight_timeout_default -> 2 and 4 go red.
Run: python3 test-worker-preflight-timeout.py [--worker PATH]
"""
import importlib.util, json, os, sys, io
from contextlib import redirect_stdout, redirect_stderr

here = os.path.dirname(os.path.abspath(__file__))
worker = os.path.join(here, "ollama-worker.py")
if "--worker" in sys.argv:
    worker = sys.argv[sys.argv.index("--worker") + 1]
spec = importlib.util.spec_from_file_location("ow_pf", worker)
ow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ow)

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def with_env(v):
    if v is None:
        os.environ.pop("WORKER_PREFLIGHT_TIMEOUT_S", None)
    else:
        os.environ["WORKER_PREFLIGHT_TIMEOUT_S"] = v
    return ow._preflight_timeout_default()


check("unset -> 30", with_env(None), 30)
check("empty -> 30", with_env(""), 30)
check("900 honored", with_env("900"), 900)
check("invalid -> 30", with_env("abc"), 30)
check("zero -> 30", with_env("0"), 30)
check("negative -> 30", with_env("-5"), 30)


class Resp:
    status = 200

    def __init__(s, b):
        s._b = b

    def read(s):
        return s._b

    def __enter__(s):
        return s

    def __exit__(s, *a):
        return False

    def getcode(s):
        return 200


def seen_timeout(env):
    with_env(env)
    seen = {}

    def fake(req, timeout=None):
        seen["t"] = timeout
        return Resp(json.dumps({
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}}],
            "message": {"content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]},
        }).encode())

    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            ow._tool_calling_preflight("http://127.0.0.1:1", "m", urlopen=fake, sleep=lambda s: None)
    except BaseException:
        pass
    return seen.get("t")


check("preflight urlopen timeout default", seen_timeout(None), 30)
check("preflight urlopen timeout 900", seen_timeout("900"), 900)
check("preflight urlopen timeout invalid", seen_timeout("x"), 30)
os.environ.pop("WORKER_PREFLIGHT_TIMEOUT_S", None)
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
