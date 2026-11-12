import bloomgauge_policy as P

NOW = 100000.0
CFG = {"picks_per_h": 2, "mgr_per_h": 4, "yield_s": 7200, "idle_s": 900, "quiet_s": 900, "handback": "always", "allow_picks": False}
NOFAIL = {"pick_fails": 0, "mgr_fails": 0, "pick_retry_at": 0.0, "mgr_retry_at": 0.0,   # no failed writes in the ledger,
          "pick_alerted": False, "mgr_alerted": False}                                 # so no failure alert raised
NOLEAK = {"manual_kept_at": None, "own_off_at": None, "leak": False, "turn_on": None}   # no kept manual row, no own Manager-off


def att(action, ts, mode="on", phase="attempt"):
    return {"action": action, "ts": ts, "mode": mode, "phase": phase}


def view(**kw):
    v = {"agent_on": True, "manager_on": True, "can_enable": True, "blocker": None,
         "current": "m1", "serving": ["m1"], "switching": False}
    v.update(kw)
    return v


def test_budget_defaults_and_own_attempts():
    assert P.budget([], [], NOW, CFG) == {"picks_left": 2, "mgr_left": 4, "picks_used": 0, "mgr_used": 0, "manual_at": None,
                                          "manual_log_at": None, **NOLEAK, **NOFAIL, "log_ok": True}
    assert P.budget(None, [], NOW, {}) == {"picks_left": 2, "mgr_left": 4, "picks_used": 0, "mgr_used": 0, "manual_at": None,
                                           "manual_log_at": None, **NOLEAK, **NOFAIL, "log_ok": True}
    ledger = [att("mgr_off", NOW - 100), att("mgr_on", NOW - 90), att("mgr_off", NOW - 80),
              att("mgr_off", NOW - 70, mode="shadow"), att("mgr_on", NOW - 60, phase="result"),
              att("mgr_off", NOW - 4000), att("pick", NOW - 50), "junk"]
    events = [{"at": NOW - 99, "event": "agent.request", "fields": {"what": "manager"}}]
    b = P.budget(ledger, events, NOW, CFG)
    assert b == {"picks_left": 1, "mgr_left": 1, "picks_used": 1, "mgr_used": 3, "manual_at": None,
                 "manual_log_at": None, **NOLEAK, **NOFAIL, "log_ok": True}


def test_budget_other_agents_and_unreadable_log():
    others = [{"at": NOW - 10 * i, "event": "agent.request", "fields": {"what": "manager"}} for i in range(1, 6)]
    others += [{"at": NOW - 500, "event": "agent.request", "fields": {"what": "change-model"}},
               {"at": NOW - 600, "event": "mode.change", "fields": {}}]
    b = P.budget([], others, NOW, CFG)
    assert b == {"picks_left": 1, "mgr_left": 1, "picks_used": 1, "mgr_used": 5, "manual_at": NOW - 600,
                 "manual_log_at": NOW - 600, **NOLEAK, **NOFAIL, "log_ok": True}
    b = P.budget([att("pick", NOW - 30)], None, NOW, dict(CFG, picks_per_h=1, mgr_per_h=6))
    assert b == {"picks_left": 0, "mgr_left": 5, "picks_used": 2, "mgr_used": 1, "manual_at": None,
                 "manual_log_at": None, **NOLEAK, **NOFAIL, "log_ok": False}


def test_decide_waits_when_app_down_or_agent_changes_off():
    lease = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    bud = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True}
    d, kept = P.decide(lease, None, {"busy": True}, NOW, CFG, bud)
    assert d == {"action": "wait", "model": None, "reason": "app-down: BloomGauge not answering"}
    assert kept == lease
    d, kept = P.decide(lease, view(agent_on=False), {"busy": False}, NOW, CFG, bud)
    assert d == {"action": "wait", "model": None, "reason": "control-unavailable: agent changes off"}
    assert kept == lease
    d, kept = P.decide(None, None, {"busy": False}, NOW, CFG, bud)
    assert d["action"] == "wait" and kept is None


def test_decide_yields_to_a_manual_change_after_the_lease():
    lease = {"state": "HELD", "since": NOW - 100, "target": "m1", "prior_manager_on": True}
    d, new = P.decide(lease, view(manager_on=True), {"busy": True}, NOW, CFG,
                      {"mgr_left": 3, "picks_left": 2, "manual_at": NOW - 50})
    assert d == {"action": "yield", "model": None, "reason": "yielded-to-owner: manual Manager change"}
    assert new is None
    # the queue had turned the Manager off and it still reads off: yield, but keep the lease (review #8)
    d, new = P.decide(lease, view(manager_on=False), {"busy": True}, NOW, CFG,
                      {"mgr_left": 3, "picks_left": 2, "manual_at": NOW - 50})
    assert d == {"action": "yield", "model": None, "reason": P.YIELD_KEEP_REASON}
    assert new == dict(lease, yielded_at=NOW - 50)
    d, new = P.decide(lease, view(manager_on=False), {"busy": True, "target": "m1"}, NOW, CFG,
                      {"mgr_left": 3, "picks_left": 2, "manual_at": NOW - 200})
    assert d["action"] == "none" and new["state"] == "HELD"


def test_decide_routes_busy_and_idle():
    bud = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True}
    d, new = P.decide(None, view(), {"busy": True, "target": "m1"}, NOW, CFG, bud)
    assert d["action"] == "mgr_off" and new["state"] == "HELD" and new["mgr_off_at"] == NOW
    d, new = P.decide(new, view(manager_on=False), {"busy": False}, NOW + 30, CFG, bud)
    assert d["reason"] == "drain: queue idle, hand-back timer started" and new["state"] == "DRAIN"
    assert P.decide(None, view(), {"busy": False}, NOW, CFG, bud) == ({"action": "none", "model": None, "reason": "idle"}, None)
