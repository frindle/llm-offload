import io
import os
import socket
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_bypass_upstream as pu

KEY = "UPSTREAM-KEY-123"
BODY = b'{"messages": [{"role": "user", "content": "hi"}]}'


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status = data, status

    def read(self):
        return self._d

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, fn):
    calls = []

    def fake(req, timeout=None, **kw):
        calls.append((req, timeout))
        return fn(req)
    monkeypatch.setattr(pu.urllib.request, "urlopen", fake)
    return calls


def test_success_forwards_body_with_bearer_key_and_20s_timeout(monkeypatch):
    calls = _patch(monkeypatch, lambda r: _Resp(b'{"choices": []}'))
    assert pu.forward_chat(BODY, KEY) == (200, b'{"choices": []}')
    req, timeout = calls[0]
    assert req.full_url == "http://127.0.0.1:8000/v1/chat/completions"
    assert req.get_method() == "POST" and req.data == BODY
    assert req.get_header("Authorization") == "Bearer " + KEY
    assert req.get_header("Content-type") == "application/json"
    assert timeout == 20 and pu.TIMEOUT_S == 20


def test_timeouts_are_504(monkeypatch):
    def boom(r):
        raise socket.timeout("timed out")
    _patch(monkeypatch, boom)
    assert pu.forward_chat(BODY, KEY)[0] == 504

    def boom2(r):
        raise urllib.error.URLError(socket.timeout("timed out"))
    _patch(monkeypatch, boom2)
    assert pu.forward_chat(BODY, KEY)[0] == 504


def test_other_upstream_failures_are_502_and_never_leak_the_key(monkeypatch):
    def refused(r):
        raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))

    def http500(r):
        raise urllib.error.HTTPError(r.full_url, 500, "boom", {}, io.BytesIO(b"upstream said " + KEY.encode()))

    def http401(r):
        raise urllib.error.HTTPError(r.full_url, 401, "no", {}, io.BytesIO(b"bad key"))
    for fn in (refused, http500, http401, lambda r: (_ for _ in ()).throw(ValueError("x"))):
        _patch(monkeypatch, fn)
        st, payload = pu.forward_chat(BODY, KEY)
        assert st == 502
        assert isinstance(payload, bytes) and KEY.encode() not in payload


def test_at_most_four_concurrent_and_slots_are_released(monkeypatch):
    held = [pu._SLOTS.acquire(blocking=False) for _ in range(4)]
    assert all(held)
    calls = _patch(monkeypatch, lambda r: _Resp(b"{}"))
    assert pu.forward_chat(BODY, KEY)[0] == 503
    assert calls == []
    for _ in held:
        pu._SLOTS.release()
    assert pu.forward_chat(BODY, KEY)[0] == 200
    _patch(monkeypatch, lambda r: (_ for _ in ()).throw(urllib.error.URLError("down")))
    for _ in range(6):
        assert pu.forward_chat(BODY, KEY)[0] == 502   # a failing call must free its slot
    assert pu.forward_chat.__name__ == "forward_chat"


def test_non_200_upstream_status_is_502(monkeypatch):
    _patch(monkeypatch, lambda r: _Resp(b"odd", status=204))
    assert pu.forward_chat(BODY, KEY)[0] == 502
