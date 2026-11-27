#!/usr/bin/env python3
"""Synthetic: the Work Board (src/board.py) and its routes in ollama-queue-api.py. Temp BOARD_DIR only.
Run: python3 tests/test_board.py   (or pytest)"""
import importlib.util, json, os, sys, tempfile, types
from pathlib import Path

SRC = Path(os.environ.get("DASHBOARD_SRC") or Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, str(SRC))
TMP = tempfile.mkdtemp(prefix="board-test-")
os.environ["BOARD_DIR"] = TMP
import board  # noqa: E402

H = {"Content-Type": "application/json", "X-Requested-With": "board"}
T0 = 1_800_000_000.0


def reset():
    for f in Path(TMP).iterdir():
        f.unlink()


def post(route, data, now=T0, headers=H):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    return board.handle("POST", "/api/board/" + route, headers, body, now=now)


def get(path, now=T0):
    return board.handle("GET", path, {}, b"", now=now)


def test_set_keeps_fields_and_needs_moves_status():
    reset()
    st, r = post("set", {"lane": "offload", "project": "plex-automation", "item": "e2e", "next": "deploy", "by": "s1"})
    assert st == 200 and r["lane"]["status"] == "active" and r["lane"]["project"] == "plex-automation", r
    st, r = post("set", {"lane": "offload", "needs": "OK to delete?"}, now=T0 + 5)
    assert r["lane"]["status"] == "needs-you" and r["lane"]["item"] == "e2e" and r["lane"]["next"] == "deploy"
    assert r["lane"]["by"] == "s1" and r["lane"]["updated"] == T0 + 5
    st, r = post("set", {"lane": "offload", "needs": ""}, now=T0 + 6)
    assert r["lane"]["status"] == "active" and r["lane"]["needs"] == "", r
    st, r = post("set", {"lane": "offload", "status": "blocked", "needs": "creds"})
    assert r["lane"]["status"] == "blocked"
    st, r = post("set", {"lane": "offload", "needs": "still creds"})
    assert r["lane"]["status"] == "blocked", "an explicit blocked status is kept"
    st, r = post("set", {"lane": "offload", "item": "e2e"})
    assert r["changed"] == [], r


def test_validation():
    reset()
    bad = [
        ({"lane": "Bad Lane"}, 400), ({"lane": ""}, 400), ({"lane": "a" * 49}, 400), ({}, 400),
        ({"lane": "x", "colour": "red"}, 400), ({"lane": "x", "status": "done"}, 400),
        ({"lane": "x", "status": "weird"}, 400), ({"lane": "x", "item": 5}, 400),
        ({"lane": "x", "project": "p"}, 200),
    ]
    for data, want in bad:
        assert post("set", data)[0] == want, data
    assert post("done", {"lane": "x", "item": "nope"})[0] == 400, "done takes no item"
    assert post("set", b"[1]")[0] == 400
    assert post("set", b"{nope")[0] == 400
    assert post("set", {"lane": "x"}, headers={"Content-Type": "text/plain", "X-Requested-With": "board"})[0] == 415
    assert post("set", {"lane": "x"}, headers={"Content-Type": "application/json"})[0] == 403
    assert post("set", b"{" + b" " * board.MAX_BODY + b"}")[0] == 413
    assert board.handle("POST", "/api/board/zap", H, b"{}")[0] == 404
    assert board.handle("DELETE", "/api/board/set", H, b"{}")[0] == 404


def test_clean_and_caps():
    reset()
    st, r = post("set", {"lane": "c", "item": "a\x1b[31mb\x07\n c  ", "next": "n" * 5000})
    assert r["lane"]["item"] == "a[31mb\n c" and len(r["lane"]["next"]) == board.CAPS["next"], r["lane"]


def test_done_prune_and_order():
    reset()
    post("set", {"lane": "old", "item": "a"}, now=T0)
    post("set", {"lane": "busy", "item": "b"}, now=T0 + 10)
    post("set", {"lane": "ask", "needs": "yes?"}, now=T0 + 1)
    st, r = post("done", {"lane": "old", "report": "all green"}, now=T0 + 20)
    assert st == 200 and r["lane"]["status"] == "done" and r["lane"]["report"] == "all green"
    assert post("done", {"lane": "nope"})[0] == 404
    st, r = post("done", {"lane": "ask"}, now=T0 + 21)
    assert r["lane"]["needs"] == "" and r["lane"]["status"] == "done", "closing clears the ask"
    post("set", {"lane": "ask2", "needs": "q"}, now=T0 + 2)
    lanes = get("/api/board", now=T0 + 30)[1]["lanes"]
    assert [l["lane"] for l in lanes] == ["ask2", "busy", "ask", "old"], [l["lane"] for l in lanes]
    lanes = get("/api/board", now=T0 + 20 + board.DONE_KEEP_S + 1)[1]["lanes"]
    assert "old" not in [l["lane"] for l in lanes] and "busy" in [l["lane"] for l in lanes]
    st, r = post("set", {"lane": "busy", "item": "again"}, now=T0 + 40)
    st, r = post("done", {"lane": "busy"}, now=T0 + 41)
    st, r = post("set", {"lane": "busy", "next": "reopened"}, now=T0 + 42)
    assert r["lane"]["status"] == "active" and r["lane"].get("item", "") == "" and r["lane"]["created"] == T0 + 42


def test_open_cap():
    reset()
    for i in range(board.MAX_OPEN):
        assert post("set", {"lane": f"l{i}"})[0] == 200
    assert post("set", {"lane": "one-more"})[0] == 409
    assert post("set", {"lane": "l0", "item": "update is fine"})[0] == 200
    post("done", {"lane": "l0"})
    assert post("set", {"lane": "one-more"})[0] == 200


def test_log():
    reset()
    post("set", {"lane": "a", "item": "1", "by": "s"}, now=T0)
    post("set", {"lane": "b", "item": "x"}, now=T0 + 1)
    post("set", {"lane": "a", "item": "2", "report": "long"}, now=T0 + 2)
    st, r = get("/api/board/log?lane=a")
    assert st == 200 and [e["changes"].get("item") for e in r["log"]] == ["2", "1"], r
    assert r["log"][0]["changes"]["report"] == "long"
    assert len(get("/api/board/log?limit=1")[1]["log"]) == 1
    assert get("/api/board/log?limit=x")[0] == 400
    old = board.LOG_ROTATE
    board.LOG_ROTATE = 10
    try:
        post("set", {"lane": "a", "item": "3"}, now=T0 + 3)
        assert (Path(TMP) / "board-log.jsonl.1").exists()
        items = [e["changes"].get("item") for e in get("/api/board/log?lane=a")[1]["log"]]
        assert items[:2] == ["3", "2"], items
    finally:
        board.LOG_ROTATE = old


def test_damaged_files():
    reset()
    (Path(TMP) / "board.json").write_text("{not json")
    assert get("/api/board")[1]["lanes"] == []
    assert post("set", {"lane": "a"})[0] == 200
    (Path(TMP) / "board-log.jsonl").write_text('half a li\n{"ts": 1, "lane": "a", "changes": {}}\n')
    assert len(get("/api/board/log?lane=a")[1]["log"]) == 1


def test_page_escapes():
    assert "innerHTML" not in board.PAGE_HTML, "lane text must go in via textContent"
    assert "prefers-color-scheme: dark" in board.PAGE_HTML and 'name="viewport"' in board.PAGE_HTML


# --- routes in ollama-queue-api.py
def _api():
    sys.argv = ["x"]
    spec = importlib.util.spec_from_file_location("api_board_route", str(SRC / "ollama-queue-api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_routes():
    api = _api()
    assert api._is_board_path("/board") and api._is_board_path("/board/") and api._is_board_path("/api/board")
    assert api._is_board_path("/api/board/log?lane=a") and not api._is_board_path("/boardx")
    assert not api._is_board_path("/api/boards")
    assert 'href="/board"' in api.FRONTEND_HTML
    Hd = next(v for v in vars(api).values() if isinstance(v, type) and hasattr(v, "log_message") and hasattr(v, "do_GET"))
    calls = []

    def mk(path, cmd):
        h = Hd.__new__(Hd)
        h.path, h.command = path, cmd
        h.client_address = ("127.0.0.1", 1)
        h.headers = {}
        h._board = lambda m: calls.append((m, path))
        return h

    for path in ("/board", "/api/board", "/api/board/log?lane=a"):
        calls.clear()
        mk(path, "GET").do_GET()
        assert calls == [("GET", path)], (path, calls)
    # A POST only reaches the board after write auth passes.
    for ok in (True, False):
        calls.clear()
        h = mk("/api/board/set", "POST")
        h._standby_gate = lambda: False
        h._drain_gate = lambda: False
        h._require_write_auth = lambda ok=ok: ok
        h.do_POST()
        assert calls == ([("POST", "/api/board/set")] if ok else []), (ok, calls)


def test_board_wrapper_serves_page_and_json():
    api = _api()
    Hd = next(v for v in vars(api).values() if isinstance(v, type) and hasattr(v, "log_message") and hasattr(v, "do_GET"))
    out = []
    h = Hd.__new__(Hd)
    h.headers = {}
    h._html = lambda html: out.append(("html", html))
    h._json = lambda obj, status=200: out.append(("json", status, obj))
    h.path = "/board"
    h._board("GET")
    assert out[-1][0] == "html" and "Work Board" in out[-1][1]
    reset()
    h.path = "/api/board"
    h._board("GET")
    assert out[-1][:2] == ("json", 200) and out[-1][2]["lanes"] == []
    h.headers = {"Content-Length": str(board.MAX_BODY + 1)}
    h.path = "/api/board/set"
    h._board("POST")
    assert out[-1][1] == 413


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
