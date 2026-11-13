#!/usr/bin/env python3
"""test-queue-drain.py -- the fallback routes (POST/GET /api/admin/drain, GET /api/admin/export), the export builder and
its round trip through the safe import, against the REAL Handler on 127.0.0.1 port 0 with made-up data and a fake token.

Proves: no token set = 403, wrong token = 401, standby = admin routes not served; drain on refuses enqueue and CPU-lane
claim / create with 503 draining and leaves other routes alone; export needs drain and zero running jobs (409);
the export is a tar whose sha256 matches X-Content-SHA256, holds only allowlisted names (no links), takes the sqlite
through the backup API, and imports into empty dirs with the manifest verified and identical file hashes;
drain off clears it. Usage: test-queue-drain.py [--api PATH]. Run on the sandbox, never on the Mac.
"""
import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def helpers():
    spec = importlib.util.spec_from_file_location("tqs_helpers", str(HERE / "test-queue-standby.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def get_raw(srv, path, headers):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    b = r.read()
    hd = dict(r.getheaders())
    c.close()
    return r.status, hd, b


def tree_hashes(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))
    a = ap.parse_args()
    H = helpers()
    tmp = Path(tempfile.mkdtemp(prefix="qa-drain-"))
    state, disp = tmp / "state", tmp / "disp"
    (disp / "cpu-jobs" / "blobs" / "j1").mkdir(parents=True)
    (state / "ollama-queue-logs").mkdir(parents=True)
    for k, v in (("QUEUE_STATE_DIR", state), ("QUEUE_DISPATCH_DIR", disp), ("CPU_LANE_DIR", disp / "cpu-jobs"),
                 ("CPU_RUNNER_TOKEN_FILE", tmp / "cpu-token"), ("CPU_LANE_LLM_LOCAL_JSON", tmp / "no-local.json"),
                 ("QEL_DIR", tmp / "qel"), ("QUEUE_BACKTEST_DIR", tmp / "bt")):
        os.environ[k] = str(v)
    os.environ.pop("QUEUE_STANDBY", None)
    os.environ.pop("QUEUE_API_TOKEN", None)
    # made-up records
    (state / "ollama-queue-state.json").write_text(json.dumps({"jobs": [
        {"id": "aaaa00000001", "status": "done"}, {"id": "aaaa00000002", "status": "pending"}]}))
    (state / "ollama-queue-logs" / "aaaa00000001.log").write_text("made-up log\n")
    (disp / "hosts.json").write_text('{"example": {"url": "http://example.invalid"}}')
    (disp / "queue-events.jsonl").write_text('{"seq": 1}\n')
    (disp / "worktrees").mkdir()
    (disp / "worktrees" / "big.txt").write_text("never exported")
    (disp / "retired").mkdir()
    os.symlink("/etc/hostname", disp / "decisions.jsonl")                      # a link: left out
    (disp / "cpu-jobs" / "blobs" / "j1" / "in.bin").write_bytes(b"blob")
    db = disp / "cpu-jobs" / "jobs.sqlite"
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("create table jobs(id text primary key, status text)")
    c.execute("insert into jobs values('c1','done')")
    c.commit()                                                                  # left open: the WAL holds the row
    api = H.load_api(a.api)
    api.QUEUE_API_TOKEN_FILE = tmp / "cfg" / "queue-api-token"
    import queue_config
    import queue_standby
    srv = H.serve(api)
    call = H.call
    TOK = "fake-drain-token"
    bearer = {"Authorization": "Bearer " + TOK}

    # ---- export builder, on its own ----------------------------------------------------------------------------------
    out = tmp / "x.tar"
    res = queue_standby.build_export(state, disp, out)
    with tarfile.open(out) as tf:
        names = [m.name for m in tf.getmembers()]
        kinds = {m.type for m in tf.getmembers()}
    check("export: no links or devices", kinds <= {tarfile.REGTYPE, tarfile.DIRTYPE}, True)
    check("export: worktrees, retired and the linked file are left out",
          [n for n in names if "worktrees" in n or "retired" in n or n.endswith("decisions.jsonl")], [])
    check("export: state, hosts, blob and sqlite are in",
          all(n in names for n in ("state/ollama-queue-state.json", "dispatch/hosts.json", "dispatch/cpu-jobs/jobs.sqlite",
                                   "dispatch/cpu-jobs/blobs/j1/in.bin", "manifest.json")), True)
    check("export: sha256 is the file's", res["tar_sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
    s2, d2 = tmp / "s2", tmp / "d2"
    s2.mkdir(); d2.mkdir()
    imp = queue_standby.import_state(out, s2, d2, sha256=res["tar_sha256"])
    check("round trip: import ok with the manifest verified", (imp["ok"], imp["manifest_ok"]), (True, True))
    c2 = sqlite3.connect(str(d2 / "cpu-jobs" / "jobs.sqlite"))
    check("round trip: sqlite row from the WAL arrived", c2.execute("select id from jobs").fetchall(), [("c1",)])
    c2.close()
    h1, h2 = tree_hashes(state), tree_hashes(s2)
    check("round trip: state files identical (marker aside)", {k: v for k, v in h2.items() if not k.startswith(".standby")},
          {k: v for k, v in h1.items() if not k.startswith(".")})
    rc = queue_standby.running_counts(state, disp)
    check("running_counts: made-up state", rc, {"running": 0, "cpu_running": 0, "pending": 1})

    # ---- routes ------------------------------------------------------------------------------------------------------
    check("no token configured: 403", call(srv, "POST", "/api/admin/drain", {"on": True})[0], 403)
    os.environ["QUEUE_API_TOKEN"] = TOK
    check("wrong token: 401", call(srv, "POST", "/api/admin/drain", {"on": True},
                                   headers={"Authorization": "Bearer nope"})[0], 401)
    check("loopback is not exempt (no header): 401", call(srv, "GET", "/api/admin/drain")[0], 401)
    check("not draining at first", call(srv, "GET", "/api/admin/drain", headers=bearer)[1].get("draining"), False)
    check("export before drain: 409", call(srv, "GET", "/api/admin/export", headers=bearer)[0], 409)
    st, body = call(srv, "POST", "/api/admin/drain", {"on": True}, headers=bearer)
    check("drain on", (st, body.get("draining"), body.get("pending")), (200, True, 1))
    check("marker file exists", queue_config.draining(), True)
    bj = dict(bearer)
    check("draining: POST /api/jobs 503", call(srv, "POST", "/api/jobs", {}, headers=bj), (503, {"error": "draining"}))
    check("draining: POST /api/jobs/upload 503", call(srv, "POST", "/api/jobs/upload", raw=b"x", headers=bj)[0], 503)
    check("draining: POST /api/cpu/claim 503", call(srv, "POST", "/api/cpu/claim", {}, headers=bj)[0], 503)
    check("draining: POST /api/cpu/jobs 503", call(srv, "POST", "/api/cpu/jobs", {}, headers=bj)[0], 503)
    check("draining: GET / still served", call(srv, "GET", "/")[0], 200)
    check("draining: a result post is not refused as draining",
          call(srv, "PUT", "/api/cpu/jobs/abc/result", {}, headers=bj)[1] != {"error": "draining"}, True)
    # a running job blocks the export
    sj = state / "ollama-queue-state.json"
    keep = sj.read_text()
    sj.write_text(json.dumps({"jobs": [{"id": "aaaa00000003", "status": "running"}]}))
    st, body = call(srv, "GET", "/api/admin/export", headers=bearer)
    check("export with a running job: 409", (st, body.get("ok")), (409, False))
    sj.write_text(keep)
    st, hd, raw = get_raw(srv, "/api/admin/export", bearer)
    check("export: 200 and the header hash matches the body", (st, hd.get("X-Content-SHA256") == hashlib.sha256(raw).hexdigest()),
          (200, True))
    s3, d3 = tmp / "s3", tmp / "d3"
    s3.mkdir(); d3.mkdir()
    (tmp / "r.tar").write_bytes(raw)
    imp = queue_standby.import_state(tmp / "r.tar", s3, d3, sha256=hd.get("X-Content-SHA256"))
    check("export from the route imports with the manifest verified", (imp["ok"], imp["manifest_ok"]), (True, True))
    check("no export temp file left behind", [p.name for p in state.iterdir() if p.name.startswith(".export-")], [])
    st, body = call(srv, "POST", "/api/admin/drain", {"on": False}, headers=bearer)
    check("drain off", (st, body.get("draining"), queue_config.draining()), (200, False, False))
    check("after drain off: POST /api/jobs reaches its own validation", call(srv, "POST", "/api/jobs", {}, headers=bj)[0], 400)
    os.environ["QUEUE_STANDBY"] = "1"
    check("standby: admin export is not served", call(srv, "GET", "/api/admin/drain", headers=bearer)[0] in (404, 503), True)
    os.environ.pop("QUEUE_STANDBY")

    bad = RESULTS.count(False)
    print("%d checks, %d failed" % (len(RESULTS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
