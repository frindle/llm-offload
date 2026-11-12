import bloomgauge_policy as P

NOW = 50000.0
CFG = {"yield_s": 7200, "allow_picks": False}
BUD = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True}


def view(**kw):
    v = {"agent_on": True, "manager_on": True, "can_enable": True, "blocker": None,
         "current": "m1", "serving": ["m1", "m2"], "switching": False}
    v.update(kw)
    return v


def test_acquire_turns_manager_off_or_holds_when_already_off():
    d, lease = P.busy_step(None, view(), {"busy": True, "target": "m1"}, NOW, CFG, BUD)
    assert d == {"action": "mgr_off", "model": "m1", "reason": "acquire: queue work; Manager off"}
    assert lease == {"state": "HELD", "since": NOW, "target": "m1", "prior_manager_on": True, "mgr_off_at": NOW}
    d, lease = P.busy_step(None, view(manager_on=False), {"busy": True, "target": None}, NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "acquire: Manager already off; holding"}
    assert lease == {"state": "HELD", "since": NOW, "target": None, "prior_manager_on": False}


def test_acquire_waits_on_backoff_switch_and_budget():
    recent = dict(BUD, manual_at=NOW - 7000)
    d, lease = P.busy_step(None, view(), {"busy": True}, NOW, CFG, recent)
    assert d == {"action": "wait", "model": None, "reason": "backoff: manual Manager change less than 2h ago"}
    assert lease is None
    d, lease = P.busy_step(None, view(), {"busy": True}, NOW, CFG, dict(BUD, manual_at=NOW - 7300))
    assert d["action"] == "mgr_off" and lease["state"] == "HELD"
    d, lease = P.busy_step(None, view(switching=True), {"busy": True}, NOW, CFG, BUD)
    assert d["reason"] == "acquire: BloomGauge is mid-change" and lease is None
    d, lease = P.busy_step(None, view(), {"busy": True}, NOW, CFG, dict(BUD, mgr_left=0))
    assert d == {"action": "wait", "model": None, "reason": "acquire: Manager budget used this hour"}
    assert lease is None
    # A fresh acquire may spend the hour's last change. Turning the Manager off again under a held lease keeps that change for
    # the hand-back instead (test_bgc_reassert; seen live 2026-11-03 11:11).
    d, lease = P.busy_step(None, view(), {"busy": True}, NOW, CFG, dict(BUD, mgr_left=1))
    assert d["action"] == "mgr_off" and lease["state"] == "HELD"


def test_held_lease_stays_and_drain_is_cancelled():
    held = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    d, lease = P.busy_step(held, view(manager_on=False), {"busy": True, "target": "m2"}, NOW, CFG, BUD)
    assert d == {"action": "none", "model": "m2", "reason": "held"}
    assert lease["target"] == "m2" and lease["since"] == 1.0 and held["target"] == "m1"
    drain = {"state": "DRAIN", "since": 1.0, "idle_since": 40000.0, "target": "m1", "prior_manager_on": True}
    d, lease = P.busy_step(drain, view(manager_on=False), {"busy": True, "target": None}, NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "drain cancelled: new queue work"}
    assert lease["state"] == "HELD" and "idle_since" not in lease and lease["target"] == "m1"
    assert drain["state"] == "DRAIN"


def test_picks_only_when_allowed_manager_off_and_target_not_sole():
    held = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    on = dict(CFG, allow_picks=True)
    d, lease = P.busy_step(held, view(manager_on=False), {"busy": True, "target": "m1"}, NOW, on, BUD)
    assert d == {"action": "pick", "model": "m1", "reason": "hold: target is not the sole served model"}
    assert lease["picked"] == "m1" and lease["picked_at"] == NOW
    sole = view(manager_on=False, current="m1", serving=["m1"])
    d, _ = P.busy_step(held, sole, {"busy": True, "target": "m1"}, NOW, on, BUD)
    assert d["action"] == "none" and d["reason"] == "held"
    d, _ = P.busy_step(held, view(manager_on=True), {"busy": True, "target": "m1"}, NOW, on, BUD)
    assert d["action"] != "pick"          # Manager on: never a pick (it is turned off again: test_bgc_reassert.py)
    d, _ = P.busy_step(held, view(manager_on=False), {"busy": True, "target": "m1"}, NOW, CFG, BUD)
    assert d["action"] == "none"
