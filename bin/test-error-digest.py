#!/usr/bin/env python3
"""Tests for the nightly container error digest (error_digest.py + error-digest-runner.py), 2026-11-15.

Fake Loki and fake Ollama on 127.0.0.1 (http.server threads); synthetic log lines only. No queue, no network.
  python3 test-error-digest.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import contextlib
import datetime as dt
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
os.environ.pop("LOKI_TOKEN", None)
import error_digest as ED  # noqa: E402

RUN = SourceFileLoader("ed_runner", str(BIN / "error-digest-runner.py")).load_module()
FAILS, N = [], [0]


def chk(name, cond, detail=""):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "\n     " + str(detail)[-1500:]))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------------------------------------ fake servers
class FakeLoki(http.server.BaseHTTPRequestHandler):
    logs = {}          # container -> [(ns, line)]
    seen_auth = []
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        FakeLoki.seen_auth.append(self.headers.get("Authorization"))
        FakeLoki.calls.append((u.path, q))
        if self.headers.get("Authorization") != "Bearer tok-test":
            self.send_response(401)
            self.end_headers()
            return
        if u.path == "/loki/api/v1/query":
            t = int(q["time"])
            res = []
            for c, rows in FakeLoki.logs.items():
                n = sum(1 for ns, line in rows if ns <= t and ED.LOGQL_ERR and _errish(line))
                if n:
                    res.append({"metric": {"container": c}, "value": [t / 1e9, str(n)]})
            body = {"status": "success", "data": {"resultType": "vector", "result": res}}
        elif u.path == "/loki/api/v1/query_range":
            c = q["query"].split('container="', 1)[1].split('"', 1)[0]
            lo, hi, lim = int(q["start"]), int(q["end"]), int(q["limit"])
            rows = sorted((ns, l) for ns, l in FakeLoki.logs.get(c, []) if lo <= ns < hi and _errish(l))[:lim]
            body = {"status": "success", "data": {"resultType": "streams", "result": [
                {"stream": {"container": c}, "values": [[str(ns), l] for ns, l in rows]}]}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        b = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


def _errish(line):
    import re
    return re.search(ED.LOGQL_ERR.replace("(?i)", ""), line, re.I) is not None


class FakeOllama(http.server.BaseHTTPRequestHandler):
    answer = None      # callable(body) -> (code, dict)
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        FakeOllama.bodies.append(body)
        code, d = FakeOllama.answer(body)
        b = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def ns_at(day, hh, mm=0):
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, hh, mm).astimezone().timestamp() * 1e9)


def quiet(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **k)
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------------------------------------ tests
def test_signature_and_level():
    sig = ED.signature
    chk("numbers, times and addresses fold into one kind",
        sig("2026-11-13 10:00:01 db connect to 192.0.2.5:5432 failed after 3 tries")
        == sig("2026-11-14 22:13:59 db connect to 192.0.2.9:5432 failed after 17 tries"))
    chk("the leading timestamp is dropped from the kind", not sig("2026-11-13 10:00:01 boom failed").startswith("<time>"),
        sig("2026-11-13 10:00:01 boom failed"))
    s = sig("request failed token=abc123secretvalue api_key: xyz789 password=hunter2 for bob@example.com")
    chk("secrets and emails never reach the kind", all(x not in s for x in ("abc123", "xyz789", "hunter2", "bob@")), s)
    s = ED.shape("GET https://host.example/api/v1/items/123?apikey=SECRET1&x=2 failed")
    chk("a URL keeps its host and path shape, never its query string", "SECRET1" not in s and "?…" in s
        and "host.example" in s, s)
    chk("words with double letters survive the number collapse", "CPU_RUNNER_TOKEN" in sig("WARNING: CPU_RUNNER_TOKEN is not set"),
        sig("WARNING: CPU_RUNNER_TOKEN is not set"))
    chk("plain error codes are not blobs", "ERR_INTERNET_DISCONNECTED" in sig("load failed: net::ERR_INTERNET_DISCONNECTED"))
    chk("long random tokens are blobs", "<blob>" in sig("bad payload aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z failed"))
    chk("short quoted names fold ('libx.so' / 'liby.so' one kind)",
        sig("[WARN] Plugin 'libx.so' has been disabled") == sig("[WARN] Plugin 'liby_long.so' has been disabled"))
    j = sig('{"level":"warn","ts":1731500000.1,"logger":"http","msg":"HTTP/2 skipped","addr":":80"}')
    chk("a JSON log line reads as level logger: msg", j.startswith("warn http: HTTP/N skipped"), j)
    chk("short hashes fold ('8a3f1b2c' / '1234b567' one kind)",
        sig("history lookup for 8a3f1b2c failed") == sig("history lookup for 1234b567 failed"),
        (sig("history lookup for 8a3f1b2c failed"), sig("history lookup for 1234b567 failed")))
    chk("plain words and codec names are not short hashes", "decade" in sig("decade check for h264 failed"))
    chk("request paths on one host are one kind",
        sig('ERR Request failed error="context canceled" dest=https://storage.example.com/plugins/gpustat?x=1 type=http')
        == sig('ERR Request failed error="context canceled" dest=https://storage.example.com/Plugins type=http'))
    chk("different hosts stay different kinds",
        sig("ERR dest=https://a.example.com/x failed") != sig("ERR dest=https://b.example.com/x failed"))
    s = sig("[Warn] HTTP Error [GET] http://192.0.2.9:9117/api/v2.0/x/api?t=s&apikey=SECRET2&q=Some%20Show&season=2 failed")
    chk("a redacted key does not cut the URL short (the query after it goes too)",
        "SECRET2" not in s and "Some" not in s and "season" not in s, s)
    chk("quoted titles with spaces and apostrophes fold",
        sig("Couldn't add release 'Lincoln Rhyme S01 1080p WEBRip' from Indexer to download queue.")
        == sig("Couldn't add release 'Grey's Anatomy S20 720p' from Indexer to download queue."))
    chk("an apostrophe inside a word is not a quote", "add release" in sig("Couldn't add release now, failed"))
    chk("id=value fields fold", sig('lvl=warn msg="deprecated" log_id=0aBcDeFgHi') == sig('lvl=warn msg="deprecated" log_id=0ZzYyXxWw1'))
    lv = ED.level
    cases = [("[Warn] HttpClient: HTTP Error 503", "warn"), ("2026-11-13 10:00:00,1 INFO waiting for critical-exit to die", None),
             ("[Info] Rss sync failed 0 times", None), ("Sync done: 0 errors, failed=0", None),
             ("rebroadcast error Error: timeout", "error"), ("level=ERROR msg=\"server terminated\"", "error"),
             ("time=x level=WARN msg=\"OOM detected; error clearing\"", "warn"), ("[supervisor] x is non-critical", None),
             ("app - ERROR - db failed", "error"), ("WARNING:root:odd", "warn"), ("[error] Error in process", "error"),
             ("status fetch failed, keeping provided info", "error"),
             ("2026-11-12T10:00:00Z INF | ERROR: Allow outbound TCP on port 7844. |", None),
             ("2026-11-12T10:00:00Z WRN Serve tunnel error error=\"timeout\"", "warn"),
             ("2026-11-12T10:00:00Z ERR Request failed error=\"unexpected EOF\"", "error")]
    bad = [(l, lv(l), w) for l, w in cases if lv(l) != w]
    chk("levels: the logger's tag wins, then the words; '0 errors' is not an error", not bad, bad)


def test_fragments():
    """Stack frames, traceback headers, echoed source lines and HTML page markup are counted, not made into kinds;
    the line carrying the message is the kind (11-15 back-fill: the model explained such fragments and made things up)."""
    rows = [
        "----------------------------------------",
        "Exception occurred during processing of request from ('192.0.2.7', 52848)",
        "Exception occurred during processing of request from----------------------------------------",
        "Traceback (most recent call last):",
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        '----------------------------------------  File "/usr/local/lib/python3.12/socketserver.py", line 362, in finish_request',
        "Exception occurred during processing of request frominternal error: BrokenPipeError: [Errno 32] Broken pipe",
        "internal error: BrokenPipeError: [Errno 32] Broken pipe",
        '    self._err(ShimError(500, "internal error"))',
        "    warnings.warn(warning_message)",
        "   at System.Net.Sockets.Socket.AwaitableSocketAsyncEventArgs.ThrowException(SocketError error, CancellationToken t)",
        "   at Object.<anonymous> (/app/server.js:10:5)",
        "--- End of inner exception stack trace ---",
        "C++ Traceback (most recent call last):",
        '<span class="cf-error-code">502</span>',
        "<P>https&#58;&#47;&#47;errors",
        '<error code="100" description="Jackett.Common.IndexerException: Exception (torrentleech): Login failed">',
        "db.connect(): failed (timeout)",
        "at 10:00 the backup failed",
        "ERROR(5): disk failed (sda)",
        '{"job": "ab12", "http": 200, "exit_code": 0, "timings": {"llm": {"calls": 3, "errors": 0}}, "event": "result_posted"}',
    ]
    info = ED.summarize_lines("app", [(i, r) for i, r in enumerate(rows)], [])
    texts = [x["text"] for x in info["sigs"]]
    chk("fragments are counted, not made into kinds", info["fragments"] == 13, (info["fragments"], texts))
    want = ["ConnectionResetError: [Errno N] Connection reset by peer", "internal error: BrokenPipeError: [Errno N] Broken pipe",
            '<error code="N" description="Jackett.Common.IndexerException: Exception (torrentleech): Login failed">',
            "db.connect(): failed (timeout)", "at N the backup failed", "ERROR(N): disk failed (sda)"]
    chk("the lines that carry the message are the kinds", sorted(texts) == sorted(want), texts)
    chk("socketserver's header and dash rules don't split one error into two kinds",
        [x["count"] for x in info["sigs"] if "BrokenPipeError" in x["text"]] == [2], info["sigs"])
    chk("'\"errors\": 0' in a JSON line is not an error", info["benign"] == 1, info["benign"])
    chk("an exception class line is fetched from Loki and is an error",
        _errish("ConnectionResetError: [Errno 104] Connection reset by peer") and
        ED.level("ConnectionResetError: [Errno 104] Connection reset by peer") == "error")
    chk("plain words are still not fetched", not _errish("Connection reset by peer, retrying"))
    md = ED.render({"day": "2026-11-14", "containers": {"app": dict(info, sigs=[])}, "model": {}})
    chk("the digest says how many fragment lines it left out", "13 stack-trace or page-markup lines" in md, md[-600:])
    chk("the model prompt names the placeholders and allows 'unclear'",
        "<ip> is just whichever address" in RUN.SYSTEM and "answer exactly: unclear" in RUN.SYSTEM)


def test_render_caps():
    mk = lambda i, lvl, n: {"id": "%s%02d" % (lvl[0], i), "level": lvl, "count": n, "text": "%s kind %d" % (lvl, i),
                            "first_ns": 0, "last_ns": 0, "new": True}
    sigs = [mk(i, "error", 100 - i) for i in range(15)] + [mk(i, "warn", 50 - i) for i in range(8)]
    md = ED.render({"day": "2026-11-14", "containers": {"app": {"sigs": sigs}}, "model": {}})
    chk("new kinds per container are capped (10 errors, 5 warnings) with the rest counted",
        md.count("`error kind") == 10 + 1 and md.count("`warn kind") == 5
        and "plus 5 rarer new error kinds and 3 rarer new warning kinds" in md, md[:1500])
    chk("the most frequent new kinds are the ones shown", "error kind 0`" in md and "error kind 14`" not in md)


def test_noise():
    rules = ED.load_noise(BIN / "error_digest_noise.json")
    chk("the shipped noise rules load", len(rules) >= 9, len(rules))
    chk("routine rule: plex seed floor", ED.noise_action(rules, "arr-webhook", "WARNING Seed floor 3d: removal deferred") == "routine")
    chk("skip rule: arr health is alerted elsewhere",
        ED.noise_action(rules, "sonarr", "[Warn] HealthCheckService: Indexers unavailable due to failures") == "skip")
    chk("rules are per container", ED.noise_action(rules, "radarr", "Seed floor 3d: removal deferred") is None)
    d = Path(tempfile.mkdtemp())
    (d / "n.json").write_text(json.dumps([{"match": "(", "action": "routine"}, {"match": "x", "action": "explode"},
                                          {"container": "a*", "match": "boom", "action": "skip"}]))
    r2 = ED.load_noise(d / "n.json")
    chk("bad rules are dropped, good ones kept", len(r2) == 1 and ED.noise_action(r2, "abc", "BOOM") == "skip", r2)
    chk("a missing noise file is no rules", ED.load_noise(d / "nope.json") == [])


def test_build_and_new(base, tmp):
    os.environ.update(LOKI_URL=base, LOKI_TOKEN="tok-test", ERROR_DIGEST_DIR=str(tmp))
    d1, d2, d3 = "2026-11-01", "2026-11-02", "2026-11-03"
    FakeLoki.logs = {
        "app": [(ns_at(d1, 1), "db connect failed after 3 tries"), (ns_at(d1, 2), "db connect failed after 9 tries"),
                (ns_at(d1, 3), "INFO all good, 0 errors"),
                (ns_at(d2, 1), "db connect failed after 4 tries"), (ns_at(d2, 5), "disk full: write error on /data"),
                (ns_at(d3, 1), "db connect failed after 1 tries")],
        "arr-webhook": [(ns_at(d2, 4), "WARNING Seed floor 2d: removal deferred"),
                        (ns_at(d2, 6), "sync to deluge failed: timeout")],
        "sonarr": [(ns_at(d2, 3), "[Warn] HealthCheckService: Indexers unavailable due to failures")],
        "quiet": [(ns_at(d2, 3), "all fine")],
        "priceless_brattain": [(ns_at(d3, 2), "[ERROR] tile build failed: no admin table")],
        "sharp_visvesvaraya": [(ns_at(d3, 7), "[ERROR] tile build failed: no admin table")],
    }
    ED.PAGE = 2   # forces paging through query_range
    rc, out, err = quiet(ED.main, ["backfill", "--days", "0"])
    chk("backfill --days 0 does nothing", rc == 0, out + err)
    for d in (d1, d2, d3):
        rc, out, err = quiet(ED.main, ["run", "--day", d, "--no-model"])
        chk("run %s rc 0" % d, rc == 0, out + err)
    r1 = json.loads((tmp / "digests" / (d1 + ".json")).read_text())
    r2 = json.loads((tmp / "digests" / (d2 + ".json")).read_text())
    r3 = json.loads((tmp / "digests" / (d3 + ".json")).read_text())
    chk("noise.json was seeded into the digest dir", (tmp / "noise.json").is_file())
    a1 = r1["containers"]["app"]
    chk("day 1: two lines, one kind, the '0 errors' INFO line is not an error",
        a1["sigs"][0]["count"] == 2 and len(a1["sigs"]) == 1 and a1["benign"] == 1, a1)
    chk("day 1 is the baseline: nothing marked new", r1["baseline"] and not any(s["new"] for s in a1["sigs"]))
    a2 = r2["containers"]["app"]
    new2 = {s["text"]: s["new"] for s in a2["sigs"]}
    chk("day 2: the repeated kind is not new, the new one is",
        new2.get("db connect failed after N tries") is False and new2.get("disk full: write error on /data") is True, new2)
    w = r2["containers"]["arr-webhook"]
    chk("routine lines are folded (counted, never a kind)", w["routine"] == 1 and len(w["sigs"]) == 1 and w["sigs"][0]["new"], w)
    chk("skip lines are left out (counted as skipped)", r2["containers"]["sonarr"]["skipped"] == 1
        and not r2["containers"]["sonarr"]["sigs"], r2["containers"].get("sonarr"))
    chk("a container with no error lines is not in the digest", "quiet" not in r2["containers"])
    chk("day 3: history covers both earlier days", r3["history_days"] == [d1, d2] and not r3["baseline"], r3["history_days"])
    oo = r3["containers"].get(ED.ONE_OFF) or {}
    chk("docker-named one-off containers share one heading and one kind",
        oo.get("names") == ["priceless_brattain", "sharp_visvesvaraya"] and len(oo.get("sigs") or []) == 1
        and oo["sigs"][0]["count"] == 2 and "priceless_brattain" not in r3["containers"], r3["containers"].keys())
    chk("the one-off heading names its containers", "### (one-off containers) (priceless_brattain, sharp_visvesvaraya)"
        in (tmp / "digests" / (d3 + ".md")).read_text())
    chk("only docker's own adjective_name pattern is one-off",
        [ED.group_of(x) for x in ("home_assistant", "ollama-queue", "sharp_visvesvaraya", "zen_lovelace2")]
        == ["home_assistant", "ollama-queue", ED.ONE_OFF, ED.ONE_OFF])
    md = (tmp / "digests" / (d2 + ".md")).read_text()
    chk("markdown: headline counts new kinds, the new error is listed under its container",
        "2 new kinds of error" in md and "### app" in md and "disk full" in md and "## Known noise" in md, md)
    chk("markdown: routine and left-out counts are shown", "arr-webhook: 1 routine line" in md and "sonarr: 1 left out" in md, md)
    chk("files are private (0600)", oct((tmp / "digests" / (d2 + ".json")).stat().st_mode & 0o777) == "0o600")
    chk("the bearer token was sent on every Loki call", set(FakeLoki.seen_auth) == {"Bearer tok-test"}, set(FakeLoki.seen_auth))
    pages = [q for p, q in FakeLoki.calls if p.endswith("query_range") and 'container="app"' in q["query"]]
    chk("query_range paged (limit 2) and asked forward", len(pages) >= 2 and all(q["direction"] == "forward" for q in pages))
    ED.PAGE = 5000
    old_cap = ED.LINE_CAP
    ED.LINE_CAP = 1
    FakeLoki.logs["app"].append((ns_at(d1, 4), "db connect failed after 5 tries"))
    rows, capped = ED.Loki(base, "tok-test").lines("app", *ED.day_bounds(d1))
    chk("per-container cap: stops and says capped", len(rows) == 1 and capped, (rows, capped))
    ED.LINE_CAP = old_cap
    os.environ["LOKI_TOKEN"] = "wrong"
    rc, out, err = quiet(ED.main, ["run", "--day", d1, "--no-model"])
    chk("a 401 from Loki: rc 1, says so, prints no token", rc == 1 and "401" in err and "wrong" not in out + err, out + err)
    os.environ["LOKI_TOKEN"] = "tok-test"
    os.environ.pop("LOKI_URL")
    os.environ.pop("LOGQ_URL", None)
    rc, out, err = quiet(ED.main, ["run", "--day", d1, "--no-model"])
    chk("no Loki URL: rc 2 with the setting to fix", rc == 2 and "LOKI_URL" in err, err)
    os.environ["LOKI_URL"] = base
    rc, out, err = quiet(ED.main, ["run", "--day", "11/03/2026", "--no-model"])
    chk("a bad --day is refused", rc == 2 and "bad --day" in err, err)
    rc, out, err = quiet(ED.main, ["show", "--day", d2])
    chk("show prints the digest", rc == 0 and out == md)
    rc, out, err = quiet(ED.main, ["grading", "--days", "3"])
    return r2


def test_model_step(tmp, r2, obase):
    day = r2["day"]
    items = ED.model_items(r2)
    chk("the model is asked only about NEW kinds", sorted(items) == ["app", "arr-webhook"]
        and [s["text"] for s in items["app"]] == ["disk full: write error on /data"], items)
    asked = []

    def ans(body):
        asked.append(body)
        user = body["messages"][1]["content"]
        ids = [l.split("id ", 1)[1].split(" ", 1)[0] for l in user.splitlines() if l.startswith("- id ")]
        lines = [{"id": i, "meaning": "The data disk is full, so the app could not save." if "disk" in user else "unclear"}
                 for i in ids]
        lines.append({"id": "deadbeef00", "meaning": "invented"})
        return 200, {"message": {"role": "assistant", "content": json.dumps({"lines": lines})}}

    FakeOllama.answer = ans
    task = tmp / "t.json"
    task.write_text(json.dumps({"day_json": str(tmp / "digests" / (day + ".json"))}))
    rc, out, err = quiet(RUN.main, ["--model", "m:8b", "--host", obase, "--task-file", str(task)])
    rec = json.loads((tmp / "digests" / (day + ".json")).read_text())
    m = rec.get("model") or {}
    disk = [s for s in rec["containers"]["app"]["sigs"] if "disk" in s["text"]][0]
    chk("runner: one call per container with new kinds", rc == 0 and len(asked) == 2, (rc, len(asked), out, err))
    chk("runner: the meaning lands on the right kind; 'unclear' and unknown ids are dropped",
        m.get("status") == "done" and m.get("lines") == {disk["id"]: "The data disk is full, so the app could not save."},
        m)
    chk("runner: asks for JSON with thinking off and a low temperature",
        asked[0].get("format", {}).get("type") == "object" and asked[0].get("think") is False
        and asked[0]["options"]["temperature"] <= 0.2)
    chk("runner: prints counts only, never log text", "disk" not in out and "kinds asked" in out, out)
    md = (tmp / "digests" / (day + ".md")).read_text()
    chk("the digest shows the model line under the new kind", "Probably: The data disk is full" in md, md)
    chk("the model prompt has no secrets or raw times (only signatures)",
        all("2026" not in b["messages"][1]["content"] for b in asked))

    # a model that refuses `think` gets one retry without it
    seen = []

    def no_think(body):
        seen.append("think" in body)
        if "think" in body:
            return 400, {"error": "model does not support thinking"}
        return 200, {"message": {"content": json.dumps({"lines": []})}}

    FakeOllama.answer = no_think
    rc, out, err = quiet(RUN.main, ["--model", "m", "--host", obase, "--task-file", str(task)])
    chk("a 400 about thinking: retried once without think", seen[:2] == [True, False] and rc == 0, (seen, rc, out))

    FakeOllama.answer = lambda b: (500, {"error": "boom"})
    rc, out, err = quiet(RUN.main, ["--model", "m", "--host", obase, "--task-file", str(task)])
    rec = json.loads((tmp / "digests" / (day + ".json")).read_text())
    chk("every call failing: rc 1, digest says the model failed, plain counts kept",
        rc == 1 and rec["model"]["status"] == "failed" and "No model notes today" in
        (tmp / "digests" / (day + ".md")).read_text() and rec["containers"]["app"]["sigs"], (rc, rec.get("model")))
    chk("runner parse: prose around JSON still parses; overlong lines are cut",
        RUN.parse('sure: {"lines":[{"id":"a","meaning":"' + "w " * 40 + '"}]}', {"a"})["a"].endswith("…"))

    # enqueue: the command and the write order
    calls = []

    class R:
        returncode, stdout = 0, "enqueued 0123456789ab  error-digest-20261102  model=m host=unraid\n"

    real = ED.subprocess.run
    ED.subprocess.run = lambda cmd, **k: calls.append(cmd) or R
    try:
        rc, out, err = quiet(ED.main, ["run", "--day", day, "--model", "m:8b"])
    finally:
        ED.subprocess.run = real
    cmd = calls[0] if calls else []
    chk("run with a model queues ONE job: allowlisted runner, unraid lane, research kind, label per day",
        rc == 0 and len(calls) == 1 and cmd[cmd.index("--runner") + 1].endswith("error-digest-runner.py")
        and cmd[cmd.index("--host") + 1] == "unraid" and cmd[cmd.index("--task-kind") + 1] == "research"
        and cmd[cmd.index("--label") + 1] == "error-digest-20261102" and "model job 0123456789ab queued" in out, (cmd, out))
    rec = json.loads((tmp / "digests" / (day + ".json")).read_text())
    chk("the digest is written (model pending) BEFORE the job is queued and left alone after",
        rec["model"] == {"status": "pending", "model": "m:8b"}, rec["model"])

    class Bad:
        returncode, stdout = 3, "[queue] REFUSED\n"

    ED.subprocess.run = lambda cmd, **k: Bad
    try:
        rc, out, err = quiet(ED.main, ["run", "--day", day, "--model", "m:8b"])
    finally:
        ED.subprocess.run = real
    rec = json.loads((tmp / "digests" / (day + ".json")).read_text())
    chk("a refused enqueue leaves plain counts and says so", rc == 0 and rec["model"]["status"] == "failed", rec["model"])


def test_supervisor_and_allowlist():
    q = (BIN / "ollama-queue.py").read_text()
    chk("the runner is allowlisted in the queue", 'str(Path.home() / "bin" / "error-digest-runner.py")' in q)
    sup = (BIN / "queue-image" / "supervisord.conf").read_text()
    chk("the container runs the digest nightly", "[program:error-digest]" in sup and "error_digest.py run" in sup)
    man = (BIN / "queue-image" / "MANIFEST").read_text().split()
    chk("the image ships the digest files", all(f in man for f in ("error_digest.py", "error-digest-runner.py",
                                                                    "error_digest_noise.json")), man[:5])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ed-test-"))
    lsrv, lbase = serve(FakeLoki)
    osrv, obase = serve(FakeOllama)
    try:
        test_signature_and_level()
        test_fragments()
        test_noise()
        test_render_caps()
        r2 = test_build_and_new(lbase, tmp)
        test_model_step(tmp, r2, obase)
        test_supervisor_and_allowlist()
    finally:
        lsrv.shutdown()
        osrv.shutdown()
    print("\n%d/%d passed" % (N[0] - len(FAILS), N[0]))
    print("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), "; ".join(FAILS)))
    if not FAILS:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
