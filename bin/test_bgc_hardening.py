"""BloomGauge control hardening (2026-11-02): failure backoff, causal matching of the activity log,
the failed-pick lease restore and the one-alert-per-streak rule. Hermetic (tmp_path, fake client)."""
import json
import os

import bloomgauge_control as C
import bloomgauge_policy as P
import bloomgauge_tick as T
from bgc_fake_client import FakeClient, Unavailable

R429 = "Unavailable: Agents can switch models or restart Darkbloom at most 2 times an hour, try later"
VIEW = {"agent_on": True, "manager_on": False, "can_enable": True, "blocker": None,
        "current": "m1", "serving": ["m1", "m2"], "switching": False}
CFG = {"idle_s": 900, "yield_s": 7200, "allow_picks": True}


def res(ts, action, ok, err=None, mode="on"):
    return {"ts": ts, "action": action, "mode": mode, "phase": "result",
            "result": "ok" if ok else "failed", "err": err}


def att(ts, action, mode="on"):
    return {"ts": ts, "action": action, "mode": mode, "phase": "attempt"}


def ev(at, event, **fields):
    return {"at": at, "event": event, "fields": fields}


# ---------------------------------------------------------------- _fail_backoff / budget
def test_fail_backoff_steps_through_the_table_and_resets_on_success():
    t = P.PICK_FAIL_BACKOFF_S
    assert P._fail_backoff([], ("pick",), t) == (0, 0.0, None)
    led = [res(1000.0, "pick", False, "boom")]
    assert P._fail_backoff(led, ("pick",), t) == (1, 1000.0 + t[0], "boom")
    led.append(res(2000.0, "pick", False, "boom2"))
    assert P._fail_backoff(led, ("pick",), t) == (2, 2000.0 + t[1], "boom2")
    led += [res(3000.0, "pick", False, "b3"), res(4000.0, "pick", False, "b4")]
    assert P._fail_backoff(led, ("pick",), t) == (4, 4000.0 + t[-1], "b4")    # capped at the last step
    led.append(res(5000.0, "pick", True))
    assert P._fail_backoff(led, ("pick",), t) == (0, 0.0, None)


def test_fail_backoff_ignores_other_kinds_shadow_rows_and_orders_by_ts():
    led = [res(900.0, "mgr_off", False, "x"), res(950.0, "pick", False, "y", mode="shadow"),
           res(2000.0, "pick", True), res(100.0, "pick", False, "junk at ts=100 sorts first")]
    assert P._fail_backoff(led, ("pick",), P.PICK_FAIL_BACKOFF_S) == (0, 0.0, None)
    assert P._fail_backoff(led, P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S) == (1, 900.0 + P.MGR_FAIL_BACKOFF_S[0], "x")


def test_a_429_waits_at_least_rate_limited_min_s():
    s, at, err = P._fail_backoff([res(1000.0, "mgr_on", False, R429)], P.MGR_ACTIONS, P.MGR_FAIL_BACKOFF_S)
    assert s == 1 and at == 1000.0 + P.RATE_LIMITED_MIN_S > 1000.0 + P.MGR_FAIL_BACKOFF_S[0]
    s, at, _ = P._fail_backoff([res(1000.0, "pick", False, R429)] * 3, ("pick",), P.PICK_FAIL_BACKOFF_S)
    assert at == 1000.0 + P.PICK_FAIL_BACKOFF_S[-1]          # a longer step is kept


def test_budget_reports_the_failure_backoff():
    led = [att(9000.0, "pick"), res(9000.0, "pick", False, "e1"),
           att(9100.0, "mgr_off"), res(9100.0, "mgr_off", False, "e2")]
    b = P.budget(led, [], 9200.0, {})
    assert (b["pick_fails"], b["pick_retry_at"]) == (1, 9000.0 + P.PICK_FAIL_BACKOFF_S[0])
    assert (b["mgr_fails"], b["mgr_retry_at"]) == (1, 9100.0 + P.MGR_FAIL_BACKOFF_S[0])
    assert b["picks_left"] == 1 and b["mgr_left"] == 3                       # a failed attempt still spends budget


# ---------------------------------------------------------------- the backoff waits
def bud(**kw):
    b = {"picks_left": 2, "mgr_left": 4, "manual_at": None, "pick_fails": 0, "mgr_fails": 0, "log_ok": True,
         "pick_retry_at": 0.0, "mgr_retry_at": 0.0, "leak": True}
    b.update(kw)
    return b


def test_pick_backs_off_after_a_failed_pick():
    lease = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    work = {"busy": True, "why": "w", "target": "m1"}
    dec, new = P.decide(lease, VIEW, work, 1000.0, CFG, bud(pick_retry_at=1300.0))
    assert dec == {"action": "wait", "model": "m1", "reason": "pick: last pick failed; backing off"}
    assert "picked" not in new
    dec, new = P.decide(lease, VIEW, work, 1300.0, CFG, bud(pick_retry_at=1300.0))
    assert dec["action"] == "pick" and new["picked"] == "m1"


def test_acquire_handback_and_ownerless_back_off_after_a_failed_manager_change():
    on = dict(VIEW, manager_on=True)
    work = {"busy": True, "why": "w", "target": "m1"}
    dec, new = P.decide(None, on, work, 1000.0, CFG, bud(mgr_retry_at=1100.0))
    assert (dec["reason"], new) == ("acquire: last Manager change failed; backing off", None)
    assert P.decide(None, on, work, 1100.0, CFG, bud(mgr_retry_at=1100.0))[0]["action"] == "mgr_off"
    lease = {"state": "DRAIN", "since": 1.0, "idle_since": 0.0, "prior_manager_on": True}
    idle = {"busy": False, "why": "", "target": None}
    dec, new = P.decide(lease, VIEW, idle, 1000.0, CFG, bud(mgr_retry_at=1100.0))
    assert dec["reason"] == "handback: last Manager change failed; backing off" and new["state"] == "HANDBACK"
    assert P.decide(lease, VIEW, idle, 1100.0, CFG, bud(mgr_retry_at=1100.0))[0]["action"] == "mgr_on"
    d = P.ownerless_step(VIEW, 5000.0, CFG, bud(mgr_retry_at=1100.0), 1000.0)
    assert d["reason"] == "ownerless idle: last Manager change failed; backing off"
    assert P.ownerless_step(VIEW, 5000.0, CFG, bud(mgr_retry_at=1100.0), 1100.0)["action"] == "mgr_on"


# ---------------------------------------------------------------- causal matching (_other_usage)
def test_own_writes_match_their_rows_and_never_count_as_manual():
    own = [att(1000.0, "mgr_off"), att(2000.0, "pick")]
    events = [ev(1001, "agent.request", what="manager", result="ok"),
              ev(1001, "control.request", what="manager", on="no", result="ok"),
              ev(1009, "mode.change", **{"from": "on", "to": "off", "why": "user-off"}),
              ev(2002, "agent.request", what="change-model", model="m1", result="ok")]
    assert P._other_usage(events, own, 0.0) == (0, 0, None)


def test_a_row_outside_the_match_window_is_someone_else():
    own = [att(1000.0, "mgr_off")]
    late = [ev(1000 + P.MATCH_LATE_S + 1, "mode.change", **{"from": "on", "to": "off", "why": "user-off"})]
    assert P._other_usage(late, own, 0.0)[2] == 1000 + P.MATCH_LATE_S + 1
    early = [ev(1000 - P.MATCH_EARLY_S - 1, "mode.change", **{"from": "on", "to": "off", "why": "user-off"})]
    assert P._other_usage(early, own, 0.0)[2] == 1000 - P.MATCH_EARLY_S - 1


def test_one_attempt_matches_one_row_per_type():
    own = [att(1000.0, "mgr_on")]
    events = [ev(1001, "mode.change", **{"from": "off", "to": "on", "why": "user-on"}),
              ev(1003, "mode.change", **{"from": "off", "to": "on", "why": "user-on"})]
    assert P._other_usage(events, own, 0.0)[2] == 1003


def test_bloomgauges_own_mode_changes_are_ignored():
    events = [ev(1000 + i, "mode.change", **{"from": "on", "to": "off", "why": w}) for i, w in enumerate(P.APP_WHYS)]
    assert P._other_usage(events, [], 0.0) == (0, 0, None)


def test_another_agents_writes_count_and_its_mode_change_is_manual():
    events = [ev(5000, "agent.request", what="change-model", model="g", result="ok"),
              ev(5000, "mode.change", **{"from": "on", "to": "off", "why": "pick"}),
              ev(5010, "agent.request", what="manager", result="ok"),
              ev(5010, "control.request", what="manager", on="yes", result="ok")]
    assert P._other_usage(events, [], 1000.0) == (1, 1, 5000)
    assert P._other_usage(events, [], 9000.0)[:2] == (0, 0)                  # older than the hour


def test_a_ui_click_is_manual_but_a_refused_one_is_not():
    assert P._other_usage([ev(7000, "control.request", what="manual", model="x", result="ok")], [], 0.0)[2] == 7000
    assert P._other_usage([ev(7000, "control.request", what="manual", result="refused")], [], 0.0)[2] is None


def test_bad_rows_are_skipped():
    events = [ev("x", "mode.change", to="off", why="user-off"), ev(float("nan"), "mode.change", to="off"),
              {"at": 5, "event": "mode.change", "fields": "junk"}, "junk", None]
    assert P._other_usage(events, [{"ts": True, "action": "mgr_off"}, "junk"], 0.0) == (0, 0, 5)


# ---------------------------------------------------------------- tick: failed writes
class PickFails(FakeClient):
    def pick_model(self, args):
        self.writes.append(("pick_model", args))
        raise Unavailable(R429)


def cfg(tmp_path, **env):
    e = {"QUEUE_BLOOMGAUGE_CONTROL": "on", "QUEUE_BG_IDLE_MIN": "0", "QUEUE_BG_QUIET_MIN": "0"}
    e.update(env)
    return C.config(e, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


BUSY = {"busy": True, "why": "1 lane row(s)", "target": "m1"}


def test_a_failed_pick_keeps_the_lease_but_not_the_pick(tmp_path):
    T._STATE["alert"] = None
    c, cl = cfg(tmp_path), PickFails(manager_on=False)
    C.lease_save(c["lease_path"], {"state": "HELD", "since": 50.0, "target": "m0", "prior_manager_on": True,
                                   "picked": "m0", "picked_at": 40.0})
    dec = T.tick(BUSY, 10000.0, c, cl)
    assert dec["action"] == "pick" and cl.writes == [("pick_model", {"model": "m1"})]
    lease = json.load(open(c["lease_path"]))
    assert (lease["target"], lease["picked"], lease["picked_at"], lease["heartbeat"]) == ("m1", "m0", 40.0, 10000.0)
    r = [x for x in rows(c) if x.get("phase") == "result"]
    assert r[-1]["result"] == "failed" and "at most 2 times an hour" in r[-1]["err"]
    assert T._STATE["alert"] is None                                           # 1 failure: no alert yet
    # the next tick waits on the 429 backoff instead of retrying
    assert T.tick(BUSY, 10030.0, c, cl)["reason"] == "pick: last pick failed; backing off"
    assert len(cl.writes) == 1


def test_a_failed_first_pick_leaves_no_pick_fields(tmp_path):
    c, cl = cfg(tmp_path), PickFails(manager_on=False)
    C.lease_save(c["lease_path"], {"state": "HELD", "since": 50.0, "target": "m1", "prior_manager_on": True})
    T.tick(BUSY, 10000.0, c, cl)
    lease = json.load(open(c["lease_path"]))
    assert "picked" not in lease and "picked_at" not in lease and lease["state"] == "HELD"


def test_a_failed_acquire_leaves_no_lease(tmp_path):
    c, cl = cfg(tmp_path), FakeClient(manager_on=True, fail_writes=True)
    assert T.tick(BUSY, 10000.0, c, cl)["action"] == "mgr_off"
    assert not os.path.exists(c["lease_path"])
    assert T.tick(BUSY, 10030.0, c, cl)["reason"] == "acquire: last Manager change failed; backing off"


def test_the_third_failure_in_a_row_raises_one_alert(tmp_path):
    T._STATE["alert"] = None
    c, cl = cfg(tmp_path), PickFails(manager_on=False)
    with open(c["ledger_path"], "a") as fh:                    # two older failures (outside the hour)
        for ts in (1000.0, 2000.0):
            fh.write(json.dumps(att(ts, "pick")) + "\n" + json.dumps(res(ts, "pick", False, "e")) + "\n")
    C.lease_save(c["lease_path"], {"state": "HELD", "since": 50.0, "target": "m1", "prior_manager_on": True})
    T.tick(BUSY, 10000.0, c, cl)
    a = T._STATE["alert"]
    assert a["key"] == "bloomgauge-control-pick" and "failed 3 picks of m1 in a row" in a["msg"]
    assert "Never call BloomGauge write tools or darkbloom by hand" in a["msg"]
    assert not any(ch.isdigit() for ch in a["msg"].split("(last:")[0].replace("3 picks", "").replace("m1", ""))
    T._STATE["alert"] = None
    T.tick(BUSY, 10000.0 + P.RATE_LIMITED_MIN_S + 4000.0, c, cl)   # 4th failure: no second alert
    assert len(cl.writes) == 2 and T._STATE["alert"] is None


def test_note_failure_keys_and_streak():
    T._STATE["alert"] = None
    mgr = {"action": "mgr_on", "model": None}
    assert T.note_failure(mgr, {"mgr_fails": 1}, {"err": "x"}, P) is None
    a = T.note_failure(mgr, {"mgr_fails": 2}, {"err": "x\n  y"}, P)
    assert a["key"] == "bloomgauge-control-manager" and "(last: x y)" in a["msg"] and T._STATE["alert"] is a
    assert T.note_failure(mgr, {"mgr_fails": 3, "mgr_alerted": True}, {"err": "x"}, P) is None
    T._STATE["alert"] = None
    # a streak whose 3rd failure raised nothing (e.g. the stale-lease guard's) alerts on the next one
    late = T.note_failure(mgr, {"mgr_fails": 3}, {"err": "x"}, P)
    assert late["key"] == "bloomgauge-control-manager" and "failed 4 Manager changes" in late["msg"]
    T._STATE["alert"] = None
