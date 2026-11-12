#!/usr/bin/env python3
"""Darkbloom cold-load tolerance (2026-10-03).

Darkbloom hosts more models than it keeps resident and LAZY-LOADS a hosted model
on its first request; with every model slot busy it REFUSES the load ("N model
slot(s) are active; cannot load '<id>'") instead of queueing it. Asserts:

  worker
    * the slot-busy refusal is recognised (exact 0.9.17 provider strings), and a
      generic 503 is NOT mistaken for it
    * on the Darkbloom lane it is waited out uncharged: more refusals than
      CHAT_RETRIES still end in success (call_ollama + streaming connect)
    * the wait is BOUNDED (SLOT_BUSY_WAIT_S), then normal accounting fails it
    * a non-Darkbloom host (Unraid) is untouched: slot text gets no special wait
    * the tool-calling preflight (the FIRST request = the lazy load) gets
      WARMUP_TIMEOUT_S on Darkbloom, 30s elsewhere
    * a Darkbloom 5xx at preflight is a server error, NOT "does not support
      tool-calling"; a non-Darkbloom 500 still IS (llama-server --jinja case)
  capacity (2026-10-03): HTTP 429 'Provider capacity is temporarily unavailable.'
    is the same bounded, uncharged wait in worker (incl. preflight) and runner
  darkbloom_chat (runner path: code-review-agent, studio-research)
    * slot-busy refusals waited out without spending the 3 transient attempts
    * bounded; a 422 body still reaches the caller intact

Run: python3 ~/bin/test-darkbloom-coldload.py   (no network, no GPU)
"""
import importlib.util
import io
import os
import sys
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
DBCHAT = Path(os.environ.get("DBCHAT_SRC") or HERE / "darkbloom_chat.py")
DB = "http://127.0.0.1:8000"
UNRAID = "http://198.51.100.145:11434"
BUSY = b'{"error":{"message":"2 model slot(s) are active; cannot load \'gemma-4-26b-qat-4bit\'"}}'
BUSY2 = b"1 cached model slot(s) are active; try again when a request finishes"
CAPACITY = b'{"error":{"type":"invalid_request_error","message":"Provider capacity is temporarily unavailable."}}'
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


OK_BODY = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{}}'


def scripted(seq, timeouts):
    seq = list(seq)

    def urlopen(req, timeout=None):
        timeouts.append(timeout)
        url = getattr(req, "full_url", str(req))
        if seq:
            code, body = seq.pop(0)
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))
        return FakeResp(OK_BODY)
    return urlopen


def worker_tests(w):
    w.log = lambda *a, **k: None
    w.LANE_5XX_SETTLE_S = 0
    w.SLOT_BUSY_STEP_S = 0.0005
    w.SLOT_BUSY_WAIT_S = 0.05
    w._lane_restart_wait = lambda h, **k: ("up", 0.0, False)   # /health OK throughout

    check("slot-busy string 1 recognised", w.is_darkbloom_slot_busy(BUSY.decode()), True)
    check("slot-busy string 2 recognised", w.is_darkbloom_slot_busy(BUSY2.decode()), True)
    check("eviction-disabled wording recognised", w.is_darkbloom_slot_busy(
        "2 model slot(s) are occupied and eviction is disabled for this load; cannot load 'x'"), True)
    check("a generic 503 is NOT slot-busy", w.is_darkbloom_slot_busy("HTTP Error 503: Service Unavailable"), False)

    def run_call(seq, host=DB):
        t = []
        orig = w.urllib.request.urlopen
        w.urllib.request.urlopen = scripted(seq, t)
        try:
            r = w.call_ollama(host, "m", [{"role": "user", "content": "hi"}], 0.0, 4096,
                              tools=False, api_style="openai")
            return ("ok", len(t))
        except RuntimeError as e:
            return ("err", len(t))
        finally:
            w.urllib.request.urlopen = orig

    n = w.CHAT_RETRIES + 3
    check(f"{n} slot-busy refusals (> CHAT_RETRIES) then success -> ok, uncharged",
          run_call([(503, BUSY)] * n), ("ok", n + 1))
    check("slot-busy as a 4xx (409) is waited out too", run_call([(409, BUSY2)] * 2), ("ok", 3))
    r = run_call([(503, BUSY)] * 500)
    check("slot-busy wait is BOUNDED: endless refusals eventually fail", r[0], "err")
    check("...and the bound is the cumulative SLOT_BUSY_WAIT_S, not 500 tries", r[1] < 500, True)
    check("lane cause names the busy slots", "slots busy" in (w._LANE_LAST_CAUSE[0] or ""), True)
    check("a plain 503 (no slot text) still fails after CHAT_RETRIES",
          run_call([(503, b"")] * 5), ("err", w.CHAT_RETRIES + 1))
    check("UNRAID host: slot text gets no special wait (unchanged accounting)",
          run_call([(503, BUSY)] * 5, host=UNRAID), ("err", w.CHAT_RETRIES + 1))

    # --- tool-calling preflight = the lazy-load request ---
    def run_pre(seq, host):
        t = []
        try:
            w._tool_calling_preflight(host, "gemma-4-26b-qat-4bit", urlopen=scripted(seq, t),
                                      sleep=lambda s: None)
            return ("ok", t, "")
        except RuntimeError as e:
            return ("err", t, str(e))
    r = run_pre([], DB)
    check("Darkbloom preflight timeout = WARMUP_TIMEOUT_S (cold load)", r[1], [w.WARMUP_TIMEOUT_S])
    check("WARMUP_TIMEOUT_S is minutes, not seconds", w.WARMUP_TIMEOUT_S >= 600, True)
    r = run_pre([], UNRAID)
    check("non-Darkbloom preflight keeps its 30s timeout", r[1], [30])
    r = run_pre([(500, b"Internal inference failure")], DB)
    check("Darkbloom 500 at preflight -> error", r[0], "err")
    check("...NOT misread as 'does not support tool-calling'",
          "does not support tool-calling" in r[2], False)
    check("...says it is not a capability verdict", "NOT a tool-calling capability verdict" in r[2], True)
    r = run_pre([(500, b'{"error":{"message":"tools param requires --jinja flag"}}')], UNRAID)
    check("non-Darkbloom 500 still = no tool-calling (llama-server --jinja case unchanged)",
          "does not support tool-calling" in r[2], True)
    r = run_pre([(503, BUSY)] * 4, DB)
    check("Darkbloom preflight waits out slot-busy refusals then passes", (r[0], len(r[1])), ("ok", 5))

    # --- HTTP 429 "Provider capacity is temporarily unavailable" (live 2026-10-03,
    # f77f4c8def05 / e8ff2cfcba0f crashed at preflight on the first one) ---
    check("capacity 429 body recognised", w.is_darkbloom_slot_busy(CAPACITY.decode()), True)
    check("a plain rate-limit 429 is NOT capacity", w.is_darkbloom_slot_busy(
        "HTTP Error 429: Too Many Requests -- body: rate limit exceeded"), False)
    r = run_pre([(429, CAPACITY)] * 4, DB)
    check("Darkbloom preflight waits out capacity 429s then passes", (r[0], len(r[1])), ("ok", 5))
    r = run_pre([(429, CAPACITY)] * 500, DB)
    check("preflight capacity wait is BOUNDED", (r[0], len(r[1]) < 500), ("err", True))
    r = run_pre([(429, b"rate limit exceeded")], DB)
    check("preflight: a plain 429 still fails at once (no wait)", (r[0], len(r[1])), ("err", 1))
    check(f"call_ollama: {n} capacity 429s (> CHAT_RETRIES) then success -> ok, uncharged",
          run_call([(429, CAPACITY)] * n), ("ok", n + 1))
    check("UNRAID host: capacity text gets no special wait",
          run_call([(429, CAPACITY)] * 5, host=UNRAID)[0], "err")


def dbchat_tests(d):
    d.SLOT_BUSY_STEP_S = 0.0005
    d.SLOT_BUSY_WAIT_S = 0.05
    slept = []

    def run(seq):
        t = []
        try:
            out = d._post("http://x", {"m": 1}, 900, sleep=slept.append, urlopen=scripted(seq, t))
            return ("ok", len(t), "")
        except urllib.error.HTTPError as e:
            return ("err", len(t), e.read().decode())
    check("runner: 5 slot-busy refusals then success -> ok (3-attempt budget untouched)",
          run([(503, BUSY)] * 5)[:2], ("ok", 6))
    r = run([(503, BUSY)] * 500)
    check("runner: slot-busy wait is bounded", (r[0], r[1] < 500), ("err", True))
    r = run([(422, b'{"error":"schema"}')])
    check("runner: 422 propagates at once with its body intact", r, ("err", 1, '{"error":"schema"}'))
    check("runner: plain 503 still limited to 3 attempts", run([(503, b"")] * 9)[:2], ("err", 3))
    check("runner: capacity 429s waited out past the 3-attempt budget (was ~15s, 2 retries)",
          run([(429, CAPACITY)] * 6)[:2], ("ok", 7))
    r = run([(429, CAPACITY)] * 500)
    check("runner: capacity wait is bounded", (r[0], r[1] < 500), ("err", True))
    check("runner: plain 429 still limited to 3 attempts", run([(429, b"rate limit")] * 9)[:2], ("err", 3))


def main():
    w = load(WORKER, "ollama_worker_coldload")
    worker_tests(w)
    d = load(DBCHAT, "darkbloom_chat_coldload")
    dbchat_tests(d)
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
