import importlib.util
import inspect
import os

_spec = importlib.util.spec_from_file_location(
    "oq_promote_gate", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama-queue.py"))
oq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oq)


def _job(jid, status="pending", **kw):
    j = {"id": jid, "label": "lbl-" + jid, "status": status}
    j.update(kw)
    return j


def _victim(**kw):
    base = dict(pause_reason="promote_preempt", pause_meta={}, preempt_intent=None,
                preempted_by="ben", resume_transcript="/t/x.json")
    base.update(kw)
    return _job("vic", "paused", **base)


def _requeue(jobs, guard):
    """Requeue with the resume guard when the function accepts one (the fix adds can_resume)."""
    params = inspect.signature(oq._requeue_promote_victims).parameters
    if "can_resume" in params:
        return oq._requeue_promote_victims(jobs, can_resume=guard)
    return oq._requeue_promote_victims(jobs)


def _pk(j):
    return j.get("bundle")


def _real_guard(jobs):
    mk = getattr(oq, "_promote_victim_can_resume", None)
    if mk is not None:
        return mk(jobs, None, _pk)
    return lambda j: oq._promote_preempt_should_resume(
        j, jobs, None, beneficiary_bundle_busy=oq._beneficiary_bundle_busy(j, jobs, _pk))


def test_victim_stays_parked_while_beneficiary_is_pending():
    v = _victim()
    jobs = [_job("ben", "pending"), v]
    rq = _requeue(jobs, _real_guard(jobs))
    assert rq == [], f"victim requeued while beneficiary pending: {rq}"
    assert v["status"] == "paused" and v["pause_reason"] == "promote_preempt", "victim not parked"
    assert v["preempted_by"] == "ben"
    assert [j["id"] for j in jobs] == ["ben", "vic"]


def test_victim_stays_parked_while_beneficiary_bundle_is_busy():
    v = _victim(bundle="V")
    jobs = [_job("ben", "done", bundle="B"), _job("sib", "running", bundle="B"), v]
    rq = _requeue(jobs, _real_guard(jobs))
    assert rq == [], f"victim requeued while beneficiary bundle busy: {rq}"
    assert v["status"] == "paused" and v["preempted_by"] == "ben"


def test_victim_requeued_once_guards_allow_it():
    v = _victim()
    jobs = [_job("ben", "done"), v]
    assert _requeue(jobs, _real_guard(jobs)) == ["vic"]
    assert v["status"] == "pending" and v["pause_reason"] is None and v["preempted_by"] is None


def test_guard_decides_per_victim():
    ok = _victim()
    held = _job("held", "paused", pause_reason="promote_preempt", preempted_by="ben2", pause_meta={})
    jobs = [_job("ben", "done"), _job("ben2", "pending"), ok, held]
    rq = _requeue(jobs, _real_guard(jobs))
    assert rq == ["vic"], f"guard not applied per victim: {rq}"
    assert held["status"] == "paused" and held["preempted_by"] == "ben2"


def test_helper_uses_both_guards_and_tick_gates_requeue():
    helper = inspect.getsource(getattr(oq, "_promote_victim_can_resume", lambda: None))
    assert "_promote_preempt_should_resume" in helper, "should_resume missing"
    assert "_beneficiary_bundle_busy" in helper, "bundle_busy missing"
    src = inspect.getsource(oq.cmd_run)
    i = src.index("_rq = _requeue_promote_victims(")
    call = src[i:src.index("if _rq:", i)]
    assert "can_resume" in call, "tick call not gated: can_resume missing"
    assert "_promote_victim_can_resume(" in call, "tick call not using the guard helper"
