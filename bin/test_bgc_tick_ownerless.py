import json
import os

import pytest

import bloomgauge_control as C
import bloomgauge_tick as T
from bgc_fake_client import FakeClient

BUSY = {"busy": True, "why": "1 lane row(s)", "target": "m1"}
IDLE = {"busy": False, "why": "queue done", "target": None}
REASON = "ownerless idle: manager off, queue idle >= idle_s -> manager on"


@pytest.fixture(autouse=True)
def fresh_state():
    T._STATE.update(idle_since=None, next_poll=0.0)


def cfg(tmp_path, mode):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": mode, "QUEUE_BG_IDLE_MIN": "1", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def leak(c, ts=10.0):
    """The queue's own successful Manager-off, never handed back (e.g. its lease file was lost):
    the only case ownerless turns the Manager on (review #2)."""
    C._ledger_append(c["ledger_path"], {"ts": ts, "action": "mgr_off", "mode": "on", "phase": "result",
                                        "result": "ok", "err": None})


def test_on_idle_manager_off_turns_on_after_idle_s(tmp_path):
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=False)
    leak(c)
    assert T.tick(IDLE, 100.0, c, cl) is None
    assert T.tick(IDLE, 130.0, c, cl) is None and cl.reads == [] and cl.writes == []
    d = T.tick(IDLE, 160.0, c, cl)
    assert (d or {}).get("action") == "mgr_on" and (d or {}).get("reason") == REASON
    assert cl.writes == [("set_manager", {"on": True})] and cl.manager_on
    assert [r["phase"] for r in rows(c)[1:]] == ["attempt", "result"] and rows(c)[1]["reason"] == REASON
    assert not os.path.exists(c["lease_path"])
    assert (T.tick(BUSY, 170.0, c, cl) or {}).get("action") == "mgr_off" and json.load(open(c["lease_path"]))["prior_manager_on"] is True


def test_shadow_logs_would_do_and_never_writes(tmp_path):
    c, cl = cfg(tmp_path, "shadow"), FakeClient(manager_on=False)
    leak(c)
    for t in (100.0, 160.0, 220.0, 400.0):
        T.tick(IDLE, t, c, cl)
    assert cl.writes == [] and [r["phase"] for r in rows(c)[1:]] == ["shadow"]
    assert rows(c)[1]["reason"] == REASON


def test_busy_restarts_idle_timer(tmp_path):
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=False)
    T.tick(IDLE, 100.0, c, cl)
    T.tick(BUSY, 130.0, c, FakeClient(manager_on=True))
    os.remove(c["lease_path"])
    T.tick(IDLE, 140.0, c, cl)
    assert T.tick(IDLE, 190.0, c, cl) is None and cl.writes == []
    assert (T.tick(IDLE, 200.0, c, cl) or {}).get("action") == "mgr_on"


def test_manager_on_polled_at_most_every_minute(tmp_path):
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=True)
    T.tick(IDLE, 100.0, c, cl)
    T.tick(IDLE, 160.0, c, cl)
    n = len(cl.reads)
    assert n > 0
    for t in (170.0, 200.0, 219.0):
        T.tick(IDLE, t, c, cl)
    assert len(cl.reads) == n
    T.tick(IDLE, 221.0, c, cl)
    assert len(cl.reads) > n and cl.writes == [] and rows(c) == []


def test_failed_write_backs_off_and_never_loops(tmp_path):
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=False, fail_writes=True)
    leak(c)
    T.tick(IDLE, 100.0, c, cl)
    T.tick(IDLE, 160.0, c, cl)
    for t in (220.0, 500.0, 1800.0, 3000.0):
        T.tick(IDLE, t, c, cl)
    assert len(cl.writes) == 1
    assert [r["phase"] for r in rows(c)[1:]] == ["attempt", "result"] and rows(c)[2]["result"] == "failed"


def test_recent_manual_change_is_not_fought(tmp_path):
    ev = [{"at": 120.0, "event": "mode.change", "fields": {}}]
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=False, events=ev)
    for t in (100.0, 160.0, 400.0):
        T.tick(IDLE, t, c, cl)
    assert cl.writes == []
