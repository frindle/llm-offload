#!/usr/bin/env python3
"""NEXT-STEP DECLARATION (2026-10-09, smoke order-mismatch).

Live: auto-author-smoke-order-mismatch (6cf6fb3afca4) failed (prose_loop) at 01:29:39; the
bundle then had NO pending row until its chain driver enqueued the -c1 continuation at
01:30:27, so bundle_commit_status read "nothing left / CPU wait" and the lane went to
smoke-costco-login-confirm at 01:30:05. A terminal job now DECLARES what happens next for its
bundle (state["_bundle_next"]) and the commitment holds/passes the lane from that, not from
whether a row exists yet. Asserts the exact timeline plus the unchanged no-declaration paths.
Usage: python3 test-queue-next-step-declaration.py [--queue PATH]   (revert: --queue <bak>)
"""
import argparse, importlib.util, json, os, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="nextstep-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ld = SourceFileLoader("queue_ns", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_ns", ld))
ld.exec_module(q)
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


need = ("next_step_for_terminal", "next_step_holds", "settle_next_steps", "_declare_terminal_next_step")
missing = [n for n in need if not hasattr(q, n)]
chk("next-step API present", missing, [])
if missing:
    print("FAIL - next-step declaration not implemented in", A.queue)
    sys.exit(1)

pk = lambda j: j.get("bundle")
B, C = "smoke-order-mismatch", "smoke-costco-login-confirm"
NOW = 1000.0
failed = {"id": "6cf6fb3afca4", "label": "auto-author-smoke-order-mismatch", "bundle": B,
          "status": "failed"}
chain_live = {"known": True, "driver_live": False, "waiting": True, "phase": "waiting"}
decl = q.next_step_for_terminal(failed, chain_live, NOW)

# --- declaration derivation (pure)
chk("failed driver-managed row + live driver -> continue by auto", (decl["kind"], decl["actor"]),
    ("continue", "ollama-dispatch-auto"))
chk("continue carries expiry 120s default", decl["expires_at"] - NOW, 120.0)
chk("no live driver -> park", q.next_step_for_terminal(failed, {"known": True}, NOW)["kind"], "park")
chk("done, no driver -> done",
    q.next_step_for_terminal(dict(failed, status="done"), {}, NOW)["kind"], "done")
chk("needs_opus, no driver -> blocked",
    q.next_step_for_terminal(dict(failed, status="needs_opus"), {}, NOW)["kind"], "blocked")
chk("gate row is not driver-managed",
    q.next_step_for_terminal(dict(failed, label="gate-6cf6"), chain_live, NOW)["kind"], "park")

# --- bundle_commit_status timeline: failed row, NO pending row
jobs = [failed]
st = lambda **kw: q.bundle_commit_status(B, jobs, pk, **kw)
chk("no declaration: unchanged ('nothing left' -> complete)", st()[0], "complete")
chk("no declaration + cpu_wait: unchanged ('waiting')", st(cpu_wait=["6947d72fdddb"])[0], "waiting")
s = st(next_step=decl, now=NOW + 10)
chk("continue declared -> working, moving", (s[0], s[2]), ("working", True))
chk("status says why", "holding lane: continuation pending (ollama-dispatch-auto)" in s[1], True)
chk("continue beats CPU-wait (the live bug)", st(next_step=decl, now=NOW + 10, cpu_wait=["x"])[0], "working")
e = st(next_step=decl, now=NOW + 500)
chk("expired -> blocked, loud text", (e[0], "EXPIRED" in e[1]), ("blocked", True))
chk("done declaration -> complete",
    st(next_step=dict(decl, kind="done", actor=None), now=NOW)[0], "complete")
chk("park declaration -> unchanged", st(next_step=dict(decl, kind="park"), now=NOW)[0], "complete")
chk("blocked declaration -> blocked", st(next_step=dict(decl, kind="blocked", why="w"), now=NOW)[0], "blocked")

# --- commitment step: lane is held for B while the continuation is pending
def step(next_step, cpu):
    state = {"jobs": [failed, {"id": "bb", "label": "auto-author-" + C, "bundle": C, "status": "pending"}],
             "_bundle_commit": {"key": B, "since": 0.0, "empty_since": None, "idle_since": None}}
    if next_step:
        state["_bundle_next"] = {B: dict(next_step)}
    d = T / "runs"
    d.mkdir(exist_ok=True)
    (d / f"{B}.json").write_text(json.dumps({"phase": "waiting", "pid": os.getpid(), "job": failed["id"],
                                             "phase_since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW))}))
    k, ev = q._apply_bundle_commit(state, pk, None, [C], NOW + 26, runs_dir=d, chain_dir=d, hooks={},
                                   cpu_outstanding={B: ["6947d72fdddb"]} if cpu else {},
                                   kick=lambda *a: True, alert=lambda *a: None)
    return k, [x[0] for x in ev], state

# chain.waiting is true in the file; make the file say ENDED-free but ts old enough? use real live driver:
k, ev, stt = step(decl, cpu=True)
chk("timeline: continue declared + CPU stage -> lane stays with B (no hop to costco)", k, B)
# a live row appearing (the -c1 continuation) discharges the declaration
state2 = {"jobs": [failed, {"id": "c1", "label": failed["label"] + "-c1", "bundle": B, "status": "pending"}],
          "_bundle_next": {B: dict(decl)}}
q.settle_next_steps(state2, pk, NOW + 30, chain_dir=T / "runs")
chk("live continuation row discharges the declaration", state2["_bundle_next"], {})
# chain ended -> actor decided to stop -> declaration dropped (existing behaviour resumes)
(T / "runs" / f"{B}.json").write_text(json.dumps({"phase": "ended", "pid": None}))
state3 = {"jobs": [failed], "_bundle_next": {B: dict(decl)}}
q.settle_next_steps(state3, pk, NOW + 30, chain_dir=T / "runs")
chk("driver ended -> declaration dropped", state3["_bundle_next"], {})
# dead driver, no extension: expiry -> blocked, recorded loudly
(T / "runs" / f"{B}.json").write_text(json.dumps({"phase": "waiting", "pid": os.getpid(), "job": "x",
    "phase_since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW))}))
state4 = {"jobs": [failed], "_bundle_next": {B: dict(decl, hard_until=NOW + 1)}}
exp = q.settle_next_steps(state4, pk, NOW + 300, chain_dir=T / "runs")
chk("expiry past hard cap -> declaration turns blocked, reported", (exp, state4["_bundle_next"][B]["kind"]),
    ([B], "blocked"))
# source seam: the terminal-status path declares
src = Path(A.queue).read_text()
chk("reap path declares at the terminal write", "_declare_terminal_next_step(state, job)" in src, True)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
