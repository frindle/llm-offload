import bloomgauge_policy as P

CFG = {"idle_s": 900}
BUD = {"mgr_left": 4, "log_ok": True, "leak": True}   # the queue left the Manager off itself


def view(**kw):
    v = {"agent_on": True, "manager_on": False, "can_enable": True, "blocker": None,
         "current": "m1", "serving": ["m1"], "switching": False}
    v.update(kw)
    return v


def test_manager_off_after_idle_turns_on():
    d = P.ownerless_step(view(), 900.0, CFG, BUD)
    assert d["action"] == "mgr_on"
    assert d["reason"] == "ownerless idle: manager off, queue idle >= idle_s -> manager on"


def test_not_idle_long_enough_or_manager_already_on_does_nothing():
    assert P.ownerless_step(view(), 899.0, CFG, BUD)["action"] == "none"
    assert P.ownerless_step(view(manager_on=True), 5000.0, CFG, BUD)["action"] == "none"


def test_unreadable_or_agent_off_does_nothing():
    assert P.ownerless_step(None, 5000.0, CFG, BUD)["action"] == "none"
    assert P.ownerless_step(view(agent_on=False), 5000.0, CFG, BUD)["action"] == "none"


def test_switching_blocker_and_budget_wait():
    assert P.ownerless_step(view(switching=True), 5000.0, CFG, BUD)["action"] == "wait"
    d = P.ownerless_step(view(can_enable=False, blocker="NO_MODEL"), 5000.0, CFG, BUD)
    assert d["action"] == "wait" and "NO_MODEL" in d["reason"]
    assert P.ownerless_step(view(), 5000.0, CFG, {"mgr_left": 0, "log_ok": True, "leak": True})["action"] == "wait"
