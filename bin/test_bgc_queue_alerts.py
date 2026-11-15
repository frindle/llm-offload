"""The queue side of the BloomGauge control hardening (2026-11-02): _control_alert rows, the
BloomGauge-aware Darkbloom hint (never a command), the drift grace, Darkbloom-only wait lanes, and
the janitor rule that closes BLOOMGAUGE CONTROL rows. Hermetic: tmp files, no notifications."""
import calendar
import importlib.util
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oq = _load("oq_bgc_alerts", "ollama-queue.py")
J = _load("eij_bgc_alerts", "escalation_index_janitor.py")


# ---------------------------------------------------------------- _control_alert
def test_control_alert_writes_one_janitor_parseable_row_per_open_condition(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_QUEUE_NO_NOTIFY", "1")
    idx = tmp_path / "ESCALATIONS.md"
    assert oq._control_alert("bloomgauge-control-pick", "failed 3 picks\n  in a row", now=1000.0, idx=idx) is True
    assert oq._control_alert("bloomgauge-control-pick", "failed 3 picks in a row", now=2000.0, idx=idx) is False
    assert oq._control_alert("darkbloom-drift", "failed 3 picks in a row", now=2000.0, idx=idx) is True
    lines = idx.read_text().splitlines()
    assert len(lines) == 2
    m = J.ROW_RE.match(lines[0])
    assert m and (m["plan"], m["who"], m["src"], m["ts"]) == (
        "bloomgauge-control-pick", "BLOOMGAUGE CONTROL", "Q", "1970-01-01T00:16:40Z")
    assert lines[0].endswith(f" - needs {oq.queue_config.operator_name()}: failed 3 picks in a row")
    idx.write_text(idx.read_text().replace("- [ ] `bloomgauge-control-pick`", "- [x] `bloomgauge-control-pick`"))
    assert oq._control_alert("bloomgauge-control-pick", "failed 3 picks in a row", now=3000.0, idx=idx) is True


def test_control_alert_dedupe_holds_across_threads(tmp_path, monkeypatch):
    # The BloomGauge control thread and the main loop can raise the same alert at the same moment: one row.
    import threading
    monkeypatch.setenv("OLLAMA_QUEUE_NO_NOTIFY", "1")
    idx = tmp_path / "ESCALATIONS.md"
    idx.write_text("# escalations\n")
    gate = threading.Barrier(2, timeout=0.5)
    real_read = oq.Path.read_text

    def read_then_wait(self, *a, **kw):
        text = real_read(self, *a, **kw)
        try:
            gate.wait()   # unserialized, both threads have read before either appends
        except threading.BrokenBarrierError:
            pass          # serialized, the other thread cannot read until this one has appended
        return text

    monkeypatch.setattr(oq.Path, "read_text", read_then_wait)
    out = []
    threads = [threading.Thread(target=lambda: out.append(
        oq._control_alert("darkbloom-drift", "Darkbloom does not serve m", now=1000.0, idx=idx))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    monkeypatch.undo()
    rows = [l for l in idx.read_text().splitlines() if l.startswith("- [ ]")]
    assert sorted(out) == [False, True] and len(rows) == 1, (out, rows)


# ---------------------------------------------------------------- hint: never a command
def test_the_darkbloom_hint_names_no_command():
    on = oq.darkbloom_fix_hint({"qwen3.6-35b-a3b-vl-mtp-mxfp8"}, mode="on")
    off = oq.darkbloom_fix_hint({"qwen3.6-35b-a3b-vl-mtp-mxfp8"}, mode="off")
    assert on.startswith("nothing to run: the queue's BloomGauge control pins qwen3.6-35b-a3b-vl-mtp-mxfp8")
    assert off.startswith(f"needs {oq.queue_config.operator_name()}: pick qwen3.6-35b-a3b-vl-mtp-mxfp8 in BloomGauge")
    for h in (on, off, oq.darkbloom_fix_hint(set(), mode="shadow")):
        assert "darkbloom start" not in h and "never run darkbloom" in h.lower()


def test_the_launch_wait_uses_the_hint():
    src = open(os.path.join(HERE, "ollama-queue.py")).read()
    body = src[src.index("def darkbloom_launch_wait"):]
    body = body[:body.index("\ndef ")]
    assert "_dbfix = darkbloom_fix_hint({dbm})" in body and "darkbloom_fix_command" not in body
    assert "Run: darkbloom start" not in src and "darkbloom_fix_command(_miss)" not in src
    worker = open(os.path.join(HERE, "ollama-worker.py")).read()
    assert "darkbloom start --local-endpoint" not in worker


def test_control_mode_is_cached_and_off_on_error(monkeypatch):
    monkeypatch.setitem(oq._BG_MODE, "at", None)
    monkeypatch.setitem(oq._BG_MODE, "mode", "off")
    monkeypatch.setitem(sys.modules, "bloomgauge_control", types.SimpleNamespace(config=lambda: {"mode": "on"}))
    assert oq._bloomgauge_control_mode(1000.0) == "on"
    monkeypatch.setitem(sys.modules, "bloomgauge_control", types.SimpleNamespace(config=lambda: 1 / 0))
    assert oq._bloomgauge_control_mode(1000.0 + oq.BG_MODE_CACHE_S - 1) == "on"          # cached
    assert oq._bloomgauge_control_mode(1000.0 + oq.BG_MODE_CACHE_S) == "off"             # re-read, broke
    assert oq.darkbloom_fix_hint({"m"}).startswith(f"needs {oq.queue_config.operator_name()}: pick m in BloomGauge")   # mode read: off


# ---------------------------------------------------------------- drift grace
def test_with_control_on_a_drift_alerts_only_after_the_grace(monkeypatch):
    monkeypatch.setattr(oq, "_darkbloom_url", lambda: "http://127.0.0.1:8000")
    st = {}
    assert oq.darkbloom_drift_alert(["m"], st, 1000.0, mode="on") is None and st["since"] == 1000.0
    assert oq.darkbloom_drift_alert(["m"], st, 1000.0 + oq.DRIFT_GRACE_S - 1, mode="on") is None
    msg = oq.darkbloom_drift_alert(["m"], st, 1000.0 + oq.DRIFT_GRACE_S, mode="on")
    assert msg.startswith("Darkbloom does not serve m but the next queued run needs it, for over 75 min")
    assert msg == oq.darkbloom_drift_alert(["m"], st, 99999.0, mode="on")       # stable text: deduped
    assert "darkbloom start" not in msg and "Never run darkbloom" in msg


def test_with_control_off_a_missing_model_alerts_at_once_and_a_down_endpoint_after_15_min(monkeypatch):
    monkeypatch.setattr(oq, "_darkbloom_url", lambda: "http://127.0.0.1:8000")
    msg = oq.darkbloom_drift_alert(["m"], {}, 1000.0, mode="off")
    assert msg.startswith("Darkbloom does not serve m but the next queued run needs it: pick it in BloomGauge")
    st = {}
    assert oq.darkbloom_drift_alert(["m"], st, 1000.0, mode="off", down=True) is None
    msg = oq.darkbloom_drift_alert(["m"], st, 1000.0 + oq.DRIFT_DOWN_GRACE_S, mode="off", down=True)
    assert msg.startswith("Darkbloom has not answered at http://127.0.0.1:8000 while the next queued run needs m: start it")


def test_the_daemon_routes_drift_to_the_control_alert_and_marks_state():
    src = open(os.path.join(HERE, "ollama-queue.py")).read()
    i = src.index("_DB_DRIFT[\"at\"] = time.time()")
    blk = src[i:i + 2200]
    assert '_control_alert("darkbloom-drift", _dmsg)' in blk and 'state["_db_drift"] = sorted(_miss)' in blk
    assert 'state.pop("_db_drift", None)' in blk and "_DB_DRIFT.update(msg=None, since=None)" in blk
    assert "_bundle_park_alert(\"DARKBLOOM-DRIFT\"" not in src


# ---------------------------------------------------------------- wait lanes
def test_with_darkbloom_configured_auto_jobs_wait_only_on_the_darkbloom_lane():
    small = {"host_pref": "auto", "model": "qwen2.5:7b"}
    assert oq.wait_job_lanes(small, db_only=False) == ["studio-db", "unraid"]
    assert oq.wait_job_lanes(small, db_only=True) == ["studio-db"]
    assert oq.wait_job_lanes({"host_pref": "auto", "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8"}, db_only=True) == ["studio-db"]
    bonsai = {"host_pref": "auto", "model": oq.BONSAI_MODEL}
    assert oq.wait_job_lanes(bonsai, db_only=True) == oq.wait_job_lanes(bonsai, db_only=False)
    assert oq.wait_job_lanes({"host_pref": "unraid", "model": "x"}, db_only=True) == ["unraid"]


# ---------------------------------------------------------------- janitor
def row(key, ts="2026-11-02T12:00:00Z"):
    return {"plan": key, "who": "BLOOMGAUGE CONTROL", "src": "Q", "ts": ts}


def facts(d, q=None, mode="on", ledger=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "runs").mkdir(exist_ok=True)
    qs = d / "q.json"
    if q is not None:
        qs.write_text(json.dumps(q))
    lp = d / "ledger.jsonl"
    if ledger is not None:
        lp.write_text("".join(json.dumps(r) + "\n" for r in ledger))
    return J.Facts(runs_dir=d / "runs", plans_dir=d / "runs", queue_state=qs, cancelled=lambda p: False,
                   bg_config={"mode": mode, "ledger_path": str(lp)})


ROW_T = float(calendar.timegm((2026, 11, 2, 12, 0, 0)))          # the default row() ts


def res(ts, action, ok):
    return {"ts": ts, "action": action, "mode": "on", "phase": "result", "result": "ok" if ok else "failed"}


def test_janitor_closes_a_drift_row_only_when_the_queue_says_it_ended(tmp_path):
    assert J.decide(row("darkbloom-drift"), facts(tmp_path))[0] is False                      # unreadable
    assert J.decide(row("darkbloom-drift"), facts(tmp_path, {"jobs": [], "_db_drift": ["m"]}))[0] is False
    assert J.decide(row("darkbloom-drift"), facts(tmp_path, {"jobs": []}))[0] is True
    assert J.decide(row("darkbloom-drift"), facts(tmp_path, {"jobs": [], "_db_drift": []}))[0] is True


def test_janitor_closes_a_write_failure_row_after_a_later_success_or_control_off(tmp_path):
    q = {"jobs": []}
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q, mode="off"))[0] is True
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q))[0] is False            # no ledger
    led = [res(ROW_T - 600, "pick", True), res(ROW_T - 30, "pick", False)]
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q, ledger=led))[0] is False
    led.append(res(ROW_T + 3600, "mgr_on", True))                                              # other kind
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q, ledger=led))[0] is False
    assert J.decide(row("bloomgauge-control-manager"), facts(tmp_path, q, ledger=led))[0] is True
    led.append(res(ROW_T + 3700, "pick", True))
    ok, why = J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q, ledger=led))
    assert ok is True and "pick write succeeded" in why
    stale = [res(ROW_T - 900, "pick", True)]                                                   # older than the row
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path, q, ledger=stale))[0] is False


def test_janitor_never_closes_on_unknown_keys_or_unreadable_sources(tmp_path):
    q = {"jobs": []}
    ok_later = [res(ROW_T + 60, "pick", True), res(ROW_T + 60, "mgr_off", True)]
    assert J.decide(row("bloomgauge-control-x"), facts(tmp_path / "a", q, ledger=ok_later))[0] is False
    f = facts(tmp_path / "b", q, ledger=ok_later)
    f._bg = False                                                       # config import failed
    assert J.decide(row("bloomgauge-control-pick"), f) == (False, "BloomGauge control config unreadable")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "ledger.jsonl").mkdir()                           # unreadable ledger
    assert J.decide(row("bloomgauge-control-pick"), facts(tmp_path / "c", q)) == (
        False, "BloomGauge control ledger unreadable")
    assert J.decide(row("bloomgauge-control-pick", ts="bad"), facts(tmp_path / "d", q, ledger=ok_later)) == (
        False, "row timestamp unreadable")
    junk = ok_later + ["not a dict", {"ts": "nan", "action": "pick", "mode": "on", "phase": "result",
                                      "result": "ok"}, dict(res(ROW_T + 999, "pick", True), mode="shadow")]
    lp = tmp_path / "e"
    fe = facts(lp, q)
    (lp / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in junk) + "{bad json\n")
    assert fe.bg_last_write(("pick",))["ts"] == ROW_T + 60              # shadow/nan/junk rows ignored


def test_janitor_closes_rows_in_place_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_QUEUE_NO_NOTIFY", "1")
    idx = tmp_path / "ESCALATIONS.md"
    oq._control_alert("darkbloom-drift", "Darkbloom does not serve m", now=ROW_T, idx=idx)
    oq._control_alert("bloomgauge-control-pick", "failed 3 picks", now=ROW_T, idx=idx)
    f = facts(tmp_path, {"jobs": []}, ledger=[res(ROW_T - 10, "pick", False)])
    closed = J.run([idx], facts=f, apply=True, log_path=tmp_path / "log.jsonl", out=lambda *a: None)
    text = idx.read_text()
    assert "- [x] `darkbloom-drift`" in text and "- [ ] `bloomgauge-control-pick`" in text
    assert len(closed) == 1
