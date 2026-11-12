"""Private doc-summary jobs (label docsum-*) must never reach the escalation watcher's claude -p review spawner."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("dew_docsum_t", HERE / "dispatch-escalation-watcher.py")
W = importlib.util.module_from_spec(sp)
sp.loader.exec_module(W)


def test_needs_opus_and_stuck_docsum_rows_are_skipped():
    txt = ("[needs_opus] aaaaaaaaaaaa docsum-1a2b3c4d  escalated=[x] something\n"
           "[needs_opus] bbbbbbbbbbbb ordinary-job-fix  escalated=[x] something\n")
    found = W.detect_stuck_jobs(status_text=txt)
    assert [e["job_id"] for e in found] == ["bbbbbbbbbbbb"]


def test_long_running_docsum_row_is_not_stuck(monkeypatch):
    monkeypatch.setattr(W, "_job_started_epoch", lambda jid: 1.0)
    txt = "[running ] aaaaaaaaaaaa docsum-1a2b3c4d\n[running ] bbbbbbbbbbbb other-job-x\n"
    found = W.detect_stuck_jobs(now=10 ** 9, status_text=txt)
    assert [e["job_id"] for e in found] == ["bbbbbbbbbbbb"]


def test_needs_info_docsum_row_is_skipped():
    base = {"status": "paused", "pause_reason": "needs_info", "pause_meta": {"question": "q", "ask_n": 1}}
    jobs = [dict(base, id="aaaaaaaaaaaa", label="docsum-1a2b3c4d"), dict(base, id="bbbbbbbbbbbb", label="x-fix")]
    assert [e["job_id"] for e in W.detect_needs_info_jobs(jobs)] == ["bbbbbbbbbbbb"]


def test_final_filter_drops_any_docsum_finding():
    found = [{"label": "docsum-1a2b3c4d", "job_id": "a"}, {"label": "x", "job_id": "b"}, {"job_id": "c"}]
    assert [e["job_id"] for e in W.drop_private_jobs(found)] == ["b", "c"]
    assert W.is_private_label("docsum-deadbeef") and not W.is_private_label("my-docsum-x") and not W.is_private_label(None)
    assert not W.is_private_label("docsum-ui-jobs") and not W.is_private_label("docsum-cli")
