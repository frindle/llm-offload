import bloomgauge_policy as P

NOW = 100000.0
HOUR_AGO = NOW - 3600


def ev(at, event, what=None):
    return {"at": at, "event": event, "kind": "manager", "fields": ({"what": what} if what else {})}


def test_unreadable_log_counts_one_of_each():
    assert P._other_usage(None, [], HOUR_AGO) == (1, 1, None)
    assert P._other_usage(None, [{"action": "mgr_off", "ts": NOW}], HOUR_AGO) == (1, 1, None)


def test_own_attempts_are_matched_and_others_counted():
    att = [{"action": "mgr_off", "ts": NOW - 100, "phase": "attempt", "mode": "on"},
           {"action": "pick", "ts": NOW - 50, "phase": "attempt", "mode": "on"}]
    events = [ev(NOW - 97, "agent.request", "manager"),      # ours (3 s)
              ev(NOW - 48, "agent.request", "change-model"),  # ours (2 s)
              ev(NOW - 500, "agent.request", "change-model"),
              ev(NOW - 400, "agent.request", "restart"),
              ev(NOW - 300, "agent.request", "follow"),
              ev(NOW - 200, "manager.decide"), "junk", None]
    assert P._other_usage(events, att, HOUR_AGO) == (2, 1, None)


def test_one_attempt_matches_one_request_and_old_rows_do_not_count():
    att = [{"action": "mgr_on", "ts": NOW - 100}]
    events = [ev(NOW - 99, "agent.request", "manager"), ev(NOW - 98, "agent.request", "manager"),
              ev(NOW - 90, "agent.request", "manager"),          # 10 s away: not ours
              ev(HOUR_AGO - 10, "agent.request", "manager"), ev(HOUR_AGO - 5, "agent.request", "change-model")]
    assert P._other_usage(events, att, HOUR_AGO) == (0, 2, None)


def test_manual_mode_change_is_the_newest_without_an_own_attempt():
    att = [{"action": "mgr_off", "ts": NOW - 1000}]
    events = [ev(NOW - 990, "mode.change"),            # 10 s after our attempt: ours
              ev(NOW - 5000, "mode.change"),           # older than an hour still counts
              ev(NOW - 300, "mode.change"), ev(NOW - 600, "mode.change")]
    assert P._other_usage(events, att, HOUR_AGO) == (0, 0, NOW - 300)
    assert P._other_usage([ev(NOW - 5000, "mode.change")], [], HOUR_AGO) == (0, 0, NOW - 5000)
    assert P._other_usage([ev(NOW - 990, "mode.change")], att, HOUR_AGO)[2] is None
