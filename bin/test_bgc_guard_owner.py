"""The stale-lease guard decides like the daemon (2026-11-02, from the simulated run test_bgc_sim.py).

The guard used to hand back on its own, narrower rules: no look at a manual change made after the lease
was taken, no failure backoff, and a budget that did not count the queue's own writes. It now reads
what a tick reads (tick.read_inputs: the view, 500 activity-log rows and the control ledger), keeps a
manual change in the ledger and decides with policy.decide on the lease with its hand-back due.
"""
import json
import os

import bloomgauge_control as C
import bloomgauge_lease_guard as G
import bloomgauge_policy as P
from bgc_fake_client import FakeClient

NOW = 100000.0
DEAD = lambda pid: False
FAIL_500 = "Unavailable: BloomGauge answered 500 for /api/optimizer/control."


def setup(tmp_path, ledger=(), **kw):
    c = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)
    lease = {"state": "HELD", "since": NOW - 3000, "target": "m1", "prior_manager_on": True,
             "mgr_off_at": NOW - 3000, "heartbeat": NOW - 700, "daemon_pid": 4242}
    lease.update(kw)
    C.lease_save(c["lease_path"], lease)
    if ledger:
        with open(c["ledger_path"], "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in ledger))
    C._LAST_LOGGED.clear()
    return c


def rows(c):
    try:
        return [json.loads(x) for x in open(c["ledger_path"]).read().splitlines()]
    except FileNotFoundError:
        return []


def lease(c):
    try:
        return json.load(open(c["lease_path"]))
    except FileNotFoundError:
        return None


def owner_pick(at):
    return {"at": at, "event": "control.request",
            "fields": {"what": "manual", "model": "m2", "why": "user", "result": "ok", "from": "mac"}}


def owner_on(at):
    return [{"at": at, "event": "control.request",
             "fields": {"what": "manager", "on": "yes", "why": "user", "result": "ok", "from": "mac"}},
            {"at": at + 4, "event": "mode.change", "fields": {"from": "off", "to": "on", "why": "user-on"}}]


def own(ts, action, result="ok", err=None, cause=None):
    a = {"ts": ts, "phase": "attempt", "action": action, "model": None, "mode": "on", "reason": "x"}
    if cause:
        a["cause"] = cause
    return [a, dict(a, phase="result", result=result, err=err)]


class LogDown(FakeClient):
    def get(self, path, params=None):
        if path == "/api/activity/log":
            self.reads.append(path)
            raise self.Unavailable("BloomGauge answered 500 for /api/activity/log.")
        return super().get(path, params)


class AgentOff(FakeClient):
    def get(self, path, params=None):
        if path == "/api/agent-access":
            self.reads.append(path)
            return {"enabled": False, "keyPath": None}
        return super().get(path, params)


def test_manual_pick_while_held_keeps_the_lease_then_hands_back_after_yield_s(tmp_path):
    c = setup(tmp_path)
    cl = FakeClient(manager_on=False, events=[owner_pick(NOW - 1000)])
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d == {"action": "yield", "model": None, "cause": "deadman", "reason": "deadman: " + P.YIELD_KEEP_REASON}
    assert cl.writes == []
    held = lease(c)
    assert held["yielded_at"] == NOW - 1000 and held["heartbeat"] == NOW - 700 and held["daemon_pid"] == 4242
    assert [r["manual_at"] for r in rows(c) if r["phase"] == "manual"] == [NOW - 1000]
    n = len(cl.reads)
    d = G.guard(NOW + 300, c, cl, alive=DEAD)
    assert d["action"] == "wait" and d["reason"] == "deadman: yielded to a manual change less than yield_s ago; lease kept"
    assert len(cl.reads) == n and cl.writes == [] and lease(c) is not None
    d = G.guard(NOW - 1000 + 7201, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and cl.writes == [("set_manager", {"on": True})] and lease(c) is None


def test_owner_turned_the_manager_on_releases_with_no_write(tmp_path):
    c = setup(tmp_path)
    cl = FakeClient(manager_on=True, events=owner_on(NOW - 500))
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "yield" and d["reason"] == "deadman: " + P.YIELD_REASON
    assert cl.writes == [] and lease(c) is None


def test_manual_change_before_the_lease_does_not_stop_the_hand_back(tmp_path):
    c = setup(tmp_path)
    cl = FakeClient(manager_on=False, events=[owner_pick(NOW - 4000)])
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and cl.writes == [("set_manager", {"on": True})] and lease(c) is None


def test_failure_backoff_from_the_ledger(tmp_path):
    c = setup(tmp_path, ledger=own(NOW - 60, "mgr_on", "failed", FAIL_500, cause="deadman"))
    cl = FakeClient(manager_on=False)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and d["reason"] == "deadman: handback: last Manager change failed; backing off"
    assert cl.writes == [] and lease(c) is not None
    d = G.guard(NOW + 61, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and lease(c) is None


def test_own_manager_changes_count_against_the_hourly_cap(tmp_path):
    led = own(NOW - 3500, "mgr_off") + own(NOW - 3000, "mgr_on") + own(NOW - 2500, "mgr_off") + own(NOW - 2000, "mgr_on")
    c = setup(tmp_path, ledger=led)
    cl = FakeClient(manager_on=False)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and d["reason"] == "deadman: handback: Manager budget used this hour"
    assert cl.writes == []
    d = G.guard(NOW + 1600, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and lease(c) is None


def test_unreadable_activity_log_or_agent_changes_off_wait_with_the_lease_kept(tmp_path):
    c = setup(tmp_path)
    cl = LogDown(manager_on=False)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and d["reason"] == "deadman: handback: activity log unreadable"
    assert cl.writes == [] and lease(c) is not None
    cl = AgentOff(manager_on=False)
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "wait" and d["reason"] == "deadman: control-unavailable: agent changes off"
    assert cl.writes == [] and lease(c) is not None


def test_corrupt_lease_waits_out_lease_ttl_s_from_its_mtime(tmp_path):
    c = setup(tmp_path)
    with open(c["lease_path"], "w") as f:
        f.write("{not json")
    os.utime(c["lease_path"], (NOW - 100, NOW - 100))
    cl = FakeClient(manager_on=False)
    assert G.guard(NOW, c, cl, alive=DEAD) is None and cl.reads == []
    os.utime(c["lease_path"], (NOW - 700, NOW - 700))
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and lease(c) is None


def test_corrupt_lease_still_yields_to_a_manual_change_in_the_last_yield_s(tmp_path):
    # the file's mtime (its since) is later than the owner's pick it had yielded to (simulated run, deadhold)
    c = setup(tmp_path)
    with open(c["lease_path"], "w") as f:
        f.write("{not json")
    os.utime(c["lease_path"], (NOW - 700, NOW - 700))
    cl = FakeClient(manager_on=False, events=[owner_pick(NOW - 1500)])
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "yield" and d["reason"] == "deadman: " + P.YIELD_KEEP_REASON and cl.writes == []
    assert lease(c)["yielded_at"] == NOW - 1500
    d = G.guard(NOW + 300, c, cl, alive=DEAD)
    assert d["action"] == "wait" and cl.writes == []
    d = G.guard(NOW - 1500 + 7201, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and lease(c) is None


def test_corrupt_lease_hands_back_over_a_manual_change_older_than_yield_s(tmp_path):
    c = setup(tmp_path)
    with open(c["lease_path"], "w") as f:
        f.write("{not json")
    os.utime(c["lease_path"], (NOW - 700, NOW - 700))
    cl = FakeClient(manager_on=False, events=[owner_pick(NOW - 7300)])
    d = G.guard(NOW, c, cl, alive=DEAD)
    assert d["action"] == "mgr_on" and lease(c) is None


def test_the_guards_failure_that_makes_the_streak_raises_its_one_alert(tmp_path):
    led = own(NOW - 900, "mgr_on", "failed", FAIL_500) + own(NOW - 600, "mgr_on", "failed", FAIL_500)
    c = setup(tmp_path, ledger=led)
    cl = FakeClient(manager_on=False, fail_writes=True)
    got = []
    d = G.guard(NOW, c, cl, alive=DEAD, alert=lambda k, m: got.append((k, m)))
    assert d["action"] == "mgr_on" and [k for k, _m in got] == ["bloomgauge-control-manager"]
    assert "failed 3 Manager changes in a row" in got[0][1]
    assert [(r["kind"], r["streak"], r.get("cause")) for r in rows(c) if r["phase"] == "alert"] == [("manager", 3, "deadman")]
    assert lease(c) is not None
    d = G.guard(NOW + 1000, c, cl, alive=DEAD, alert=lambda k, m: got.append((k, m)))
    assert d["action"] == "mgr_on" and len(got) == 1     # same streak: no second alert


def test_a_streak_the_guard_alerted_is_not_alerted_again_by_the_daemon(tmp_path):
    import bloomgauge_tick as T
    led = own(NOW - 900, "mgr_on", "failed", FAIL_500) + own(NOW - 600, "mgr_on", "failed", FAIL_500)
    c = setup(tmp_path, ledger=led)
    cl = FakeClient(manager_on=False, fail_writes=True)
    G.guard(NOW, c, cl, alive=DEAD, alert=lambda k, m: None)
    T._STATE.update(alert=None, idle_since=None, next_poll=0.0)
    T._STATE.pop("deliver", None)
    T.tick({"busy": False, "why": "t", "target": None}, NOW + 1000, dict(c, idle_s=0), cl)
    assert cl.writes[-1] == ("set_manager", {"on": True}) and T._STATE["alert"] is None


def test_the_tick_delivers_through_the_queues_hook_when_it_is_set():
    import bloomgauge_tick as T
    got = []
    T._STATE.update(alert=None, deliver=lambda k, m: got.append(k))
    try:
        a = T.note_failure({"action": "pick", "model": "m1"}, {"pick_fails": 2}, {"err": "x"}, P)
    finally:
        T._STATE.pop("deliver", None)
    assert got == ["bloomgauge-control-pick"] and a["key"] == got[0] and T._STATE["alert"] is None
