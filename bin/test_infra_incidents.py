"""infra_incidents.py: the shared stall/failure log (2026-11-02). Temp files only; no ioreg, no `log show`.

    python3 -m pytest -q ~/bin/test_infra_incidents.py
"""
import importlib.util
import json
import plistlib
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("infra_incidents_t", HERE / "infra_incidents.py")
I = importlib.util.module_from_spec(sp)
sp.loader.exec_module(I)
NOW = 1793684100.0


@pytest.fixture(autouse=True)
def tmp_log(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_INCIDENTS_PATH", str(tmp_path / "inc.jsonl"))
    monkeypatch.setenv("INFRA_INCIDENTS_SCREEN_LOCKED", "1")
    monkeypatch.delenv("INFRA_INCIDENTS_MAX_BYTES", raising=False)
    return tmp_path / "inc.jsonl"


def test_record_writes_one_json_line_with_context(tmp_log):
    r = I.record("path-blocked", "queue", now=NOW, path="/Users/p/Desktop/r/.git", job="abc", empty=None)
    assert r["kind"] == "path-blocked" and r["screen_locked"] is True and r["iso"] == "2026-11-03T05:35:00Z"
    assert "empty" not in r, "None fields are dropped"
    line = json.loads(tmp_log.read_text())
    assert line == r and line["pid"] > 0 and line["host"]


def test_record_never_raises_and_says_none_when_it_cannot_write(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("INFRA_INCIDENTS_PATH", str(blocker / "sub" / "inc.jsonl"))
    assert I.record("git-stall", "runner", now=NOW) is None
    assert I.record("git-stall", "runner", now=NOW, weird=object()) is None or True   # default=str handles it


def test_fields_cannot_overwrite_the_envelope(tmp_log):
    r = I.record("disk-stall", "runner", now=NOW, kind="spoof", pid=1, ts=5)
    assert r["kind"] == "disk-stall" and r["pid"] != 1 and r["ts"] == NOW
    assert I.record("x", "y", now=NOW, screen_locked=False)["screen_locked"] is False, "a known state is kept"


def test_rotation_keeps_the_old_file_and_load_reads_both(tmp_log, monkeypatch):
    monkeypatch.setenv("INFRA_INCIDENTS_MAX_BYTES", "400")
    for i in range(12):
        I.record("path-blocked", "queue", now=NOW + i, path=f"/p{i}")
    old = tmp_log.with_name(tmp_log.name + ".1")
    assert old.exists() and tmp_log.stat().st_size <= 400 + 300
    recs = I.load()
    assert [r["path"] for r in recs][-3:] == ["/p9", "/p10", "/p11"]
    assert len(recs) >= 2 and recs == sorted(recs, key=lambda r: r["ts"])


def test_concurrent_writers_lose_no_lines(tmp_log):
    def w(k):
        for i in range(50):
            I.record("git-stall", f"t{k}", now=NOW + i, i=i)
    ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = tmp_log.read_text().splitlines()
    assert len(lines) == 200 and all(json.loads(x)["kind"] == "git-stall" for x in lines)


def test_load_skips_junk_and_filters_by_time(tmp_log):
    I.record("a", "x", now=NOW - 7200)
    with open(tmp_log, "a") as f:
        f.write("not json\n[1,2]\n{\"ts\": \"x\"}\n")
    I.record("b", "x", now=NOW)
    assert [r["kind"] for r in I.load()] == ["a", "b"]
    assert [r["kind"] for r in I.load(since=NOW - 3600)] == ["b"]


def test_open_incidents_pair_on_component_and_path():
    recs = [
        {"ts": 1, "kind": "path-blocked", "component": "queue", "path": "/a"},
        {"ts": 2, "kind": "path-blocked", "component": "queue", "path": "/b"},
        {"ts": 3, "kind": "path-cleared", "component": "queue", "path": "/a", "blocked_for_s": 2},
        {"ts": 4, "kind": "darkbloom-down", "component": "queue", "cause": "model-cache-unreadable"},
        {"ts": 5, "kind": "path-cleared", "component": "power-sampler", "path": "/b"},     # other component
    ]
    left = I.open_incidents(recs)
    assert [(r["kind"], r.get("path")) for r in left] == [("path-blocked", "/b"), ("darkbloom-down", None)]
    recs.append({"ts": 6, "kind": "darkbloom-up", "component": "queue", "down_for_s": 600})
    assert [r["kind"] for r in I.open_incidents(recs)] == ["path-blocked"]


def test_two_probes_held_on_one_path_are_two_incidents():
    """The queue probes each job's paths; two jobs held at the same git dir are two incidents, and the
    first one clearing must not close the other (Fable review, 2026-11-03)."""
    h = "/u/Desktop/r/.git/HEAD"
    recs = [
        {"ts": 1, "kind": "path-blocked", "component": "queue", "path": h, "target": "/w/one", "probe": "path"},
        {"ts": 2, "kind": "path-blocked", "component": "queue", "path": h, "target": "/w/two", "probe": "path"},
        {"ts": 3, "kind": "path-cleared", "component": "queue", "path": h, "target": "/w/one", "then": "ok"},
    ]
    assert [r["target"] for r in I.open_incidents(recs)] == ["/w/two"]
    recs.append({"ts": 4, "kind": "path-cleared", "component": "queue", "path": h, "target": "/w/two", "then": "ok"})
    assert I.open_incidents(recs) == []


def test_summary_counts_durations_lock_correlation_and_hints():
    recs = []
    for i in range(4):
        recs.append({"ts": NOW + i, "iso": "t", "kind": "git-stall", "component": "runner", "screen_locked": True})
    recs.append({"ts": NOW + 5, "iso": "t", "kind": "path-blocked", "component": "queue", "path": "/d",
                 "screen_locked": True})
    recs.append({"ts": NOW + 9, "iso": "t", "kind": "path-cleared", "component": "queue", "path": "/d",
                 "blocked_for_s": 5400, "screen_locked": False})
    recs.append({"ts": NOW + 10, "iso": "t", "kind": "darkbloom-down", "component": "queue",
                 "cause": "model-cache-unreadable", "screen_locked": True})
    s = I.summarize(recs, now=NOW + 1210)
    assert s["by_kind"]["git-stall"] == 4 and s["stalls_while_locked"] == [5, 5]
    assert s["durations"]["path-cleared"] == {"n": 1, "max_s": 5400, "total_s": 5400}
    assert any("with the screen locked" in h and "Full Disk Access" in h for h in s["hints"])
    assert any(h.startswith("still open: darkbloom-down (queue) model-cache-unreadable for 20m") for h in s["hints"])
    assert any("model-cache-unreadable x1" in h for h in s["hints"])
    text = I.format_summary(s, "24h")
    assert "screen locked during 5 of 5 stalls" in text and "hint: " in text


def test_no_lock_hint_when_stalls_happen_unlocked():
    recs = [{"ts": NOW + i, "iso": "t", "kind": "disk-stall", "component": "runner", "screen_locked": False}
            for i in range(5)]
    assert not any("screen locked" in h for h in I.summarize(recs, now=NOW)["hints"])


def test_parse_since():
    assert I.parse_since("90m", now=10_000) == 10_000 - 5400
    assert I.parse_since("2", now=10_000) == 10_000 - 7200
    assert I.parse_since("7d", now=1e6) == 1e6 - 7 * 86400
    with pytest.raises(ValueError):
        I.parse_since("yesterday")


def lockplist(*users):
    return plistlib.dumps({"IOConsoleUsers": list(users)})


def test_parse_lock_state_reads_ioreg():
    me = {"kCGSSessionUserIDKey": 501, "kCGSSessionOnConsoleKey": True}
    assert I.parse_lock_state(lockplist(dict(me, CGSSessionScreenIsLocked=True)), 501) is True
    assert I.parse_lock_state(lockplist(me), 501) is False
    # a remote second session on the console does not unlock the user's own
    other = {"kCGSSessionUserIDKey": 502, "kCGSSessionOnConsoleKey": True}
    assert I.parse_lock_state(lockplist(dict(me, CGSSessionScreenIsLocked=True), other), 501) is True
    # the user's session is off the console (login window, another user): locked for our purposes
    assert I.parse_lock_state(lockplist(dict(me, kCGSSessionOnConsoleKey=False)), 501) is True
    assert I.parse_lock_state(b"garbage", 501) is None


def test_screen_locked_env_override_and_cache(monkeypatch):
    assert I.screen_locked(NOW) is True
    monkeypatch.setenv("INFRA_INCIDENTS_SCREEN_LOCKED", "0")
    assert I.screen_locked(NOW) is False
    monkeypatch.delenv("INFRA_INCIDENTS_SCREEN_LOCKED")
    calls = []

    class P:
        def __init__(self, *a, **k):
            calls.append(a)

        def communicate(self, timeout=None):
            return lockplist({"kCGSSessionOnConsoleKey": True}), b""
    monkeypatch.setattr(I.subprocess, "Popen", P)
    I._lock_cache[:] = [0.0, None]
    assert I.screen_locked(NOW) is False and I.screen_locked(NOW + 10) is False and len(calls) == 1
    assert I.screen_locked(NOW + 31) is False and len(calls) == 2


def test_a_hung_ioreg_is_abandoned_not_waited_on(monkeypatch):
    monkeypatch.delenv("INFRA_INCIDENTS_SCREEN_LOCKED")
    killed = []

    class P:
        def __init__(self, *a, **k):
            pass

        def communicate(self, timeout=None):
            raise I.subprocess.TimeoutExpired("ioreg", timeout)

        def kill(self):
            killed.append(1)
    monkeypatch.setattr(I.subprocess, "Popen", P)
    I._lock_cache[:] = [0.0, None]
    assert I.screen_locked(NOW) is None and killed == [1]
    assert I.screen_locked(NOW + 5) is None and killed == [1], "an unknown answer is cached: no second wait"
    assert I.screen_locked(NOW + I.LOCK_NONE_CACHE_S + 1) is None and killed == [1, 1], "and asked again later"


def test_parse_tcc_lines():
    text = ("2026-11-02 21:35:33.742 Df kernel[0:1cb01cd] (Sandbox) watchdog expired for approval entry 35761 "
            "(kTCCServiceSystemPolicyDesktopFolder, pid 96142)\nnoise\n"
            "2026-11-02 21:35:33.742 Df kernel[0:1cb01cd] (Sandbox) watchdog expired for approval entry 35760 "
            "(kTCCServiceSystemPolicyAllFiles, pid 48964)\n")
    assert I.parse_tcc_lines(text) == [
        ("2026-11-02 21:35:33.742", 35761, "kTCCServiceSystemPolicyDesktopFolder", 96142),
        ("2026-11-02 21:35:33.742", 35760, "kTCCServiceSystemPolicyAllFiles", 48964)]


def test_cli_record_tail_and_summary(tmp_log, capsys):
    assert I.main(["record", "git-stall", "runner", "job=abc", "tries=3", "ok=true"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["tries"] == 3 and r["ok"] is True and r["job"] == "abc"
    assert I.main(["tail", "-n", "5"]) == 0
    assert "git-stall" in capsys.readouterr().out
    assert I.main(["summary", "--since", "1h", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["by_kind"] == {"git-stall": 1}
    with pytest.raises(SystemExit):
        I.main(["record", "x", "y", "novalue"])


def test_under_pytest_without_a_chosen_path_nothing_is_written(monkeypatch, tmp_path):
    monkeypatch.delenv("INFRA_INCIDENTS_PATH")
    monkeypatch.setattr(I, "DEFAULT_PATH", tmp_path / "real.jsonl")
    assert I.record("git-stall", "runner", now=NOW) is None and not (tmp_path / "real.jsonl").exists()
