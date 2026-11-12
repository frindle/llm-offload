"""Soft-gate needs-review status end to end, hermetic (no GPU, no live queue, no network):
runner RESULT line, queue parse/apply, dashboard grouping, and the Accept endpoint chain vs Clear (dismiss-only)."""
import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("QCTL_BIN", str(HERE))


def _load(name, fname):
    ld = SourceFileLoader(name, str(HERE / fname))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


runner = _load("nr_runner", "mini-swe-runner.py")
queue = _load("nr_queue", "ollama-queue.py")
api = _load("nr_api", "ollama-queue-api.py")

OK = (True, "")


def gates(**bad):
    names = ["test_unchanged", "test_green", "diff_reach", "fn_scope", "no_hacks", "red_without_fix", "suite", "literals", "scope"]
    return {n: ((False, "x") if n in bad else OK) for n in names}


# ---- runner: final_result wiring ----
def test_runner_soft_only_is_pass_needs_review_rc0():
    rc, line = runner.final_result(gates(literals=1, fn_scope=1), "a=ok", " tail")
    assert rc == 0 and line.startswith("PASS needs-review soft=fn_scope,literals ") and "mode=two-phase" in line


def test_runner_hard_failure_still_fails():
    rc, line = runner.final_result(gates(fn_scope=1, no_hacks=1), "a=ok", "")
    assert rc == 1 and line.startswith("FAIL mode=two-phase")


def test_runner_all_green_plain_pass_and_chain_stays_strict():
    assert runner.final_result(gates(), "s", "") == (0, "PASS mode=two-phase s")
    rc, line = runner.final_result(gates(fn_scope=1), "s", "", chain=True)
    assert rc == 1 and line.startswith("FAIL")


# ---- queue: parse + apply ----
def test_parse_needs_review_not_failed():
    v, cls, reason = queue.parse_runner_result("RESULT PASS needs-review soft=fn_scope,literals mode=two-phase x=ok\n")
    assert v.startswith("NEEDS-REVIEW") and cls is None and "fn_scope,literals" in reason
    assert queue.parse_runner_result("RESULT PASS mode=two-phase x=ok")[0] == "PASS"
    assert queue.parse_runner_result("RESULT FAIL step-1 x")[0].startswith("FAIL")


def test_apply_marks_needs_review_without_failing(tmp_path, monkeypatch):
    log = tmp_path / "l.log"
    log.write_text("RESULT PASS needs-review soft=fn_scope,literals mode=two-phase\n")
    job = {"runner": "mini-swe-runner.py", "status": "done", "live_log_path": str(log)}
    assert queue._apply_runner_result(job) is True
    assert job["needs_review"] is True and job["needs_review_gates"] == ["fn_scope", "literals"]
    assert job["status"] == "done" and not job.get("failure_class")
    plain = {"runner": "mini-swe-runner.py", "status": "done", "live_log_path": str(log)}
    log.write_text("RESULT PASS mode=two-phase\n")
    queue._apply_runner_result(plain)
    assert "needs_review" not in plain


# ---- dashboard grouping ----
def test_dashboard_group_is_amber_needs_review_not_good_or_failed():
    rows = [{"id": "j1", "label": "feat-s1", "needs_review": True, "needs_review_gates": ["fn_scope"], "raw_verdict": "NEEDS-REVIEW (soft: fn_scope)",
             "verdict": "NEEDS-REVIEW (soft: fn_scope)", "has_gate": False, "status": "done", "timestamp": "2026-10-25T00:00:00Z"}]
    api._annotate_run_groups(rows, [], auto_runs={}, acted={})
    api._annotate_eyes_labels(rows)
    r = rows[0]
    assert r["group"] == "needs_eyes" and r["raw_verdict"] == "NEEDS-REVIEW" and "fn_scope" in r["verdict"]
    assert r["eyes_flavor"] == "softgate" and r["eyes_severity"] == "warn" and "needs review" in r["eyes_label"]


# ---- Accept endpoint chain vs Clear ----
class _QM:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def close_lookup(self, j):
        self.calls.append(("lookup", j))
        return ("bund", "bund-s1")

    def close_accept(self, b, j):
        self.calls.append(("accept", b, j))
        if self.fail == "accept":
            raise RuntimeError("boom")
        return True

    def close_resolve(self, j):
        self.calls.append(("resolve", j))
        return True


def test_accept_runs_chain_in_order_then_archives():
    qm, arch = _QM(), []
    res = api._accept_run("j1", qm=qm, archive=lambda j, **k: arch.append(j) or {"ok": True})
    assert qm.calls == [("lookup", "j1"), ("accept", "bund", "j1"), ("resolve", "j1")] and arch == ["j1"]
    assert res["ok"] and res["accepted"] and res["cleared"]


def test_accept_refusal_stops_chain_and_does_not_archive():
    qm, arch = _QM(fail="accept"), []
    res = api._accept_run("j1", qm=qm, archive=lambda j, **k: arch.append(j) or {"ok": True})
    assert not res["ok"] and res["code"] == 409 and arch == [] and ("resolve", "j1") not in qm.calls


def test_clear_does_not_accept(monkeypatch):
    qm = _QM()
    monkeypatch.setattr(api, "_qctl", lambda: qm)
    monkeypatch.setattr(api, "_archive_run", lambda j, override=None, how=None: {"ok": True, "id": j, "moved": []})

    class H:
        out = None
        path = "/api/runs/j1/clear"
        def _read_json_body(self): return {}
        def _json(self, o, status=200): H.out = o
        def _text(self, *a, **k): pass

    h = H()
    api.Handler._clear_run(h, "j1")
    assert h.out["ok"] and qm.calls == []


def test_accept_route_present():
    # The dashboard's Accept button went with the Run Status panel (ollama-queue-dashboard 67447e8, pinned there by
    # tests/test_dashboard_runstatus_removed.py). Accept stays: POST /api/runs/<id>/accept, and `qctl close --accept`.
    src = (HERE / "ollama-queue-api.py").read_text()
    assert r'/api/runs/([^/]+)/accept$' in src and "return self._accept_run(m.group(1))" in src
    assert "does NOT accept" in src  # Clear confirm text says dismiss-only


if __name__ == "__main__":  # pytest-style; `python3 test-needs-review.py` runs it under pytest (the name has no test_ prefix)
    import sys

    import pytest
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "--import-mode=importlib", __file__]))
