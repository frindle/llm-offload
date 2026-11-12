"""qctl close: resolve step + Dismiss (default) / Accept (--accept) split. Hermetic: the
close_* steps and _run are stubbed, so nothing touches the live queue or dashboard."""
import argparse
import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
os.environ.setdefault("QCTL_BIN", str(HERE))
_loader = SourceFileLoader("qctl_close_dismiss_t", str(HERE / "qctl"))
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("qctl_close_dismiss_t", _loader))
_loader.exec_module(q)


class _Log(list):
    rc = None


@pytest.fixture
def calls(monkeypatch):
    log = _Log()
    monkeypatch.setattr(q, "close_lookup", lambda j: ("feat", "feat-s1"))
    monkeypatch.setattr(q, "close_markers", lambda b, j, by, r: log.append("markers") or ["feat"])
    monkeypatch.setattr(q, "close_clear_run", lambda j: log.append("clear") or True)
    monkeypatch.setattr(q, "close_verify", lambda j: log.append("verify") or True)
    rc = {"code": 0, "out": ""}

    def fake_run(cmd, dry, timeout=600):
        log.append([os.path.basename(str(cmd[1]))] + [str(c) for c in cmd[2:]])
        return rc["code"], rc["out"]

    monkeypatch.setattr(q, "_run", fake_run)
    log.rc = rc
    return log


def _args(**kw):
    d = dict(job_id="j1", by="abc123", reason="shipped", dry_run=False, accept=False)
    d.update(kw)
    return argparse.Namespace(**d)


def test_default_is_dismiss_only(calls):
    assert q.cmd_close(_args()) == 0
    assert calls == ["markers", "clear", "verify", ["ollama-queue.py", "resolve", "j1"]]


def test_accept_adds_accept_bundle_after_resolve(calls):
    assert q.cmd_close(_args(accept=True)) == 0
    assert calls[-2:] == [["ollama-queue.py", "resolve", "j1"],
                          ["ollama-queue.py", "accept-bundle", "feat", "--job", "j1"]]


def test_resolve_of_already_gone_job_is_fine_other_failure_refused(calls):
    calls.rc.update(code=1, out="[queue] job not found: j1")
    assert q.cmd_close(_args()) == 0
    calls.rc.update(code=1, out="boom")
    with pytest.raises(q.Refused):
        q.cmd_close(_args())


def test_accept_failure_is_refused(calls, monkeypatch):
    def run(cmd, dry, timeout=600):
        return (1, "x") if "accept-bundle" in cmd else (0, "")
    monkeypatch.setattr(q, "_run", run)
    with pytest.raises(q.Refused):
        q.cmd_close(_args(accept=True))


def test_dry_run_runs_nothing(calls):
    assert q.cmd_close(_args(dry_run=True, accept=True)) == 0
    assert calls == []


if __name__ == "__main__":  # pytest-style; `python3 test-qctl-close-dismiss.py` runs it under pytest (the name has no test_ prefix)
    import sys

    import pytest
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "--import-mode=importlib", __file__]))
