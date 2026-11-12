"""Hardening of the docsum runner/extractor: loopback-only model endpoint, extraction caps, no tracebacks."""
import json
import os
import sys
import types

import pytest

import docsum_cli
import docsum_extract
import docsum_run
from docsum_make_test_pdf import make_pdf


def fake_call(system, user):
    return "## Bottom line\nok (p. 1)\n## Key facts\n- a (p. 1)\n## Risks and gaps\n- none\n## Citations\np. 1"


def mkjob(tmp_path, pages):
    src = tmp_path / "d.pdf"
    make_pdf(str(src), pages)
    root = tmp_path / "private"
    return docsum_cli.prepare_job(str(src), "scenario text", str(root)), root


def status(job, root):
    return json.load(open(os.path.join(str(root), job["job_id"], "status.json")))


@pytest.mark.parametrize("base,ok", [("http://127.0.0.1:8000", True), ("http://localhost:8000", True), ("http://[::1]:8000", True),
                                     ("http://192.0.2.15:8000", False), ("https://api.example.com", False), ("", False), (None, False)])
def test_make_call_requires_loopback_base_url(monkeypatch, base, ok):
    fake = types.SimpleNamespace(base_url=lambda: base, chat=lambda *a, **k: ("x", "stop"))
    monkeypatch.setitem(sys.modules, "darkbloom_chat", fake)
    if ok:
        assert callable(docsum_run.make_call("darkbloom", "m"))
    else:
        with pytest.raises(ValueError, match="E_NOT_LOCAL"):
            docsum_run.make_call("darkbloom", "m")


def test_run_job_reports_e_not_local_code_only(tmp_path, monkeypatch, capsys):
    job, root = mkjob(tmp_path, ["Page one " + "text " * 30])
    monkeypatch.setitem(sys.modules, "darkbloom_chat", types.SimpleNamespace(base_url=lambda: "http://8.8.8.8:1"))
    assert docsum_run.run_job(job["task_file"], "m", "darkbloom", 32768) == 1
    assert status(job, root) == {"ok": False, "code": "E_NOT_LOCAL"}
    assert "Traceback" not in capsys.readouterr().out


def test_extract_page_cap(tmp_path):
    p = tmp_path / "many.pdf"
    make_pdf(str(p), ["page " + "x " * 30] * 5)
    with pytest.raises(ValueError, match="E_TOO_MANY_PAGES"):
        docsum_extract.extract_pages(str(p), max_pages=3)
    assert len(docsum_extract.extract_pages(str(p), max_pages=5)) == 5


def test_extract_wall_time_cap(tmp_path):
    p = tmp_path / "t.pdf"
    make_pdf(str(p), ["page " + "x " * 30] * 3)
    with pytest.raises(ValueError, match="E_EXTRACT_TIMEOUT"):
        docsum_extract.extract_pages(str(p), max_seconds=-1)


def test_pdfplumber_fallback_does_not_duplicate_pages(tmp_path, monkeypatch):
    p = tmp_path / "f.pdf"
    make_pdf(str(p), ["one " * 20, "two " * 20, "three " * 20])

    class P:
        def __init__(self, n):
            self.n = n

        def extract_text(self):
            if self.n == 2:
                raise RuntimeError("boom")      # pypdf fails mid-document after 2 good pages
            return "pypdf-%d" % self.n

    class R:
        pages = [P(0), P(1), P(2)]
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: R())
    out = docsum_extract.extract_pages(str(p))
    assert len(out) == 3 and not any(t.startswith("pypdf-") for t in out)


def test_unreadable_scenario_or_meta_gives_code_not_traceback(tmp_path, capsys):
    pages = ["Page one " + "text " * 40, "Page two " + "more " * 40]
    job, root = mkjob(tmp_path, pages)
    os.remove(os.path.join(str(root), job["job_id"], "scenario.txt"))
    assert docsum_run.run_job(job["task_file"], "m", "darkbloom", 32768, call=fake_call) == 1
    assert status(job, root) == {"ok": False, "code": "E_READ"}
    job2, root2 = mkjob(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, pages)
    open(os.path.join(str(root2), job2["job_id"], "meta.json"), "w").write("{not json")
    assert docsum_run.run_job(job2["task_file"], "m", "darkbloom", 32768, call=fake_call) == 1
    assert status(job2, root2)["code"] == "E_READ"
    assert "Traceback" not in capsys.readouterr().out


def test_render_or_write_failure_gives_e_write(tmp_path, monkeypatch, capsys):
    job, root = mkjob(tmp_path, ["Page one " + "text " * 40, "Page two " + "more " * 40])
    monkeypatch.setattr(docsum_run, "render_pdf", lambda lines: (_ for _ in ()).throw(RuntimeError("secret text")))
    assert docsum_run.run_job(job["task_file"], "m", "darkbloom", 32768, call=fake_call) == 1
    assert status(job, root) == {"ok": False, "code": "E_WRITE"}
    out = capsys.readouterr()
    assert "secret text" not in out.out + out.err and "Traceback" not in out.out + out.err


def test_job_dir_deleted_mid_run_does_not_traceback(tmp_path, monkeypatch, capsys):
    import shutil
    job, root = mkjob(tmp_path, ["Page one " + "text " * 40, "Page two " + "more " * 40])
    jd = os.path.join(str(root), job["job_id"])

    def call(s, u):
        shutil.rmtree(jd)
        return fake_call(s, u)
    assert docsum_run.run_job(job["task_file"], "m", "darkbloom", 32768, call=call) == 1
    assert "Traceback" not in capsys.readouterr().out


def test_main_converts_unexpected_exception_to_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(docsum_run, "_go_silent", lambda: None)   # the real one dup2()s fd 2: covered in a subprocess below
    monkeypatch.setattr(docsum_run, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("document text")))
    assert docsum_run.main(["--model", "m", "--task-file", "x"]) == 1
    out = capsys.readouterr()
    assert "E_INTERNAL" in out.out and "document text" not in out.out + out.err


def test_extract_hard_timeout_on_single_pathological_page(tmp_path, monkeypatch):
    import time
    import pypdf
    p = tmp_path / "slow.pdf"
    make_pdf(str(p), ["page " + "x " * 30] * 2)

    class Page:
        def extract_text(self):
            while True:          # pathological page: pure-Python spin
                pass

    class R:
        pages = [Page()]
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: R())
    t0 = time.time()
    with pytest.raises(ValueError, match="E_EXTRACT_TIMEOUT"):
        docsum_extract.extract_pages(str(p), max_seconds=1)
    assert time.time() - t0 < 5
    import signal
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0          # alarm cleaned up


def test_run_job_maps_hard_timeout_to_code(tmp_path, monkeypatch):
    job, root = mkjob(tmp_path, ["Page one " + "text " * 40])
    monkeypatch.setattr(docsum_run, "extract_pages", lambda p: (_ for _ in ()).throw(ValueError("E_EXTRACT_TIMEOUT")))
    assert docsum_run.run_job(job["task_file"], "m", "darkbloom", 32768, call=fake_call) == 1
    assert status(job, root)["code"] == "E_EXTRACT_TIMEOUT"


def test_warnings_from_pdf_text_never_reach_stdout_or_stderr(tmp_path):
    """Crafted PDF: a pypdf warning/log record quoting a canary must not reach the runner's stdout/stderr (queue log)."""
    import subprocess
    canary = "ZXQ-WARN-CANARY-9913"
    src = tmp_path / "w.pdf"
    make_pdf(str(src), ["Page one " + "text " * 40, "Page two " + "more " * 40])
    root = tmp_path / "private"
    job = docsum_cli.prepare_job(str(src), "scenario", str(root))
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text("")
    code = (
        "import sys, logging, warnings, os\n"
        "sys.path.insert(0, %r)\n"
        "import docsum_run, docsum_extract\n"
        "real = docsum_extract.extract_pages\n"
        "def noisy(path):\n"
        "    logging.getLogger('pypdf').warning('Ignoring wrong pointing object ' + %r)\n"
        "    warnings.warn('pdfminer says ' + %r)\n"
        "    os.write(2, b'raw stderr ' + %r.encode())\n"
        "    return real(path)\n"
        "docsum_run.extract_pages = noisy\n"
        "docsum_run.make_call = lambda h, m: (lambda s, u: '## Bottom line\\nx (p. 1)\\n## Key facts\\n- a\\n## Risks and gaps\\n- b\\n## Citations\\np. 1')\n"
        "sys.exit(docsum_run.main(['--model','m','--task-file',%r]))\n"
    ) % (os.path.dirname(os.path.abspath(__file__)), canary, canary, canary, job["task_file"])
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert canary not in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert "OK" in r.stdout and r.returncode == 0
