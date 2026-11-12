"""Orchestration + privacy of the queue runner and CLI wrapper. Fake model callable only (no GPU, no network)."""
import json
import os
import stat
import subprocess
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

import docsum_cli
import docsum_run
from docsum_make_test_pdf import make_pdf

CANARY_DOC = "ZXQ-DOC-CANARY-7731"
CANARY_SC = "ZXQ-SCENARIO-CANARY-5519"
CANARY_NAME = "ZXQ-NAME-CANARY-2208"


def fake_call(system, user):
    return "## Bottom line\nRent is $1,250.00 (p. 1)\n## Key facts\n- due 2026-12-01 (p. 2)\n## Risks and gaps\n- none stated\n## Citations\np. 1-2"


def job(tmp_path, pages, name="doc.pdf"):
    src = tmp_path / name
    make_pdf(str(src), pages)
    root = tmp_path / "private"
    return docsum_cli.prepare_job(str(src), "tenant deciding on renewal " + CANARY_SC, str(root)), root


def test_end_to_end_with_fake_model_writes_md_and_valid_pdf(tmp_path, capsys):
    from pypdf import PdfReader
    j, root = job(tmp_path, ["Page one " + "text " * 20, "Page two " + "more " * 20])
    rc = docsum_run.run_job(j["task_file"], "m", "darkbloom", 32768, call=fake_call)
    assert rc == 0
    d = Path(j["dir"])
    md = (d / "summary.md").read_text()
    for part in ("# Document summary", "**Scenario:**", "## Bottom line", "## Key facts", "## Risks and gaps", "## Citations"):
        assert part in md
    rd = PdfReader(str(d / "summary.pdf"))
    txt = "\n".join(p.extract_text() for p in rd.pages)
    assert "Document summary" in txt and "$1,250.00" in txt and "Bottom line" in txt
    assert json.loads((d / "status.json").read_text())["ok"] is True
    for n in ("summary.md", "summary.pdf", "status.json"):
        assert stat.S_IMODE(os.stat(d / n).st_mode) == 0o600


def test_scanned_pdf_fails_with_code_only(tmp_path, capsys):
    j, _ = job(tmp_path, ["", "", ""])
    assert docsum_run.run_job(j["task_file"], "m", "darkbloom", 32768, call=fake_call) == 1
    out = capsys.readouterr()
    assert "code=E_SCANNED_NO_OCR" in out.out
    assert json.loads((Path(j["dir"]) / "status.json").read_text()) == {"ok": False, "code": "E_SCANNED_NO_OCR"}


def test_map_reduce_used_when_text_exceeds_context(tmp_path):
    calls = []

    def call(system, user):
        calls.append(user)
        return fake_call(system, user)

    j, _ = job(tmp_path, ["Section %d " % i + "word " * 400 for i in range(6)])
    assert docsum_run.run_job(j["task_file"], "m", "darkbloom", 1000, call=call) == 0  # max_chars floor 4000
    assert len(calls) > 2
    assert "## Bottom line" in calls[-1]


def test_model_failure_gives_code_not_excerpt(tmp_path, capsys):
    def boom(system, user):
        raise RuntimeError("upstream echoed: " + CANARY_DOC)

    j, _ = job(tmp_path, ["Plenty of text here " * 10])
    assert docsum_run.run_job(j["task_file"], "m", "darkbloom", 32768, call=boom) == 1
    out = capsys.readouterr()
    assert "code=E_MODEL_CALL" in out.out and CANARY_DOC not in out.out + out.err


def test_bad_task_file_code(tmp_path, capsys):
    p = tmp_path / "t.json"
    p.write_text("{}")
    assert docsum_run.run_job(str(p), "m", "darkbloom", 32768, call=fake_call) == 2
    assert "E_TASK" in capsys.readouterr().out


def test_canary_only_in_private_dir(tmp_path, capsys):
    """After a run with a canary in the PDF, scenario and file name, it appears nowhere but the private dir."""
    src = tmp_path / ("%s.pdf" % CANARY_NAME)
    make_pdf(str(src), ["Clause 1 %s monthly fee $99.00 " % CANARY_DOC + "filler " * 30, "Clause 2 " + "filler " * 30])
    root = tmp_path / "private"
    j = docsum_cli.prepare_job(str(src), "I am a landlord " + CANARY_SC, str(root))
    # What the queue/CLI would hand to the queue: the task file path content, label, argv.
    cmd = build_cmd(j)
    assert all(c not in " ".join(cmd) for c in (CANARY_DOC, CANARY_SC, CANARY_NAME))
    assert not any(c in open(j["task_file"]).read() for c in (CANARY_DOC, CANARY_SC, CANARY_NAME))

    def echo_call(system, user):  # a model that parrots its input: the summary legitimately carries the canary
        return "## Bottom line\n" + CANARY_DOC + "\n## Key facts\n" + CANARY_SC + "\n## Risks and gaps\nx\n## Citations\np. 1"

    assert docsum_run.run_job(j["task_file"], "m", "darkbloom", 32768, call=echo_call) == 0
    cap = capsys.readouterr()
    for secret in (CANARY_DOC, CANARY_SC, CANARY_NAME):
        assert secret not in cap.out and secret not in cap.err
    # the private dir DOES hold them (proves the canaries were really processed)
    d = Path(j["dir"])
    assert CANARY_DOC in (d / "summary.md").read_text()
    assert CANARY_NAME in (d / "meta.json").read_text()
    assert CANARY_SC in (d / "scenario.txt").read_text()
    # nothing outside the private dir contains a canary
    for f in tmp_path.rglob("*"):
        if f.is_file() and root not in f.parents and f != src:
            blob = f.read_bytes()
            assert all(s.encode() not in blob for s in (CANARY_DOC, CANARY_SC, CANARY_NAME)), f


def build_cmd(j):
    loader = importlib.machinery.SourceFileLoader("doc_summary_cli", str(Path(__file__).resolve().parent / "doc-summary"))
    spec = importlib.util.spec_from_loader("doc_summary_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod.build_enqueue_cmd(j, "qwen3.6-35b-a3b-vl-mtp-mxfp8", None)


def test_run_job_uses_live_served_model_not_flag(tmp_path, monkeypatch):
    j, _ = job(tmp_path, ["hello world " * 20])
    seen = []
    monkeypatch.setattr(docsum_cli, "current_model", lambda served=None: "live-now")
    monkeypatch.setattr(docsum_run, "make_call", lambda host, model: seen.append(model) or fake_call)
    assert docsum_run.run_job(j["task_file"], "stale-flag", "darkbloom", 32768) == 0
    assert seen == ["live-now"]


def test_run_job_fails_when_nothing_served(tmp_path, monkeypatch):
    j, _ = job(tmp_path, ["hello world " * 20])

    def none(served=None):
        raise ValueError("E_MODEL_NOT_SERVED")
    monkeypatch.setattr(docsum_cli, "current_model", none)
    assert docsum_run.run_job(j["task_file"], "m", "darkbloom", 32768) == 1
    assert json.load(open(Path(j["dir"]) / "status.json"))["code"] == "E_MODEL_NOT_SERVED"


def test_enqueue_command_shape(tmp_path):
    j, _ = job(tmp_path, ["hello world " * 20])
    cmd = build_cmd(j)
    assert "--runner" in cmd and cmd[cmd.index("--runner") + 1].endswith("docsum_run.py")
    assert cmd[cmd.index("--label") + 1] == "docsum-" + j["job_id"]
    assert cmd[cmd.index("--task-kind") + 1] == "research"
    assert cmd[cmd.index("--model") + 1] == "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    assert cmd[cmd.index("--task-file") + 1] == j["task_file"]
    assert "--num-ctx" not in cmd


def test_cli_rejects_unserved_model_with_code_only():
    p = subprocess.run([str(Path(__file__).resolve().parent / "doc-summary"), "/nonexistent.pdf", "--scenario", CANARY_SC],
                       capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr.strip() == "doc-summary: E_NO_FILE"
    assert CANARY_SC not in p.stdout + p.stderr
