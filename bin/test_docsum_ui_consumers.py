"""Private docsum jobs must never reach any consumer that feeds Claude or a tool-using agent."""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(name, fname):
    path = os.path.join(HERE, fname)
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ROW = {"id": "aaaa11112222", "label": "docsum-0a1b2c3d", "status": "failed", "model": "m", "enqueued_at": "2026-11-01",
       "gate": "", "files": 0, "cwd": "/x"}
REAL = {"id": "bbbb11112222", "label": "real-work-fix", "status": "failed", "model": "m", "enqueued_at": "2026-11-01",
        "gate": "", "files": 0, "cwd": "/y"}


def test_handoff_emit_collect_excludes_docsum(monkeypatch):
    he = load("he_t", "handoff-emit.py")
    monkeypatch.setattr(he, "load_jobs", lambda: [dict(ROW), dict(REAL)])
    pending, complete = he.collect({})[:2]
    labels = [j.get("label") for j in pending + complete]
    assert "docsum-0a1b2c3d" not in labels and "real-work-fix" in labels


def test_handoff_emit_collect_keeps_builder_docsum_labels(monkeypatch):
    he = load("he_t3", "handoff-emit.py")
    rows = [dict(ROW, label="docsum-ui-jobs", id="cccc11112222"), dict(ROW, label="docsum-cli", id="dddd11112222")]
    monkeypatch.setattr(he, "load_jobs", lambda: rows)
    pending, complete = he.collect({})[:2]
    assert {j["label"] for j in pending + complete} == {"docsum-ui-jobs", "docsum-cli"}


def test_all_helpers_agree_and_only_8hex_is_private():
    import docsum_private
    mods = [load("he_t4", "handoff-emit.py"), load("ac_t4", "handoff-autoclear.py"), load("ht_t4", "handoff-triage.py"),
            load("df_t4", "ollama-diagnosis-followup.py"), load("w_t4", "dispatch-escalation-watcher.py"), docsum_private]
    for lab, want in (("docsum-0a1b2c3d", True), ("docsum-ui-jobs", False), ("docsum-cli", False), ("docsum-0a1b2c3", False),
                      ("docsum-0a1b2c3d9", False), ("docsum-0A1B2C3D", False), ("x-docsum-0a1b2c3d", False), (None, False)):
        for m in mods:
            assert m.is_private_label(lab) is want, (m.__name__, lab)


def test_fallback_copy_matches_shared_regex():
    """Scripts carry an ImportError fallback (they get copied to temp dirs by tests): same pattern as the shared module."""
    import docsum_private
    for f in ("handoff-emit.py", "handoff-autoclear.py", "handoff-triage.py", "ollama-diagnosis-followup.py",
              "dispatch-escalation-watcher.py"):
        assert docsum_private.PRIVATE_LABEL_RE.pattern in open(os.path.join(HERE, f)).read(), f


def test_builder_labels_not_excluded_elsewhere():
    ac = load("ac_t5", "handoff-autoclear.py")
    df = load("df_t5", "ollama-diagnosis-followup.py")
    ht = load("ht_t5", "handoff-triage.py")
    assert not ac.is_private({"label": "docsum-ui-jobs"})
    assert df.is_diagnosis_job({"label": "docsum-ui-x", "task_kind": "research"}) is True
    assert ht.hard_exclusion({"label": "docsum-cli", "status": "done", "gate": "PASS"}, None, set()) != "private docsum job"


def test_autoclear_followup_refuses_docsum(tmp_path):
    ac = load("ac_t", "handoff-autoclear.py")
    calls = []
    assert ac.enqueue_followup(dict(ROW), "investigate", runner=lambda argv: calls.append(argv)) is None
    assert calls == []


def test_autoclear_plan_never_digests_or_classifies_docsum():
    ac = load("ac_t2", "handoff-autoclear.py")
    seen = []

    def fake_claude(prompt, **k):
        seen.append(prompt)
        return "[]"
    data = {"complete": [dict(ROW), dict(REAL)], "acted": {}}
    out = ac.plan(data, [], {}, {}, use_llm=True, claude=fake_claude, now=1.0)
    ids = [r["id"] for r, *_ in out["clear"] + out["followup"] + out["keep"]]
    assert ROW["id"] not in ids
    assert all("docsum-0a1b2c3d" not in p and ROW["id"] not in p for p in seen)


def test_diagnosis_followup_ignores_docsum():
    df = load("df_t", "ollama-diagnosis-followup.py")
    assert df.is_diagnosis_job({"label": "docsum-0a1b2c3d", "task_kind": "research"}) is False
    assert df.is_diagnosis_job({"label": "x-diag", "task_kind": "research"}) is True


def test_handoff_triage_excludes_docsum():
    ht = load("ht_t", "handoff-triage.py")
    item = {"label": "docsum-0a1b2c3d", "status": "done", "gate": "PASS"}
    assert ht.hard_exclusion(item, None, set()) == "private docsum job"
