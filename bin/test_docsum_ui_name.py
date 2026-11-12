"""Reference-name feature: sanitization, fallbacks, escaping, rename, and name-never-in-logs. Synthetic data only."""
import json
import os
from urllib.parse import unquote

import docsum_ui_name as N
from test_docsum_ui_server import UI, synth_pdf, form, B, MODEL  # noqa: F401
import pytest

SECRET = "ZXQ-REFNAME-SECRET-4410"


def test_clean_strips_path_control_quotes_and_caps():
    assert N.clean_name('../../etc/pass"wd\x00\n') == "etcpasswd"
    assert N.clean_name('a\\b/c"d') == "abcd"
    assert N.clean_name("x" * 300) == "x" * 100
    assert N.clean_name("  Lease  \t Q4 ") == "Lease Q4"
    assert N.clean_name(None) == "" and N.clean_name("\x07\x1b") == ""


def test_clean_keeps_unicode():
    assert N.clean_name("Müller – 契約書 📄") == "Müller – 契約書 📄"


def test_download_filename_unicode_and_quotes():
    h = N.download_filename('Mü"ller 契約', "abcd1234", "pdf")
    assert h.startswith('attachment; filename="M_ller __.pdf"') or 'filename="M' in h
    assert '"' not in h.split('filename="', 1)[1].split('"')[0]
    assert unquote(h.split("UTF-8''")[1]) == "Müller 契約.pdf"
    assert "\n" not in h and "\r" not in h


def test_download_filename_fallbacks():
    assert 'filename="summary-abcd1234.pdf"' in N.download_filename("", "abcd1234", "pdf")
    assert 'filename="summary-abcd1234.md"' in N.download_filename("//..", "abcd1234", "md")


def post_json(ui, path, obj):
    b = json.dumps(obj).encode()
    return ui.req("POST", path, b, {"Content-Type": "application/json", "Content-Length": str(len(b))})


def upload_named(ui, tmp_path, name):
    pdf = synth_pdf(tmp_path)
    body = form([("name", None, name.encode()), ("scenario", None, b"s"), ("file", "orig-file.pdf", pdf)])
    s, _, b = ui.req("POST", "/api/upload", body, {"Content-Type": "multipart/form-data; boundary=" + B, "Content-Length": str(len(body))})
    assert s == 200
    jid = json.loads(b)["id"]
    ui.wait_done(jid)
    return jid


def test_name_stored_listed_downloaded_and_not_logged(tmp_path):
    ui = UI(tmp_path)
    try:
        jid = upload_named(ui, tmp_path, SECRET + '<img src=x onerror=alert(1)>"')
        jobs = json.loads(ui.req("GET", "/api/jobs")[2])["jobs"]
        j = [x for x in jobs if x["id"] == jid][0]
        assert j["title"].startswith(SECRET) and j["name"] == "orig-file.pdf"
        s, h, _ = ui.req("GET", "/job/%s/pdf" % jid)
        assert s == 200 and SECRET in unquote(h["Content-Disposition"])
        assert "<" not in h["Content-Disposition"].split("filename*")[0]
        # queue labels / commands never carry the name
        assert SECRET not in json.dumps(ui.q.cmds) and SECRET not in json.dumps(ui.q.rows())
        assert os.stat(os.path.join(ui.root, jid, "meta.json")).st_mode & 0o777 == 0o600
        # rename, then clear
        s, _, b = post_json(ui, "/api/rename/" + jid, {"name": "New/Name"})
        assert s == 200 and json.loads(b)["name"] == "NewName"
        assert "NewName" in unquote(ui.req("GET", "/job/%s/md" % jid)[1]["Content-Disposition"])
        post_json(ui, "/api/rename/" + jid, {"name": ""})
        assert 'filename="summary-%s.pdf"' % jid in ui.req("GET", "/job/%s/pdf" % jid)[1]["Content-Disposition"]
        assert SECRET not in "\n".join(ui.log) and "NewName" not in "\n".join(ui.log) and "orig-file" not in "\n".join(ui.log)
    finally:
        ui.close()


def test_rename_guards(tmp_path):
    ui = UI(tmp_path)
    try:
        assert post_json(ui, "/api/rename/zzzz", {"name": "x"})[0] == 400
        assert post_json(ui, "/api/rename/deadbeef", {"name": "x"})[0] == 404
        b = b'{"name":"x"}'
        assert ui.req("POST", "/api/rename/deadbeef", b, {"Content-Length": str(len(b))}, origin=False)[0] == 403
        big = json.dumps({"name": "a" * 5000}).encode()
        assert ui.req("POST", "/api/rename/deadbeef", big, {"Content-Length": str(len(big))})[0] == 413
        assert post_json(ui, "/api/rename/deadbeef", {"name": 5})[0] == 400
    finally:
        ui.close()


def test_page_has_name_field_and_renders_via_textcontent():
    from docsum_ui_page import page_html
    p = page_html()
    assert 'id="refname"' in p and 'append("name"' in p and "tdn.textContent=jo.title" in p
    assert "innerHTML" not in p
