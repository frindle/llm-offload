#!/usr/bin/env python3
"""Tests for digest lines (2026-11-16): digest_lines.py (POST /api/digest-lines rules and storage), the error digest's
per-app sections (error_digest.py) and the API route wiring (ollama-queue-api.py)."""
import datetime as dt
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import digest_lines as L  # noqa: E402
import error_digest as ED  # noqa: E402

FAILS, N = [], [0]
TOK = "d" * 32
AUTH = {"Authorization": "Bearer " + TOK}
TODAY = dt.date(2026, 11, 16)


def chk(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name, detail)


def post(ddir, body, headers=AUTH, path="/api/digest-lines", method="POST", today=TODAY):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return L.handle(method, path, headers, raw, ddir=ddir, today=today)


def body(**kw):
    d = {"app": "resell", "day": "2026-11-16", "lines": ["3 orders synced", "1 pending review"]}
    d.update(kw)
    return d


def t_auth():
    os.environ.pop("QUEUE_DIGEST_TOKEN", None)
    os.environ.pop("QUEUE_DIGEST_TOKEN_FILE", None)
    with tempfile.TemporaryDirectory() as td:
        s, o = post(td, body())
        chk("no token on the queue -> 503", s == 503 and o["error"] == "E_OFF", (s, o))
        os.environ["QUEUE_DIGEST_TOKEN"] = "short"
        chk("a short token counts as none", post(td, body())[0] == 503)
        os.environ["QUEUE_DIGEST_TOKEN"] = TOK
        chk("no header -> 401", post(td, body(), headers={})[0] == 401)
        chk("wrong token -> 401", post(td, body(), headers={"Authorization": "Bearer " + "x" * 32})[0] == 401)
        chk("prompt token is not this token", post(td, body(), headers={"Authorization": "Basic " + TOK})[0] == 401)
        s, o = post(td, body())
        chk("right token -> 200", s == 200 and o == {"ok": True, "day": "2026-11-16", "lines": 2}, (s, o))
        f = Path(td) / "tok"
        f.write_text(TOK + "\n")
        os.environ.pop("QUEUE_DIGEST_TOKEN")
        os.environ["QUEUE_DIGEST_TOKEN_FILE"] = str(f)
        chk("token from the file", post(td, body())[0] == 200)
        os.environ.pop("QUEUE_DIGEST_TOKEN_FILE")
        os.environ["QUEUE_DIGEST_TOKEN"] = TOK


def t_validate():
    with tempfile.TemporaryDirectory() as td:
        def bad(name, b, want="E_BODY", code=400, detail=None):
            s, o = post(td, b)
            chk(name, s == code and o.get("error") == want and (detail is None or detail in o.get("detail", "")), (s, o))
        bad("not JSON", b"{nope")
        bad("not an object", b"[1]")
        bad("unknown app", body(app="plex"), detail="unknown app")
        bad("bad day", body(day="11/16/2026"))
        bad("a compact day (fromisoformat takes it; the file name would not match)", body(day="20261116"))
        bad("a week day", body(day="2026-W47-1"))
        bad("day 3 back", body(day="2026-11-13"))
        bad("day in the future", body(day="2026-11-17"))
        chk("day 2 back ok", post(td, body(day="2026-11-14"))[0] == 200)
        bad("lines not a list", body(lines="3 orders"))
        bad("11 lines", body(lines=["n"] * 11))
        bad("a non-string line", body(lines=["ok", 3]))
        bad("201 chars", body(lines=["x" * 201]), detail="line 1 is over")
        chk("200 chars ok", post(td, body(lines=["x" * 200]))[0] == 200)
        bad("an email", body(lines=["ok", "mail a@b.c"]), want="E_PRIVATE", detail="line 2 looks private")
        bad("an amount", body(lines=["total $12"]), want="E_PRIVATE")
        bad("an order number", body(lines=["order 1123456 synced"]), want="E_PRIVATE")
        bad("an order number with dashes", body(lines=["112-1234567-7654321"]), want="E_PRIVATE")
        chk("5 digits ok (a count)", post(td, body(lines=["12345 items scanned"]))[0] == 200)
        bad("an amount with cents", body(lines=["total 12.99"]), want="E_PRIVATE")
        bad("an amount with a comma", body(lines=["total 12,99"]), want="E_PRIVATE")
        bad("a currency word", body(lines=["total 12 USD"]), want="E_PRIVATE")
        bad("dollars", body(lines=["12 dollars"]), want="E_PRIVATE")
        bad("a euro sign", body(lines=["\u20ac12"]), want="E_PRIVATE")
        bad("a pound sign", body(lines=["\u00a312"]), want="E_PRIVATE")
        resell = ["3 new orders (amazon 2, walmart 1).", "1 new order with no card set.",
                  "Order syncs: 4 runs, 12 orders added or updated.", "2 gift cards submitted to CardCenter.",
                  "Waiting on tracking: 3 orders, 1 past the ship-by date.", "Unread API errors: 2.",
                  "1,000 items scanned"]
        chk("resell's own line shapes and a thousands count still pass", post(td, body(lines=resell))[0] == 200)
        bad("needs_you not a bool", body(needs_you="yes"))
        chk("413 over the body cap", post(td, b" " * (L.MAX_BODY + 1))[0] == 413)
        chk("other path -> 404", post(td, body(), path="/api/digest-lines/x")[0] == 404)
        chk("GET -> 405", post(td, body(), method="GET")[0] == 405)
        os.environ["DIGEST_LINES_APPS"] = "resell, bloom-ledger"
        chk("apps list from env", post(td, body(app="bloom-ledger"))[0] == 200)
        os.environ.pop("DIGEST_LINES_APPS")


def t_store():
    with tempfile.TemporaryDirectory() as td:
        post(td, body(lines=["first"]))
        post(td, body(lines=["  3 orders\tsynced\n", "", "1 pending"], needs_you=True))
        f = Path(td) / "extra" / "resell-2026-11-16.json"
        chk("file 0600", stat.S_IMODE(f.stat().st_mode) == 0o600, oct(f.stat().st_mode))
        chk("dir 0700", stat.S_IMODE(f.parent.stat().st_mode) == 0o700)
        r = L.read_day("2026-11-16", td)
        chk("last POST wins, control chars and blank lines cleaned",
            r == [{"app": "resell", "title": "Resell", "lines": ["3 orders synced", "1 pending"], "needs_you": True}], r)
        post(td, body(lines=[]))
        chk("[] clears the day", L.read_day("2026-11-16", td) == [])
        chk("another day: nothing", L.read_day("2026-11-15", td) == [])
        (Path(td) / "extra" / "resell-2026-11-15.json").write_text("{broken")
        chk("a broken file is skipped", L.read_day("2026-11-15", td) == [])
        os.environ["DIGEST_LINES_APPS"] = "resell,bloom-ledger"
        (Path(td) / "extra" / "resell-2026-11-15.json").write_text("[1, 2]")          # JSON, but not an object
        (Path(td) / "extra" / "bloom-ledger-2026-11-15.json").write_text(json.dumps({"lines": ["2 runs"]}))
        r = L.read_day("2026-11-15", td)
        chk("one app's bad file never drops the next app's lines", [x["app"] for x in r] == ["bloom-ledger"], r)
        os.environ.pop("DIGEST_LINES_APPS")
        (Path(td) / "extra" / "plex-2026-11-16.json").write_text(json.dumps({"lines": ["x"]}))
        chk("an app not allowed is not read", L.read_day("2026-11-16", td) == [])
        old = Path(td) / "extra" / "resell-2026-10-01.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 20 * 86400,) * 2)
        post(td, body())
        chk("old files swept", not old.exists())
        chk("read_day never raises", L.read_day("2026-11-16", "/nonexistent/x") == [])


def rec(owner=(), extra=None):
    al = {"owner": [{"what": "A RAR unpack failed", "containers": ["arr-webhook"], "count": 1,
                     "first_ns": 0, "last_ns": 0, "sample": "boom"} for _ in owner],
          "held": [], "claude": [], "new_for_claude": []}
    r = {"day": "2026-11-16", "alerts": al, "containers": {}}
    if extra is not None:
        r["extra"] = extra
    return r


def t_digest():
    ex = [{"app": "resell", "title": "Resell", "lines": ["3 orders synced"], "needs_you": False}]
    s0, t0 = ED.notify_text(rec())
    s1, t1 = ED.notify_text(rec(extra=ex))
    chk("subject unchanged without needs_you", s0 == s1 == "Unraid Mon Nov 16: nothing needs you", (s0, s1))
    chk("no extra: old text", "Nothing needs you today." in t0 and "Resell" not in t0)
    chk("section with its lines", "\nResell:\n- 3 orders synced\n" in t1 and "No errors need you today." in t1, t1)
    chk("section before the counts line", t1.index("Resell:") < t1.index("Also logged"))
    exn = [dict(ex[0], needs_you=True)]
    s2, t2 = ED.notify_text(rec(extra=exn))
    chk("needs_you in the subject", s2 == "Unraid Mon Nov 16: Resell needs you", s2)
    chk("needs_you in the section", "Resell (needs you):" in t2)
    s3, _ = ED.notify_text(rec(owner=[1], extra=exn))
    chk("needs_you + an error", s3 == "Unraid Mon Nov 16: Resell needs you; 1 thing for you", s3)
    md = "\n".join(ED.render_extra(ex))
    chk("md section", md.startswith("## Resell\n\n- 3 orders synced"), md)
    chk("md nothing when none", ED.render_extra(None) == [])
    src = (BIN / "error_digest.py").read_text()
    chk("cmd_run reads the day's lines", "digest_lines.read_day(day, ddir)" in src)
    chk("render adds the sections", "out += render_extra(rec.get(\"extra\"))" in src)


def t_api_wiring():
    src = (BIN / "ollama-queue-api.py").read_text()
    post_src = src[src.index("    def do_POST(self):"):src.index("    def do_DELETE(self):")]
    chk("POST route before the queue write auth",
        post_src.index("_is_digest_lines_path(self.path)") < post_src.index("self._require_write_auth()"))
    chk("POST route after the drain gate", post_src.index("self._drain_gate()") < post_src.index("_is_digest_lines_path"))
    ns = {}
    start = src.index("def _is_digest_lines_path(")
    exec(src[start:src.index("\n\n\n", start)], ns)   # just the function: module globals may follow it
    f = ns["_is_digest_lines_path"]
    chk("path match", f("/api/digest-lines") and f("/api/digest-lines/") and f("/api/digest-lines?x=1"))
    chk("no prefix match", not f("/api/digest-linesX") and not f("/api/digest-lines/abc") and not f("/api/jobs"))
    m = (BIN / "queue-image" / "MANIFEST")
    if m.exists():
        chk("in the image MANIFEST", "\ndigest_lines.py\n" in m.read_text())


os.environ["QUEUE_DIGEST_TOKEN"] = TOK
for t in (t_auth, t_validate, t_store, t_digest, t_api_wiring):
    try:
        t()
    except Exception as e:  # noqa: BLE001
        FAILS.append(t.__name__)
        import traceback
        traceback.print_exc()
        print("FAIL", t.__name__, "raised", type(e).__name__, e)
print("ALL PASS (%d checks)" % N[0] if not FAILS else "%d FAILED of %d" % (len(FAILS), N[0]))
sys.exit(1 if FAILS else 0)
