import json
import os

import bloomgauge_control as C
import bloomgauge_tick as T
from bgc_fake_client import FakeClient

BUSY = {"busy": True, "why": "1 lane row(s)", "target": "m1"}
IDLE = {"busy": False, "why": "queue done", "target": None}


def cfg(tmp_path, mode, **env):
    e = {"QUEUE_BLOOMGAUGE_CONTROL": mode, "QUEUE_BG_IDLE_MIN": "0", "QUEUE_BG_QUIET_MIN": "0"}
    e.update(env)
    return C.config(e, tmp_path)


def rows(c):
    p = c["ledger_path"]
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def test_off_or_idle_without_lease_reads_nothing(tmp_path):
    cl = FakeClient()
    assert T.tick(BUSY, 100.0, cfg(tmp_path, "off"), cl) is None
    assert T.tick(IDLE, 100.0, cfg(tmp_path, "on"), cl) is None
    assert T.tick(IDLE, 100.0, cfg(tmp_path, "shadow"), cl) is None
    assert cl.reads == [] and rows(cfg(tmp_path, "on")) == []


def test_on_busy_turns_manager_off_and_saves_lease(tmp_path):
    c, cl = cfg(tmp_path, "on"), FakeClient(manager_on=True)
    d = T.tick(BUSY, 100.0, c, cl)
    assert d["action"] == "mgr_off" and cl.writes == [("set_manager", {"on": False})]
    lease = json.load(open(c["lease_path"]))
    assert lease["state"] == "HELD" and lease["heartbeat"] == 100.0 and lease["daemon_pid"] == os.getpid()
    assert lease["mode"] == "on" and lease["prior_manager_on"] is True
    for t in (130.0, 160.0):
        T.tick(IDLE, t, c, cl)
    assert cl.writes[-1] == ("set_manager", {"on": True}) and not os.path.exists(c["lease_path"])


def test_shadow_uses_its_own_lease_and_never_writes(tmp_path):
    c, cl = cfg(tmp_path, "shadow"), FakeClient(manager_on=True)
    assert T.tick(BUSY, 100.0, c, cl)["action"] == "mgr_off"
    assert os.path.exists(c["shadow_lease_path"]) and not os.path.exists(c["lease_path"])
    assert json.load(open(c["shadow_lease_path"]))["mode"] == "shadow"
    T.tick(IDLE, 130.0, c, cl)
    assert T.tick(IDLE, 160.0, c, cl)["action"] == "mgr_on"
    assert cl.writes == [] and not os.path.exists(c["shadow_lease_path"])
    assert [r["phase"] for r in rows(c)] == ["shadow", "decision", "shadow"]


def test_real_lease_is_handed_back_when_mode_is_not_on(tmp_path):
    (tmp_path / "session-heartbeat").write_text("")
    c = cfg(tmp_path, "shadow", QUEUE_BG_IDLE_MIN="10", QUEUE_BG_QUIET_MIN="10")
    c["heartbeat_at"] = 95.0
    C.lease_save(c["lease_path"], {"state": "DRAIN", "since": 1.0, "idle_since": 99.0, "prior_manager_on": True})
    cl = FakeClient(manager_on=False)
    d = T.tick(BUSY, 100.0, c, cl)
    assert d["action"] == "mgr_on" and cl.writes == [("set_manager", {"on": True})]
    assert not os.path.exists(c["lease_path"]) and rows(c)[0]["mode"] == "on"


def test_no_client_or_failed_write_keeps_state(tmp_path):
    c = cfg(tmp_path, "on", QUEUE_BG_MCP_PATH=str(tmp_path / "missing_agent_mcp.py"))
    d = T.tick(BUSY, 100.0, c)
    assert d == {"action": "wait", "model": None, "reason": "control-unavailable: agent_mcp.py not found"}
    assert rows(c)[-1]["reason"] == d["reason"] and not os.path.exists(c["lease_path"])
    cl = FakeClient(manager_on=True, fail_writes=True)
    assert T.tick(BUSY, 130.0, c, cl)["action"] == "mgr_off"
    assert not os.path.exists(c["lease_path"]) and rows(c)[-1]["result"] == "failed"
