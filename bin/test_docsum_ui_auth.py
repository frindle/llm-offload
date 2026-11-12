"""No-auth behavior of the docsum web UI (no pairing, token or cookie), slow-upload deadline, quiet polling log."""
import json
import os
import socket
import time

import docsum_server
from test_docsum_ui_server import UI, B, form, synth_pdf, MODEL  # noqa: F401  (fixtures reuse the in-process server)


def mk(tmp_path, **kw):
    u = UI(tmp_path)
    for k, v in kw.items():
        setattr(u.cfg, k, v)
    return u


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.02)
    return pred()


def test_no_token_or_cookie_needed(tmp_path):
    u = mk(tmp_path)
    try:
        s, _, b = u.req("GET", "/")
        assert s == 200 and b"<html" in b.lower()
        assert u.req("GET", "/api/jobs")[0] == 200
        assert u.req("GET", "/pair?t=x")[0] == 404
        assert u.req("GET", "/job/0a1b2c3d/pdf")[0] == 404
        assert u.req("GET", "/nope")[0] == 404
        assert not os.path.exists(os.path.join(u.root, ".ui-token"))
        assert not hasattr(docsum_server, "load_or_create_token")
    finally:
        u.close()


def test_proxy_style_headers_accepted(tmp_path):
    u = mk(tmp_path)
    try:
        h = {"CF-Connecting-IP": "1.2.3.4", "Cf-Access-Jwt-Assertion": "x", "X-Forwarded-For": "8.8.8.8"}
        assert u.req("GET", "/", headers=h)[0] == 200
        assert u.upload(synth_pdf(tmp_path))[0] == 200
    finally:
        u.close()


def test_cross_origin_post_still_refused(tmp_path):
    u = mk(tmp_path)
    try:
        s, _, b = u.req("POST", "/api/delete/0a1b2c3d", headers={"Origin": "http://evil.example"})
        assert (s, json.loads(b)) == (403, {"error": "E_CROSS_ORIGIN"})
        s, _, _ = u.req("POST", "/api/upload", origin=False)
        assert s == 403
    finally:
        u.close()


def test_polling_200_not_logged_but_errors_are(tmp_path):
    u = mk(tmp_path)
    try:
        u.req("GET", "/api/jobs")
        assert not any("route=jobs" in l for l in u.log)
        u.req("GET", "/nope")
        assert wait_for(lambda: any("route=other status=404" in l for l in u.log))
    finally:
        u.close()


def test_stalled_upload_frees_its_slot(tmp_path):
    u = mk(tmp_path, read_deadline=1.0)
    socks = []
    try:
        for _ in range(2):                                        # two stalled clients take both slots
            s = socket.create_connection(("127.0.0.1", u.port))
            s.sendall(("POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                       "Content-Type: multipart/form-data; boundary=%s\r\nContent-Length: 5000\r\n\r\npartial"
                       % (u.port, u.port, B)).encode())
            socks.append(s)
        time.sleep(0.3)
        pdf = synth_pdf(tmp_path)
        assert u.upload(pdf)[0] == 429                            # slots are held while they stall
        for s in socks:
            s.settimeout(5)
            assert b" 408 " in s.recv(4096).split(b"\r\n")[0] + b" "
        assert u.upload(pdf)[0] == 200                            # deadline released the slots
    finally:
        for s in socks:
            s.close()
        u.close()
