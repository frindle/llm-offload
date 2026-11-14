#!/usr/bin/env python3
"""test-queue-standby.py -- standby mode (QUEUE_STANDBY=1) of the queue API and the safe state import, run against the REAL
Handler on 127.0.0.1 port 0 with made-up data and fake tokens.

Proves: outside standby nothing changes (writes reach their own validation, /api/standby/* is 404); in standby every
POST / PUT / DELETE (the /api/cpu/* writes too) answers 503 {"error":"standby"} before auth, GETs still work, the admin
routes need the bearer (401 / 403, no loopback exemption); the import refuses absolute paths, '..', links, devices,
names outside the allowlist, a bad or missing sha256, an oversize archive and a non-empty state dir without ?replace=1;
a good import lands the files, a replace keeps the old state as a backup; the cases upload (POST /api/standby/cases)
takes only cases/, keeps the exec bit, replaces the folder whole, leaves no stage or backup folder and is refused while a
back-test runs; agent_post and notify do nothing in standby.

Usage: test-queue-standby.py [--api PATH]. Exit 0 = all pass. Run it on the sandbox, never on the Mac.
"""
import argparse
import hashlib
import http.client
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []
FAILED = []   # repeated in the last lines: the publish gate keeps only a test's output tail


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    line = ("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}")
    if not ok:
        FAILED.append(line)
    print(line)


def load_api(path):
    sys.path.insert(0, str(Path(path).parent))
    spec = importlib.util.spec_from_file_location("oqapi_standby", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def serve(api):
    srv = api.ThreadingServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(srv, method, path, body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    h = dict(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    b = r.read()
    c.close()
    try:
        return r.status, json.loads(b)
    except ValueError:
        return r.status, b


def tar_bytes(members, manifest=None):
    """members: list of (name, bytes | ('exec', bytes) | ('link', target) | ('dev',) | ('dir',))."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tf:
        for name, content in members:
            ti = tarfile.TarInfo(name)
            if isinstance(content, tuple) and content[0] == "link":
                ti.type, ti.linkname = tarfile.SYMTYPE, content[1]
                tf.addfile(ti)
            elif isinstance(content, tuple) and content[0] == "dev":
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
            elif isinstance(content, tuple) and content[0] == "exec":
                ti.size, ti.mode = len(content[1]), 0o755
                tf.addfile(ti, io.BytesIO(content[1]))
            elif isinstance(content, tuple) and content[0] == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(content)
                tf.addfile(ti, io.BytesIO(content))
        if manifest is not None:
            mb = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(mb)
            tf.addfile(ti, io.BytesIO(mb))
    return bio.getvalue()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="qa-standby-"))
    state, disp = tmp / "state", tmp / "disp"
    state.mkdir()
    disp.mkdir()
    os.environ["QUEUE_STATE_DIR"] = str(state)
    os.environ["QUEUE_DISPATCH_DIR"] = str(disp)
    os.environ["QUEUE_BACKTEST_DIR"] = str(tmp / "bt")
    os.environ["CPU_LANE_DIR"] = str(disp / "cpu-jobs")
    os.environ["CPU_RUNNER_TOKEN_FILE"] = str(tmp / "cpu-token")
    os.environ["CPU_LANE_LLM_LOCAL_JSON"] = str(tmp / "no-local.json")
    os.environ["QEL_DIR"] = str(tmp / "qel")
    os.environ.pop("QUEUE_STANDBY", None)
    os.environ.pop("QUEUE_API_TOKEN", None)
    api = load_api(a.api)
    api.QUEUE_API_TOKEN_FILE = tmp / "cfg" / "queue-api-token"
    import queue_config
    import queue_standby
    TOK = "fake-standby-token"
    bearer = {"Authorization": "Bearer " + TOK}
    srv = serve(api)

    # ---- not standby: as before ------------------------------------------------------------------------------------
    check("off: standby() False", queue_config.standby(), False)
    check("off: POST /api/jobs reaches its own validation", call(srv, "POST", "/api/jobs", {})[0], 400)
    check("off: /api/standby/import is 404", call(srv, "POST", "/api/standby/import", raw=b"x", headers=bearer)[0], 404)
    check("off: /api/standby/cases is 404", call(srv, "POST", "/api/standby/cases", raw=b"x", headers=bearer)[0], 404)
    check("off: GET /api/standby/backtest/abc is 404", call(srv, "GET", "/api/standby/backtest/abc", headers=bearer)[0], 404)

    # ---- standby ---------------------------------------------------------------------------------------------------
    os.environ["QUEUE_STANDBY"] = "1"
    check("on: standby() True", queue_config.standby(), True)
    for m, p, b in (("POST", "/api/jobs", {}), ("POST", "/api/jobs/zzzz/kill", None), ("DELETE", "/api/jobs/zzzz", None),
                    ("PUT", "/api/jobs/zzzz", {}), ("POST", "/api/cpu/lease", {}), ("POST", "/api/cpu/jobs", {}),
                    ("PUT", "/api/cpu/jobs/abc/result", {}), ("POST", "/api/hold", {}), ("POST", "/api/mirror/events", {})):
        st, body = call(srv, m, p, b)
        check("on: %s %s -> 503 standby" % (m, p), (st, body), (503, {"error": "standby"}))
    check("on: 503 also with a valid bearer", call(srv, "POST", "/api/jobs", {}, headers=bearer)[0], 503)
    check("on: GET / still served", call(srv, "GET", "/")[0], 200)
    check("on: GET /api/jobs still served", call(srv, "GET", "/api/jobs")[0], 200)

    # admin routes: auth first
    st, _ = call(srv, "POST", "/api/standby/import", raw=b"x")
    check("on: import with no token configured -> 403", st, 403)
    (tmp / "cfg").mkdir()
    api.QUEUE_API_TOKEN_FILE.write_text(TOK + "\n")
    check("on: import without bearer -> 401", call(srv, "POST", "/api/standby/import", raw=b"x")[0], 401)
    check("on: import wrong bearer -> 401", call(srv, "POST", "/api/standby/import", raw=b"x", headers={"Authorization": "Bearer nope"})[0], 401)
    check("on: backtest status without bearer -> 401", call(srv, "GET", "/api/standby/backtest/abcdefabcdef")[0], 401)
    check("on: backtest unknown id -> 404", call(srv, "GET", "/api/standby/backtest/abcdefabcdef", headers=bearer)[0], 404)
    check("on: backtest bad parts -> 400", call(srv, "POST", "/api/standby/backtest", {"parts": ["B9"]}, headers=bearer)[0], 400)

    # ---- import: refusals leave the dirs empty ------------------------------------------------------------------------
    def imp(blob, sha_hex=None, qs="", hdr=None):
        h = dict(bearer)
        h["X-Content-SHA256"] = sha_hex if sha_hex is not None else sha(blob)
        h.update(hdr or {})
        return call(srv, "POST", "/api/standby/import" + qs, raw=blob, headers=h)

    def empty():
        left = [p.name for p in list(state.iterdir()) + list(disp.iterdir()) if p.name not in (".backtest", "cpu-jobs", "ollama-queue-state.lock")]
        if left:
            print("     left:", left)
        return not left

    good = [("state/ollama-queue-state.json", b'{"jobs": []}'), ("state/ollama-queue-logs/j1.json", b"{}"),
            ("dispatch/hosts.json", b"{}"), ("dispatch/cpu-jobs/note.txt", b"x")]
    bad = {
        "absolute path": [("/etc/evil", b"x")],
        "dot-dot": [("state/../../evil", b"x")],
        "symlink": [("state/ollama-queue-logs", ("link", "/etc"))],
        "device": [("state/ollama-queue-logs/dev", ("dev",))],
        "unknown top": [("other/x", b"x")],
        "name outside the allowlist": [("state/not-allowed.json", b"x")],
        "dispatch worktrees": [("dispatch/worktrees/x", b"x")],
        "backslash": [("state\\x", b"x")],
    }
    for nm, members in bad.items():
        blob = tar_bytes(members)
        st, body = imp(blob)
        check("import refuses %s (%s)" % (nm, st), st in (400, 422), True)
        check("import refused %s: nothing written" % nm, empty(), True)
    blob = tar_bytes(good)
    check("import: missing sha header -> 400", call(srv, "POST", "/api/standby/import", raw=blob, headers=bearer)[0], 400)
    check("import: wrong sha -> 422", imp(blob, "0" * 64)[0], 422)
    check("import: wrong sha wrote nothing", empty(), True)
    os.environ["QUEUE_STANDBY_IMPORT_MAX_MB"] = "0"
    st, _ = imp(blob)
    check("import: oversize unpacked refused", st in (400, 413, 422), True)
    check("import: oversize wrote nothing", empty(), True)
    os.environ.pop("QUEUE_STANDBY_IMPORT_MAX_MB")
    badm = tar_bytes(good, manifest={"files": {"state/ollama-queue-state.json": "0" * 64}})
    st, _ = imp(badm)
    check("import: manifest hash mismatch refused", st, 422)
    check("import: manifest mismatch wrote nothing", empty(), True)

    # ---- good import -----------------------------------------------------------------------------------------------
    st, body = imp(blob)
    check("import: good tarball -> 200 ok", (st, body.get("ok") if isinstance(body, dict) else None), (200, True))
    check("import: state file landed", (state / "ollama-queue-state.json").read_bytes(), b'{"jobs": []}')
    check("import: log landed", (state / "ollama-queue-logs" / "j1.json").is_file(), True)
    check("import: dispatch file landed", (disp / "hosts.json").is_file(), True)
    check("import: no stage dir left", [p.name for p in state.iterdir() if p.name.startswith(".standby-stage")], [])
    check("import: marker written", (state / ".standby-imported").is_file(), True)
    st, body = imp(blob)
    check("import: non-empty without replace -> 409", st, 409)
    blob2 = tar_bytes([("state/ollama-queue-state.json", b'{"jobs": [1]}'), ("dispatch/hosts.json", b'{"a":1}')])
    st, body = imp(blob2, qs="?replace=1")
    check("import: replace -> 200", st, 200)
    check("import: replace swapped the state", (state / "ollama-queue-state.json").read_bytes(), b'{"jobs": [1]}')
    backups = [p for p in state.iterdir() if p.name.startswith(".standby-backup-")]
    check("import: replace kept a backup of the old state", len(backups) == 1 and (backups[0] / "ollama-queue-state.json").read_bytes() == b'{"jobs": []}', True)
    check("import: old-only files are gone from the live dir", (state / "ollama-queue-logs" / "j1.json").exists(), False)

    # ---- cases upload (POST /api/standby/cases): the back-test's case files ------------------------------------------
    bt = Path(os.environ["QUEUE_BACKTEST_DIR"])
    cases_dir = bt / "cases"

    def cases(blob, sha_hex=None):
        h = dict(bearer)
        h["X-Content-SHA256"] = sha_hex if sha_hex is not None else sha(blob)
        return call(srv, "POST", "/api/standby/cases", raw=blob, headers=h)

    def bt_clean():
        return [p.name for p in bt.iterdir() if p.name.startswith((".standby-stage-", ".standby-backup-", ".standby-upload-"))] if bt.is_dir() else []
    cgood = [("cases/b3.jsonl", b'{"id": "a"}\n{"id": "b"}\n'), ("cases/bases/x/verify.sh", b"exit 0\n"),
             ("cases/bases/x/run.sh", ("exec", b"#!/bin/sh\nexit 0\n")),
             ("cases/diffs/a.diff", b""), ("cases/b6/routes.json", b"[]")]
    for nm, members in {"state top": [("state/ollama-queue-state.json", b"{}")],
                        "manifest": [("manifest.json", b"{}")],
                        "symlink": [("cases/bases/x/link", ("link", "/etc/passwd"))],
                        "dot-dot": [("cases/../evil", b"x")],
                        "device": [("cases/dev", ("dev",))],
                        "cases as a file": [("cases", b"x")]}.items():
        st, _ = cases(tar_bytes(members))
        check("cases: refuses %s (%s)" % (nm, st), st in (400, 422), True)
        check("cases: refused %s wrote nothing" % nm, (cases_dir.exists(), bt_clean()), (False, []))
    check("cases: wrong sha -> 422", cases(tar_bytes(cgood), "0" * 64)[0], 422)
    check("cases: missing sha header -> 400", call(srv, "POST", "/api/standby/cases", raw=tar_bytes(cgood), headers=bearer)[0], 400)
    check("cases: without bearer -> 401", call(srv, "POST", "/api/standby/cases", raw=b"x")[0], 401)
    check("cases: wrong sha / no bearer wrote nothing", (cases_dir.exists(), bt_clean()), (False, []))
    st, body = cases(tar_bytes(cgood))
    check("cases: good tarball -> 200 ok", (st, body.get("ok") if isinstance(body, dict) else None), (200, True))
    check("cases: counts b3 lines and sees b6", (body.get("cases") or {}).get("b3"), 2)
    check("cases: b6 routes seen", (body.get("cases") or {}).get("b6"), True)
    check("cases: files landed", (cases_dir / "bases" / "x" / "verify.sh").read_bytes(), b"exit 0\n")
    check("cases: exec bit kept", (os.stat(cases_dir / "bases" / "x" / "run.sh").st_mode & 0o111) != 0, True)
    check("cases: plain file not executable", (os.stat(cases_dir / "bases" / "x" / "verify.sh").st_mode & 0o111), 0)
    check("cases: no stage, backup or upload left behind", bt_clean(), [])
    st, body = cases(tar_bytes([("cases/b3.jsonl", b'{"id": "c"}\n')]))
    check("cases: second upload -> 200", st, 200)
    check("cases: second upload replaced the folder whole", ((cases_dir / "bases").exists(), (cases_dir / "b3.jsonl").read_bytes()),
          (False, b'{"id": "c"}\n'))
    check("cases: state untouched by a cases upload", (state / "ollama-queue-state.json").read_bytes(), b'{"jobs": [1]}')
    run = bt / "abcdef012345"
    run.mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({"state": "running", "started": time.time()}))
    check("cases: refused while a back-test runs -> 409", cases(tar_bytes(cgood))[0], 409)
    check("cases: refused while running left the old cases", (cases_dir / "b3.jsonl").read_bytes(), b'{"id": "c"}\n')
    (run / "status.json").write_text(json.dumps({"state": "done"}))

    # ---- nothing leaves the container in standby ------------------------------------------------------------------
    import http.server
    hits = []

    class Peer(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            out = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    peer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    os.environ["QUEUE_AGENT_URL"] = "http://127.0.0.1:%d" % peer.server_address[1]
    os.environ["QUEUE_AGENT_TOKEN"] = "fake-agent-token"
    check("standby: agent_post returns None", queue_config.agent_post("/hold", {"x": 1}), None)
    check("standby: no request reached the Mac status service", hits, [])
    os.environ.pop("QUEUE_STANDBY")
    check("live: agent_post reaches the peer", (queue_config.agent_post("/hold", {"x": 1}), len(hits)), ({"ok": True}, 1))
    check("tests ran %d checks" % len(RESULTS), len(RESULTS) > 40, True)
    bad_n = RESULTS.count(False)
    for line in FAILED:
        print("again: " + line)
    print("\n%d checks, %d failed" % (len(RESULTS), bad_n))
    sys.exit(1 if bad_n else 0)


if __name__ == "__main__":
    main()
