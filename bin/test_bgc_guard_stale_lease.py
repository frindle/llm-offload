import json
import os

import bloomgauge_control as C
import bloomgauge_lease_guard as G
from bgc_fake_client import FakeClient

NOW = 10000.0
DEAD = lambda pid: False


def setup(tmp_path, **lease):
    c = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)
    base = {"state": "HELD", "since": 1.0, "heartbeat": NOW - 700, "daemon_pid": 4242, "prior_manager_on": True}
    base.update(lease)
    C.lease_save(c["lease_path"], base)
    return c


def test_no_lease_fresh_heartbeat_or_live_daemon_does_nothing(tmp_path):
    cl = FakeClient(manager_on=False)
    c = C.config({}, tmp_path)
    assert G.guard(NOW, c, cl, alive=DEAD) is None
    c = setup(tmp_path, heartbeat=NOW - 100)
    assert G.guard(NOW, c, cl, alive=DEAD) is None
    c = setup(tmp_path)
    assert G.guard(NOW, c, cl, alive=lambda pid: pid == 4242) is None
    assert cl.reads == [] and cl.writes == [] and os.path.exists(c["lease_path"])


def test_stale_lease_dead_daemon_turns_manager_on_and_clears(tmp_path):
    c, cl = setup(tmp_path), FakeClient(manager_on=False)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and d["cause"] == "deadman"
    assert cl.writes == [("set_manager", {"on": True})] and not os.path.exists(c["lease_path"])
    got = [json.loads(x) for x in open(c["ledger_path"]).read().splitlines()]
    assert [(r["phase"], r["cause"], r["mode"]) for r in got] == [("attempt", "deadman", "on"), ("result", "deadman", "on")]


def test_manager_already_on_just_releases(tmp_path):
    c, cl = setup(tmp_path), FakeClient(manager_on=True)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "none" and cl.writes == []
    assert not os.path.exists(c["lease_path"])
    c2 = setup(tmp_path / "p", prior_manager_on=False)
    d = G.guard(NOW, dict(c2, handback="prior"), FakeClient(manager_on=False), alive=DEAD)
    assert d["action"] == "none" and not os.path.exists(c2["lease_path"])


def test_blocked_or_unreachable_keeps_the_lease(tmp_path):
    c, cl = setup(tmp_path), FakeClient(manager_on=False, can_enable=False, blocker="wider-list")
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and "wider-list" in d["reason"] and d["reason"].startswith("deadman: handback blocked")
    assert cl.writes == [] and os.path.exists(c["lease_path"])
    d = G.guard(NOW, c, FakeClient(down=True), alive=DEAD)
    assert d == {"action": "wait", "model": None, "cause": "deadman", "reason": "deadman: BloomGauge not answering; lease kept"}
    assert os.path.exists(c["lease_path"])


def test_failed_write_or_no_budget_keeps_the_lease(tmp_path):
    c = setup(tmp_path)
    d = G.guard(NOW, c, FakeClient(manager_on=False, fail_writes=True), alive=DEAD)
    assert d["action"] == "mgr_on" and os.path.exists(c["lease_path"])
    busy = [{"at": NOW - i, "event": "agent.request", "fields": {"what": "manager"}} for i in range(1, 7)]
    cl = FakeClient(manager_on=False, events=busy)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and cl.writes == [] and os.path.exists(c["lease_path"])
