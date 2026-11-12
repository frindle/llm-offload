"""Attribution of activity-log rows to the queue's own writes (2026-11-02, from the simulated run).

A Manager change is the queue's own only when the request row that caused it is matched to an own
attempt whose outcome agrees with that row's result; a failed or refused own write never takes another
agent's (or the owner's) successful change for its own.
"""
import bloomgauge_policy as P

T = 100000.0
HOUR_AGO = T - 3000
FAIL_500 = "Unavailable: BloomGauge answered 500 for /api/optimizer/control."
RATE_429 = ("Unavailable: Agents can switch models or restart Darkbloom at most 2 times an hour, and change the "
            "Manager at most 6 times. Try again in about 5 min.")
NO_ANSWER = "Unavailable: BloomGauge is not answering on this Mac. Open the BloomGauge app and try again."


def agent_req(at, result, what="manager"):
    return {"at": at, "event": "agent.request", "fields": {"what": what, "model": None, "result": result}}


def ctl_req(at, result, on="no"):
    return {"at": at, "event": "control.request",
            "fields": {"what": "manager", "on": on, "why": "user", "result": result, "from": "mac"}}


def flip(at, to="off", why=None):
    return {"at": at, "event": "mode.change", "fields": {"from": "on" if to == "off" else "off", "to": to,
                                                        "why": why or ("user-" + to)}}


def own(ts, action, result="ok", err=None):
    a = {"ts": ts, "phase": "attempt", "action": action, "mode": "on"}
    return [a, dict(a, phase="result", result=result, err=err)]


def usage(ledger, events):
    att = [r for r in ledger if r["phase"] == "attempt"]
    return P._other_usage(events, P._outcomes(ledger, att), HOUR_AGO)


def test_failed_own_write_does_not_take_another_agents_change():
    led = own(T, "mgr_off", "failed", FAIL_500)
    events = [agent_req(T + 3, "failed"), ctl_req(T + 3, "failed"),     # the queue's failed write
              agent_req(T + 20, "ok"), ctl_req(T + 20, "ok"), flip(T + 25)]  # another agent's
    assert usage(led, events) == (0, 1, T + 25)


def test_refused_own_write_does_not_take_the_owners_click():
    led = own(T, "mgr_off", "failed", RATE_429)
    events = [agent_req(T + 2, "refused"), ctl_req(T + 30, "ok"), flip(T + 33)]
    assert usage(led, events) == (0, 0, T + 33)


def test_own_successful_change_is_not_manual():
    led = own(T, "mgr_off")
    events = [agent_req(T + 3, "ok"), ctl_req(T + 3, "ok"), flip(T + 8)]
    assert usage(led, events) == (0, 0, None)


def test_failed_but_effective_own_change_with_no_other_request_is_own():
    led = own(T, "mgr_off", "failed", FAIL_500)
    events = [agent_req(T + 3, "failed"), ctl_req(T + 3, "failed"), flip(T + 9)]
    assert usage(led, events) == (0, 0, None)


def test_unanswered_own_write_takes_the_change_it_made():
    led = own(T, "mgr_off", "failed", NO_ANSWER)
    events = [agent_req(T + 20, "ok"), ctl_req(T + 20, "ok"), flip(T + 24)]
    assert usage(led, events) == (0, 0, None)


def test_unsent_own_pick_never_takes_a_pick():
    led = own(T, "pick", "failed", "ValueError: m9 is not a model this Mac can serve. See get_manager.")
    assert usage(led, [flip(T + 5, why="pick")]) == (0, 0, T + 5)
    owner = {"at": T + 2, "event": "control.request",
             "fields": {"what": "manual", "model": "m2", "why": "user", "result": "ok", "from": "mac"}}
    assert usage(led, [owner, flip(T + 5, why="pick")]) == (0, 0, T + 5)


def test_app_changes_are_never_manual():
    assert usage([], [flip(T, "on", "recovery"), flip(T + 100, "off", "blocked")]) == (0, 0, None)


def test_outcomes_from_the_result_rows():
    led = (own(T, "mgr_off") + own(T + 1, "mgr_on", "failed", FAIL_500)
           + own(T + 2, "pick", "failed", "ValueError: on must be true or false.")
           + own(T + 3, "mgr_off", "failed", NO_ANSWER)
           + [{"ts": T + 4, "phase": "attempt", "action": "mgr_on", "mode": "on"}])
    att = [r for r in led if r["phase"] == "attempt"]
    assert [a["outcome"] for a in P._outcomes(led, att)] == ["ok", "failed", "none", "unknown", "unknown"]
