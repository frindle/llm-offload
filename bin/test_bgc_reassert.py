"""2026-11-02: the queue holds the lease and has work, but the Manager is on (BloomGauge turned it back
on itself, e.g. 'recovery' when a timed Manual run ends, or a hand-back counted as failed landed after
all): the queue turns it off again, with the acquire's waits, once the Manager has read on for
REASSERT_CONFIRM_S (a manual change can show in the view before its activity-log row lands). A manual
change still yields, and a re-assert always leaves one Manager change this hour for the hand-back."""
import json
import os

import bloomgauge_control as C
import bloomgauge_policy as P
import bloomgauge_tick as T
from bgc_fake_client import FakeClient

NOW = 50000.0
CFG = {"yield_s": 7200, "allow_picks": True, "idle_s": 900}
BUD = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True}
WORK = {"busy": True, "why": "1 lane row(s)", "target": "m1"}
HELD = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": False}
SEEN = dict(HELD, mgr_on_seen_at=NOW - P.REASSERT_CONFIRM_S)      # read on for the whole confirm window
CONFIRMING = "hold: Manager on; confirming it stays on"
KEPT = "hold: Manager on; Manager budget used this hour (one kept for the hand-back)"
OFF = {"action": "mgr_off", "model": "m1", "reason": "hold: Manager came back on; Manager off"}


def view(**kw):
    v = {"agent_on": True, "manager_on": True, "can_enable": True, "blocker": None,
         "current": "m2", "serving": ["m2"], "switching": False}
    v.update(kw)
    return v


def ev(at, event, **fields):
    return {"at": at, "event": event, "fields": fields}


def test_a_manager_that_came_back_on_is_turned_off_again_once_confirmed():
    held = dict(HELD)
    d, lease = P.busy_step(held, view(), WORK, NOW, CFG, BUD)
    assert d == {"action": "wait", "model": "m1", "reason": CONFIRMING}
    assert lease == dict(HELD, mgr_on_seen_at=NOW) and held == HELD       # stamped on a copy
    d, again = P.busy_step(lease, view(), WORK, NOW + P.REASSERT_CONFIRM_S - 1, CFG, BUD)
    assert d["reason"] == CONFIRMING and again == lease                   # still confirming: stamp kept
    seen = dict(SEEN)
    d, lease = P.busy_step(seen, view(), WORK, NOW, CFG, BUD)
    assert d == OFF
    assert lease == dict(HELD, mgr_off_at=NOW, prior_manager_on=True)     # the stamp is spent
    assert seen == SEEN                                                   # input not mutated
    drain = dict(SEEN, state="DRAIN", idle_since=NOW - 100)
    d, lease = P.busy_step(drain, view(), WORK, NOW, CFG, BUD)
    assert d == OFF and lease["state"] == "HELD" and "idle_since" not in lease


def test_it_waits_like_an_acquire_and_keeps_the_lease():
    cases = [
        (dict(HELD, since=NOW - 100), BUD, view(), "hold: Manager on; the queue's own change may not show yet"),
        (dict(SEEN, since=NOW - 100), BUD, view(), "hold: Manager on; the queue's own change may not show yet"),
        (dict(SEEN, mgr_off_at=NOW - 100), BUD, view(), "hold: Manager on; the queue's own change may not show yet"),
        (dict(HELD, mgr_on_seen_at=NOW - 30), BUD, view(), CONFIRMING),
        (SEEN, dict(BUD, log_ok=False), view(), "hold: Manager on; activity log unreadable"),
        (SEEN, {k: v for k, v in BUD.items() if k != "log_ok"}, view(), "hold: Manager on; activity log unreadable"),
        (SEEN, BUD, view(switching=True), "hold: Manager on; BloomGauge is mid-change"),
        (SEEN, dict(BUD, mgr_left=0), view(), KEPT),
        (SEEN, dict(BUD, mgr_left=1), view(), KEPT),
        (SEEN, dict(BUD, mgr_retry_at=NOW + 1), view(), "hold: Manager on; last Manager change failed; backing off"),
    ]
    for lease, bud, v, reason in cases:
        d, new = P.busy_step(dict(lease), v, WORK, NOW, CFG, bud)
        assert d == {"action": "wait", "model": "m1", "reason": reason}, reason
        assert new == lease, reason                                       # no mgr_off_at, no state change
    d, _ = P.busy_step(dict(SEEN, since=NOW - P.REASSERT_GRACE_S), view(), WORK, NOW, CFG, BUD)
    assert d == OFF                                                       # the grace is a strict window
    d, _ = P.busy_step(dict(SEEN), view(), WORK, NOW, CFG, dict(BUD, mgr_left=2))
    assert d == OFF                                                       # leaves one for the hand-back


def test_the_confirm_window_starts_over_after_a_tick_gap_or_a_clock_jump():
    gap = dict(SEEN, heartbeat=NOW - P.REASSERT_CONFIRM_S - 1)            # no tick looked for a while
    d, new = P.busy_step(gap, view(), WORK, NOW, CFG, BUD)
    assert d["reason"] == CONFIRMING and new == dict(gap, mgr_on_seen_at=NOW)
    ahead = dict(HELD, mgr_on_seen_at=NOW + 600)                          # the clock went back
    d, new = P.busy_step(ahead, view(), WORK, NOW, CFG, BUD)
    assert d["reason"] == CONFIRMING and new["mgr_on_seen_at"] == NOW
    d, _ = P.busy_step(dict(SEEN, heartbeat=NOW - 30), view(), WORK, NOW, CFG, BUD)
    assert d == OFF                                                       # ticks 30 s apart: confirmed


def test_decide_drops_the_stamp_whenever_the_manager_is_not_seen_on():
    stamped = dict(HELD, mgr_on_seen_at=NOW - 30)
    off = view(manager_on=False, current="m1", serving=["m1"])
    for v, work in ((off, WORK), (off, {"busy": False}), (None, WORK), (view(agent_on=False), WORK)):
        _, new = P.decide(dict(stamped), v, work, NOW, CFG, BUD)
        assert new is not None and "mgr_on_seen_at" not in new, (v, work)
    _, new = P.decide(dict(stamped), view(), WORK, NOW, CFG, BUD)
    assert new == stamped                                                 # still on: kept
    held = dict(HELD)
    assert P.decide(held, None, WORK, NOW, CFG, BUD)[1] is held           # nothing to drop: same lease


def test_app_turn_on_reasserts_but_a_manual_one_yields():
    lease = dict(SEEN, since=NOW - 3600)
    app_on = [ev(NOW - 90, "mode.change", why="recovery", to="on", **{"from": "off"})]
    bud = P.budget([], app_on, NOW, {})
    assert bud["manual_at"] is None and bud["log_ok"] is True
    d, _ = P.decide(lease, view(), WORK, NOW, CFG, bud)
    assert d == OFF
    user_on = [ev(NOW - 90, "mode.change", why="user-on", to="on", **{"from": "off"})]
    d, new = P.decide(lease, view(), WORK, NOW, CFG, P.budget([], user_on, NOW, {}))
    assert d == {"action": "yield", "model": None, "reason": "yielded-to-owner: manual Manager change"} and new is None
    assert P.budget([], None, NOW, {})["log_ok"] is False


def test_a_manual_turn_on_that_is_logged_late_is_never_fought():
    """BloomGauge writes a request's row when it answers it (the mode.change at its next optimizer
    tick), so the view can show a manual Manager-on before the log does: the first tick only stamps,
    and once the row lands the queue yields with nothing sent."""
    lease = dict(HELD, since=NOW - 3600)
    d, lease = P.decide(lease, view(), WORK, NOW, CFG, P.budget([], [], NOW, {}))
    assert d["reason"] == CONFIRMING and lease["mgr_on_seen_at"] == NOW
    ui_on = [ev(NOW + 2, "control.request", what="manager", on="yes", result="ok")]
    d, lease = P.decide(lease, view(), WORK, NOW + 30, CFG, P.budget([], ui_on, NOW + 30, {}))
    assert d["action"] == "yield" and lease is None


def test_manager_off_paths_are_unchanged():
    d, lease = P.busy_step(dict(HELD), view(manager_on=False, current="m1", serving=["m1"]), WORK, NOW, CFG, BUD)
    assert d == {"action": "none", "model": "m1", "reason": "held"} and lease == HELD
    d, _ = P.busy_step(dict(HELD), view(manager_on=False), WORK, NOW, CFG, BUD)
    assert d["action"] == "pick"


# ---------------------------------------------------------------- tick level
def cfg(tmp_path):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on", "QUEUE_BG_IDLE_MIN": "0", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def held_since_50(c):
    C.lease_save(c["lease_path"], {"state": "HELD", "since": 50.0, "target": "m1", "prior_manager_on": False})


class LogDown(FakeClient):
    def get(self, path, params=None):
        if path == "/api/activity/log":
            self.reads.append(path)
            raise self.Unavailable("log unreadable")
        return super().get(path, params)


def test_tick_confirms_then_turns_it_off_once_and_records_it(tmp_path):
    c = cfg(tmp_path)
    held_since_50(c)
    cl = FakeClient(manager_on=True, events=[ev(9900.0, "mode.change", why="recovery", to="on", **{"from": "off"})])
    assert T.tick(WORK, 10000.0, c, cl)["reason"] == CONFIRMING
    assert T.tick(WORK, 10030.0, c, cl)["reason"] == CONFIRMING
    assert cl.writes == [] and json.load(open(c["lease_path"]))["mgr_on_seen_at"] == 10000.0
    assert T.tick(WORK, 10060.0, c, cl) == OFF and cl.writes == [("set_manager", {"on": False})]
    lease = json.load(open(c["lease_path"]))
    assert (lease["state"], lease["mgr_off_at"], lease["prior_manager_on"], lease["since"]) == ("HELD", 10060.0, True, 50.0)
    assert "mgr_on_seen_at" not in lease
    T.tick(WORK, 10090.0, c, cl)                                          # Manager off now: no second set_manager
    assert [w for w in cl.writes if w[0] == "set_manager"] == [("set_manager", {"on": False})]


def test_tick_failed_reassert_keeps_the_old_lease_and_backs_off(tmp_path):
    c = cfg(tmp_path)
    held_since_50(c)
    cl = FakeClient(manager_on=True, fail_writes=True)
    assert T.tick(WORK, 10000.0, c, cl)["reason"] == CONFIRMING
    assert T.tick(WORK, 10060.0, c, cl)["action"] == "mgr_off"
    lease = json.load(open(c["lease_path"]))
    assert "mgr_off_at" not in lease and lease["prior_manager_on"] is False and lease["state"] == "HELD"
    assert lease["mgr_on_seen_at"] == 10000.0                             # the old lease: still confirmed
    assert rows(c)[-1]["result"] == "failed"
    assert T.tick(WORK, 10090.0, c, cl)["reason"] == "hold: Manager on; last Manager change failed; backing off"
    assert len(cl.writes) == 1


def test_tick_never_reasserts_while_the_log_is_unreadable(tmp_path):
    c = cfg(tmp_path)
    held_since_50(c)
    cl = LogDown(manager_on=True)
    assert T.tick(WORK, 10000.0, c, cl)["reason"] == CONFIRMING
    assert T.tick(WORK, 10060.0, c, cl)["reason"] == "hold: Manager on; activity log unreadable"
    assert cl.writes == [] and os.path.exists(c["lease_path"])


def test_tick_a_manual_turn_on_that_is_logged_late_yields_with_nothing_sent(tmp_path):
    c = cfg(tmp_path)
    held_since_50(c)
    cl = FakeClient(manager_on=True)                                      # on in the view, not in the log yet
    assert T.tick(WORK, 10000.0, c, cl)["reason"] == CONFIRMING
    cl.events.append(ev(10003.0, "control.request", what="manager", on="yes", result="ok"))
    assert T.tick(WORK, 10030.0, c, cl)["action"] == "yield"
    assert not os.path.exists(c["lease_path"])
    assert T.tick(WORK, 10090.0, c, cl)["reason"] == "backoff: manual Manager change less than 2h ago"
    assert cl.writes == []
