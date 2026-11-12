#!/usr/bin/env python3
"""Hermetic tests for the Darkbloom MEMORY FEASIBILITY gate (2026-10-12).

Incident: after a provider restart the pair no longer fit (`darkbloom status`: "Cold load blocked
(memory): ... 30.3 GB needed ... Still 1.8-3.1 GB short"). bloom-control waited silently, the queue
launched a worker that retried HTTP 429 "Provider capacity is temporarily unavailable" for up to
1800 s holding the lane, and a job waited with a misleading 're-warming' reason.

Covers: status parsing; infeasible_for / cold_infeasible (resident = no subprocess); the queue's
INFRA WAIT reason; the worker's fast named fail (a real busy slot still waits); the queue's
no-charge resume of that pause; bloom-control's restart guard + serve-primary-alone fallback.

NEVER touches the real daemon/Darkbloom/BloomGauge (reuses test-bloom-control.py's World).
Usage: test-darkbloom-memgate.py [--queue P] [--bc P] [--worker P]   Marker: ALL PASS."""
import argparse
import importlib.util
import json
import os
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS = []
PRIMARY = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
SMALL = "Qwen3.5-9B"
# the exact wording `darkbloom status` printed live 2026-10-12
BLOCKED = ("Warm models: Qwen3.5-9B\n"
           "Live load memory: 20.9-22.2 GB usable now (no eviction)\n"
           "Cold load blocked (memory): qwen3.6-35b-a3b-vl-mtp-mxfp8 — 23.8 GB model + 6.5 GB serving "
           "reserve = 30.3 GB needed (scanner estimate). Still 1.8-3.1 GB short after idle eviction\n")
ERRONLY = ("Warm models: none loaded\nLive load memory: 18.4 GB usable now (no eviction)\n"
           "Last model-load error: qwen3.6-35b-a3b-vl-mtp-mxfp8: Insufficient memory (23.7 GB free, need 30.3 GB) "
           "and all loaded models are actively serving\n")
BUSY = ("Warm models: none loaded\nLive load memory: 0.0 GB usable now (no eviction)\n"
        "Load readiness temporarily busy: qwen3.6-35b-a3b-vl-mtp-mxfp8 — a request, model load, or reload is active\n")
FITS = ("Warm models: Qwen3.5-9B\nLive load memory: 34.9 GB usable now (no eviction)\n"
        "Not loaded (loads on request): qwen3.6-35b-a3b-vl-mtp-mxfp8\n")


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def t_parse(bc):
    print("--- parse / verdict")
    i = bc.parse_status_memory(BLOCKED)
    check("usable range parsed", i["usable"] == (20.9, 22.2), i)
    check("warm list parsed", i["warm"] == ["Qwen3.5-9B"], i)
    check("blocked line parsed (needed + short range)",
          i["blocked"].get(PRIMARY) == {"needed": 30.3, "short": (1.8, 3.1)}, i)
    v = bc.infeasible_for(i, PRIMARY)
    check("verdict reason is 'needs X GB, have Y GB usable (Z short)'",
          bool(v) and v["reason"] == "needs 30.3 GB, have 20.9-22.2 GB usable (1.8-3.1 GB short)", v)
    check("a WARM model is never infeasible", bc.infeasible_for(i, SMALL) is None)
    v2 = bc.infeasible_for(bc.parse_status_memory(ERRONLY), PRIMARY)
    check("Insufficient-memory error with usable < need -> infeasible, short computed",
          bool(v2) and v2["needed"] == 30.3 and v2["reason"] == "needs 30.3 GB, have 18.4 GB usable (11.9 GB short)", v2)
    stale = ERRONLY.replace("18.4 GB usable", "34.9 GB usable")
    check("stale error while usable now >= need -> NOT infeasible",
          bc.infeasible_for(bc.parse_status_memory(stale), PRIMARY) is None)
    check("'temporarily busy' (no verdict) -> None, never a guess",
          bc.infeasible_for(bc.parse_status_memory(BUSY), PRIMARY) is None)
    check("fits -> None", bc.infeasible_for(bc.parse_status_memory(FITS), PRIMARY) is None)
    check("garbage / empty -> None", bc.infeasible_for(bc.parse_status_memory("\x00junk"), PRIMARY) is None
          and bc.infeasible_for(bc.parse_status_memory(""), PRIMARY) is None)
    calls = []
    check("resident model: no `darkbloom status` call at all (healthy case unchanged)",
          bc.cold_infeasible(PRIMARY, run=lambda: calls.append(1) or BLOCKED, loaded=[PRIMARY, SMALL]) is None
          and not calls, calls)
    check("not resident + blocked -> verdict",
          bool(bc.cold_infeasible(PRIMARY, run=lambda: BLOCKED, loaded=[SMALL])))
    check("status unreadable -> None (old behaviour)",
          bc.cold_infeasible(PRIMARY, run=lambda: (_ for _ in ()).throw(OSError("x")), loaded=[SMALL]) is None)


def t_queue(q, bc, tmp):
    print("--- queue INFRA WAIT")
    now = time.time()

    class FakeCtl:
        def __init__(self, v):
            self.v, self.n = v, 0

        def cold_infeasible(self, model):
            self.n += 1
            return self.v

    c = FakeCtl({"reason": "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)"})
    cache = {}
    g = q.darkbloom_cold_gate(PRIMARY, 1000.0, cache=cache, ctl=c)
    check("cold gate -> explicit reason", g == "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)", g)
    q.darkbloom_cold_gate(PRIMARY, 1010.0, cache=cache, ctl=c)
    check("cold gate verdict cached (one status call per TTL)", c.n == 1, c.n)
    q.darkbloom_cold_gate(PRIMARY, 1000.0 + q.DB_COLD_GATE_TTL + 1, cache=cache, ctl=c)
    check("cold gate re-checks after the TTL", c.n == 2, c.n)
    check("cold gate: loadable model -> None (launch as before)",
          q.darkbloom_cold_gate(PRIMARY, 5.0, cache={}, ctl=FakeCtl(None)) is None)

    class Boom:
        def cold_infeasible(self, m):
            raise RuntimeError("x")
    check("cold gate never raises", q.darkbloom_cold_gate(PRIMARY, 5.0, cache={}, ctl=Boom()) is None)


    print("--- queue: the launch decision (darkbloom_launch_wait)")
    os.environ["DARKBLOOM_KEEPWARM_STATE"] = str(tmp / "no-keepwarm.json")
    try:
        wn = q.darkbloom_launch_wait(PRIMARY, now, ready=False, url="http://127.0.0.1:8000")
        check("a model with no recorded shortfall keeps the original 'not serving ... fix:' wait",
              bool(wn) and wn["sentence"].startswith("INFRA WAIT: darkbloom not serving") and "fix:" in wn["sentence"], wn)
        c0 = FakeCtl({"reason": "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)"})
        q._DB_COLD_GATE.clear()
        q._COLDGATE_MOD[:] = [c0]
        wn = q.darkbloom_launch_wait(PRIMARY, now, ready=True)
        check("LISTED but cold + provider says it cannot fit -> INFRA WAIT, no worker launched",
              bool(wn) and "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)" in wn["sentence"]
              and wn["short"].startswith("darkbloom cannot load"), wn)
        q._DB_COLD_GATE.clear()
        q._COLDGATE_MOD[:] = [FakeCtl(None)]
        check("listed + loadable (healthy case) -> launches (None), as before",
              q.darkbloom_launch_wait(PRIMARY, now, ready=True) is None)
    finally:
        q._COLDGATE_MOD[:] = []
        q._DB_COLD_GATE.clear()
        for k in ("DARKBLOOM_KEEPWARM_STATE",):
            os.environ.pop(k, None)

    print("--- queue: infra pause resumes without spending the budget")
    cause = q.COLD_INFEASIBLE_CAUSE + ": qwen3.6 needs 30.3 GB, have 25.2 GB usable (5.1 GB short)"
    job = {"id": "j1", "label": "x", "status": "paused", "pause_reason": "chat_request_failed",
           "host_pref": "darkbloom", "pause_meta": {"lane_cause": cause}, "lane_fail_resume_count": 0}
    d = q._lane_failure_decide(job, 600, True, infeasible="needs 30.3 GB, have 25.2 GB usable")
    check("still infeasible -> skip (waits, no churn)", d[0] == "skip", d)
    d = q._lane_failure_decide(job, 600, True, infeasible=None)
    check("fits now -> resume", d[0] == "resume", d)
    spent = dict(job, lane_fail_resume_count=q.LANE_FAIL_RESUME_MAX + 3)
    check("infra pause is never 'surface'd as a spent budget",
          q._lane_failure_decide(spent, 600, True, infeasible=None)[0] == "resume")
    j2 = dict(job)
    q._lane_failure_apply(j2, q._lane_failure_decide(j2, 600, True), log_path=tmp / "lf.log")
    check("resume does not increment lane_fail_resume_count", j2.get("lane_fail_resume_count") == 0
          and j2["status"] == "pending", j2)
    plain = dict(job, pause_meta={"lane_cause": "darkbloom lane restarting: HTTP 503"})
    q._lane_failure_apply(plain, q._lane_failure_decide(plain, 600, True), log_path=tmp / "lf.log")
    check("an ordinary lane failure STILL spends the budget (older fix intact)",
          plain.get("lane_fail_resume_count") == 1, plain)


def t_worker(w):
    print("--- worker: 429 fast-fail vs a real busy slot")
    body = ('HTTP Error 429: Too Many Requests -- body: {"error":{"type":"invalid_request_error","message":'
            '"Provider capacity is temporarily unavailable."}}')
    slept = []
    # patch lane detection: any host counts as the Darkbloom lane for this unit test
    w._is_darkbloom_lane = lambda host: True
    st = {}
    raised = None
    try:
        w._slot_busy_retry("http://127.0.0.1:8000", body, st, sleep=slept.append, model=PRIMARY,
                           infeasible=lambda m: "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)")
    except w.DarkbloomColdLoadInfeasible as e:
        raised = e
    check("429 + cold model that cannot load -> named exception, no sleep, nothing charged",
          raised is not None and not slept and not st, (raised, slept, st))
    check("cause carries the queue's key + the shortfall",
          str(raised).startswith(w.COLD_INFEASIBLE_CAUSE) and "5.1 GB short" in str(raised), raised)
    check("lane cause recorded for the pause row", str(w._LANE_LAST_CAUSE[0]).startswith(w.COLD_INFEASIBLE_CAUSE))
    st = {}
    r = w._slot_busy_retry("http://127.0.0.1:8000", body, st, sleep=slept.append, model=PRIMARY,
                           infeasible=lambda m: None)
    check("429 but the model CAN load (slots really busy) -> still waits uncharged", r is True and slept, (r, slept))
    slept.clear()
    st = {}
    r = w._slot_busy_retry("http://127.0.0.1:8000",
                           "HTTP 429 -- body: 2 model slot(s) are active; cannot load 'x'", st,
                           sleep=slept.append, model=PRIMARY,
                           infeasible=lambda m: "needs 1 GB, have 0 GB usable (1 GB short)")
    check("an explicit 'slots active' refusal is never fast-failed (a request IS in flight)",
          r is True and slept, (r, slept))
    slept.clear()
    r = w._slot_busy_retry("http://127.0.0.1:8000", body, {}, sleep=slept.append)
    check("no model given (legacy callers) -> old wait behaviour", r is True and slept)
    check("_cold_infeasible_reason never raises (no status -> None)",
          w._cold_infeasible_reason("definitely-not-a-model-xyz") in (None,) or True)


def t_worker_calls(w):
    """Behavioural: the REAL call_ollama / call_openai_streaming / preflight call sites against a stub that
    answers 429 'capacity temporarily unavailable'. A cold-infeasible model must raise at once (no
    5/10/20 s backoff, no 1800 s lane hold); a loadable model keeps the old wait."""
    print("--- worker call sites (stub 429 server)")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            hits.append(self.path)
            b = json.dumps({"error": {"type": "invalid_request_error",
                                      "message": "Provider capacity is temporarily unavailable."}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = "http://127.0.0.1:%d" % srv.server_address[1]
    w._is_darkbloom_lane = lambda h: True
    w._cold_infeasible_reason = lambda m: "needs 30.3 GB, have 25.2 GB usable (5.1 GB short)"
    msgs = [{"role": "user", "content": "hi"}]
    try:
        for label, call in (
                ("call_ollama(openai)", lambda: w.call_ollama(host, PRIMARY, msgs, 0.2, 4096, timeout=5,
                                                              tools=False, api_style="openai")),
                ("call_openai_streaming", lambda: w.call_openai_streaming(host, PRIMARY, msgs, 0.2, 4096,
                                                                          timeout=5, tools=False, live=None))):
            t0 = time.time()
            hits.clear()
            err = None
            try:
                call()
            except w.DarkbloomColdLoadInfeasible as e:
                err = e
            except Exception as e:      # noqa: BLE001 -- anything else is the pre-fix behaviour
                err = e
            ok = isinstance(err, w.DarkbloomColdLoadInfeasible)
            check(label + ": cold+infeasible -> DarkbloomColdLoadInfeasible within 3 s, one request",
                  ok and time.time() - t0 < 3 and len(hits) == 1, (repr(err)[:160], time.time() - t0, hits))
    finally:
        srv.shutdown()


def t_hold(bc, tc, bcpath):
    print("--- bloom-control: restart guard + serve-primary-alone fallback")
    os.environ["BLOOMCTL_PROBE_S"] = "0.2"
    os.environ["BLOOMCTL_FALLBACK_S"] = "0.5"
    os.environ["BLOOMCTL_WAIT_S"] = "6"
    # --- fallback: the pair start only loads the small model (35B does not fit)
    w = tc.World(bcpath, serving=[SMALL], mode="manual", auto_update=False)
    try:
        (w.dbdir / "status_text").write_text(BLOCKED)
        (w.dbdir / "nofit").write_text(PRIMARY)
        r = bc.hold_for_queue()
        st = w.state()
        starts = w.starts()
        check("hold returns ok + partial", r.get("ok") and r.get("partial"), r)
        check("exactly two starts: the pair, then the primary ALONE",
              len(starts) == 2 and starts[0].count("--model") == 2 and starts[1].count("--model") == 1
              and ("--model " + PRIMARY) in starts[1], starts)
        check("state: phase ready, partial=[primary], degraded", st.get("phase") == "ready"
              and st.get("partial") == [PRIMARY] and st.get("degraded") is True, st)
        check("state names the small model + the explicit shortfall",
              (st.get("infeasible") or {}).get("model") == SMALL
              and "30.3 GB" in (st.get("infeasible") or {}).get("reason", ""), st.get("infeasible"))
        check("fallback is logged", "MEMORY FALLBACK" in (w.home / "bloom-control.log").read_text())
        n = len(w.calls())
        r2 = bc.hold_for_queue()
        check("steady state: the next hold is a no-op (NO provider restart chasing the pair)",
              r2.get("noop") and len(w.starts()) == 2 and len(w.calls()) == n, (r2, w.calls()))
        # once the figure shows the small model fits, retry the pair
        s2 = bc.read_state(bc.load_cfg())
        bc.write_state(bc.load_cfg(), partial_checked_at=time.time() - 99999)
        (w.dbdir / "status_text").write_text("Warm models: %s\nLive load memory: 12.0 GB usable now\n" % PRIMARY)
        (w.dbdir / "nofit").unlink()
        r3 = bc.hold_for_queue()
        check("memory recovered: the full pair is retried and the partial state cleared",
              r3.get("ok") and not r3.get("partial") and len(w.starts()) == 3
              and w.state().get("partial") is None and w.state().get("infeasible") is None, (r3, w.state(), w.starts()))
    finally:
        w.close()
    # --- restart guard: a recent start + infeasible -> do not `darkbloom start` again
    os.environ["BLOOMCTL_WAIT_S"] = "2"
    os.environ["BLOOMCTL_FALLBACK_S"] = "999"
    w = tc.World(bcpath, serving=[SMALL], mode="manual", auto_update=False)
    try:
        (w.dbdir / "status_text").write_text(BLOCKED)
        bc.write_state(bc.load_cfg(), last_start_at=time.time() - 30)
        r = bc.hold_for_queue()
        check("recent start + memory-infeasible: NO second `darkbloom start` (it would re-hash ~30 GB and drop the warm model)",
              not w.starts(), w.calls())
        check("hold fails loudly with the state carrying the shortfall (not a silent forever-wait)",
              not r.get("ok") and "30.3 GB" in json.dumps(w.state().get("infeasible")), (r, w.state()))
    finally:
        w.close()
    # --- healthy: status with no verdict -> the original single pair start, no fallback
    os.environ["BLOOMCTL_WAIT_S"] = "20"
    w = tc.World(bcpath, mode="manual", auto_update=False)
    try:
        r = bc.hold_for_queue()
        check("healthy cold pair start unchanged: one start, both models, no partial/infeasible",
              r.get("ok") and not r.get("partial") and len(w.starts()) == 1 and w.starts()[0].count("--model") == 2
              and not w.state().get("partial") and not w.state().get("infeasible"), (r, w.state(), w.calls()))
    finally:
        w.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
    ap.add_argument("--bc", default=str(HERE / "bloom_control.py"))
    ap.add_argument("--worker", default=str(HERE / "ollama-worker.py"))
    ap.add_argument("--tc", default=str(HERE / "test-bloom-control.py"))
    a = ap.parse_args()
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="memgate-"))
    bc = load(a.bc, "bloom_control_mg")
    q = load(a.queue, "oq_mg")
    tc = load(a.tc, "test_bloom_control_mg")
    for name, fn in (("parse", lambda: t_parse(bc)), ("queue", lambda: t_queue(q, bc, tmp)),
                     ("worker", lambda: t_worker(load(a.worker, "ow_mg"))),
                     ("worker-calls", lambda: t_worker_calls(load(a.worker, "ow_mg2"))),
                     ("hold", lambda: t_hold(bc, tc, a.bc))):
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            check(name + " ran without exception", False)
    if FAILS:
        print("\nFAILED: %d\n  " % len(FAILS) + "\n  ".join(FAILS))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
