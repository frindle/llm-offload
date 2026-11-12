"""The queue follows its own Manager-on until BloomGauge settles it (2026-11-03).

BloomGauge answers a Manager-on at once with an operation (pending) and turns the Manager on later,
after its readiness wait (up to 3 x 600 s over a running Darkbloom), or blocks it ("Could not verify
this command. Check Darkbloom on the Mac before retrying.") and leaves the Manager Manual. On
2026-11-02 21:31 a hand-back was blocked that way; the queue had dropped its lease once the POST was
answered and read the blocked operation as mid-change, so the Manager stayed Manual all night.

Now the ledger keeps the operation id of an answered Manager-on and its outcome; a blocked (cancelled,
timed out, lost) one is a failed Manager change (backoff, retry, one alert per streak), the owner's
Manual click during it is not; the hand-back lease stays until it settles. A turn-on that BloomGauge
finishes late is still the queue's own change, not a manual one.

Hermetic: tmp ledger and lease files, fake BloomGauge clients; nothing live is read or written.
"""
import calendar
import json
import os

import pytest

import bloomgauge_control as C
import bloomgauge_lease_guard as G
import bloomgauge_policy as P
import bloomgauge_tick as T
import escalation_index_janitor as J
from bgc_fake_client import FakeClient, Unavailable

NOW = 100000.0
HOUR_AGO = NOW - P.HOUR
BLOCKED = "Could not verify this command. Check Darkbloom on the Mac before retrying."
MANUAL = "Manual mode selected. Automatic switching is paused; the current model keeps serving."
IDLE = {"busy": False, "why": "queue done", "target": None}
BUSY = {"busy": True, "why": "1 lane row(s)", "target": None}
CFG = {"yield_s": 7200, "idle_s": 900, "handback": "always", "allow_picks": True}


def att(ts, action, mode="on"):
    return {"ts": ts, "phase": "attempt", "action": action, "mode": mode}


def res(ts, action, ok=True, mode="on", op_id=None, err=None):
    r = {"ts": ts, "phase": "result", "action": action, "mode": mode, "result": "ok" if ok else "failed", "err": err}
    if op_id is not None:
        r["op_id"] = op_id
    return r


def own(ts, action, ok=True, op_id=None, err=None):
    return [att(ts, action), res(ts, action, ok, op_id=op_id, err=err)]


def out(attempt_ts, status, detail="", mode="on", op_id="a", ts=None):
    return {"ts": attempt_ts + 60 if ts is None else ts, "phase": "outcome", "action": "mgr_on", "mode": mode,
            "attempt_ts": attempt_ts, "op_id": op_id, "status": status, "detail": detail}


def agent_req(at, result="ok", what="manager"):
    return {"at": at, "event": "agent.request", "fields": {"what": what, "model": None, "result": result}}


def ctl_req(at, on, result="ok"):
    return {"at": at, "event": "control.request",
            "fields": {"what": "manager", "on": on, "why": "user", "result": result, "from": "mac"}}


def flip(at, to="on", why="user-on"):
    return {"at": at, "event": "mode.change", "fields": {"from": "off" if to == "on" else "on", "to": to, "why": why}}


def usage(ledger, events):
    mine = [r for r in ledger if r.get("phase") == "attempt"]
    return P._other_usage(events, P._outcomes(ledger, mine), HOUR_AGO)


def view(**kw):
    v = {"agent_on": True, "manager_on": False, "can_enable": True, "blocker": None, "current": "m1",
         "serving": ["m1", "m2"], "switching": False, "op_id": None, "op_status": None, "op_detail": ""}
    v.update(kw)
    return v


def bud(**kw):
    b = {"picks_left": 2, "mgr_left": 4, "manual_at": None, "log_ok": True, "mgr_retry_at": 0.0,
         "pick_retry_at": 0.0, "leak": True, "turn_on": None}
    b.update(kw)
    return b


# ---------------------------------------------------------------- the ledger, pure
def test_settled_keeps_the_newest_outcome_per_attempt_and_ignores_shadow_rows():
    led = [out(10.0, "blocked", "x"), out(10.0, "active"), out(20.0, "cancelled", mode="shadow"),
           {"phase": "outcome", "mode": "on", "attempt_ts": "bad"}, "junk"]
    assert P._settled(led) == {10.0: ("active", "")}
    assert P._settled(None) == {}


def test_turned_on_is_false_only_for_a_settled_turn_on_that_failed():
    settled = {10.0: ("blocked", "d"), 20.0: ("active", ""), 30.0: ("overridden", ""), 35.0: ("superseded", ""),
               36.0: ("timeout", ""), 37.0: ("lost", ""), 38.0: ("cancelled", "")}
    assert [P._turned_on(res(t, "mgr_on"), settled) for t in (10.0, 20.0, 30.0, 35.0, 36.0, 37.0, 38.0)] == [
        False, True, True, True, False, False, False]
    assert P._turned_on(res(40.0, "mgr_on"), settled) is True              # still under way: not failed (yet)
    assert P._turned_on(res(10.0, "mgr_off"), settled) is True             # only a Manager-on has an outcome


def test_a_blocked_turn_on_is_a_failed_manager_change_backed_off_from_its_write():
    led = [res(1000.0, "mgr_on", op_id="a"), out(1000.0, "blocked", BLOCKED)]
    streak, retry, err = P._fail_backoff(led, P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S)
    assert (streak, retry) == (1, 1000.0 + 900.0)
    assert err == P.turn_on_err("blocked", BLOCKED) and err.startswith("turn-on blocked: Could not verify")
    led += [res(2000.0, "mgr_on", op_id="b"), out(2000.0, "timeout", "still turning on")]
    assert P._fail_backoff(led, P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S)[:2] == (2, 2000.0 + 1800.0)
    led += [res(4000.0, "mgr_on", ok=False, err="Unavailable: BloomGauge answered 500 for /api/optimizer/control.")]
    assert P._fail_backoff(led, P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S)[:2] == (3, 4000.0 + 900.0)   # a plain failure
    assert [ts for ts, _e in P._fail_run(led, P.MGR_ACTIONS)] == [4000.0, 2000.0, 1000.0]
    pending = led + [res(5000.0, "mgr_on", op_id="c")]
    assert P._fail_backoff(pending, P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S) == (0, 0.0, None)      # under way: no streak
    for ok in ("active", "superseded", "overridden"):
        assert P._fail_run(pending + [out(5000.0, ok)], P.MGR_ACTIONS) == [], ok
    assert len(P._fail_run(pending + [out(5000.0, "cancelled", MANUAL)], P.MGR_ACTIONS)) == 4
    assert P._fail_run([res(1000.0, "mgr_on", op_id="a"), out(1000.0, "blocked", mode="shadow")], P.MGR_ACTIONS) == []


def test_own_off_at_skips_a_turn_on_that_did_not_turn_the_manager_on():
    led = [res(100.0, "mgr_off"), res(200.0, "mgr_on", op_id="a")]
    assert P._own_off_at(led) is None                                      # under way: counts as on
    assert P._own_off_at(led + [out(200.0, "blocked")]) == 100.0           # the queue's off stands: a leak again
    assert P._own_off_at(led + [out(200.0, "overridden")]) is None
    assert P._own_off_at(led + [out(200.0, "active")]) is None


def test_turn_on_is_the_newest_manager_change_when_it_is_an_answered_unsettled_manager_on():
    assert P._turn_on([]) is None and P._turn_on(None) is None
    assert P._turn_on([res(100.0, "mgr_on", op_id="a")]) == {"ts": 100.0, "op_id": "a"}
    assert P._turn_on([res(100.0, "mgr_on")]) is None                      # answered without an operation
    assert P._turn_on([res(100.0, "mgr_on", op_id="")]) is None
    assert P._turn_on([res(100.0, "mgr_on", ok=False, op_id="a")]) is None
    assert P._turn_on([res(100.0, "mgr_on", op_id="a"), res(200.0, "mgr_off")]) is None    # a newer change
    assert P._turn_on([res(100.0, "mgr_on", op_id="a"), out(100.0, "blocked")]) is None   # settled
    assert P._turn_on([res(100.0, "mgr_on", op_id="a", mode="shadow")]) is None
    assert P._turn_on([res(100.0, "mgr_on", op_id="a"), res(150.0, "pick")]) == {"ts": 100.0, "op_id": "a"}
    assert P._turn_on([res(200.0, "mgr_on", op_id="b"), res(100.0, "mgr_off")]) == {"ts": 200.0, "op_id": "b"}


def test_turn_on_outcome_reads_the_operation():
    pend = {"ts": NOW, "op_id": "a"}
    run = view(manager_on=True, switching=True, op_id="a", op_status="pending")
    assert P.turn_on_outcome(pend, run, NOW + 60) == (None, "")
    assert P.turn_on_outcome(pend, dict(run, op_status="verifying"), NOW + 60) == (None, "")   # unknown: running
    st, detail = P.turn_on_outcome(pend, run, NOW + P.TURN_ON_WAIT_S)
    assert st == "timeout" and "1860 s after the request" in detail
    assert P.turn_on_outcome(pend, dict(run, op_status="active"), NOW + 60) == ("active", "")
    blocked = view(op_id="a", op_status="blocked", op_detail=BLOCKED)
    assert P.turn_on_outcome(pend, blocked, NOW + 60) == ("blocked", BLOCKED)
    assert P.turn_on_outcome(pend, blocked, NOW + 60, manual_at=NOW + 30) == ("blocked", BLOCKED)
    cancelled = view(op_id="a", op_status="cancelled", op_detail=MANUAL)
    assert P.turn_on_outcome(pend, cancelled, NOW + 60) == ("cancelled", MANUAL)
    assert P.turn_on_outcome(pend, cancelled, NOW + 60, manual_at=NOW + 30)[0] == "overridden"
    assert P.turn_on_outcome(pend, cancelled, NOW + 60, manual_at=NOW - P.MATCH_EARLY_S)[0] == "overridden"
    assert P.turn_on_outcome(pend, cancelled, NOW + 60, manual_at=NOW - 100)[0] == "cancelled"   # before the request
    other = view(op_id="b", op_status="pending", switching=True, manager_on=True)
    assert P.turn_on_outcome(pend, other, NOW + 60) == (None, "")
    assert P.turn_on_outcome(pend, other, NOW + P.TURN_ON_WAIT_S)[0] == "timeout"
    assert P.turn_on_outcome(pend, view(op_id="b", op_status="active", manager_on=True), NOW + 60)[0] == "superseded"
    assert P.turn_on_outcome(pend, view(), NOW + 60, manual_at=NOW + 10)[0] == "overridden"
    assert P.turn_on_outcome(pend, view(), NOW + 60)[0] == "lost"
    assert P.turn_on_outcome(None, run, NOW) == (None, "") and P.turn_on_outcome(pend, None, NOW) == (None, "")


def test_budget_carries_the_turn_on_and_counts_a_blocked_one_as_failed():
    led = own(NOW - 100, "mgr_on", op_id="a")
    b = P.budget(led, [], NOW, {})
    assert (b["turn_on"], b["mgr_fails"], b["mgr_left"]) == ({"ts": NOW - 100, "op_id": "a"}, 0, 3)
    b = P.budget(led + [out(NOW - 100, "blocked", BLOCKED)], [], NOW, {})
    assert (b["turn_on"], b["mgr_fails"], b["mgr_retry_at"]) == (None, 1, NOW - 100 + 900)


def test_ownerless_and_hand_back_wait_while_a_turn_on_is_under_way():
    pend = {"ts": NOW - 60, "op_id": "a"}
    d = P.ownerless_step(view(), 5000.0, CFG, bud(turn_on=pend), NOW)
    assert d == {"action": "wait", "model": None, "reason": "ownerless idle: BloomGauge is turning the Manager on"}
    assert P.ownerless_step(view(), 5000.0, CFG, bud(), NOW)["action"] == "mgr_on"
    lease = {"state": "HANDBACK", "since": NOW - 5000, "prior_manager_on": True, "idle_since": NOW - 2000}
    d, new = P.idle_step(lease, view(manager_on=True), NOW, CFG, bud(turn_on=pend))
    assert d["reason"] == "handback: BloomGauge is turning the Manager on" and new["state"] == "HANDBACK"


# ---------------------------------------------------------------- a late turn-on is the queue's own
def test_a_turn_on_bloomgauge_finishes_late_is_the_queues_own_change():
    led = own(NOW, "mgr_on", op_id="a")
    req = [agent_req(NOW + 1), ctl_req(NOW + 1, "yes")]
    for late in (60.0, 179.0, 181.0, 600.0, 1790.0):
        assert usage(led, req + [flip(NOW + late)]) == (0, 0, None), late
    assert usage(led, req + [flip(NOW + 600.0, why=None)]) == (0, 0, None)   # past HINT_SECONDS: no why
    assert usage(led, [flip(NOW + 600.0)]) == (0, 0, None)                   # its request rows scrolled out
    assert usage(led, req + [flip(NOW + 1900.0)]) == (0, 0, NOW + 1900.0)    # past TURN_ON_WAIT_S: not ours


def test_the_owners_on_click_while_the_queues_turn_on_waits_still_counts_as_manual():
    led = own(NOW, "mgr_on", op_id="a")
    req = [agent_req(NOW + 1), ctl_req(NOW + 1, "yes")]
    assert usage(led, req + [ctl_req(NOW + 500, "yes"), flip(NOW + 600)]) == (0, 0, NOW + 600)
    # clicked more than MATCH_LATE_S before the change: the newest request (the owner's) caused it
    assert usage(led, req + [ctl_req(NOW + 300, "yes"), flip(NOW + 600)]) == (0, 0, NOW + 600)


def test_the_late_window_stops_at_the_previous_mode_change():
    led = own(NOW, "mgr_on", op_id="a")
    between = [flip(NOW + 100, "on", "startup"), flip(NOW + 400, "off", "blocked")]   # BloomGauge's own
    assert usage(led, [agent_req(NOW + 1), ctl_req(NOW + 1, "yes")] + between + [flip(NOW + 700)]) == (0, 0, NOW + 700)
    assert usage(led, between + [flip(NOW + 700)]) == (0, 0, NOW + 700)


def test_only_an_answered_turn_on_explains_a_late_change():
    failed = own(NOW, "mgr_on", ok=False, err="Unavailable: BloomGauge answered 500 for /api/optimizer/control.")
    unanswered = own(NOW, "mgr_on", ok=False,
                     err="Unavailable: BloomGauge is not answering on this Mac. Open the BloomGauge app and try again.")
    for led in (failed, unanswered, own(NOW, "mgr_off")):
        assert usage(led, [flip(NOW + 600)]) == (0, 0, NOW + 600)
    assert usage(failed, [flip(NOW + 100)]) == (0, 0, None)                  # in the usual window: as before


# ---------------------------------------------------------------- through the tick
class OpClient(FakeClient):
    """BloomGauge with its Manager-on operation (optimizer_control.py, beta 2026-11-02): the POST is answered
    at once with the operation pending, and the Manager reads on (automatic mode 'on', phase waiting) while
    it runs; the test then settles it (activate, block) or the owner clicks Manual (click_manual). Its
    activity log gets the rows a real write logs (agent.request + control.request at clock)."""

    def __init__(self, answer=True, **kw):
        super().__init__(**kw)
        self.clock, self.op, self.n, self.answer = NOW, None, 0, answer

    def pending(self):
        return self.op is not None and self.op["status"] in P.OP_RUNNING

    def get(self, path, params=None):
        if path != "/api/optimizer/control" or self.down:
            return super().get(path, params)
        self.reads.append(path)
        auto = {"mode": "on" if (self.manager_on or self.pending()) else "manual",
                "canEnable": self.can_enable and not self.pending()}
        return {"automatic": auto, "operation": dict(self.op or {})}

    def set_manager(self, args):
        self.writes.append(("set_manager", args))
        if self.fail_writes:
            raise Unavailable("TURN_ON")
        on = bool(args.get("on"))
        self.events += [agent_req(self.clock), ctl_req(self.clock, "yes" if on else "no")]
        if on:
            self.n += 1
            self.op = {"id": f"op-{self.n}", "status": "pending", "detail": "Checking readiness before turning On."}
            if not self.answer:
                return {"ok": True}
            return {"lastRequestId": self.op["id"], "operation": dict(self.op), "automatic": {"mode": "on"}}
        self._manual(self.clock)
        return {"lastRequestId": "manual-request", "operation": dict(self.op or {})}

    def _manual(self, at):
        if self.pending():
            self.op = dict(self.op, status="cancelled", detail=MANUAL)
        if self.manager_on:
            self.events.append(flip(at + 1, "off", "user-off"))
        self.manager_on = False

    def activate(self, at):
        self.op = dict(self.op, status="active", detail="On.")
        self.manager_on = True
        self.events.append(flip(at))

    def block(self, detail=BLOCKED):
        self.op = dict(self.op, status="blocked", detail=detail)

    def click_manual(self, at):
        self.events.append(ctl_req(at, "no"))       # a UI click: no agent.request
        self._manual(at)


@pytest.fixture(autouse=True)
def fresh_state():
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    deliver = T._STATE.pop("deliver", None)
    yield
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    if deliver is not None:
        T._STATE["deliver"] = deliver


def cfg(tmp_path):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on", "QUEUE_BG_IDLE_MIN": "1", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c, phase=None):
    p = c["ledger_path"]
    got = [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []
    return [r for r in got if phase is None or r.get("phase") == phase]


def handed_off(tmp_path, beat=NOW - 30.0, pid=None, **kw):
    """The queue turned the Manager off 5000 s ago and has been idle 2000 s: its hand-back is due."""
    c, cl = cfg(tmp_path), OpClient(manager_on=False, **kw)
    for r in own(NOW - 5000.0, "mgr_off"):
        C._ledger_append(c["ledger_path"], r)
    cl.events += [agent_req(NOW - 5000.0), ctl_req(NOW - 5000.0, "no"), flip(NOW - 4999.0, "off", "user-off")]
    C.lease_save(c["lease_path"], {"state": "DRAIN", "since": NOW - 5000.0, "target": "m1", "prior_manager_on": True,
                                   "idle_since": NOW - 2000.0, "mgr_off_at": NOW - 5000.0, "heartbeat": beat,
                                   "daemon_pid": os.getpid() if pid is None else pid, "mode": "on"})
    return c, cl


def step(c, cl, now, work=IDLE):
    cl.clock = now
    return T.tick(work, now, c, cl)


def statuses(c):
    return [r["status"] for r in rows(c, "outcome")]


def test_the_hand_back_follows_its_turn_on_until_active_then_releases(tmp_path):
    c, cl = handed_off(tmp_path)
    d = step(c, cl, NOW)
    assert d["action"] == "mgr_on" and cl.writes == [("set_manager", {"on": True})]
    assert rows(c, "result")[-1]["op_id"] == "op-1" and C.lease_load(c["lease_path"])["state"] == "HANDBACK"
    for t in range(30, 690, 30):                                           # BloomGauge's readiness wait
        d = step(c, cl, NOW + t)
        assert d["reason"] == "handback: BloomGauge is turning the Manager on", t
    assert len(cl.writes) == 1 and statuses(c) == []
    cl.activate(NOW + 700.0)                                               # on, 700 s after the request
    d = step(c, cl, NOW + 730.0)
    assert d["reason"] == "handback: Manager already on; released" and C.lease_load(c["lease_path"]) is None
    o = rows(c, "outcome")
    assert [(r["attempt_ts"], r["op_id"], r["status"]) for r in o] == [(NOW, "op-1", "active")]
    assert rows(c, "manual") == []                                         # the late change is the queue's own
    d = step(c, cl, NOW + 800.0, BUSY)                                     # new work: no 2 h manual backoff
    assert d["action"] == "mgr_off" and cl.writes[-1] == ("set_manager", {"on": False})


def test_a_blocked_turn_on_is_retried_after_the_backoff_and_alerts_once_per_streak(tmp_path):
    c, cl = handed_off(tmp_path)
    tries = [NOW, NOW + 900.0, NOW + 2700.0, NOW + 6300.0]                # 900, 1800, 3600 s after each write
    for i, t in enumerate(tries):
        if i:
            assert step(c, cl, t - 1.0)["reason"] == "handback: last Manager change failed; backing off"
        assert step(c, cl, t)["action"] == "mgr_on" and cl.op["id"] == f"op-{i + 1}"
        cl.block()
        d = step(c, cl, t + 30.0)
        assert d["reason"] == "handback: last Manager change failed; backing off"
        assert statuses(c) == ["blocked"] * (i + 1)
        assert P.budget(rows(c), cl.events, t + 30.0, c)["mgr_fails"] == i + 1
        alerted = T._STATE["alert"]
        if i == 2:
            assert alerted["key"] == "bloomgauge-control-manager" and "failed 3 Manager changes in a row" in alerted["msg"]
            assert "turn-on blocked: Could not verify" in alerted["msg"]
            T._STATE["alert"] = None
        else:
            assert alerted is None, i
        assert len(rows(c, "alert")) == (1 if i >= 2 else 0)
        assert C.lease_load(c["lease_path"])["state"] == "HANDBACK"
    assert step(c, cl, NOW + 9900.0)["action"] == "mgr_on"
    cl.activate(NOW + 9960.0)
    assert step(c, cl, NOW + 9990.0)["reason"] == "handback: Manager already on; released"
    assert statuses(c)[-1] == "active" and P.budget(rows(c), cl.events, NOW + 9990.0, c)["mgr_fails"] == 0
    assert C.lease_load(c["lease_path"]) is None and T._STATE["alert"] is None


def test_a_restarted_daemon_settles_the_turn_on_from_the_ledger(tmp_path):
    c, cl = handed_off(tmp_path)
    assert step(c, cl, NOW)["action"] == "mgr_on"
    T._STATE.clear()                                                       # a new daemon process
    T._STATE.update({"thread": None, "last": 0.0, "idle_since": None, "next_poll": 0.0, "alert": None,
                     "log_bad_since": None, "log_bad_n": 0, "log_alerted": False})
    assert step(c, cl, NOW + 60.0)["reason"] == "handback: BloomGauge is turning the Manager on"
    cl.block()
    assert step(c, cl, NOW + 120.0)["reason"] == "handback: last Manager change failed; backing off"
    T._STATE.update(idle_since=None, next_poll=0.0)                        # and again
    assert step(c, cl, NOW + 600.0)["reason"] == "handback: last Manager change failed; backing off"
    assert step(c, cl, NOW + 900.0)["action"] == "mgr_on" and statuses(c) == ["blocked"]


def test_work_arriving_mid_turn_on_waits_for_it_then_turns_the_manager_off(tmp_path):
    c, cl = handed_off(tmp_path)
    assert step(c, cl, NOW)["action"] == "mgr_on"
    reasons = {step(c, cl, NOW + t, BUSY)["reason"] for t in range(30, 600, 30)}
    assert reasons == {"hold: Manager on; confirming it stays on", "hold: Manager on; BloomGauge is mid-change"}
    assert len(cl.writes) == 1
    cl.activate(NOW + 600.0)
    d = step(c, cl, NOW + 610.0, BUSY)
    assert d["action"] == "mgr_off" and d["reason"] == "hold: Manager came back on; Manager off"
    assert statuses(c) == ["active"] and rows(c, "manual") == []


def test_the_owners_manual_click_mid_turn_on_is_their_call_not_a_failure(tmp_path):
    c, cl = handed_off(tmp_path)
    assert step(c, cl, NOW)["action"] == "mgr_on"
    cl.click_manual(NOW + 100.0)
    d = step(c, cl, NOW + 130.0)
    assert d["action"] == "yield" and d["reason"] == P.YIELD_KEEP_REASON
    assert statuses(c) == ["overridden"] and rows(c, "alert") == [] and T._STATE["alert"] is None
    assert P.budget(rows(c), cl.events, NOW + 130.0, c)["mgr_fails"] == 0
    assert C.lease_load(c["lease_path"])["yielded_at"] == NOW + 100.0
    for t in (NOW + 200.0, NOW + 3600.0, NOW + 7299.0):
        assert step(c, cl, t)["reason"] == "handback: manual change less than yield_s ago"
    assert len(cl.writes) == 1
    assert step(c, cl, NOW + 7300.0)["action"] == "mgr_on" and cl.op["id"] == "op-2"


def test_an_answer_naming_no_operation_releases_the_lease_at_once(tmp_path):
    c, cl = handed_off(tmp_path, answer=False)
    assert step(c, cl, NOW)["action"] == "mgr_on"
    assert C.lease_load(c["lease_path"]) is None and rows(c, "result")[-1]["op_id"] is None
    assert P.budget(rows(c), cl.events, NOW + 30.0, c)["turn_on"] is None
    assert step(c, cl, NOW + 30.0) is None and statuses(c) == []          # nothing left to follow


def test_bloomgauge_down_mid_turn_on_keeps_the_lease_and_settles_once_it_answers(tmp_path):
    c, cl = handed_off(tmp_path)
    assert step(c, cl, NOW)["action"] == "mgr_on"
    cl.down = True
    for t in (60.0, 600.0, 3000.0):
        assert step(c, cl, NOW + t)["reason"] == "app-down: BloomGauge not answering"
    assert statuses(c) == [] and C.lease_load(c["lease_path"])["state"] == "HANDBACK"
    cl.down = False
    cl.block()
    assert step(c, cl, NOW + 3060.0)["action"] == "mgr_on"                # backoff from the write long past
    assert statuses(c) == ["blocked"]


# ---------------------------------------------------------------- the stale-lease guard
def test_the_guard_keeps_the_stale_lease_until_bloomgauge_settles_its_turn_on(tmp_path):
    c, cl = handed_off(tmp_path, beat=NOW - 5000.0, pid=999999)
    alerts = []
    guard = lambda now: G.guard(now=now, cfg=c, client=cl, alive=lambda p: False,
                                alert=lambda k, m: alerts.append((k, m)))
    assert guard(NOW)["action"] == "mgr_on" and C.lease_load(c["lease_path"]) is not None
    d = guard(NOW + 300.0)
    assert d["reason"] == "deadman: handback: BloomGauge is turning the Manager on" and len(cl.writes) == 1
    cl.activate(NOW + 500.0)
    d = guard(NOW + 600.0)
    assert d["reason"] == "deadman: nothing to hand back; lease released" and C.lease_load(c["lease_path"]) is None
    assert statuses(c) == ["active"] and alerts == []


def test_the_guard_retries_a_blocked_turn_on_and_alerts_at_the_streak(tmp_path):
    c, cl = handed_off(tmp_path, beat=NOW - 5000.0, pid=999999)
    alerts = []
    guard = lambda now: G.guard(now=now, cfg=c, client=cl, alive=lambda p: False,
                                alert=lambda k, m: alerts.append(k))
    for t in (NOW, NOW + 900.0, NOW + 2700.0):
        assert guard(t)["action"] == "mgr_on"
        cl.block()
        assert guard(t + 300.0)["reason"] == "deadman: handback: last Manager change failed; backing off"
    assert alerts == ["bloomgauge-control-manager"] and statuses(c) == ["blocked"] * 3
    assert [r.get("cause") for r in rows(c, "alert")] == ["deadman"]
    assert C.lease_load(c["lease_path"]) is not None


# ---------------------------------------------------------------- the janitor
ROW_T = float(calendar.timegm((2026, 11, 2, 12, 0, 0)))


def jrow(key):
    return {"plan": key, "who": "BLOOMGAUGE CONTROL", "src": "Q", "ts": "2026-11-02T12:00:00Z"}


def jfacts(d, ledger):
    d.mkdir(parents=True, exist_ok=True)
    (d / "runs").mkdir(exist_ok=True)
    (d / "q.json").write_text(json.dumps({"jobs": []}))
    lp = d / "ledger.jsonl"
    lp.write_text("".join(json.dumps(r) + "\n" for r in ledger))
    return J.Facts(runs_dir=d / "runs", plans_dir=d / "runs", queue_state=d / "q.json", cancelled=lambda p: False,
                   bg_config={"mode": "on", "ledger_path": str(lp)})


def test_the_janitor_closes_the_manager_alert_only_once_a_turn_on_went_through(tmp_path):
    tracked = res(ROW_T + 60, "mgr_on", op_id="a")
    close = lambda key, led, n=[0]: J.decide(jrow(key), jfacts(tmp_path / str(n.append(1) or len(n)), led))
    ok, why = close("bloomgauge-control-manager", [tracked])
    assert ok is False and "still under way" in why
    for st in ("blocked", "cancelled", "overridden", "timeout", "lost"):
        ok, why = close("bloomgauge-control-manager", [tracked, out(ROW_T + 60, st)])
        assert ok is False and st in why, st
    for st in ("active", "superseded"):
        ok, why = close("bloomgauge-control-manager", [tracked, out(ROW_T + 60, st)])
        assert ok is True and st in why, st
    assert close("bloomgauge-control-manager", [res(ROW_T + 60, "mgr_on")])[0] is True        # no operation: as before
    assert close("bloomgauge-control-manager", [tracked, res(ROW_T + 90, "mgr_off")])[0] is True
    ok, why = close("bloomgauge-control-log", [tracked])                 # the log key: any answered write
    assert ok is True and "mgr_on write succeeded" in why
