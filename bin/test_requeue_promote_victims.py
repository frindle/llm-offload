import importlib.util
import inspect
import os

_spec = importlib.util.spec_from_file_location(
    "oq_promote_requeue", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama-queue.py"))
oq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oq)


def _job(jid, status="pending", **kw):
    j = {"id": jid, "label": "lbl-" + jid, "status": status}
    j.update(kw)
    return j


def _victim(**kw):
    base = dict(pause_reason="promote_preempt", pause_meta={}, preempt_intent=None,
                preempted_by="ben", resume_transcript="/t/x.json",
                error="preempt-killed with no checkpoint transcript in its log -- resuming will restart from scratch")
    base.update(kw)
    return _job("vic", "paused", **base)


def test_victim_becomes_pending_and_fields_are_cleared():
    v = _victim()
    jobs = [_job("ben"), v]
    assert oq._requeue_promote_victims(jobs) == ["vic"]
    assert v["status"] == "pending"
    assert v["pause_reason"] is None and v["pause_meta"] is None
    assert v["preempt_intent"] is None and v["preempted_by"] is None
    assert v["error"] is None
    assert v["resume_transcript"] == "/t/x.json"


def test_victim_sits_right_behind_beneficiary_and_others_keep_order():
    v = _victim()
    jobs = [_job("ben"), _job("x1"), _job("x2"), _job("done1", "done"), v, _job("x3")]
    oq._requeue_promote_victims(jobs)
    ids = [j["id"] for j in jobs]
    assert ids[:2] == ["ben", "vic"]
    assert [i for i in ids if i != "vic"] == ["ben", "x1", "x2", "done1", "x3"]


def test_other_pauses_and_statuses_are_left_alone():
    ext = _job("ext", "paused", pause_reason="external_sigterm", preempted_by="ben")
    gate = _job("gate", "paused", pause_reason="gate_preempt")
    run = _job("run", "running", pause_reason="promote_preempt")
    keep_err = _victim(error="some other error")
    jobs = [_job("ben"), ext, gate, run, keep_err]
    assert oq._requeue_promote_victims(jobs) == ["vic"]
    assert ext["status"] == "paused" and ext["pause_reason"] == "external_sigterm"
    assert gate["status"] == "paused" and gate["pause_reason"] == "gate_preempt"
    assert run["status"] == "running" and run["pause_reason"] == "promote_preempt"
    assert keep_err["error"] == "some other error"
    assert [j["id"] for j in jobs] == ["ben", "vic", "ext", "gate", "run"]
    assert oq._requeue_promote_victims([_job("ben"), ext]) == []


def test_missing_beneficiary_keeps_position_but_still_pending():
    v = _victim(preempted_by="gone")
    jobs = [_job("a"), v, _job("b")]
    assert oq._requeue_promote_victims(jobs) == ["vic"]
    assert [j["id"] for j in jobs] == ["a", "vic", "b"]
    assert v["status"] == "pending" and v["preempted_by"] is None


def test_tick_calls_requeue_before_the_old_resume_loop():
    src = inspect.getsource(oq.cmd_run)
    call = src.find('_requeue_promote_victims(state["jobs"])')
    assert call != -1
    assert call < src.index("for _pj2 in state[\"jobs\"]:")
