"""`ollama-queue.py resolve <id>` also marks that job's bundle slice superseded
(key `bundle#sid`, reason 'resolved') in bundle-superseded.json."""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_sp = importlib.util.spec_from_file_location("oq_resolve_mark", HERE / "ollama-queue.py")
Q = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(Q)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(Q, "LOCK_PATH", tmp_path / "state.lock")
    sup = tmp_path / "sup.json"
    monkeypatch.setenv("OLLAMA_SUPERSEDED_FILE", str(sup))

    def put(*jobs):
        (tmp_path / "state.json").write_text(json.dumps({"jobs": list(jobs)}))

    def marks():
        return json.loads(sup.read_text()) if sup.exists() else {}

    def ids():
        return [j["id"] for j in json.loads((tmp_path / "state.json").read_text())["jobs"]]
    return put, marks, ids


def test_resolve_marks_bundle_slice_superseded(env):
    put, marks, ids = env
    put({"id": "aaa111bbb222", "label": "x-fix", "status": "failed", "bundle": "my-bundle"},
        {"id": "ccc333", "label": "y-fix", "status": "failed", "bundle": "my-bundle"})
    assert Q.resolve_job("aaa111bbb222") == {"resolved": "aaa111bbb222"}
    m = marks()
    assert list(m) == ["my-bundle#aaa111bbb222"]
    assert m["my-bundle#aaa111bbb222"]["reason"] == "resolved"
    assert ids() == ["ccc333"]


def test_resolve_without_bundle_still_succeeds_and_marks_nothing(env):
    put, marks, ids = env
    put({"id": "ddd444", "label": "z-fix", "status": "failed"},
        {"id": "eee555", "label": "w-fix", "status": "failed", "bundle": "  "})
    assert Q.resolve_job("ddd444") == {"resolved": "ddd444"}
    assert Q.resolve_job("eee555") == {"resolved": "eee555"}
    assert marks() == {}
    assert ids() == []


def test_resolve_survives_marker_write_failure(env, monkeypatch, tmp_path):
    put, marks, ids = env
    bad = tmp_path / "afile"
    bad.write_text("x")
    monkeypatch.setenv("OLLAMA_SUPERSEDED_FILE", str(bad / "sub" / "sup.json"))
    put({"id": "fff666", "label": "v-fix", "status": "done", "bundle": "b2"})
    assert Q.resolve_job("fff666") == {"resolved": "fff666"}
    assert ids() == []


def test_resolve_still_refuses_pending(env):
    put, marks, ids = env
    put({"id": "ggg777", "label": "u-fix", "status": "pending", "bundle": "b3"})
    with pytest.raises(Q.QueueActionError):
        Q.resolve_job("ggg777")
    assert marks() == {} and ids() == ["ggg777"]
