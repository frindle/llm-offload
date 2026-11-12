#!/usr/bin/env python3
"""The queue holds Darkbloom only to the NEXT run's model, and a provider restart cannot push a
Studio-pinned job onto Unraid or refuse its enqueue (2026-11-07).

What happened (review-test runs, 2026-11-07 17:25): the queue's BloomGauge control asked for
gemma-4-26b (8-bit); the provider refused the load on memory and BloomGauge restored the 4-bit with a
full provider restart. While it restarted, ~/.darkbloom/local.json was missing, so:
  * 705248c04dfd (gemma-4-26b, --host studio-db) fell through _candidate_lanes to the auto branch,
    launched on Unraid and failed on Ollama's 404 "model 'gemma-4-26b' not found";
  * 6 `enqueue --host studio-db` calls were refused at once by _validate_host.
And the darkbloom-drift check counted every pending row's model, so runs queued behind a long run on
another model raised the 75-min needs-the owner alert (the owner: "why doesn't the queue just run them in
order and worry about 1 run at a time?").

Pins:
  * bloom_lane_head: the first running lane row, else the first lane row (held/off-lane rows skipped)
  * darkbloom_drift_need: only the head's model; rows behind it are not drift
  * bloomgauge_hook: the control's target is unchanged (same head)
  * _candidate_lanes: studio-db / darkbloom prefs hold (no lane) while the endpoint is missing
  * _darkbloom_url_wait / _validate_host: an enqueue waits out a restart, then refuses
  * _sticky_s_from_env: the new default argument leaves DARKBLOOM_STICKY_S's 60 s default alone

Usage:  python3 ~/bin/test-darkbloom-head-drift.py
Revert: QUEUE=/path/to/the-old/ollama-queue.py python3 this.py   -> FAIL (old code lacks the pins)
"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE", HERE / "ollama-queue.py"))
G8 = "gemma-4-26b"
Q36 = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
DB = "http://127.0.0.1:8000"
UNRAID = "http://198.51.100.145:11434"
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def need(name, fn):
    """The function under test, or a FAIL (old code) and None."""
    global fails
    f = getattr(q, fn, None)
    if f is None:
        print(f"FAIL - {name}: {fn} does not exist")
        fails += 1
    return f


os.environ.pop("DARKBLOOM_BASE_URL", None)
spec = importlib.util.spec_from_file_location("q_under_test", str(QUEUE))
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


def row(i, model, status="pending", pref="studio-db", **kw):
    return {"id": f"{i:012d}", "label": f"bench-review-{i}", "model": model, "status": status,
            "host_pref": pref, **kw}


# the 2026-11-07 shape: the 8-bit run at the head, the q36 runs queued behind it
g8 = [row(i, G8) for i in range(1, 6)]
q36 = [row(i, Q36) for i in range(100, 105)]

# ---- 1. bloom_lane_head ----
head = need("bloom_lane_head exists", "bloom_lane_head")
if head:
    chk("head: no jobs -> None", head([]), None)
    chk("head: the first pending lane row", head(g8 + q36)["id"], g8[0]["id"])
    run = row(200, Q36, status="running")
    chk("head: a running lane row wins over earlier pending rows", head(g8 + [run])["id"], run["id"])
    held = row(300, G8, user_hold=True)
    chk("head: a held row is skipped", head([held] + q36)["id"], q36[0]["id"])
    off = row(301, "qwen3:14b", pref="unraid")
    chk("head: an Unraid-pinned row is not lane work", head([off] + q36)["id"], q36[0]["id"])
    chk("head: done rows are not lane work", head([row(302, G8, status="done")] + q36)["id"], q36[0]["id"])

# ---- 2. darkbloom_drift_need: only the next run's model ----
dn = need("darkbloom_drift_need exists", "darkbloom_drift_need")
if dn:
    on = lambda j: True                                   # noqa: E731
    ident = lambda m: m                                   # noqa: E731
    chk("need: the 8-bit at the head, q36 behind -> only the 8-bit", dn(g8 + q36, on, ident), {G8})
    chk("need: served = the head's model -> no drift though q36 rows wait",
        q.darkbloom_drift(dn(g8 + q36, on, ident), [G8]), [])
    chk("need: served = a model only rows BEHIND need -> drift on the head's model",
        q.darkbloom_drift(dn(g8 + q36, on, ident), [Q36]), [G8])
    chk("need: a running q36 row -> its model", dn(g8 + [row(200, Q36, status="running")], on, ident), {Q36})
    chk("need: no lane work -> none", dn([row(1, G8, status="done")], on, ident), set())
    chk("need: the head is off the Darkbloom lane -> none", dn(g8, lambda j: False, ident), set())

    def boom(j):
        raise KeyError("host_pref")
    chk("need: an on_lane that raises -> none (never takes the tick down)", dn(g8, boom, ident), set())
    chk("need: a head with no model -> none", dn([row(1, None)], on, ident), set())
    chk("need: the model goes through model_of", dn(g8, on, lambda m: m.upper()), {G8.upper()})


# ---- 3. the control's target is the same head ----
class FakeBgt:
    _STATE = {}
    seen = []

    @staticmethod
    def wanted():
        return True

    @classmethod
    def run_async(cls, work, now):
        cls.seen.append(work)


q._darkbloom_record = lambda: {"api_key": "test-not-a-real-key"}
w1 = q.bloomgauge_hook({"jobs": g8 + q36}, lambda j: None, now=1.0, bgt=FakeBgt)
chk("hook: target = the head row's model", (w1 or {}).get("target"), G8)
w2 = q.bloomgauge_hook({"jobs": g8 + [row(200, Q36, status="running")]}, lambda j: None, now=1.0, bgt=FakeBgt)
chk("hook: a running row's model wins", (w2 or {}).get("target"), Q36)
w3 = q.bloomgauge_hook({"jobs": []}, lambda j: None, now=1.0, bgt=FakeBgt)
chk("hook: no lane work -> no target", (w3 or {}).get("target", "missing"), None)


# ---- 4. _candidate_lanes: a studio-db pin never lands on Unraid ----
class FakeW:  # offline host table: Studio's Ollama retired, Unraid the only Ollama host
    KNOWN_OLLAMA_HOSTS = {"unraid": UNRAID}
    BIG_HOST_NAME = "studio"
    SMALL_HOST_NAME = "unraid"


q._apply_fit_routing = lambda job, ww: ("unraid", "fits unraid (stub)", False)
q._host_url_for = lambda ww, n: ww.KNOWN_OLLAMA_HOSTS.get(n)
q._model_size_cached = lambda ww, m: 10 * 1024 ** 3
q._host_budget_or_zero = lambda ww, n: 40 * 1024 ** 3
q._darkbloom_served_models = lambda: {"gemma-4-26b-qat-4bit"}    # mid-switch: the 8-bit not listed
q._DARKBLOOM_URL_GOOD[0] = None
q._darkbloom_record = lambda: {"api_key": "test-not-a-real-key"}
for pref in ("studio-db", "darkbloom"):
    chk(f"lanes: endpoint up, --host {pref} -> the Darkbloom lane",
        q._candidate_lanes(row(1, G8, pref=pref), FakeW), [DB])
q._darkbloom_record = lambda: {}                                  # local.json missing (restart)
q._DARKBLOOM_URL_GOOD[0] = None
for pref in ("studio-db", "darkbloom"):
    chk(f"lanes: endpoint missing, --host {pref} -> holds (no lane), never Unraid",
        q._candidate_lanes(row(1, G8, pref=pref), FakeW), [])
chk("lanes: endpoint missing, an Ollama-tag model on auto still uses Ollama",
    q._candidate_lanes(row(1, "qwen3:14b", pref="auto"), FakeW), [UNRAID])
chk("lanes: an Unraid-pinned Ollama model is unaffected",
    q._candidate_lanes(row(1, "qwen3:14b", pref="unraid"), FakeW), [UNRAID])

# ---- 5. enqueue waits out a provider restart ----
uw = need("_darkbloom_url_wait exists", "_darkbloom_url_wait")
if uw:
    t = [0.0]
    naps = []

    def clock():
        return t[0]

    def nap(s):
        naps.append(s)
        t[0] += s

    reads = iter([None, None, DB])
    q._darkbloom_url = lambda: next(reads, DB)
    chk("wait: missing, then back on the 3rd read -> the URL",
        uw(wait_s=60, step_s=3, sleep=nap, clock=clock, installed=True), DB)
    chk("wait: it slept twice (one step each)", naps, [3, 3])
    naps.clear()
    q._darkbloom_url = lambda: None
    chk("wait: never back -> None after the wait",
        uw(wait_s=9, step_s=3, sleep=nap, clock=clock, installed=True), None)
    chk("wait: it gave up after wait_s", sum(naps), 9)
    naps.clear()
    chk("wait: provider not installed -> None at once",
        (uw(wait_s=60, step_s=3, sleep=nap, clock=clock, installed=False), naps), (None, []))
    chk("wait: kill switch (wait 0) -> None at once",
        (uw(wait_s=0, step_s=3, sleep=nap, clock=clock, installed=True), naps), (None, []))
    q._darkbloom_url = lambda: DB
    chk("wait: endpoint up -> the URL, no sleep",
        (uw(wait_s=60, step_s=3, sleep=nap, clock=clock, installed=True), naps), (DB, []))


def host_ok(h):
    try:
        q._validate_host(h)
        return True
    except SystemExit:
        return False


if uw:
    q._darkbloom_url_wait = lambda *a, **k: DB
    chk("enqueue: --host studio-db accepted once the endpoint is back", host_ok("studio-db"), True)
    q._darkbloom_url_wait = lambda *a, **k: None
    chk("enqueue: --host studio-db refused when it never comes back", host_ok("studio-db"), False)
    chk("enqueue: auto is unaffected", host_ok("auto"), True)

# ---- 6. env parsing ----
chk("env: DARKBLOOM_STICKY_S default stays 60", q._sticky_s_from_env(None), 60.0)
try:
    chk("env: the enqueue wait defaults to 180", q._sticky_s_from_env("", 180.0), 180.0)
    chk("env: off -> 0", q._sticky_s_from_env("off", 180.0), 0.0)
    chk("env: garbage -> the default", q._sticky_s_from_env("soon", 180.0), 180.0)
    chk("env: a number is taken", q._sticky_s_from_env("30", 180.0), 30.0)
except TypeError as e:
    print(f"FAIL - env: _sticky_s_from_env takes no default ({e})")
    fails += 1

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
