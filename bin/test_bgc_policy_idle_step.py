import bloomgauge_policy as P

NOW = 50000.0
CFG = {"idle_s": 900, "quiet_s": 900, "heartbeat_at": None, "handback": "always"}
BUD = {"mgr_left": 3, "picks_left": 2, "manual_at": None, "log_ok": True}
ON = {"action": "mgr_on", "model": None, "reason": "handback: queue idle for idle_s; Manager on"}


def view(**kw):
    v = {"agent_on": True, "manager_on": False, "can_enable": True, "blocker": None,
         "current": "m1", "serving": ["m1"], "switching": False}
    v.update(kw)
    return v


def drained(**kw):
    l = {"state": "DRAIN", "since": 1.0, "idle_since": NOW - 1000, "target": "m1", "prior_manager_on": True}
    l.update(kw)
    return l


HB = dict(drained(), state="HANDBACK")   # the lease an answered hand-back keeps


def test_no_lease_is_idle_and_held_starts_the_timer():
    assert P.idle_step(None, view(), NOW, CFG, BUD) == ({"action": "none", "model": None, "reason": "idle"}, None)
    held = {"state": "HELD", "since": 1.0, "target": "m1", "prior_manager_on": True}
    d, lease = P.idle_step(held, view(), NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "drain: queue idle, hand-back timer started"}
    assert lease["state"] == "DRAIN" and lease["idle_since"] == NOW and held["state"] == "HELD"


def test_waits_for_queue_idle_and_ignores_claude_sessions():
    lease_in = drained(idle_since=NOW - 899)
    d, lease = P.idle_step(lease_in, view(), NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "drain: queue idle less than idle_s"}
    assert lease["state"] == "DRAIN" and lease["idle_since"] == NOW - 899
    # a fresh Claude heartbeat must NOT delay the hand-back (queue idle only)
    d, lease = P.idle_step(drained(), view(), NOW, dict(CFG, heartbeat_at=NOW - 1), BUD)
    assert d["action"] == "mgr_on" and lease == HB
    assert P.idle_step(drained(), view(), NOW, dict(CFG, heartbeat_at=NOW - 901), BUD) == (ON, HB)
    assert P.idle_step(drained(), view(), NOW, dict(CFG, idle_s=0, quiet_s=0, heartbeat_at=NOW), BUD) == (ON, HB)


def test_hand_back_turns_manager_on_and_keeps_the_lease_until_it_settles():
    # 2026-11-03: BloomGauge answers a Manager-on at once and may block it afterwards, so the lease stays
    # (HANDBACK) until the tick sees the turn-on settle; the Manager then reads on and the lease goes
    assert P.idle_step(drained(), view(), NOW, CFG, BUD) == (ON, HB)
    d, lease = P.idle_step(drained(prior_manager_on=False), view(), NOW, CFG, BUD)
    assert d["action"] == "mgr_on" and lease == dict(HB, prior_manager_on=False)
    d, lease = P.idle_step(drained(prior_manager_on=False), view(), NOW, dict(CFG, handback="prior"), BUD)
    assert d == {"action": "none", "model": None, "reason": "handback: Manager was off before; released"}
    assert lease is None
    d, lease = P.idle_step(drained(state="HANDBACK"), view(manager_on=True), NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "handback: Manager already on; released"}
    assert lease is None


def test_hand_back_waits_mid_change_blocked_or_out_of_budget():
    d, lease = P.idle_step(drained(), view(switching=True), NOW, CFG, BUD)
    assert d["action"] == "wait" and d["reason"] == "handback: BloomGauge is mid-change"
    assert lease["state"] == "HANDBACK"
    d, lease = P.idle_step(drained(), view(can_enable=False, blocker="wider-list"), NOW, CFG, BUD)
    assert d == {"action": "wait", "model": None, "reason": "handback blocked: wider-list"}
    assert lease["state"] == "HANDBACK"
    d, _ = P.idle_step(drained(), view(can_enable=False), NOW, CFG, BUD)
    assert d["reason"] == "handback blocked: Manager cannot be enabled"
    d, lease = P.idle_step(drained(), view(), NOW, CFG, dict(BUD, mgr_left=0))
    assert d == {"action": "wait", "model": None, "reason": "handback: Manager budget used this hour"}
    assert lease["state"] == "HANDBACK"


def test_hand_back_waits_while_its_turn_on_is_under_way():
    pend = dict(BUD, turn_on={"ts": NOW - 30, "op_id": "op-1"})
    # the operation reads the Manager as on (and mid-change) while it runs: wait, lease kept as HANDBACK
    for v in (view(manager_on=True, switching=True), view(manager_on=True), view()):
        d, lease = P.idle_step(HB, v, NOW, CFG, pend)
        assert d == {"action": "wait", "model": None, "reason": "handback: BloomGauge is turning the Manager on"}
        assert lease == HB
    d, lease = P.idle_step(drained(), view(manager_on=True, switching=True), NOW, CFG, pend)
    assert d["action"] == "wait" and lease["state"] == "HANDBACK"
    # still idle less than idle_s, or handback 'prior' with the Manager off before: those come first
    d, _ = P.idle_step(drained(idle_since=NOW - 10), view(), NOW, CFG, pend)
    assert d["reason"] == "drain: queue idle less than idle_s"
    d, lease = P.idle_step(drained(prior_manager_on=False), view(), NOW, dict(CFG, handback="prior"), pend)
    assert d["action"] == "none" and lease is None
    # settled: the Manager reads on -> released, no write
    d, lease = P.idle_step(HB, view(manager_on=True), NOW, CFG, BUD)
    assert d == {"action": "none", "model": None, "reason": "handback: Manager already on; released"} and lease is None
    # settled as blocked (the tick logged it as a failure): the backoff, then a retry
    d, lease = P.idle_step(HB, view(), NOW, CFG, dict(BUD, mgr_retry_at=NOW + 600))
    assert d["reason"] == "handback: last Manager change failed; backing off" and lease == HB
    assert P.idle_step(HB, view(), NOW + 600, CFG, dict(BUD, mgr_retry_at=NOW + 600)) == (ON, HB)
