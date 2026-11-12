"""docsum web UI server: upload rules, id safety, opaque names, log hygiene, delete, and an end-to-end HTTP smoke test.
Synthetic PDFs only (docsum_make_test_pdf). The queue is faked in-process: no GPU, no real queue rows, no network."""
import contextlib
import http.client
import io
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

import docsum_cli
import docsum_run
import docsum_server
from docsum_make_test_pdf import make_pdf

CANARY_DOC = "ZXQ-DOC-CANARY-7731"
CANARY_SC = "ZXQ-SCENARIO-CANARY-5519"
CANARY_NAME = "ZXQ-NAME-CANARY-2208"
MODEL = "live-model-under-test"
B = "----docsumtest"


def fake_model(system, user):
    return "## Bottom line\nRent is $1,250.00 (p. 1)\n## Key facts\n- due 2026-12-01 (p. 2)\n## Risks and gaps\n- none\n## Citations\np. 1-2"


class FakeQueue:
    """Stands in for the ollama queue + runner: enqueue() records the real doc-summary command, then a worker thread runs
    docsum_run.run_job with a fake model once `go` is set. rows() reports pending/running like the queue state file."""

    def __init__(self, autostart=True):
        self.status, self.cmds, self.stdout, self.threads = {}, [], io.StringIO(), []
        self.go = threading.Event()
        if autostart:
            self.go.set()

    def rows(self):
        return [{"id": "q" + k, "label": "docsum-" + k, "status": v} for k, v in self.status.items()]

    def enqueue(self, job, model):
        from importlib.machinery import SourceFileLoader
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(docsum_server.__file__)), "doc-summary")
        spec = importlib.util.spec_from_loader("ds_cli", SourceFileLoader("ds_cli", path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.cmds.append(mod.build_enqueue_cmd(job, model, None))
        jid = job["job_id"]
        self.status[jid] = "pending"

        def work():
            self.go.wait(10)
            self.status[jid] = "running"
            with contextlib.redirect_stdout(self.stdout):
                rc = docsum_run.run_job(job["task_file"], model, "darkbloom", 32768, call=fake_model)
            self.status[jid] = "done" if rc == 0 else "failed"
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.threads.append(t)
        return True


class UI:
    def __init__(self, tmp_path, max_pdf=docsum_server.MAX_PDF, queue=None, enqueue_ok=True):
        self.root = str(tmp_path / "private")
        self.log = []
        self.q = queue or FakeQueue()
        self.resolved = []
        enq = self.q.enqueue if enqueue_ok else (lambda job, model: False)
        self.cfg = docsum_server.Config(root=self.root, rows=self.q.rows, served=lambda: [MODEL, "other-model"], enqueue=enq,
                                        resolve=lambda j, **kw: self.resolved.append(j), max_pdf=max_pdf, log=self.log.append)
        self.srv = docsum_server.make_server(self.cfg, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.t.join(5)

    def req(self, method, path, body=None, headers=None, origin=True, auth=True):
        h = {"Host": "127.0.0.1:%d" % self.port}
        if origin and method == "POST":
            h["Origin"] = "http://127.0.0.1:%d" % self.port
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        out = (r.status, dict(r.getheaders()), data)
        c.close()
        return out

    def upload(self, pdf, scenario="Tenant deciding on renewal " + CANARY_SC, filename=CANARY_NAME + ".pdf", model=MODEL, **kw):
        body = form([("scenario", None, scenario.encode()), ("model", None, model.encode()), ("file", filename, pdf)])
        return self.req("POST", "/api/upload", body, {"Content-Type": "multipart/form-data; boundary=" + B, "Content-Length": str(len(body))}, **kw)

    def wait_done(self, jid, want=("done", "failed"), timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            for j in json.loads(self.req("GET", "/api/jobs")[2])["jobs"]:
                if j["id"] == jid and j["status"] in want:
                    return j
            time.sleep(0.05)
        raise AssertionError("job did not reach %s" % (want,))


def form(parts):
    out = b""
    for name, filename, data in parts:
        d = 'Content-Disposition: form-data; name="%s"' % name + ('; filename="%s"' % filename if filename else "")
        out += ("--%s\r\n%s\r\n\r\n" % (B, d)).encode() + data + b"\r\n"
    return out + ("--%s--\r\n" % B).encode()


def synth_pdf(tmp_path, pages=None, name="s.pdf"):
    p = tmp_path / name
    make_pdf(str(p), pages or ["Page one " + CANARY_DOC + " " + "text " * 20, "Page two " + "more " * 20])
    return p.read_bytes()


@pytest.fixture
def ui(tmp_path):
    u = UI(tmp_path)
    yield u
    u.close()


def test_page_served_with_security_headers(ui):
    s, h, body = ui.req("GET", "/")
    assert s == 200 and h["Content-Type"].startswith("text/html") and b'id="drop"' in body and b"<select" not in body
    assert "default-src 'none'" in h["Content-Security-Policy"] and h["Cache-Control"] == "no-store"


def test_upload_rules_magic_size_scenario_model(tmp_path):
    u = UI(tmp_path, max_pdf=5000)
    try:
        good = synth_pdf(tmp_path)
        assert len(good) < 5000
        assert json.loads(u.upload(b"GIF89a not a pdf")[2]) == {"error": "E_NOT_PDF"}
        assert u.upload(b"x" * 20000)[0] == 413
        assert u.upload(good, scenario="   ")[0] == 200       # scenario is optional
        assert u.upload(good, model="client-chosen-ignored")[0] == 200   # a model field is ignored
        assert u.q.cmds[-1][u.q.cmds[-1].index("--model") + 1] == MODEL
        big_pdf = b"%PDF-1.4\n" + b"0" * 5000
        assert json.loads(u.upload(big_pdf)[2]) == {"error": "E_TOO_BIG"}
        s, _, body = u.req("POST", "/api/upload", b"junk", {"Content-Type": "text/plain"})
        assert (s, json.loads(body)) == (400, {"error": "E_BAD_UPLOAD"})
        s, _, body = u.upload(good)
        assert s == 200 and re.fullmatch(r"[0-9a-f]{8}", json.loads(body)["id"])
        assert not [n for n in os.listdir(u.root) if n.startswith(".up-")]  # temp upload dir always cleaned
    finally:
        u.close()


def test_enqueue_failure_leaves_no_job_dir(tmp_path):
    u = UI(tmp_path, enqueue_ok=False)
    try:
        s, _, body = u.upload(synth_pdf(tmp_path))
        assert (s, json.loads(body)["error"]) == (502, "E_ENQUEUE")
        assert [n for n in os.listdir(u.root) ] == []
    finally:
        u.close()


def test_cross_origin_rejected_proxy_headers_accepted_over_http(ui, tmp_path):
    pdf = synth_pdf(tmp_path)
    body = form([("file", "a.pdf", pdf), ("scenario", None, b"s")])
    s, _, b = ui.req("POST", "/api/upload", body, {"Content-Type": "multipart/form-data; boundary=" + B, "Origin": "http://evil.example"})
    assert (s, json.loads(b)) == (403, {"error": "E_CROSS_ORIGIN"})
    s, _, b = ui.req("POST", "/api/upload", body, {"Content-Type": "multipart/form-data; boundary=" + B}, origin=False)
    assert s == 403
    assert not os.path.isdir(ui.root) or os.listdir(ui.root) == []
    for hdr in ({"CF-Connecting-IP": "203.0.113.9"}, {"X-Forwarded-For": "8.8.8.8"}, {"Cf-Access-Jwt-Assertion": "x.y.z"}):
        for path in ("/", "/api/jobs"):
            assert ui.req("GET", path, headers=hdr)[0] == 200, (hdr, path)
    assert ui.req("GET", "/", headers={"Host": "queue.example.com"})[0] == 200


def test_public_client_address_accepted(ui, monkeypatch):
    handler = ui.srv.RequestHandlerClass
    monkeypatch.setattr(handler, "client_ip", lambda self: "8.8.8.8")
    assert ui.req("GET", "/")[0] == 200
    assert ui.req("GET", "/api/jobs")[0] == 200


@pytest.mark.parametrize("bad", ["..", "../x", "%2e%2e", "ABCDEF12", "abcdef1", "abcdef123", "abcdef1g", "abcdef12/../..", "a" * 64, "abcd%00ef"])
def test_traversal_and_malformed_ids_rejected(ui, tmp_path, bad):
    secret = Path(ui.root).parent / "outside-secret"
    secret.mkdir(exist_ok=True)
    (secret / "summary.pdf").write_bytes(b"%PDF-SECRET")
    for kind in ("pdf", "md"):
        s, _, body = ui.req("GET", "/job/%s/%s" % (bad, kind))
        assert s in (400, 404) and b"SECRET" not in body
    s, _, body = ui.req("POST", "/api/delete/" + bad)
    assert s in (400, 404)
    assert secret.exists() and (secret / "summary.pdf").exists()


def test_symlinked_job_dir_is_not_deleted_or_followed(ui, tmp_path):
    os.makedirs(ui.root, mode=0o700, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "summary.pdf").write_bytes(b"%PDF-X")
    os.symlink(str(target), os.path.join(ui.root, "deadbeef"))
    assert ui.req("POST", "/api/delete/deadbeef")[0] == 404
    assert (target / "summary.pdf").exists()


def test_safe_name():
    assert docsum_server.safe_name("../../etc/passwd.pdf") == "passwd.pdf"
    assert docsum_server.safe_name("C:\\x\\y.PDF") == "y.PDF"
    assert docsum_server.safe_name("") == "document.pdf" and docsum_server.safe_name("a\x00\n.txt") == "document.pdf"


def test_full_flow_smoke_over_http_with_log_hygiene(tmp_path):
    q = FakeQueue(autostart=False)
    u = UI(tmp_path, queue=q)
    try:
        assert u.req("GET", "/", headers={"CF-Connecting-IP": "203.0.113.9"})[0] == 200

        pdf = synth_pdf(tmp_path)
        s, _, b = u.upload(pdf)
        assert s == 200
        jid = json.loads(b)["id"]
        d = Path(u.root) / jid
        assert stat.S_IMODE(os.stat(u.root).st_mode) == 0o700 and stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert json.loads((d / "meta.json").read_text())["src_name"] == CANARY_NAME + ".pdf"  # original name lives only here
        assert stat.S_IMODE(os.stat(d / "input.pdf").st_mode) == 0o600

        queued = u.wait_done(jid, want=("queued",))
        assert queued["status"] == "queued" and queued["name"] == CANARY_NAME + ".pdf"
        assert queued["has_pdf"] is False
        assert u.req("GET", "/job/%s/pdf" % jid)[0] == 404

        q.go.set()
        done = u.wait_done(jid)
        assert done["status"] == "done" and done["pages"] == 2 and done["has_pdf"] and done["has_md"] and done["code"] is None

        # download: opaque Content-Disposition names, real content
        s, h, body = u.req("GET", "/job/%s/pdf" % jid)
        assert s == 200 and body.startswith(b"%PDF-") and h["Content-Disposition"] == 'attachment; filename="summary-%s.pdf"' % jid
        assert CANARY_NAME not in h["Content-Disposition"] and h["Content-Type"] == "application/pdf"
        s, h, body = u.req("GET", "/job/%s/md" % jid)
        assert s == 200 and b"## Bottom line" in body and h["Content-Disposition"] == 'attachment; filename="summary-%s.md"' % jid

        # log hygiene: canaries (file name, scenario, PDF text) appear nowhere Claude could read
        logtxt = "\n".join(u.log)
        rowtxt = json.dumps(q.rows()) + json.dumps(q.cmds)
        runner_out = q.stdout.getvalue()
        assert u.log and "route=upload" in logtxt and jid in logtxt
        for canary in (CANARY_DOC, CANARY_SC, CANARY_NAME):
            for label, blob in (("server log", logtxt), ("queue rows", rowtxt), ("runner stdout/done.json", runner_out)):
                assert canary not in blob, (canary, label)
        assert "docsum-" + jid in rowtxt  # the label is the opaque id only
        for line in u.log:
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d route=\w+ (status=\d+ bytes=\d+ job=[0-9a-f-]+|port=\d+|exc=\w+)", line), line

        # delete wipes the whole job dir
        s, _, b = u.req("POST", "/api/delete/" + jid)
        assert s == 200 and json.loads(b) == {"deleted": jid}
        assert not d.exists() and u.resolved == [jid]
        assert json.loads(u.req("GET", "/api/jobs")[2]) == {"jobs": []}
        assert u.req("GET", "/job/%s/pdf" % jid)[0] == 404
    finally:
        u.close()


def test_failed_job_reports_code_only(tmp_path):
    u = UI(tmp_path)
    try:
        scanned = synth_pdf(tmp_path, ["", "", ""], "scan.pdf")
        jid = json.loads(u.upload(scanned)[2])["id"]
        j = u.wait_done(jid)
        assert j["status"] == "failed" and j["code"] == "E_SCANNED_NO_OCR" and not j["has_pdf"]
        assert CANARY_SC not in json.dumps({k: v for k, v in j.items() if k != "name"})
    finally:
        u.close()


def test_handle_error_logs_class_name_only(tmp_path):
    u = UI(tmp_path)
    try:
        try:
            raise RuntimeError("secret " + CANARY_SC)
        except RuntimeError:
            u.srv.handle_error(None, ("127.0.0.1", 1))
        assert any("exc=RuntimeError" in l for l in u.log) and CANARY_SC not in "\n".join(u.log)
    finally:
        u.close()


def test_log_message_never_prints(ui, capsys):
    ui.req("GET", "/api/jobs?q=" + CANARY_SC)
    err = capsys.readouterr()
    assert CANARY_SC not in err.out + err.err and CANARY_SC not in "\n".join(ui.log)


def test_default_port_is_free_near_7690():
    assert docsum_server.DEFAULT_PORT == 7690


def test_real_enqueue_builds_cli_command_with_opaque_label(monkeypatch, tmp_path):
    seen = []

    class R:
        returncode = 0
    monkeypatch.setattr(docsum_server.subprocess, "run", lambda cmd, **k: seen.append(cmd) or R())
    job = {"job_id": "0a1b2c3d", "dir": str(tmp_path / "0a1b2c3d"), "task_file": str(tmp_path / "0a1b2c3d" / "task.json")}
    assert docsum_server.enqueue_job(job, MODEL) is True
    cmd = seen[0]
    assert cmd[cmd.index("--label") + 1] == "docsum-0a1b2c3d" and cmd[cmd.index("--bundle") + 1] == "docsum"
    assert cmd[cmd.index("--runner") + 1].endswith("docsum_run.py") and cmd[cmd.index("--model") + 1] == MODEL
    assert CANARY_NAME not in " ".join(cmd) and CANARY_SC not in " ".join(cmd)


def test_handler_has_socket_timeout(ui):
    assert docsum_server.make_handler(ui.cfg).timeout == 30


def test_upload_concurrency_cap_returns_busy(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    held = []

    def slow_enqueue(job, model):
        held.append(1)
        entered.set()
        gate.wait(10)
        return True
    u = UI(tmp_path)
    u.cfg.enqueue = slow_enqueue
    pdf = synth_pdf(tmp_path)
    try:
        assert docsum_server.MAX_CONCURRENT_UPLOADS == 2
        res = []
        ts = [threading.Thread(target=lambda: res.append(u.upload(pdf)[0])) for _ in range(2)]
        for t in ts:
            t.start()
        end = time.time() + 10
        while len(held) < 2 and time.time() < end:
            time.sleep(0.02)
        assert len(held) == 2
        assert u.upload(pdf)[0] == 429            # third concurrent upload refused
        gate.set()
        for t in ts:
            t.join(10)
        assert res == [200, 200]
        assert u.upload(pdf)[0] == 200            # slots were released
    finally:
        gate.set()
        u.close()


@pytest.mark.parametrize("status,verb", [("failed", ["resolve"]), ("error", ["resolve"]), ("needs_opus", ["resolve"]),
                                         ("running", ["cancel", "--force"]), ("pending", ["cancel", "--force"]),
                                         ("queued", ["cancel", "--force"]), ("paused", ["cancel", "--force"])])
def test_dispose_row_cancels_or_resolves_by_status(status, verb):
    calls = []
    rows = lambda: [{"id": "q1", "label": "docsum-0a1b2c3d", "status": status},
                    {"id": "q2", "label": "docsum-ffffffff", "status": status}, {"id": "q3", "label": "other", "status": status}]
    docsum_server.dispose_row("0a1b2c3d", rows=rows, run=calls.append, clear=lambda q: None)
    assert len(calls) == 1 and calls[0][2:] == verb + ["q1"]


@pytest.mark.parametrize("status", ["done", "completed", "finished"])
def test_dispose_row_never_force_stops_finished(status):
    calls, cleared = [], []
    rows = lambda: [{"id": "q1", "label": "docsum-0a1b2c3d", "status": status}]
    docsum_server.dispose_row("0a1b2c3d", rows=rows, run=calls.append, clear=cleared.append)
    assert calls == [] and cleared == ["q1"]


def test_delete_disposes_row_before_wiping_dir(tmp_path):
    u = UI(tmp_path)
    order = []
    jid = "0a1b2c3d"
    d = Path(u.root) / jid
    d.mkdir(parents=True, mode=0o700)
    (d / "meta.json").write_text("{}")
    u.cfg.resolve = lambda j, **kw: order.append((j, d.exists()))
    try:
        assert u.req("POST", "/api/delete/" + jid)[0] == 200
        assert order == [(jid, True)] and not d.exists()
    finally:
        u.close()


def test_dispose_row_done_waits_for_runner_instead_of_force_stop():
    calls, cleared, state = [], [], ["running", "running", "done"]
    rows = lambda: [{"id": "q1", "label": "docsum-0a1b2c3d", "status": state.pop(0) if len(state) > 1 else state[0]}]
    docsum_server.dispose_row("0a1b2c3d", rows=rows, run=calls.append, clear=cleared.append, done=True, sleep=lambda s: None)
    assert calls == [] and cleared == ["q1"]


def test_dispose_row_done_still_running_is_left_alone():
    calls, cleared = [], []
    rows = lambda: [{"id": "q1", "label": "docsum-0a1b2c3d", "status": "running"}]
    docsum_server.dispose_row("0a1b2c3d", rows=rows, run=calls.append, clear=cleared.append, done=True, wait=3, sleep=lambda s: None)
    assert calls == [] and cleared == []
