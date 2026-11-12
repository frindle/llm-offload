"""Owner first (Fable review #2/#8, 2026-11-02).

#2: with no lease, the queue turns an off Manager back on only when it left it off itself (its newest
successful Manager change was a Manager-off, and no manual change came after it). A manual change is
also kept in the ledger, so it still counts once it scrolls out of the activity log.
#8: a manual change while the queue holds the Manager off keeps the lease (yielded_at): no change for
yield_s, then the usual hand-back, so the Manager is never left off with nobody to turn it back on.
"""
import json
import os

import pytest

import bloomgauge_control as C
import bloomgauge_lease_guard as G
import bloomgauge_policy as P
import bloomgauge_tick as T
from bgc_fake_client import FakeClient

NOW = 100000.0
CFG = {"yield_s": 7200, "idle_s": 900, "handback": "always", "allow_picks": True}
BUSY = {"busy": True, "why": "1 lane row(s)", "target": "m1"}
IDLE = {"busy": False, "why": "queue done", "target": None}
HELD_REASON = "held: yielded to a manual change; no changes for yield_s"


def res(ts, action, ok=True, mode="on"):
    return {"ts": ts, "action": action, "mode": mode, "phase": "result", "result": "ok" if ok else "failed", "err": None}


def kept(at, ts=None):
    return {"ts": at if ts is None else ts, "phase": "manual", "action": "none", "mode": "on", "manual_at": at}


def ev(at, to):
    return {"at": at, "event": "mode.change", "fields": {"why": "user", "to": to}}


def view(**kw):
    v = {"agent_on": True, "manager_on": False, "can_enable": True, "blocker": None,
         "current": "m2", "serving": ["m1", "m2"], "switching": False}
    v.update(kw)
    return v


def bud(**kw):
    b = {"picks_left": 2, "mgr_left": 4, "manual_at": None, "log_ok": True, "mgr_retry_at": 0.0, "pick_retry_at": 0.0}
    b.update(kw)
    return b


# ---------------------------------------------------------------- #2: the leak, pure
def test_leak_only_after_the_queues_own_successful_manager_off():
    leak = lambda led, events=(): P.budget(led, list(events), NOW, {})["leak"]
    assert leak([]) is False and leak(None) is False
    assert leak([res(NOW - 500, "mgr_off")]) is True
    assert P.budget([res(NOW - 500, "mgr_off")], [], NOW, {})["own_off_at"] == NOW - 500
    assert leak([res(NOW - 500, "mgr_off"), res(NOW - 400, "mgr_on")]) is False             # handed back
    assert leak([res(NOW - 500, "mgr_off"), res(NOW - 400, "mgr_on", ok=False)]) is True    # a failed turn-on is retried
    assert leak([res(NOW - 500, "mgr_off", ok=False)]) is False                             # a failed off never counts
    assert leak([res(NOW - 500, "mgr_on"), res(NOW - 400, "mgr_off", ok=False)]) is False
    assert leak([res(NOW - 500, "mgr_off", mode="shadow")]) is False                        # shadow never wrote
    assert leak([res(NOW - 500, "mgr_off"), res(NOW - 300, "pick")]) is True                # picks do not matter
    assert leak([res(NOW - 400, "mgr_on"), res(NOW - 500, "mgr_off")]) is False             # newest by ts, not order


def test_a_manual_change_after_the_queues_off_means_no_leak_even_once_it_leaves_the_log():
    led = [res(NOW - 5000, "mgr_off")]
    b = P.budget(led, [ev(NOW - 4100, "on"), ev(NOW - 4000, "off")], NOW, {})
    assert (b["manual_at"], b["manual_log_at"], b["manual_kept_at"], b["leak"]) == (NOW - 4000, NOW - 4000, None, False)
    b = P.budget(led + [kept(NOW - 4000)], [], NOW, {})                                     # scrolled out of the log
    assert (b["manual_at"], b["manual_log_at"], b["manual_kept_at"], b["leak"]) == (NOW - 4000, None, NOW - 4000, False)
    b = P.budget(led + [kept(NOW - 4000)], None, NOW, {})                                   # log unreadable: kept still counts
    assert (b["manual_at"], b["log_ok"]) == (NOW - 4000, False)
    assert P.budget([kept(NOW - 6000), res(NOW - 5000, "mgr_off")], [], NOW, {})["leak"] is True   # manual before the off
    assert P.budget([kept(NOW - 4000), kept(NOW - 4500), kept("junk")], [], NOW, {})["manual_kept_at"] == NOW - 4000


def test_ownerless_step_leaves_an_owners_off_alone():
    d = P.ownerless_step(view(), 5000.0, CFG, bud(leak=False), NOW)
    assert d == {"action": "none", "model": None, "reason": "ownerless idle: Manager off, not by the queue; left to its owner"}
    assert P.ownerless_step(view(), 5000.0, CFG, bud(), NOW)["action"] == "none"            # no leak key: never
    assert P.ownerless_step(view(), 5000.0, CFG, bud(leak=True), NOW)["action"] == "mgr_on"


# ---------------------------------------------------------------- #2: through the tick
@pytest.fixture(autouse=True)
def fresh_state():
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    yield
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None)


def cfg(tmp_path, mode="on"):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": mode, "QUEUE_BG_IDLE_MIN": "1", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def idle_ticks(c, cl, start, n, step=70.0):
    for i in range(n):
        T.tick(IDLE, start + i * step, c, cl)


def test_tick_never_turns_on_a_manager_it_did_not_leave_off(tmp_path):
    c, cl = cfg(tmp_path), FakeClient(manager_on=False)
    idle_ticks(c, cl, 100.0, 40)
    assert cl.writes == [] and rows(c) == [] and cl.reads                                    # it looked, and left it
    C._ledger_append(c["ledger_path"], res(50.0, "mgr_off"))
    C._ledger_append(c["ledger_path"], res(60.0, "mgr_on"))                                  # handed back already
    idle_ticks(c, cl, 5000.0, 40)
    assert cl.writes == []


def test_tick_repairs_its_own_leak_once_and_never_repeats(tmp_path):
    c, cl = cfg(tmp_path), FakeClient(manager_on=False)
    C._ledger_append(c["ledger_path"], res(10.0, "mgr_off"))                                 # lease file lost after an off
    idle_ticks(c, cl, 100.0, 4)
    assert cl.writes == [("set_manager", {"on": True})] and cl.manager_on
    cl.manager_on = False                                                                    # off again, not by the queue
    idle_ticks(c, cl, 1000.0, 200)                                                           # ~4 h of idle ticks
    assert cl.writes == [("set_manager", {"on": True})]


def test_tick_keeps_a_manual_change_past_the_log_window(tmp_path):
    c = cfg(tmp_path)
    C._ledger_append(c["ledger_path"], res(10.0, "mgr_off"))
    cl = FakeClient(manager_on=False, events=[ev(40.0, "on"), ev(50.0, "off")])              # the owner, after the queue
    idle_ticks(c, cl, 100.0, 5)
    manual = [r for r in rows(c) if r.get("phase") == "manual"]
    assert [(r["manual_at"], r["mode"]) for r in manual] == [(50.0, "on")]                   # written once
    cl.events = []                                                                           # scrolled out of the log
    idle_ticks(c, cl, 9000.0, 60)                                                            # past yield_s
    assert cl.writes == [] and len([r for r in rows(c) if r.get("phase") == "manual"]) == 1


def test_tick_busy_acquire_backs_off_on_a_kept_manual_change(tmp_path):
    c = cfg(tmp_path)
    C._ledger_append(c["ledger_path"], kept(NOW - 600))
    cl = FakeClient(manager_on=True)                                                         # the log no longer shows it
    d = T.tick(BUSY, NOW, c, cl)
    assert d["reason"] == "backoff: manual Manager change less than 2h ago" and cl.writes == []


# ---------------------------------------------------------------- #8: yield keeps the lease, pure
LEASE = {"state": "HELD", "since": NOW - 1000, "target": "m1", "prior_manager_on": True, "mgr_off_at": NOW - 1000}


def test_yield_keeps_the_lease_while_the_queue_holds_the_manager_off():
    b = bud(manual_at=NOW - 100)
    d, lease = P.decide(dict(LEASE, mgr_on_seen_at=NOW - 5), view(), BUSY, NOW, CFG, b)
    assert d == {"action": "yield", "model": None, "reason": P.YIELD_KEEP_REASON}
    assert lease == dict(LEASE, yielded_at=NOW - 100)
    d, lease2 = P.decide(lease, view(), BUSY, NOW + 30, CFG, b)                              # no pick, no re-yield
    assert d == {"action": "none", "model": "m1", "reason": HELD_REASON} and lease2["yielded_at"] == NOW - 100
    d, gone = P.decide(lease, view(manager_on=True), BUSY, NOW + 30, CFG, b)                  # on again: nothing to hand back
    assert d == {"action": "none", "model": "m1", "reason": "yielded: Manager on again; lease released"} and gone is None
    d, after = P.decide(lease, view(), BUSY, NOW - 100 + 7200, CFG, b)                       # yield_s over: normal again
    assert d["action"] == "pick" and after["picked"] == "m1"


def test_a_kept_lease_hands_back_only_after_yield_s():
    b = bud(manual_at=NOW - 100)
    _, lease = P.decide(dict(LEASE), view(), BUSY, NOW, CFG, b)
    d, lease = P.decide(lease, view(), IDLE, NOW + 60, CFG, b)
    assert d["reason"] == "drain: queue idle, hand-back timer started" and lease["state"] == "DRAIN"
    d, lease = P.decide(lease, view(), IDLE, NOW + 60 + 901, CFG, b)
    assert d == {"action": "wait", "model": None, "reason": "handback: manual change less than yield_s ago"}
    assert lease["state"] == "HANDBACK"
    d, done = P.decide(lease, view(), IDLE, NOW - 100 + 7200, CFG, b)
    assert d["action"] == "mgr_on" and done["state"] == "HANDBACK"     # kept until the turn-on settles
    d, done = P.decide(done, view(manager_on=True), IDLE, NOW - 100 + 7230, CFG, b)   # it did: released
    assert d["action"] == "none" and done is None


def test_a_new_manual_change_on_a_kept_lease_yields_again():
    _, lease = P.decide(dict(LEASE), view(), BUSY, NOW, CFG, bud(manual_at=NOW - 100))
    d, gone = P.decide(lease, view(manager_on=True), BUSY, NOW + 60, CFG, bud(manual_at=NOW + 50))
    assert d["reason"] == P.YIELD_REASON and gone is None
    d, again = P.decide(lease, view(), BUSY, NOW + 60, CFG, bud(manual_at=NOW + 50))
    assert d["reason"] == P.YIELD_KEEP_REASON and again["yielded_at"] == NOW + 50


def test_yield_drops_the_lease_when_the_manager_was_off_before_or_reads_on():
    b = bud(manual_at=NOW - 100)
    d, gone = P.decide(dict(LEASE, prior_manager_on=False), view(), BUSY, NOW, CFG, b)
    assert d["reason"] == P.YIELD_REASON and gone is None
    d, gone = P.decide(dict(LEASE), view(manager_on=True), BUSY, NOW, CFG, b)
    assert d["reason"] == P.YIELD_REASON and gone is None
    d, kept_ = P.decide(dict(LEASE), view(), BUSY, NOW, CFG, bud(manual_at=NOW - 2000))     # before the lease
    assert d["action"] != "yield" and "yielded_at" not in kept_


# ---------------------------------------------------------------- #8: through the tick and the guard
def test_tick_keeps_the_lease_file_and_hands_back_after_yield_s(tmp_path):
    c = cfg(tmp_path)
    C.lease_save(c["lease_path"], dict(LEASE))
    cl = FakeClient(manager_on=False, events=[ev(NOW - 100, "on"), ev(NOW - 90, "off")])
    d = T.tick(BUSY, NOW, c, cl)
    assert d["reason"] == P.YIELD_KEEP_REASON and cl.writes == []
    held = json.load(open(c["lease_path"]))
    assert held["yielded_at"] == NOW - 90 and held["state"] == "HELD"
    assert T.tick(BUSY, NOW + 30, c, cl)["reason"] == HELD_REASON
    assert T.tick(IDLE, NOW + 60, c, cl)["reason"] == "drain: queue idle, hand-back timer started"
    assert T.tick(IDLE, NOW + 1000, c, cl)["reason"] == "handback: manual change less than yield_s ago"
    assert cl.writes == [] and os.path.exists(c["lease_path"])
    d = T.tick(IDLE, NOW - 90 + 7200, c, cl)
    assert d["action"] == "mgr_on" and cl.writes == [("set_manager", {"on": True})]
    assert not os.path.exists(c["lease_path"])


def test_guard_waits_on_a_yielded_lease_then_hands_back(tmp_path):
    c = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)
    C.lease_save(c["lease_path"], dict(LEASE, heartbeat=NOW - 700, daemon_pid=4242, yielded_at=NOW - 600))
    cl = FakeClient(manager_on=False)
    d = G.guard(NOW, c, cl, alive=lambda pid: False)
    assert d["reason"] == "deadman: yielded to a manual change less than yield_s ago; lease kept"
    assert cl.reads == [] and cl.writes == [] and os.path.exists(c["lease_path"])
    d = G.guard(NOW - 600 + 7200, c, cl, alive=lambda pid: False)
    assert d["action"] == "mgr_on" and not os.path.exists(c["lease_path"])
