import bloomgauge_control as C
import bloomgauge_policy as P
import bloomgauge_tick as T
from bgc_fake_client import FakeClient


def test_wanted_follows_mode_and_real_lease(tmp_path):
    assert T.wanted(C.config({"QUEUE_BLOOMGAUGE_CONTROL": "shadow"}, tmp_path)) is True
    assert T.wanted(C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)) is True
    off = C.config({}, tmp_path)
    assert T.wanted(off, C) is False
    (tmp_path / "bloomgauge-lease.shadow.json").write_text("{}")
    assert T.wanted(off, C) is False
    (tmp_path / "bloomgauge-lease.json").write_text("{}")
    assert T.wanted(off, C) is True


def test_read_inputs_reads_view_events_and_ledger(tmp_path):
    cfg = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)
    (tmp_path / "bloomgauge-control.jsonl").write_text('{"phase": "attempt", "ts": 1}\nnot json\n\n{"phase": "result"}\n')
    cl = FakeClient(manager_on=True, events=[{"at": 5, "event": "mode.change"}])
    view, events, ledger = T.read_inputs(cl, cfg, None, "on", P)
    assert view["manager_on"] is True and view["agent_on"] is True and view["serving"] == ["m1", "m2"]
    assert events == [{"at": 5, "event": "mode.change"}]
    assert ledger == [{"phase": "attempt", "ts": 1}, {"phase": "result"}]
    assert "/api/activity/log" in cl.reads


def test_shadow_simulates_its_own_manager_off(tmp_path):
    cfg = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "shadow"}, tmp_path)
    lease = {"state": "HELD", "since": 1.0, "mgr_off_at": 1.0}
    view, _e, ledger = T.read_inputs(FakeClient(manager_on=True), cfg, lease, "shadow", P)
    assert view["manager_on"] is False and ledger == []
    view, _e, _l = T.read_inputs(FakeClient(manager_on=True), cfg, lease, "on", P)
    assert view["manager_on"] is True
    view, _e, _l = T.read_inputs(FakeClient(manager_on=True), cfg, {"state": "HELD", "since": 1.0}, "shadow", P)
    assert view["manager_on"] is True


def test_app_down_gives_none(tmp_path):
    cfg = C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)
    view, events, ledger = T.read_inputs(FakeClient(down=True), cfg, None, "on", P)
    assert view is None and events is None and ledger == []
