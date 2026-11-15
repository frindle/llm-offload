#!/usr/bin/env python3
"""test-queue-api-auth.py -- write-route auth (QUEUE_API_TOKEN) and the two mirror routes of the queue dashboard API
(ollama-queue-api.py), run against the REAL Handler on 127.0.0.1 port 0.

Proves: no token = every route open (as before); token + loopback peer = open; token + non-loopback peer (simulated by a
Handler subclass that reports a LAN address) without / with a wrong / with a right bearer = 401 / 401 / served; a loopback
peer that carries a Cloudflare / proxy header still needs the token; /api/cpu/* and read-only GETs are untouched; the token
comes from the env first, else a file; the mirror routes need the token (non-loopback), page by `after` / `limit` (cap
200), read the rotated `.1` file, skip half lines and tolerate missing files.

Usage: test-queue-api-auth.py [--api PATH]. Exit 0 = all pass. Run it on the sandbox, never on the Mac.
"""
import argparse
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


class Hdrs(dict):
    """Case-insensitive .get, like the real header object."""
    def __init__(self, d):
        super().__init__({k.lower(): v for k, v in d.items()})

    def get(self, k, default=None):
        return super().get(k.lower(), default)


def load_api(path):
    sys.path.insert(0, str(Path(path).parent))
    try:
        import queue_config  # noqa: F401
    except ImportError:                      # the dashboard branch imports it from ~/bin; stand in for it
        m = types.ModuleType("queue_config")
        m.state_dir = lambda: Path(tempfile.mkdtemp(prefix="qa-state-"))
        sys.modules["queue_config"] = m
    spec = importlib.util.spec_from_file_location("oqapi_auth", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def serve(api, peer=None):
    """Real Handler on loopback; with peer, the handler believes the client connected from that address."""
    H = api.Handler
    if peer:
        class H(api.Handler):                # noqa: N801
            def __init__(self, request, client_address, server):
                super().__init__(request, (peer, client_address[1]), server)
    srv = api.ThreadingServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    h = dict(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    b = r.read()
    hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    try:
        return r.status, json.loads(b), hd
    except ValueError:
        return r.status, b, hd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))   # same default as test-cpu-lane-api.py
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="qa-auth-"))
    os.environ["CPU_LANE_DIR"] = str(tmp / "cpu-jobs")
    os.environ["CPU_RUNNER_TOKEN_FILE"] = str(tmp / "cpu-token")
    os.environ["CPU_LANE_LLM_LOCAL_JSON"] = str(tmp / "no-local.json")
    os.environ["QEL_DIR"] = str(tmp / "qel")
    os.environ.pop("QUEUE_API_TOKEN", None)
    api = load_api(a.api)
    api.QUEUE_API_TOKEN_FILE = tmp / "cfg" / "queue-api-token"
    LAN = "192.0.2.7"
    TOK = "s3cret-token-value"
    bearer = {"Authorization": "Bearer " + TOK}
    # a state-changing route that reaches its own validation (400) without touching any queue state
    POST = ("POST", "/api/jobs", {})
    DELETE = ("DELETE", "/api/jobs/zzzz", None)
    KILL = ("POST", "/api/jobs/zzzz/kill", None)

    # ---- helper units -------------------------------------------------------------------------------------------
    check("loopback: 127.0.0.1 / ::1 / ::ffff:127.0.0.1", [api._is_loopback_peer(x) for x in ("127.0.0.1", "::1", "::ffff:127.0.0.1")], [True] * 3)
    check("loopback: LAN, public, junk are not", [api._is_loopback_peer(x) for x in (LAN, "8.8.8.8", "", "nope")], [False] * 4)
    check("helper: no token -> always ok", api._write_auth_ok(LAN, Hdrs({}), token=""), True)
    check("helper: token, LAN peer, no header -> no", api._write_auth_ok(LAN, Hdrs({}), token=TOK), False)
    check("helper: token, LAN peer, right bearer -> ok", api._write_auth_ok(LAN, Hdrs({"Authorization": "Bearer " + TOK}), token=TOK), True)
    check("helper: scheme is case-insensitive", api._write_auth_ok(LAN, Hdrs({"Authorization": "bearer " + TOK}), token=TOK), True)
    check("helper: wrong / prefix / empty / Basic -> no", [api._write_auth_ok(LAN, Hdrs({"Authorization": v}), token=TOK)
          for v in ("Bearer nope", "Bearer " + TOK[:-1], "Bearer ", "Basic " + TOK, TOK)], [False] * 5)
    check("helper: loopback peer -> ok without header", api._write_auth_ok("127.0.0.1", Hdrs({}), token=TOK), True)
    check("helper: loopback + Cf-Ray -> needs the token", api._write_auth_ok("127.0.0.1", Hdrs({"Cf-Ray": "x"}), token=TOK), False)
    check("helper: loopback + X-Forwarded-For + right bearer -> ok",
          api._write_auth_ok("127.0.0.1", Hdrs({"X-Forwarded-For": "1.2.3.4", "Authorization": "Bearer " + TOK}), token=TOK), True)

    # ---- token source: env, then file, then none -------------------------------------------------------------------
    check("token: none configured", api._queue_api_token(), "")
    (tmp / "cfg").mkdir()
    api.QUEUE_API_TOKEN_FILE.write_text("filetok\nignored\n")
    check("token: from the file (first line)", api._queue_api_token(), "filetok")
    os.environ["QUEUE_API_TOKEN"] = "envtok"
    check("token: env wins over the file", api._queue_api_token(), "envtok")
    os.environ.pop("QUEUE_API_TOKEN")
    api.QUEUE_API_TOKEN_FILE.write_text("\n")
    check("token: an empty file is no token", api._queue_api_token(), "")
    os.remove(api.QUEUE_API_TOKEN_FILE)

    lo, lan = serve(api), serve(api, LAN)

    # ---- no token: exactly as before ------------------------------------------------------------------------------------
    for nm, srv in (("loopback", lo), ("LAN peer", lan)):
        check("no token / %s: POST /api/jobs reaches its own validation" % nm, call(srv, *POST)[0], 400)
        check("no token / %s: DELETE reaches the handler (not 401)" % nm, call(srv, *DELETE)[0] != 401, True)
        want = 200 if nm == "loopback" else 403      # new routes: with no token, loopback only
        check("no token / %s: mirror events -> %d" % (nm, want), call(srv, "GET", "/api/mirror/events")[0], want)
        check("no token / %s: mirror escalations -> %d" % (nm, want), call(srv, "GET", "/api/mirror/escalations")[0], want)
    check("no token / loopback + Cf header: mirror refused",
          call(lo, "GET", "/api/mirror/events", headers={"Cf-Ray": "x"})[0], 403)
    # /api/results: durable job results for a remote `ollama-queue.py results` (fake rows; the log dir is not read)
    rows = [{"id": "r1", "label": "fix-a", "verdict": "pass", "status": "done", "timestamp": "2026-11-12T03:00:00"},
            {"id": "r2", "label": "fix-b", "verdict": "fail", "status": "done", "timestamp": "2026-11-12T02:00:00"},
            {"id": "r3", "label": "other", "verdict": "pass", "status": "done", "timestamp": "2026-11-12T01:00:00"}]
    api.q._iter_job_results = lambda log_dir=None: [dict(r) for r in rows]
    check("no token / LAN peer: results -> 403", call(lan, "GET", "/api/results")[0], 403)
    check("no token / loopback: results -> 200", call(lo, "GET", "/api/results")[0], 200)
    check("no token / loopback + Cf header: results refused", call(lo, "GET", "/api/results", headers={"Cf-Ray": "x"})[0], 403)

    # /api/cli: run an allowlisted command on the queue host. No token: loopback only, and never with a proxy header.
    CLI_OK = ("POST", "/api/cli", {"tool": "qctl", "argv": ["--help"]})
    check("no token / LAN peer: /api/cli -> 403", call(lan, *CLI_OK)[0], 403)
    st, body, _h = call(lo, *CLI_OK)
    check("no token / loopback: /api/cli --help runs (rc 0, usage on stdout)", (st, body.get("rc"), "usage" in body.get("stdout", "").lower()), (200, 0, True))
    check("no token / loopback + Cf header: /api/cli -> 403", call(lo, *CLI_OK, headers={"Cf-Ray": "x"})[0], 403)

    # ---- token configured -------------------------------------------------------------------------------------------------
    os.environ["QUEUE_API_TOKEN"] = TOK
    st, body, hd = call(lan, *POST)
    check("token / LAN: POST without bearer -> 401 JSON + WWW-Authenticate", (st, body.get("error"), hd.get("www-authenticate")), (401, "unauthorized", "Bearer"))
    for nm, req in (("POST", POST), ("DELETE", DELETE), ("kill", KILL), ("PUT", ("PUT", "/api/jobs", {}))):
        check("token / LAN: %s without bearer -> 401" % nm, call(lan, *req)[0], 401)
        check("token / LAN: %s wrong bearer -> 401" % nm, call(lan, *req, headers={"Authorization": "Bearer wrong"})[0], 401)
    check("token / LAN: POST right bearer -> served (400 validation)", call(lan, *POST, headers=bearer)[0], 400)
    check("token / LAN: DELETE right bearer -> past the gate", call(lan, *DELETE, headers=bearer)[0] != 401, True)
    check("token / LAN: kill right bearer -> past the gate", call(lan, *KILL, headers=bearer)[0] != 401, True)
    check("token / LAN: docsum POST without bearer -> 401", call(lan, "POST", "/docsum/api/x", {})[0], 401)
    check("token / LAN: bypass POST without bearer -> 401", call(lan, "POST", "/api/bypass/x", {})[0], 401)
    check("token / LAN: chat POST without bearer -> 401", call(lan, "POST", "/api/chat/x", {})[0], 401)
    check("token / loopback: POST open", call(lo, *POST)[0], 400)
    check("token / loopback: DELETE open", call(lo, *DELETE)[0] != 401, True)
    check("token / loopback + Cf-Ray: POST needs the bearer", call(lo, *POST, headers={"Cf-Ray": "abc"})[0], 401)
    check("token / loopback + Cf-Ray + bearer: ok", call(lo, *POST, headers={"Cf-Ray": "abc", **bearer})[0], 400)
    check("token / LAN: /api/cpu/health stays open (own auth)", call(lan, "GET", "/api/cpu/health")[0], 200)
    cs = call(lan, "POST", "/api/cpu/claim", {"runner_id": "r"})[0]
    check("token / LAN: /api/cpu/* keeps its OWN auth (401 from the runner token, not our gate)", cs, 401)
    check("token / LAN: a runner-token header is not accepted as the queue token",
          call(lan, "POST", "/api/cpu/claim", {"runner_id": "r"}, headers=bearer)[0], 401)
    check("token / LAN: read-only GET /api/cpu-lane stays open", call(lan, "GET", "/api/cpu-lane")[0], 200)
    check("token / LAN: read-only GET /api/hosts stays open", call(lan, "GET", "/api/hosts")[0] != 401, True)

    # ---- /api/results with a token: ------------------------------------------------------------------------------------------------
    check("results: LAN peer without bearer -> 401", call(lan, "GET", "/api/results")[0], 401)
    check("results: LAN peer with a wrong bearer -> 401", call(lan, "GET", "/api/results", headers={"Authorization": "Bearer x"})[0], 401)
    r = call(lan, "GET", "/api/results", headers=bearer)
    check("results: right bearer -> every row, newest first", (r[0], [x["id"] for x in r[1]["results"]]), (200, ["r1", "r2", "r3"]))
    r = call(lan, "GET", "/api/results?verdict=pass&limit=1", headers=bearer)
    check("results: verdict + limit filter like the CLI", [x["id"] for x in r[1]["results"]], ["r1"])
    r = call(lan, "GET", "/api/results?label=FIX&verdict=fail", headers=bearer)
    check("results: label is a case-insensitive substring", [x["id"] for x in r[1]["results"]], ["r2"])
    check("results: limit=junk -> 400", call(lan, "GET", "/api/results?limit=abc", headers=bearer)[0], 400)
    check("results: limit=0 -> all", len(call(lan, "GET", "/api/results?limit=0", headers=bearer)[1]["results"]), 3)

    # ---- mirror routes -------------------------------------------------------------------------------------------------------------
    qel = tmp / "qel"
    ev = lambda n: {"seq": n, "ts": "2026-11-12T10:00:00+0000", "id": "job%d" % n, "label": "x", "state": "done"}
    # missing files: empty but valid
    r = call(lan, "GET", "/api/mirror/events", headers=bearer)
    check("mirror: missing files -> empty events, last_seq 0, failed null", (r[0], r[1]["events"], r[1]["last_seq"], r[1]["failed"]), (200, [], 0, None))
    r = call(lan, "GET", "/api/mirror/escalations", headers=bearer)
    check("mirror: missing escalation files -> nulls", (r[0], r[1]["files"]), (200, {"ESCALATIONS.md": None, "READY-TO-LAND.md": None}))
    check("mirror: events need the token on a LAN peer", call(lan, "GET", "/api/mirror/events")[0], 401)
    check("mirror: escalations need the token on a LAN peer", call(lan, "GET", "/api/mirror/escalations")[0], 401)
    check("mirror: wrong token -> 401", call(lan, "GET", "/api/mirror/events", headers={"Authorization": "Bearer x"})[0], 401)
    check("mirror: loopback peer is open", call(lo, "GET", "/api/mirror/events")[0], 200)

    qel.mkdir()
    (qel / "queue-events.jsonl.1").write_text("".join(json.dumps(ev(n)) + "\n" for n in range(1, 6)))
    (qel / "queue-events.jsonl").write_text("".join(json.dumps(ev(n)) + "\n" for n in range(6, 11)) + '{"seq": 11, "id": "ha')
    (qel / "queue-events.failed.json").write_text(json.dumps({"ts": 1.5, "failed": [{"id": "job3"}]}))
    r = call(lan, "GET", "/api/mirror/events", headers=bearer)[1]
    check("mirror: rotated .1 first, then the live file, oldest first; the half line is skipped",
          ([e["seq"] for e in r["events"]], r["last_seq"]), (list(range(1, 11)), 10))
    check("mirror: failed snapshot passed through", r["failed"], {"ts": 1.5, "failed": [{"id": "job3"}]})
    check("mirror: generated_at is a number", isinstance(r["generated_at"], float), True)
    r = call(lan, "GET", "/api/mirror/events?after=7", headers=bearer)[1]
    check("mirror: after=7", [e["seq"] for e in r["events"]], [8, 9, 10])
    check("mirror: after=10 (caught up) -> none, last_seq still 10", (call(lan, "GET", "/api/mirror/events?after=10", headers=bearer)[1]["events"],
          call(lan, "GET", "/api/mirror/events?after=10", headers=bearer)[1]["last_seq"]), ([], 10))
    r = call(lan, "GET", "/api/mirror/events?after=2&limit=3", headers=bearer)[1]
    check("mirror: limit=3 -> the OLDEST three above after", [e["seq"] for e in r["events"]], [3, 4, 5])
    check("mirror: limit=0 / -5 / junk are clamped, never an error", [len(call(lan, "GET", "/api/mirror/events?limit=%s" % v, headers=bearer)[1]["events"]) for v in ("0", "-5", "abc")], [1, 1, 10])
    check("mirror: after=junk -> from the start", len(call(lan, "GET", "/api/mirror/events?after=zz", headers=bearer)[1]["events"]), 10)
    (qel / "queue-events.jsonl").write_text("".join(json.dumps(ev(n)) + "\n" for n in range(6, 506)))
    r = call(lan, "GET", "/api/mirror/events?limit=100000", headers=bearer)[1]
    check("mirror: limit is capped at 200 (default also 200)", (len(r["events"]), len(call(lan, "GET", "/api/mirror/events", headers=bearer)[1]["events"]), r["last_seq"]), (200, 200, 505))
    r = call(lan, "GET", "/api/mirror/events?after=200&limit=200", headers=bearer)[1]
    check("mirror: paging continues across the cap", (r["events"][0]["seq"], r["events"][-1]["seq"]), (201, 400))
    (qel / "queue-events.jsonl.1").unlink()
    check("mirror: no .1 file is fine", call(lan, "GET", "/api/mirror/events?after=500", headers=bearer)[1]["events"][0]["seq"], 501)
    (qel / "queue-events.failed.json").write_text("{not json")
    check("mirror: a damaged failed snapshot -> null, not a 500", call(lan, "GET", "/api/mirror/events?after=500", headers=bearer)[1]["failed"], None)
    (qel / "escalations").mkdir()
    (qel / "escalations" / "ESCALATIONS.md").write_text("- [ ] one\n")
    (qel / "escalations" / "OTHER.md").write_text("never served")
    r = call(lan, "GET", "/api/mirror/escalations", headers=bearer)[1]
    check("mirror: escalations -> the two names only; absent = null", r["files"], {"ESCALATIONS.md": "- [ ] one\n", "READY-TO-LAND.md": None})
    (qel / "escalations" / "READY-TO-LAND.md").write_text("x" * (api.MIRROR_MAX_TEXT_BYTES + 5000))
    r = call(lan, "GET", "/api/mirror/escalations", headers=bearer)[1]
    check("mirror: a huge file is cut to the cap", len(r["files"]["READY-TO-LAND.md"]) <= api.MIRROR_MAX_TEXT_BYTES, True)
    check("mirror: query string on escalations is fine", call(lan, "GET", "/api/mirror/escalations?x=1", headers=bearer)[0], 200)

    # ---- /api/cli with a token: ----------------------------------------------------------------------------------------------------
    def cli(srv, body, headers=None, raw=None):
        return call(srv, "POST", "/api/cli", body, headers=headers if headers is not None else bearer)
    check("cli: LAN without bearer -> 401", call(lan, *CLI_OK)[0], 401)
    check("cli: LAN with a wrong bearer -> 401", call(lan, *CLI_OK, headers={"Authorization": "Bearer wrong"})[0], 401)
    check("cli: LAN with the bearer and a Cf header -> 403", call(lan, *CLI_OK, headers={"Cf-Connecting-Ip": "1.2.3.4", **bearer})[0], 403)
    check("cli: loopback with the bearer and a Cf header -> 403", call(lo, *CLI_OK, headers={"Cf-Ray": "x", **bearer})[0], 403)
    st, body, _h = cli(lan, CLI_OK[2])
    check("cli: LAN with the bearer -> 200 {rc, stdout, stderr, truncated}",
          (st, body.get("rc"), "usage" in body.get("stdout", "").lower(), body.get("stderr"), body.get("truncated")), (200, 0, True, "", False))
    st, body, _h = cli(lan, {"tool": "ollama-queue.py", "argv": ["ttc", "--help"]})
    check("cli: ollama-queue.py ttc --help runs for real", (st, body.get("rc")), (200, 0))
    for nm, req in (("a tool path", {"tool": "/bin/echo", "argv": ["x"]}),
                    ("a relative tool path", {"tool": "../ollama-queue.py", "argv": ["status"]}),
                    ("an unknown tool", {"tool": "bash", "argv": ["-c", "id"]}),
                    ("no tool", {"argv": ["status"]}),
                    ("a verb outside the allowlist (run)", {"tool": "ollama-queue.py", "argv": ["run"]}),
                    ("enqueue", {"tool": "ollama-queue.py", "argv": ["enqueue", "--help"]}),
                    ("clear-card", {"tool": "ollama-queue.py", "argv": ["clear-card", "x"]}),
                    ("results", {"tool": "ollama-queue.py", "argv": ["results"]}),
                    ("needs-opus", {"tool": "ollama-queue.py", "argv": ["needs-opus"]}),
                    ("a qctl verb that does not exist", {"tool": "qctl", "argv": ["triage", "x"]}),
                    ("an ollama-queue verb sent to qctl", {"tool": "qctl", "argv": ["focus", "x"]}),
                    ("an empty argv", {"tool": "qctl", "argv": []}),
                    ("65 args", {"tool": "qctl", "argv": ["why"] + ["x"] * 64}),
                    ("a NUL byte", {"tool": "qctl", "argv": ["why", "a\0b"]}),
                    ("an arg over 4096 chars", {"tool": "qctl", "argv": ["why", "x" * 4097]}),
                    ("over 64 KB in all", {"tool": "qctl", "argv": ["why"] + ["y" * 4000] * 17}),
                    ("a non-string arg", {"tool": "qctl", "argv": ["why", 5]}),
                    ("argv not a list", {"tool": "qctl", "argv": "why x"}),
                    ("a body that is not an object", ["qctl"])):
        check("cli: 400 for " + nm, cli(lan, req)[0], 400)
    # the subprocess: no shell, stdin closed, FORCE_LOCAL in the env, the state dir as cwd, the tool in the API's own dir
    seen_calls = []
    real_run = api.subprocess.run

    def fake_run(cmd, **kw):
        seen_calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=3, stdout=b"o" * (api.Handler._CLI_MAX_OUT + 10) + b"TAIL", stderr=b"err")
    api.subprocess.run = fake_run
    try:
        st, body, _h = cli(lan, {"tool": "ollama-queue.py", "argv": ["focus", "job-1", "--x", "a b;c"]})
    finally:
        api.subprocess.run = real_run
    cmd, kw = seen_calls[0] if seen_calls else ([], {})
    check("cli: argv list, our interpreter, the API's own tool, no shell",
          (cmd[:1] == [sys.executable], os.path.basename(cmd[1]) if len(cmd) > 1 else None, cmd[2:], kw.get("shell")),
          (True, "ollama-queue.py", ["focus", "job-1", "--x", "a b;c"], None))
    check("cli: tool sits beside the API file", os.path.dirname(cmd[1]), os.path.dirname(os.path.abspath(a.api)))
    check("cli: env has OLLAMA_QUEUE_FORCE_LOCAL=1 and QCTL_FORCE_LOCAL=1",
          (kw["env"].get("OLLAMA_QUEUE_FORCE_LOCAL"), kw["env"].get("QCTL_FORCE_LOCAL")), ("1", "1"))
    check("cli: stdin closed, 180 s timeout, state dir as cwd",
          (kw["stdin"] == api.subprocess.DEVNULL, kw["timeout"], kw["cwd"]), (True, 180, str(api.queue_config.state_dir())))
    check("cli: rc as returned (3 is still HTTP 200), stdout cut to its last 256 KB, truncated true",
          (st, body["rc"], len(body["stdout"]), body["stdout"].endswith("TAIL"), body["stderr"], body["truncated"]),
          (200, 3, api.Handler._CLI_MAX_OUT, True, "err", True))

    def slow_run(cmd, **kw):
        raise api.subprocess.TimeoutExpired(cmd, 180)
    api.subprocess.run = slow_run
    try:
        st, body, _h = cli(lan, {"tool": "qctl", "argv": ["status"]})
    finally:
        api.subprocess.run = real_run
    check("cli: a timeout -> JSON 504", (st, body.get("ok")), (504, False))

    # the token never appears in a response
    check("token never echoed", TOK in json.dumps(call(lan, *POST)[1]), False)
    for s in (lo, lan):
        s.shutdown()
        s.server_close()
    bad = RESULTS.count(False)
    print("\n%d checks, %d failed" % (len(RESULTS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
