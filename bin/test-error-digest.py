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
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
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


def eq(name, got, want, detail=""):
    chk(name, got == want, "got %r, want %r %s" % (got, want, detail))


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
             ("2026-11-12T10:00:00Z ERR Request failed error=\"unexpected EOF\"", "error"),
             ("[Doorbell] Socket closed, hadError=false", None), ("[Doorbell] Socket closed, hadError=true", "error"),
             ("[critical-exit] watching for FATAL on: app, xvfb", None),
             ("[critical-exit] app entered FATAL, stopping the container", "error")]
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
    rp = ED.shape('{"job": "ab12", "http": 200, "timings": {"llm": {"calls": 1238, "errors": 1}}, "event": "result_posted"}')
    chk("a nonzero nested error count is kept in a JSON line's shape", rp == "result_posted (llm errors N)", rp)
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


def test_noise_local_adds():
    d = Path(tempfile.mkdtemp())
    (d / "noise.json").write_text(json.dumps([{"container": "app", "match": "local only", "action": "skip"},
                                              {"container": "arr-webhook", "match": "Seed floor", "action": "skip"}]))
    rules = ED.noise_rules(d)
    chk("a local noise.json adds rules to the shipped ones", ED.noise_action(rules, "app", "x local only x") == "skip"
        and ED.noise_action(rules, "sonarr", "[Warn] HealthCheckService: Indexers unavailable") == "skip")
    chk("the local file is checked first", ED.noise_action(rules, "arr-webhook", "Seed floor 3d") == "skip")
    n_shipped = len(ED.load_noise(BIN / "error_digest_noise.json"))
    (d / "noise.json").write_text((BIN / "error_digest_noise.json").read_text())
    chk("an old copy of the shipped file counts once", len(ED.noise_rules(d)) == n_shipped, len(ED.noise_rules(d)))
    chk("cameras dropping off is routine (the operator already gets camera notifications)",
        ED.noise_action(ED.noise_rules(d), "scrypted", "connect EHOSTUNREACH 192.0.2.3:554") == "routine")


def test_alerts():
    shipped = json.loads((BIN / "error_digest_alerts.json").read_text())
    rules = ED.load_alerts(BIN / "error_digest_alerts.json")
    chk("every shipped alert rule loads", len(rules) == len(shipped) and len(rules) >= 20, (len(rules), len(shipped)))
    chk("shipped rules route only to owner or claude", {r["route"] for r in rules} == {"owner", "claude"})
    chk("a bracketed component before the level reads as that level",
        ED.level("[queue] WARNING: couldn't query http://h/api/ps (<urlopen error [Errno 111] Connection refused>)")
        == "warn" and ED.level("[entrypoint] WARNING: GITHUB_TOKEN is not set") == "warn", ED.level("[queue] WARNING: x"))

    def route(c, line):
        hits = ED.match_alerts(c, [(1, line)], rules)
        return rules[next(iter(hits))]["route"] if hits else None
    cases = [("arr-webhook", "[rar] unpack of /dl/x.rar failed: CRC error", "owner"),
             ("autobrr", "irc: banned from network", "owner"),
             ("sonarr", "[Warn] Deluge: Unable to connect to Deluge", "owner"),
             ("unpackerr", "Extraction Failed: /dl/x: no compressed files found", None),
             ("unpackerr", "Extraction Failed: /dl/x: crc error", "owner"),
             ("homebridge", "[11/7/2026, 4:00:10 AM] [Lutron] Failed to re-subscribe to events", "owner"),
             ("ollama", "error: out of memory", "claude"),
             ("vault", "committer: git push timed out", "claude"),
             ("radarr", "[Warn] Unable to send notification to Webhook", "claude"),
             ("binhex-jackett", "Error: Login failed", "owner"),
             # planned stops are not for Claude: supervisord's stop/start lines, Postgres closing on shutdown
             ("ollama-queue", "WARN stopped: queue-api (terminated by SIGTERM)", None),
             ("ollama-queue", "WARN stopped: queue-daemon (exit status 143)", None),
             ("ollama-queue", "WARN received SIGTERM indicating exit request", None),
             ("ollama-queue", "CRIT Server 'unix_http_server' running without any HTTP authentication checking", None),
             ("ollama-queue", "WARN exited: queue-daemon (exit status 1; not expected)", "claude"),
             ("ollama-queue", "[queue] ERROR: dispatch failed: database is locked", "claude"),
             # a catch-all rule sees only the lines the digest counts (supervisord names the error-digest program)
             ("ollama-queue", "2026-11-16 00:03:18,984 INFO spawned: 'error-digest' with pid 74", None),
             ("ollama-queue", "2026-11-16 00:01:47,658 INFO waiting for cpu-prefetch, error-digest, queue-api to die", None),
             ("ollama", "time=2026-11-16T05:07:00.000Z level=INFO msg=\"no errors\"", None),
             ("nominatim", "UTC [88] nominatim@nominatim FATAL:  terminating connection due to administrator command", None),
             ("nominatim", "UTC [90] nominatim@nominatim FATAL:  the database system is in recovery mode", "claude")]
    bad = [(c, l, route(c, l), want) for c, l, want in cases if route(c, l) != want]
    chk("shipped rules route sample lines as graded", not bad, bad)
    chk("the catch-all Claude rules only see counted lines", all(r["counted_only"] for r in rules if r["rx"].pattern == "."),
        [r["globs"] for r in rules if r["rx"].pattern == "." and not r["counted_only"]])
    loose = ED.load_alerts(BIN / "error_digest_alerts.json")
    loose[0] = dict(loose[0], globs=["autobrr"], rx=re.compile("banned", re.I))
    chk("a rule without counted_only still matches an INFO-tagged line",
        bool(ED.match_alerts("autobrr", [(1, "INF irc: banned from network")], loose[:1])))
    nz = ED.load_noise(BIN / "error_digest_noise.json")
    chk("planned-stop lines count as routine, a crash does not",
        ED.noise_action(nz, "ollama-queue", "WARN stopped: queue-api (terminated by SIGTERM)") == "routine"
        and ED.noise_action(nz, "ollama-queue", "CRIT Server 'unix_http_server' running without any HTTP authentication checking") == "routine"
        and ED.noise_action(nz, "nominatim", "UTC [88] nominatim@nominatim FATAL:  terminating connection due to administrator command") == "routine"
        and ED.noise_action(nz, "ollama-queue", "WARN exited: queue-daemon (exit status 1; not expected)") is None
        and ED.noise_action(nz, "nominatim", "UTC [90] nominatim@nominatim FATAL:  the database system is in recovery mode") is None)

    d = Path(tempfile.mkdtemp())
    (d / "a.json").write_text(json.dumps([
        {"route": "owner", "container": "web*", "match": "fatal thing", "what": "Fatal"},
        {"route": "owner", "container": "web*", "match": "bad login", "what": "Logins", "min_count": 3},
        {"route": "owner", "container": "web*", "match": "backend down", "what": "Backend", "sustain_min": 30, "gap_min": 10},
        {"route": "owner", "container": "web*", "match": "held thing", "what": "Held", "hold": "not yet"},
        {"route": "claude", "container": ["db", "cache"], "match": ".", "what": "DB"},
        {"route": "owner", "match": "fatal", "what": "never: the first rule wins"},
        {"route": "nobody", "match": "x", "what": "bad route"}, {"route": "owner", "match": "(", "what": "bad regex"},
        {"route": "owner", "match": "x"}]))
    ar = ED.load_alerts(d / "a.json")
    chk("bad alert rules are dropped", len(ar) == 6, len(ar))
    m = 60 * 10**9
    rows = [(1 * m, "fatal thing happened"), (2 * m, "bad login for x"), (3 * m, "bad login for y"),
            (4 * m, "held thing")] + [(k * 5 * m, "backend down") for k in range(0, 6)]   # one run of 25 min
    acc = {}
    for i, h in ED.match_alerts("web1", rows, ar).items():
        acc.setdefault(i, []).append(("web1", h))
    for i, h in ED.match_alerts("db", [(9 * m, "slow query")], ar).items():
        acc.setdefault(i, []).append(("db", h))
    sig_cov = ED.sig_id("web1", ED.signature("fatal thing happened"))
    cs = {"web1": {"sigs": [{"id": sig_cov, "new": True, "level": "error", "count": 1, "text": "fatal thing happened"}]},
          "other": {"sigs": [{"id": "n1", "new": True, "level": "error", "count": 4, "text": "brand new error"},
                             {"id": "n2", "new": True, "level": "warn", "count": 9, "text": "a new warning"},
                             {"id": "n3", "new": False, "level": "error", "count": 9, "text": "old error"}]}}
    al = ED.decide_alerts(acc, ar, cs)
    chk("a matched owner rule is for the owner, once per rule", [i["what"] for i in al["owner"]] == ["Fatal"], al["owner"])
    held = {i["what"]: i["held"] for i in al["held"]}
    chk("under min_count, under sustain_min and hold are held back with the reason",
        set(held) == {"Logins", "Backend", "Held"} and "2× today, 3 needed" in held["Logins"]
        and "longest run 25 min, 30 needed" in held["Backend"] and held["Held"] == "not yet", held)
    chk("claude rules go to claude", [i["what"] for i in al["claude"]] == ["DB"], al["claude"])
    chk("new unrouted error kinds go to claude; covered, warning and old kinds do not",
        [n["id"] for n in al["new_for_claude"]] == ["n1"], al["new_for_claude"])
    rows2 = [(k * 5 * m, "backend down") for k in range(0, 6)] + [(200 * m + k * 5 * m, "backend down") for k in range(8)]
    acc2 = {i: [("web1", h)] for i, h in ED.match_alerts("web1", rows2, ar).items()}
    al2 = ED.decide_alerts(acc2, ar, {})
    chk("a run of 35 min (lines 5 min apart) passes sustain_min 30", [i["what"] for i in al2["owner"]] == ["Backend"]
        and al2["owner"][0]["longest_min"] == 35.0, al2)
    start = 1000 * m
    today = [(start + k * 5 * m, "backend down") for k in range(3)]            # 00:00 to 00:10: 10 min today
    acc3 = {i: [("web1", h)] for i, h in ED.match_alerts("web1", today, ar).items()}
    bi = next(iter(acc3))
    eq("a run that began before midnight: held on its own (10 min today)",
       [i["what"] for i in ED.decide_alerts(acc3, ar, {})["held"]], ["Backend"])
    al3 = ED.decide_alerts(acc3, ar, {}, before={bi: [start - k * 5 * m for k in range(5, 0, -1)]})
    chk("... measured whole with the 25 min before midnight it is 35 min and sent; the count stays today's",
        [i["what"] for i in al3["owner"]] == ["Backend"] and al3["owner"][0]["longest_min"] == 35.0
        and al3["owner"][0]["count"] == 3, al3)
    al4 = ED.decide_alerts(acc3, ar, {}, before={bi: [start - k * 5 * m for k in range(9, 3, -1)]})
    chk("... lines before midnight that ended more than the gap before it are not joined to today's run",
        [i["what"] for i in al4["held"]] == ["Backend"], al4)
    (d / "n.json").write_text(json.dumps([{"route": "owner", "match": "unpack", "what": "Unpack", "names": True},
                                          {"route": "owner", "match": "other", "what": "Other"}]))
    an = ED.load_alerts(d / "n.json")
    accn = {i: [("c", h)] for i, h in ED.match_alerts("c", [(m, "unpack of Secret.Name.Here.x failed"),
                                                             (2 * m, "other 'Quoted Name' at /data/Some Dir/f")],
                                                     an).items()}
    aln = ED.decide_alerts(accn, an, {})
    _, ntn = ED.notify_text({"day": "2026-11-02", "alerts": aln, "containers": {}})
    chk("the message leaves out the sample of a names rule, and folds quoted names and paths in the others",
        "Secret" not in ntn and "Quoted" not in ntn and "Some Dir" not in ntn and "- Unpack (c;" in ntn
        and "e.g. other '<q>' at <path>" in ntn and [i.get("names") for i in aln["owner"]] == [True, None], ntn)
    md = "".join(ED.render_alerts(al))
    chk("render: owner, held and claude sections",
        "## For you" in md and "**Fatal**" in md and "## Held back" in md and "## For Claude" in md
        and "brand new error" in md, md)
    chk("render: an empty owner list says so", "Nothing needs you today." in "\n".join(ED.render_alerts(al2 | {"owner": []})))


class FakeNotify:
    calls = []
    code = 202

    def __init__(self, real):
        self.real = real

    def __call__(self, req, timeout=None):
        if not getattr(req, "full_url", "").startswith(("https://notify.test", "http://notify.test")):
            return self.real(req, timeout=timeout)     # Loki
        FakeNotify.calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data),
                                 "Authorization" in req.headers))     # req.headers is what a redirect copies
        FakeNotify.last = req
        if FakeNotify.code >= 300:
            raise urllib.error.HTTPError(req.full_url, FakeNotify.code, "no", {}, io.BytesIO(b"{}"))

        class R:
            status = FakeNotify.code

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def test_notify(base):
    tmp = Path(tempfile.mkdtemp(prefix="ed-notify-"))
    y = ED._yesterday()
    os.environ.update(LOKI_URL=base, LOKI_TOKEN="tok-test", ERROR_DIGEST_DIR=str(tmp))
    FakeLoki.logs = {"homebridge": [(ns_at(y, 4), "[Lutron] Failed to re-subscribe to events, error ECONNRESET")],
                     "ollama": [(ns_at(y, 5), "error: CUDA out of memory")],
                     "vaultwarden": [(ns_at(y, 6), "[ERROR] Username or password is incorrect. Try again. IP: 192.0.2.9")]}
    real, waits = ED.urllib.request.urlopen, ED.NOTIFY_WAITS
    ED.urllib.request.urlopen = FakeNotify(real)
    ED.NOTIFY_WAITS = (0, 0)
    try:
        for k in ("ERROR_DIGEST_NOTIFY_URL", "ERROR_DIGEST_NOTIFY_TOKEN", "ERROR_DIGEST_NOTIFY_TOKEN_FILE"):
            os.environ.pop(k, None)
        rc, out, err = quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        nt = (tmp / "digests" / (y + ".notify.txt")).read_text()
        chk("no notify URL: shadow, nothing sent, the message is written", rc == 0 and r["notify"]["status"] == "shadow"
            and not FakeNotify.calls and nt.startswith("Subject: Unraid ") and "1 thing for you" in nt, (out, err, nt))
        chk("the message: owner item and its sample; vaultwarden under 3 held back and only counted; claude items "
            "only counted", "Lutron Pico buttons went silent" in nt and "e.g. [Lutron] Failed to re-subscribe" in nt
            and "Held back: 1 item (not sent" in nt and "Vaultwarden" not in nt
            and "CUDA" not in nt and "1 item for Claude" in nt, nt)
        tf = tmp / "tok"
        tf.write_text("notify-secret\n")
        os.environ.update(ERROR_DIGEST_NOTIFY_URL="http://notify.test/send", ERROR_DIGEST_NOTIFY_TOKEN_FILE=str(tf))
        quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        chk("a plain-http notify URL is refused", r["notify"]["status"] == "failed" and not FakeNotify.calls, r["notify"])
        os.environ["ERROR_DIGEST_NOTIFY_URL"] = "https://notify.test/send"
        FakeNotify.code = 500
        quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        chk("a 500 is tried 3 times, then recorded as failed and marked for a later resend (see test_tick)",
            r["notify"] == {**r["notify"], "status": "failed", "http": 500, "tries": 3, "retry_later": True}
            and len(FakeNotify.calls) == 3,
            (r["notify"], len(FakeNotify.calls)))
        FakeNotify.code, FakeNotify.calls = 400, []
        quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        chk("a 400 is not tried again in the same run", r["notify"]["status"] == "failed" and len(FakeNotify.calls) == 1
            and "tries" not in r["notify"], (r["notify"], len(FakeNotify.calls)))
        FakeNotify.code, FakeNotify.calls = 202, []
        rc, out, err = quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        c = FakeNotify.calls
        chk("sent once with the bearer token from the file; subject and text in the body",
            r["notify"]["status"] == "sent" and len(c) == 1 and c[0][1] == "Bearer notify-secret"
            and c[0][2]["app"] == "error-digest" and c[0][2]["ref"] == y and "for you" in c[0][2]["subject"]
            and "Lutron" in c[0][2]["text"], (r.get("notify"), c))
        chk("the bearer is an unredirected header (never passed on to a redirect's target)", c and not c[0][3], c)
        nxt = urllib.request.HTTPRedirectHandler().redirect_request(FakeNotify.last, None, 302, "Found", {},
                                                                    "https://elsewhere.test/x")
        chk("urllib's own redirect of the send carries no bearer", nxt is not None
            and nxt.full_url == "https://elsewhere.test/x" and not nxt.has_header("Authorization")
            and FakeNotify.last.has_header("Authorization"), nxt and nxt.header_items())
        chk("the token is never printed", "notify-secret" not in out + err)
        quiet(ED.main, ["run", "--no-model"])
        quiet(ED.main, ["run", "--no-model", "--day", y])
        yy = (dt.date.fromisoformat(y) - dt.timedelta(days=1)).isoformat()
        (tmp / "digests" / (yy + ".json")).write_text(json.dumps({"day": yy, "containers": {}, "alerts": {
            "owner": [{"what": "Lutron Pico buttons went silent (re-subscribe failed)"}], "held": []}}))
        rec = ED.build_day(ED.Loki(base, "tok-test"), y, tmp, ED.noise_rules(tmp), say=lambda *a: None,
                           arules=ED.alert_rules(tmp))
        subj, text = ED.notify_text(rec)
        chk("an owner item also seen the day before is marked ongoing; the subject counts the new ones",
            rec["alerts"]["owner"][0]["seen_days"] == 1 and subj.endswith("1 thing for you, none new")
            and "also on 1 of the last 7 days" in text, (subj, text))
        chk("a rerun of a sent day sends nothing and keeps the sent record", len(FakeNotify.calls) == 1
            and json.loads((tmp / "digests" / (y + ".json")).read_text())["notify"]["status"] == "sent")
        (tmp / "digests" / (y + ".json")).unlink()
        quiet(ED.main, ["run", "--no-model", "--no-send"])
        quiet(ED.main, ["backfill", "--days", "2", "--no-model"])
        old = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        quiet(ED.main, ["run", "--no-model", "--day", old])
        chk("--no-send, backfill and an older --day never send", len(FakeNotify.calls) == 1, FakeNotify.calls[1:])
        (tmp / "digests" / (y + ".json")).unlink()
        FakeNotify.code, FakeNotify.calls = 409, []
        quiet(ED.main, ["run", "--no-model"])
        r = json.loads((tmp / "digests" / (y + ".json")).read_text())
        chk("a 409 (the mail Worker already mailed this day) is recorded as sent, not tried again",
            r["notify"]["status"] == "sent" and r["notify"]["http"] == 409 and len(FakeNotify.calls) == 1, r["notify"])
        chk("the send holds a lock file in the digest dir, private", (tmp / ".notify.lock").exists()
            and (tmp / ".notify.lock").stat().st_mode & 0o077 == 0)
        FakeNotify.code = 202
    finally:
        ED.urllib.request.urlopen, ED.NOTIFY_WAITS = real, waits
        for k in ("ERROR_DIGEST_NOTIFY_URL", "ERROR_DIGEST_NOTIFY_TOKEN_FILE"):
            os.environ.pop(k, None)
    shutil.rmtree(tmp, ignore_errors=True)


def test_tick(base):
    """The container's tick: one build and send a day, catch-up after a stop or an outage, never a second email."""
    tmp = Path(tempfile.mkdtemp(prefix="ed-tick-"))
    y = ED._yesterday()
    dj, ts = tmp / "digests" / (y + ".json"), tmp / ".tick.json"
    tf = tmp / "tok"
    tf.write_text("notify-secret\n")
    os.environ.update(LOKI_URL=base, LOKI_TOKEN="tok-test", ERROR_DIGEST_DIR=str(tmp),
                      ERROR_DIGEST_NOTIFY_URL="https://notify.test/send", ERROR_DIGEST_NOTIFY_TOKEN_FILE=str(tf))
    FakeLoki.logs = {"homebridge": [(ns_at(y, 4), "[Lutron] Failed to re-subscribe to events, error ECONNRESET")]}
    saved = (ED.urllib.request.urlopen, ED.NOTIFY_WAITS, ED.TICK_AT, ED.RESEND_EVERY_S, ED.BUILD_EVERY_S,
             ED.send_notify)
    ED.urllib.request.urlopen = FakeNotify(saved[0])
    ED.NOTIFY_WAITS = (0,)
    FakeNotify.code, FakeNotify.calls = 202, []

    def tick():
        return quiet(ED.main, ["tick", "--no-model"])

    def fresh():
        dj.unlink(missing_ok=True)
        ts.unlink(missing_ok=True)
        FakeNotify.calls = []
    try:
        ED.TICK_AT = (24, 0)
        rc, out, err = tick()
        chk("tick: before 05:07 nothing is built or sent", rc == 0 and not dj.exists() and not FakeNotify.calls)
        ED.TICK_AT = (0, 0)
        rc, out, err = tick()
        r = json.loads(dj.read_text())
        chk("tick: the first tick from 05:07 builds yesterday and sends it once", rc == 0
            and r["notify"]["status"] == "sent" and len(FakeNotify.calls) == 1, (rc, r.get("notify"), err))
        before = dj.read_text()
        for _ in range(3):
            tick()
        chk("tick: later ticks on a finished day build and send nothing", len(FakeNotify.calls) == 1
            and dj.read_text() == before and json.loads(ts.read_text()).get("builds") == 1)

        # stopped during the send (supervisord TERM between the send and the write): the digest is already on disk
        # as pending, and the next tick sends it from there without rebuilding
        fresh()

        def killed(rec, waits=None):
            raise KeyboardInterrupt
        ED.send_notify = killed
        try:
            tick()
        except KeyboardInterrupt:
            pass
        ED.send_notify = saved[5]
        r = json.loads(dj.read_text())
        chk("a run stopped during its send leaves the digest written, the send pending",
            r["notify"]["status"] == "pending" and not FakeNotify.calls, r.get("notify"))
        r["containers"]["marker"] = {"sigs": []}
        dj.write_text(json.dumps(r))
        rc, out, err = tick()
        r = json.loads(dj.read_text())
        chk("the next tick sends the pending message once, from the stored digest (not rebuilt)",
            r["notify"]["status"] == "sent" and r["notify"].get("resent") and len(FakeNotify.calls) == 1
            and "marker" in r["containers"], r.get("notify"))
        tick()
        chk("...and not again", len(FakeNotify.calls) == 1)

        # a send that failed for a reason worth retrying is sent again later, one try per tick, until it goes
        fresh()
        FakeNotify.code = 500
        tick()
        r = json.loads(dj.read_text())
        chk("a 5xx after the in-run retry: failed, marked retry_later", r["notify"]["status"] == "failed"
            and r["notify"].get("retry_later") and len(FakeNotify.calls) == 2, (r.get("notify"), FakeNotify.calls))
        ED.RESEND_EVERY_S = 3600
        tick()
        chk("not sent again before RESEND_EVERY_S", len(FakeNotify.calls) == 2)
        ED.RESEND_EVERY_S = 0
        tick()
        chk("sent again after it, one try", len(FakeNotify.calls) == 3)
        FakeNotify.code = 202
        tick()
        tick()
        r = json.loads(dj.read_text())
        chk("once the mail service is back: sent, and not again", r["notify"]["status"] == "sent"
            and len(FakeNotify.calls) == 4, (r.get("notify"), len(FakeNotify.calls)))
        fresh()
        FakeNotify.code = 400
        tick()
        tick()
        r = json.loads(dj.read_text())
        chk("a 400 (the request is wrong) is never resent", r["notify"]["status"] == "failed"
            and not r["notify"].get("retry_later") and len(FakeNotify.calls) == 1, (r.get("notify"), FakeNotify.calls))
        FakeNotify.code = 202
        ED.RESEND_EVERY_S = saved[3]

        # Loki down: the build is tried again every BUILD_EVERY_S, at most TICK_MAX_BUILDS times
        fresh()
        os.environ["LOKI_URL"] = "http://127.0.0.1:9"
        ED.BUILD_EVERY_S = 3600
        rcs = [tick()[0] for _ in range(3)]
        chk("Loki down: one try, then nothing until BUILD_EVERY_S", rcs == [1, 0, 0]
            and json.loads(ts.read_text())["builds"] == 1, (rcs, ts.read_text()))
        ED.BUILD_EVERY_S = 0
        rcs = [tick()[0] for _ in range(ED.TICK_MAX_BUILDS + 2)]
        chk("Loki down: at most TICK_MAX_BUILDS builds a day", rcs.count(1) == ED.TICK_MAX_BUILDS - 1
            and json.loads(ts.read_text())["builds"] == ED.TICK_MAX_BUILDS and not dj.exists(), (rcs, ts.read_text()))
        ts.unlink()
        os.environ["LOKI_URL"] = base
        tick()
        chk("Loki back: the next try builds and sends", json.loads(dj.read_text())["notify"]["status"] == "sent"
            and len(FakeNotify.calls) == 1)
        fresh()
        os.environ.pop("LOKI_URL")
        outs = [tick() for _ in range(3)]
        chk("no LOKI_URL: one line a day, then quiet", outs[0][0] == 2 and outs[1][0] == 0 and outs[2][0] == 0
            and not (outs[1][2] + outs[2][2]).strip() and not dj.exists(), outs)
        os.environ["LOKI_URL"] = base

        # an older image's 05:07 run already sent (or failed without retry_later): the new tick sends nothing
        fresh()
        quiet(ED.main, ["run", "--no-model"])
        r = json.loads(dj.read_text())
        r["notify"] = {"status": "failed", "http": 500, "at": r["notify"]["at"]}
        dj.write_text(json.dumps(r))
        FakeNotify.calls = []
        ED.RESEND_EVERY_S = 0
        tick()
        chk("a day built before the tick existed is neither rebuilt nor sent again", not FakeNotify.calls
            and json.loads(dj.read_text())["notify"]["status"] == "failed")
        chk("the tick state file is private", ts.stat().st_mode & 0o077 == 0)
    finally:
        (ED.urllib.request.urlopen, ED.NOTIFY_WAITS, ED.TICK_AT, ED.RESEND_EVERY_S, ED.BUILD_EVERY_S,
         ED.send_notify) = saved
        FakeNotify.code = 202
        for k in ("ERROR_DIGEST_NOTIFY_URL", "ERROR_DIGEST_NOTIFY_TOKEN_FILE"):
            os.environ.pop(k, None)
        os.environ["LOKI_URL"] = base
    shutil.rmtree(tmp, ignore_errors=True)


def test_notify_lock():
    """The lock really excludes: a resend waits for a holder (a run mid-send), then sees its result."""
    import fcntl
    tmp = Path(tempfile.mkdtemp(prefix="ed-lock-"))
    (tmp / "digests").mkdir()
    y = ED._yesterday()
    dj = tmp / "digests" / (y + ".json")
    dj.write_text(json.dumps({"day": y, "containers": {}, "alerts": {"owner": [{"what": "x"}], "held": []},
                              "notify": {"status": "pending"}}))
    fd = os.open(str(tmp / ".notify.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    calls, done = [], threading.Event()
    real = ED.send_notify
    ED.send_notify = lambda rec, waits=None: calls.append(1) or {"status": "sent"}
    try:
        th = threading.Thread(target=lambda: (ED._resend(tmp, y), done.set()), daemon=True)
        th.start()
        time.sleep(0.4)
        chk("a resend blocks while another holder has the lock", not done.is_set() and not calls)
        r = json.loads(dj.read_text())
        r["notify"] = {"status": "sent"}       # the holder (a run) finished its send
        dj.write_text(json.dumps(r))
        os.close(fd)
        th.join(5)
        chk("...and once it is released, re-reads under the lock and sends nothing", done.is_set() and not calls)
    finally:
        ED.send_notify = real
    shutil.rmtree(tmp, ignore_errors=True)


def test_mail_sample():
    cases = [("Import failed for 'A Title (2019)'", "'<q>'", "Title"),
             ('Album "Some Album" not found', '"<q>"', "Some Album"),
             ("could not move /data/media/Some Folder/x.mkv: denied", "move <path>", "Some Folder"),
             ("cannot open C:\\Downloads\\x.mkv", "open <path>", "Downloads"),
             ("grab Some.Show.S01E02.1080p.WEB-GRP failed", "grab <name> failed", "Show"),
             ("GET https://api.example.test/v2/users/77/orders failed", "https://api.example.test/…", "users"),
             ("GET http://192.0.2.7:8080/api/v1/items/77 failed, will retry", "<ip>/… failed, will retry", "api"),
             ('Album "Some \\"Live\\" Album" not found', '"<q>" not found', "Live"),
             ("could not open:/data/media/Some Folder/x.mkv", "open:<path>", "Some"),
             ("missing ./media/Some Folder/x.mkv", "missing <path>", "Some"),
             ("could not move media/Movies/Some Folder/x.mkv: denied", "move <path>", "Movies"),
             ("cannot open \\\\nas\\media\\Some Folder\\x.mkv", "open <path>", "Some"),
             ("bad link file:///data/media/Some Folder/x.mkv", "link <path>", "Some"),
             ("import failed (path=/data/media/Some Folder/x.mkv)", "(path=<path>", "Some")]
    for line, want, gone in cases:
        got = ED.mail_sample(ED.shape(line))
        chk("mail sample folds %r" % want, want in got and gone not in got, got)
    keep = "[Lutron] Failed to re-subscribe to events, error ECONNRESET; N/N tries"
    eq("mail sample leaves a plain error line alone", ED.mail_sample(keep), keep)


def test_midnight(base):
    """A Deluge outage from 23:35 to 00:10: the shipped rule (sustain 30) holds it on day 1 (20 min) and sends it on
    day 2, measured whole (35 min), with Loki asked for the lines before midnight."""
    tmp = Path(tempfile.mkdtemp(prefix="ed-midnight-"))
    d1, d2 = "2026-11-05", "2026-11-06"
    line = "[Warn] Deluge: Unable to connect to Deluge, please check your settings"
    FakeLoki.logs = {"sonarr": [(ns_at(d1, 23, mm), line) for mm in range(35, 60, 5)] +
                               [(ns_at(d2, 0, mm), line) for mm in range(0, 15, 5)]}
    lk, ar = ED.Loki(base, "tok-test"), ED.alert_rules(tmp)
    r1 = ED.build_day(lk, d1, tmp, ED.noise_rules(tmp), say=lambda *a: None, arules=ar)
    r2 = ED.build_day(lk, d2, tmp, ED.noise_rules(tmp), say=lambda *a: None, arules=ar)
    what = "Sonarr/Radarr/Lidarr could not reach Deluge"
    chk("midnight: day 1 holds the 20 min it saw", [i["what"] for i in r1["alerts"]["held"]] == [what], r1["alerts"])
    o2 = r2["alerts"]["owner"]
    chk("midnight: day 2 sends it, the run measured whole (35 min), today's 3 lines counted",
        [i["what"] for i in o2] == [what] and o2[0]["longest_min"] == 35.0 and o2[0]["count"] == 3, r2["alerts"])
    FakeLoki.logs = {"sonarr": [(ns_at(d2, 0, 30), line)]}
    FakeLoki.calls = []
    ED.build_day(lk, d2, tmp, ED.noise_rules(tmp), say=lambda *a: None, arules=ar)
    chk("midnight: no look back when the first line is more than the gap after midnight",
        sum(1 for pth, q in FakeLoki.calls if pth.endswith("query_range")) == 1, FakeLoki.calls)
    shutil.rmtree(tmp, ignore_errors=True)


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
    chk("the shipped noise rules apply with no noise.json in the digest dir", not (tmp / "noise.json").exists())
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
    chk("the container runs the digest tick (daily build plus catch-up)", "[program:error-digest]" in sup
        and "every.sh 300 nice -n 10 python3 %(ENV_HOME)s/bin/error_digest.py tick" in sup)
    man = (BIN / "queue-image" / "MANIFEST").read_text().split()
    chk("the image ships the digest files", all(f in man for f in ("error_digest.py", "error-digest-runner.py",
                                                                    "error_digest_noise.json")), man[:5])


class FakeQueueApi(http.server.BaseHTTPRequestHandler):
    reply = None       # callable(path, query) -> (code, body bytes)
    seen = []          # (path, query, Authorization)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        FakeQueueApi.seen.append((u.path, u.query, self.headers.get("Authorization")))
        code, b = FakeQueueApi.reply(u.path, u.query)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


def test_api_read():
    tmp = Path(tempfile.mkdtemp(prefix="ed-api-"))
    eq("api: no digests dir -> an empty list, not an error", ED.api_read("", tmp), (200, {"days": []}))
    d = tmp / "digests"
    d.mkdir()
    al = {"owner": [{"what": "a"}], "claude": [{"what": "b"}], "held": [{"what": "c"}, {"what": "d"}],
          "new_for_claude": [{"container": "x", "id": "1", "count": 2, "text": "t"}], "new_for_claude_more": 3}
    (d / "2026-11-10.json").write_text(json.dumps({"day": "2026-11-10", "alerts": al, "model": {"status": "done", "model": "m"},
                                                   "notify": {"status": "failed", "why": "https://hook.example/SECRET"}}))
    (d / "2026-11-10.md").write_text("# digest 10\n")
    (d / "2026-11-11.json").write_text(json.dumps({"day": "2026-11-11"}))
    (d / "2026-11-12.json").write_text("[1, 2]")
    (d / "2026-11-13.json").write_text("{not json")
    (d / "notes.json").write_text("{}")
    (d / "2026-11-1x.json").write_text("{}")
    (d / "2026-11-10.json.tmp").write_text("{}")
    st, obj = ED.api_read("", tmp)
    eq("api: list is newest first, only real day files", (st, [r["day"] for r in obj["days"]]),
        (200, ["2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10"]), obj)
    r10 = obj["days"][-1]
    eq("api: list counts (for Claude = claude + new + more)", (r10["owner"], r10["claude"], r10["held"]), (1, 5, 2), r10)
    eq("api: only the send and model statuses are passed on", (r10["notify"], r10["model"]), ("failed", "done"), r10)
    chk("api: nothing else of the send record leaks", "SECRET" not in json.dumps(obj), obj)
    eq("api: damaged or non-object records count as empty", [(r["owner"], r["notify"]) for r in obj["days"][:2]],
        [(0, None), (0, None)], obj)
    st, obj = ED.api_read("day=2026-11-10", tmp)
    eq("api: a day -> its Markdown and routed items", (st, obj["md"], obj["alerts"]["claude"], obj["md_truncated"]),
        (200, "# digest 10\n", [{"what": "b"}], False), obj)
    eq("api: a day keeps the send record private too", ("SECRET" not in json.dumps(obj), obj["notify"]), (True, "failed"))
    eq("api: a day with JSON but no Markdown -> empty md",
        (lambda r: (r[0], r[1]["md"]))(ED.api_read("day=2026-11-11", tmp)), (200, ""))
    eq("api: a day with nothing -> 404", ED.api_read("day=2026-11-20", tmp)[0], 404)
    bad = ["day=..%2F..%2Fetc%2Fpasswd", "day=2026-11-10%0A", "day=2026-13-40", "day=2026-11-10%2F..%2F..%2Fx",
           "day=20261110", "day=2026-11-1x", "day=%2Ftmp%2Fx"]
    eq("api: anything but a real YYYY-MM-DD -> 400", [ED.api_read(q, tmp)[0] for q in bad], [400] * len(bad))
    (d / "2026-11-14.md").write_text("x" * (ED.API_MAX_MD + 10))
    st, obj = ED.api_read("day=2026-11-14", tmp)
    eq("api: Markdown is capped", (st, len(obj["md"]), obj["md_truncated"]), (200, ED.API_MAX_MD, True))
    for k in range(20):
        (d / ("2026-10-%02d.json" % (k + 1))).write_text("{}")
    eq("api: the list stops at API_DAYS", len(ED.api_read("", tmp)[1]["days"]), ED.API_DAYS)
    old = ED.digest_dir
    ED.digest_dir = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        eq("api: an unexpected error -> 500 with the type only", ED.api_read(""), (500, {"error": "RuntimeError"}))
    finally:
        ED.digest_dir = old
    shutil.rmtree(tmp, ignore_errors=True)


def test_remote_show():
    import types
    srv, base = serve(FakeQueueApi)
    fake = types.ModuleType("queue_config")
    fake.queue_api = lambda: (base, "tok-remote")
    fake.queue_api_headers = lambda: {"Authorization": "Bearer tok-remote"}
    fake.queue_is_remote = lambda: True
    saved_mod, saved_dir = sys.modules.get("queue_config"), os.environ.pop("ERROR_DIGEST_DIR", None)
    sys.modules["queue_config"] = fake
    try:
        eq("remote: used when the queue is on another machine", ED._use_remote(), True)
        os.environ["ERROR_DIGEST_DIR"] = "/nonexistent"
        eq("remote: never when ERROR_DIGEST_DIR is set (the container, the tests)", ED._use_remote(), False)
        os.environ.pop("ERROR_DIGEST_DIR")
        FakeQueueApi.reply = lambda path, q: (200, json.dumps(
            {"day": "2026-11-10", "md": "# remote digest\n", "md_truncated": False}).encode())
        rc, out, err = quiet(ED.main, ["show", "--day", "2026-11-10"])
        eq("remote show prints the served Markdown", (rc, out), (0, "# remote digest\n"), err)
        eq("remote show asks for that day with the queue token",
            FakeQueueApi.seen[-1], ("/api/error-digest", "day=2026-11-10", "Bearer tok-remote"))
        chk("remote show never prints the token", "tok-remote" not in out + err)
        FakeQueueApi.reply = lambda path, q: (200, json.dumps({"days": [
            {"day": "2026-11-10", "owner": 1, "claude": 5, "held": 2, "notify": "sent", "model": "done"}]}).encode())
        rc, out, err = quiet(ED.main, ["days"])
        eq("remote days lists the counts", (rc, out.strip()),
            (0, "2026-11-10  for you 1, for Claude 5, held 2, message sent, model done"), err)
        eq("remote days asks without a day", FakeQueueApi.seen[-1][:2], ("/api/error-digest", ""))
        FakeQueueApi.reply = lambda path, q: (404, b"not found")
        rc, out, err = quiet(ED.main, ["show", "--day", "2026-11-10"])
        chk("remote show on an older image says the route is missing", rc == 1 and "older image" in err, err)
        FakeQueueApi.reply = lambda path, q: (404, json.dumps({"error": "no digest for 2026-11-10"}).encode())
        rc, out, err = quiet(ED.main, ["show", "--day", "2026-11-10"])
        chk("remote show with no digest for the day says so", rc == 1 and "no digest for 2026-11-10" in err, err)
        FakeQueueApi.reply = lambda path, q: (401, json.dumps({"ok": False, "error": "unauthorized"}).encode())
        rc, out, err = quiet(ED.main, ["days"])
        chk("remote days on a refused token: rc 1, says 401, no token", rc == 1 and "401" in err
            and "tok-remote" not in out + err, err)
        srv.shutdown()
        srv.server_close()
        rc, out, err = quiet(ED.main, ["show", "--day", "2026-11-10"])
        chk("remote show with the queue down: rc 1, says it did not answer", rc == 1 and "did not answer" in err, err)
    finally:
        if saved_mod is not None:
            sys.modules["queue_config"] = saved_mod
        else:
            sys.modules.pop("queue_config", None)
        if saved_dir is not None:
            os.environ["ERROR_DIGEST_DIR"] = saved_dir


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
        test_noise_local_adds()
        test_alerts()
        test_notify(lbase)
        test_tick(lbase)
        test_notify_lock()
        test_mail_sample()
        test_midnight(lbase)
        test_supervisor_and_allowlist()
        test_api_read()
        test_remote_show()
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
