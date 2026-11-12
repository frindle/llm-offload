"""2026-11-02 (review fix): the queue's BloomGauge control makes no write and takes no lease while the
manager activity log is unreadable (bud log_ok not True): a manual change could not be seen then, so
an acquire, pick, hand-back or ownerless turn-on could fight the owner. read_inputs counts a malformed
answer as unreadable and reads BloomGauge's PAGE_MAX rows; a log that stays unreadable while
BloomGauge answers raises one alert (bloomgauge-control-log), which the janitor closes once the log
reads again. Hermetic: tmp files, fake client, no notifications."""
import calendar
import importlib.util
import json
import os

import pytest

import bloomgauge_control as C
import bloomgauge_policy as P
import bloomgauge_tick as T
from bgc_fake_client import FakeClient

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = 50000.0
CFG = {"yield_s": 7200, "allow_picks": True, "idle_s": 900, "handback": "always"}
OK = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True, "leak": True}
BAD = dict(OK, log_ok=False)
NOKEY = {k: v for k, v in OK.items() if k != "log_ok"}
BUSY = {"busy": True, "why": "1 lane row(s)", "target": "m1"}
IDLE = {"busy": False, "why": "queue done", "target": None}


def view(**kw):
    v = {"agent_on": True, "manager_on": True, "can_enable": True, "blocker": None,
         "current": "m2", "serving": ["m1", "m2"], "switching": False}
    v.update(kw)
    return v


@pytest.fixture(autouse=True)
def fresh_state():
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    yield
    T._STATE.update(alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)


def cfg(tmp_path, mode="on"):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": mode, "QUEUE_BG_IDLE_MIN": "1", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


class LogDown(FakeClient):
    """BloomGauge answers, its activity log does not (log_mode 'raise'), or answers in a bad shape."""
    def __init__(self, log_mode="raise", **kw):
        super().__init__(**kw)
        self.log_mode, self.params = log_mode, []

    def get(self, path, params=None):
        if path == "/api/activity/log":
            self.reads.append(path)
            self.params.append(params)
            if self.log_mode == "raise":
                raise self.Unavailable("log unreadable")
            if self.log_mode == "ok":
                return {"events": list(self.events)}
            return self.log_mode
        return super().get(path, params)


# ---------------------------------------------------------------- the policy gates
@pytest.mark.parametrize("bud", [BAD, NOKEY])
def test_acquire_takes_no_lease_while_the_log_is_unreadable(bud):
    for v in (view(), view(manager_on=False)):                 # incl. the no-write "already off; holding"
        d, lease = P.busy_step(None, v, BUSY, NOW, CFG, bud)
        assert d == {"action": "wait", "model": None, "reason": "acquire: activity log unreadable"}
        assert lease is None
    d, lease = P.busy_step(None, view(manager_on=False), BUSY, NOW, CFG, OK)
    assert d["reason"] == "acquire: Manager already off; holding" and lease["state"] == "HELD"


@pytest.mark.parametrize("bud", [BAD, NOKEY])
def test_no_pick_while_the_log_is_unreadable(bud):
    held = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    d, lease = P.busy_step(held, view(manager_on=False), BUSY, NOW, CFG, bud)
    assert d == {"action": "wait", "model": "m1", "reason": "pick: activity log unreadable"}
    assert "picked" not in lease
    d, _ = P.busy_step(held, view(manager_on=False), BUSY, NOW, CFG, OK)
    assert d["action"] == "pick"


@pytest.mark.parametrize("bud", [BAD, NOKEY])
def test_no_handback_while_the_log_is_unreadable_but_writeless_releases_still_run(bud):
    drain = {"state": "DRAIN", "since": 1.0, "idle_since": NOW - 901, "target": "m1", "prior_manager_on": True}
    d, lease = P.idle_step(drain, view(manager_on=False), NOW, CFG, bud)
    assert d == {"action": "wait", "model": None, "reason": "handback: activity log unreadable"}
    assert lease["state"] == "HANDBACK" and drain["state"] == "DRAIN"
    d, lease = P.idle_step(lease, view(manager_on=False), NOW, CFG, OK)
    assert d["action"] == "mgr_on" and lease["state"] == "HANDBACK"     # kept until the turn-on settles
    d, lease = P.idle_step(drain, view(manager_on=True), NOW, CFG, bud)       # already on: released, no write
    assert d["action"] == "none" and lease is None
    prior_off = dict(drain, prior_manager_on=False)
    d, lease = P.idle_step(prior_off, view(manager_on=False), NOW, dict(CFG, handback="prior"), bud)
    assert d["reason"] == "handback: Manager was off before; released" and lease is None


@pytest.mark.parametrize("bud", [BAD, NOKEY])
def test_no_ownerless_turn_on_while_the_log_is_unreadable(bud):
    d = P.ownerless_step(view(manager_on=False), 5000.0, CFG, bud, NOW)
    assert d == {"action": "wait", "model": None, "reason": "ownerless idle: activity log unreadable"}
    assert P.ownerless_step(view(manager_on=False), 5000.0, CFG, OK, NOW)["action"] == "mgr_on"
    assert P.ownerless_step(view(manager_on=False), 10.0, CFG, bud, NOW)["action"] == "none"   # not idle long enough


def test_budget_marks_the_log_unreadable_only_when_events_is_none():
    assert P.budget([], None, NOW, {})["log_ok"] is False
    assert P.budget([], [], NOW, {})["log_ok"] is True


# ---------------------------------------------------------------- read_inputs
@pytest.mark.parametrize("answer,expect", [
    ({"events": [{"at": 5, "event": "mode.change"}]}, [{"at": 5, "event": "mode.change"}]),
    ({"events": []}, []),
    ({}, None), ({"events": None}, None), ({"events": "x"}, None), ({"events": {"a": 1}}, None),
    (["not", "a", "dict"], None), (None, None), ("text", None),
])
def test_read_inputs_counts_a_malformed_log_answer_as_unreadable(tmp_path, answer, expect):
    cl = LogDown(log_mode=answer)
    view_, events, _ = T.read_inputs(cl, cfg(tmp_path), None, "on", P)
    assert view_ is not None and events == expect
    assert cl.params == [{"kind": "manager", "limit": T.LOG_LIMIT}] and T.LOG_LIMIT == 500


# ---------------------------------------------------------------- the tick
def test_tick_busy_with_no_lease_waits_and_writes_nothing(tmp_path):
    c = cfg(tmp_path)
    for mgr in (True, False):
        cl = LogDown(manager_on=mgr)
        d = T.tick(BUSY, 10000.0, c, cl)
        assert d == {"action": "wait", "model": None, "reason": "acquire: activity log unreadable"}
        assert cl.writes == [] and not os.path.exists(c["lease_path"])
    cl = LogDown(log_mode="ok", manager_on=True)                       # the log reads again: acquire
    assert T.tick(BUSY, 10030.0, c, cl)["action"] == "mgr_off" and os.path.exists(c["lease_path"])


def test_tick_holds_a_drained_lease_instead_of_handing_back(tmp_path):
    c = cfg(tmp_path)
    C.lease_save(c["lease_path"], {"state": "DRAIN", "since": 50.0, "idle_since": 100.0, "target": "m1",
                                   "prior_manager_on": True})
    cl = LogDown(manager_on=False)
    assert T.tick(IDLE, 10000.0, c, cl)["reason"] == "handback: activity log unreadable"
    assert cl.writes == [] and json.load(open(c["lease_path"]))["state"] == "HANDBACK"
    cl = LogDown(log_mode="ok", manager_on=False)
    assert T.tick(IDLE, 10030.0, c, cl)["action"] == "mgr_on" and not os.path.exists(c["lease_path"])


def test_ownerless_tick_writes_nothing_while_the_log_is_unreadable(tmp_path):
    c = cfg(tmp_path)
    leaked = {"ts": 10.0, "action": "mgr_off", "mode": "on", "phase": "result", "result": "ok", "err": None}
    C._ledger_append(c["ledger_path"], leaked)   # the queue left the Manager off itself
    cl = LogDown(manager_on=False)
    assert T.tick(IDLE, 100.0, c, cl) is None
    d = T.tick(IDLE, 160.0, c, cl)
    assert d == {"action": "wait", "model": None, "reason": "ownerless idle: activity log unreadable"}
    assert cl.writes == []


# ---------------------------------------------------------------- the alert
def test_note_log_alerts_once_after_three_bad_reads_over_30_min_while_the_app_answers(tmp_path):
    c, v = cfg(tmp_path), view()
    assert T.note_log(v, None, 1000.0, c, C) is None
    assert T.note_log(v, None, 1000.0 + T.LOG_ALERT_S - 1, c, C) is None          # not 30 min yet
    assert T.note_log(None, None, 1000.0 + T.LOG_ALERT_S + 5, c, C) is None        # app down: not counted
    assert T._STATE["log_bad_n"] == 2
    a = T.note_log(v, None, 1000.0 + T.LOG_ALERT_S, c, C)
    assert a["key"] == "bloomgauge-control-log" and T._STATE["alert"] == a
    assert "never call bloomgauge write tools" in a["msg"].lower() and "darkbloom" in a["msg"]
    assert not any(ch.isdigit() for ch in a["msg"].replace("30+", "").replace("/api/", ""))   # stable text
    T._STATE["alert"] = None
    assert T.note_log(v, None, 9000.0, c, C) is None and T._STATE["alert"] is None   # once per streak
    assert rows(c) == []
    assert T.note_log(v, [], 9030.0, c, C) is None                                   # reads again
    assert [(r["phase"], r["reason"]) for r in rows(c)] == [("decision", T.LOG_BACK_REASON)]
    assert T._STATE["log_bad_n"] == 0 and T._STATE["log_alerted"] is False
    T.note_log(v, [], 9060.0, c, C)
    assert len(rows(c)) == 1                                                         # logged once


def test_note_log_a_good_read_resets_the_streak_and_shadow_never_alerts(tmp_path):
    c, v = cfg(tmp_path), view()
    T.note_log(v, None, 0.0, c, C)
    T.note_log(v, None, 100.0, c, C)
    T.note_log(v, [], 200.0, c, C)                                                   # transient: reset, no row
    assert T._STATE["log_bad_since"] is None and rows(c) == []
    T.note_log(v, None, 300.0, c, C)
    T.note_log(v, None, 400.0, c, C)
    assert T.note_log(v, None, 300.0 + T.LOG_ALERT_S, c, C)["key"] == "bloomgauge-control-log"
    T._STATE.update(alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    for t in (0.0, 1000.0, 5000.0):
        assert T.note_log(v, None, t, c, C, eff="shadow") is None


def test_tick_raises_the_log_alert_for_the_queue_to_pop(tmp_path):
    c = cfg(tmp_path)
    cl = LogDown(manager_on=True)
    for t in (10000.0, 10900.0, 10000.0 + T.LOG_ALERT_S):
        T.tick(BUSY, t, c, cl)
    assert (T._STATE.get("alert") or {}).get("key") == "bloomgauge-control-log"
    assert cl.writes == []


# ---------------------------------------------------------------- the janitor closes it
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


J = _load("eij_bgc_logok", "escalation_index_janitor.py")
ROW_T = float(calendar.timegm((2026, 11, 2, 12, 0, 0)))
ROW = {"plan": "bloomgauge-control-log", "who": "BLOOMGAUGE CONTROL", "src": "Q", "ts": "2026-11-02T12:00:00Z"}


def facts(d, mode="on", ledger=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "runs").mkdir(exist_ok=True)
    lp = d / "ledger.jsonl"
    if ledger is not None:
        lp.write_text("".join(json.dumps(r) + "\n" for r in ledger))
    return J.Facts(runs_dir=d / "runs", plans_dir=d / "runs", queue_state=d / "q.json", cancelled=lambda p: False,
                   bg_config={"mode": mode, "ledger_path": str(lp)})


def test_janitor_closes_the_log_alert_only_once_the_log_reads_again(tmp_path):
    assert J.CONTROL_LOG_BACK == T.LOG_BACK_REASON and J.CONTROL_LOG_KEY == T.LOG_ALERT_KEY
    assert J.decide(ROW, facts(tmp_path / "a", mode="off"))[0] is True
    assert J.decide(ROW, facts(tmp_path / "b", ledger=[]))[0] is False
    waiting = [{"ts": ROW_T + 60, "action": "wait", "mode": "on", "phase": "decision",
                "reason": "handback: activity log unreadable"}]
    assert J.decide(ROW, facts(tmp_path / "c", ledger=waiting)) == (
        False, "the activity log has not read again since the alert")
    back = waiting + [{"ts": ROW_T + 90, "action": "none", "mode": "on", "phase": "decision", "reason": T.LOG_BACK_REASON}]
    ok, why = J.decide(ROW, facts(tmp_path / "d", ledger=back))
    assert ok is True and "activity log read again" in why
    stale = [dict(back[1], ts=ROW_T - 3600)]
    assert J.decide(ROW, facts(tmp_path / "e", ledger=stale))[0] is False
    wrote = [{"ts": ROW_T + 120, "action": "mgr_on", "mode": "on", "phase": "result", "result": "ok"}]
    ok, why = J.decide(ROW, facts(tmp_path / "f", ledger=wrote))
    assert ok is True and "mgr_on write succeeded" in why
    shadow = [dict(back[1], mode="shadow")]
    assert J.decide(ROW, facts(tmp_path / "g", ledger=shadow))[0] is False
