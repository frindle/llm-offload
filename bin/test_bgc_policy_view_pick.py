import bloomgauge_policy as P

MC = {"currentModel": "m1", "servingModels": ["m1", "m2", 3], "providerControl": {"status": "running"}}
OC = {"automatic": {"mode": "manual", "canEnable": False, "blocker": {"code": "wider-list"}},
      "operation": {"status": "active"}}
ACC = {"enabled": True, "keyPath": "/k/agent.key"}


def test_read_view_flattens_manager_off_and_blocked():
    v = P.read_view(MC, OC, ACC)
    assert v == {"agent_on": True, "manager_on": False, "can_enable": False, "blocker": "wider-list",
                 "current": "m1", "serving": ["m1", "m2"], "switching": False,
                 "op_id": None, "op_status": "active", "op_detail": ""}
    on = P.read_view(MC, {"automatic": {"mode": "auto", "canEnable": True}, "operation": {"status": "active"}}, ACC)
    assert on["manager_on"] is True and on["can_enable"] is True and on["blocker"] is None


def test_read_view_switching_agent_and_bad_inputs():
    starting = P.read_view(dict(MC, providerControl={"status": "starting"}), OC, {"enabled": True})
    assert starting["switching"] is True
    assert starting["agent_on"] is False
    assert P.read_view({"switchingBy": "agent"}, {}, ACC)["switching"] is True
    assert P.read_view({"providerControl": {"operationPending": True}}, {}, ACC)["switching"] is True
    assert P.read_view({}, {"operation": {"status": "pending"}}, ACC)["switching"] is True
    empty = P.read_view(None, "x", [])
    assert empty["switching"] is False and empty["manager_on"] is False and empty["serving"] == []
    assert empty["agent_on"] is False and empty["current"] is None
    assert P.read_view({}, {"currentModel": "m9"}, ACC)["current"] == "m9"


def test_pick_step_picks_without_mutating_the_lease():
    lease = {"state": "HELD", "since": 1.0, "target": "m2"}
    d, new = P._pick_step(lease, {"switching": False}, "m2", 5000.0, {"picks_left": 1, "log_ok": True})
    assert d == {"action": "pick", "model": "m2", "reason": "hold: target is not the sole served model"}
    assert new["picked"] == "m2" and new["picked_at"] == 5000.0
    assert new["since"] == 1.0 and new["state"] == "HELD"
    assert "picked" not in lease


def test_pick_step_waits_once_per_hour_mid_change_and_on_budget():
    lease = {"state": "HELD", "picked": "m2", "picked_at": 4000.0}
    d, new = P._pick_step(lease, {"switching": False}, "m2", 5000.0, {"picks_left": 2, "log_ok": True})
    assert d == {"action": "wait", "model": "m2", "reason": "pick: already sent for this model this hour"}
    assert new == lease
    d, _ = P._pick_step(lease, {"switching": False}, "m2", 4000.0 + 3601, {"picks_left": 2, "log_ok": True})
    assert d["action"] == "pick"
    d, _ = P._pick_step(lease, {"switching": False}, "m3", 5000.0, {"picks_left": 2, "log_ok": True})
    assert d["action"] == "pick" and d["model"] == "m3"
    d, new = P._pick_step({"state": "HELD"}, {"switching": True}, "m2", 1.0, {"picks_left": 2, "log_ok": True})
    assert d["reason"] == "pick: BloomGauge is mid-change" and d["action"] == "wait" and new == {"state": "HELD"}
    d, _ = P._pick_step({"state": "HELD"}, {"switching": False}, "m2", 1.0, {"picks_left": 0, "log_ok": True})
    assert d == {"action": "wait", "model": "m2", "reason": "pick: pick budget used this hour"}


def test_read_view_operation_settled_or_running():
    """2026-11-03: a blocked or cancelled Manager-on keeps its status until the next operation; it is not
    a change in progress (it read as mid-change for 12 h). Running or unknown statuses are."""
    for st in ("active", "blocked", "cancelled"):
        v = P.read_view(MC, {"automatic": {"mode": "manual"}, "operation": {"id": "op-1", "status": st,
                                                                            "detail": "Could not verify\n  this."}}, ACC)
        assert v["switching"] is False and v["op_id"] == "op-1" and v["op_status"] == st, st
        assert v["op_detail"] == "Could not verify this."
    for st in ("pending", "starting", "waiting", "something-new"):
        v = P.read_view(MC, {"automatic": {"mode": "on"}, "operation": {"id": "op-2", "status": st}}, ACC)
        assert v["switching"] is True and v["op_status"] == st and v["manager_on"] is True, st
    # no operation, or one without a status/id: not switching, no op fields
    for oc in ({}, {"operation": {}}, {"operation": {"id": "", "status": None}}, {"operation": "x"}):
        v = P.read_view(MC, oc, ACC)
        assert v["switching"] is False and v["op_id"] is None and v["op_status"] is None and v["op_detail"] == "", oc
    assert P.read_view(MC, {"operation": {"id": 5, "status": 7, "detail": "d" * 300}}, ACC)["op_detail"] == "d" * 200
