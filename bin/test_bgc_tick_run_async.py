import threading

import bloomgauge_tick as T

NEVER = {"tick_s": -1e18}


def _join():
    th = T._STATE["thread"]
    if th is not None:
        th.join(5)
    return th


def test_runs_fn_on_a_daemon_thread_and_throttles():
    _join()
    seen = []
    base = 4e15
    assert T.run_async({"busy": True}, base, {"tick_s": 30}, fn=lambda w, n, c: seen.append((w, n, c))) is True
    th = _join()
    assert th.daemon is True and th.name == "bloomgauge-control"
    assert seen == [({"busy": True}, base, {"tick_s": 30})]
    assert T.run_async({"busy": False}, base + 10, {"tick_s": 30}, fn=lambda w, n, c: seen.append(n)) is False
    assert T.run_async({"busy": False}, base + 31, {"tick_s": 30}, fn=lambda w, n, c: seen.append(n)) is True
    _join()
    assert seen[1:] == [base + 31]


def test_never_overlaps_a_running_step():
    _join()
    gate, seen = threading.Event(), []

    def slow(w, n, c):
        gate.wait(5)
        seen.append(n)
    assert T.run_async({}, 1.0, NEVER, fn=slow) is True
    assert T.run_async({}, 2.0, NEVER, fn=slow) is False
    gate.set()
    _join()
    assert seen == [1.0]
    assert T.run_async({}, 3.0, NEVER, fn=lambda w, n, c: seen.append(n)) is True
    _join()
    assert seen == [1.0, 3.0]


def test_errors_go_to_stderr_not_the_caller(capsys):
    _join()

    def boom(w, n, c):
        raise ValueError("bad state")
    assert T.run_async({}, 5.0, NEVER, fn=boom) is True
    _join()
    err = capsys.readouterr().err
    assert "bloomgauge-control" in err and "ValueError" in err and "bad state" in err


def test_a_bare_call_is_throttled_too():
    # 2026-11-03 review of job dcbe12803851: with now=None the throttle was skipped, so
    # run_async(work) started a BloomGauge step on every call
    _join()
    T._STATE["last"] = 0.0
    seen = []
    assert T.run_async({}, None, {"tick_s": 3600}, fn=lambda w, n, c: seen.append(n)) is True
    _join()
    assert len(seen) == 1 and isinstance(seen[0], float) and seen[0] > 0
    assert T.run_async({}, None, {"tick_s": 3600}, fn=lambda w, n, c: seen.append(n)) is False
    _join()
    assert len(seen) == 1
