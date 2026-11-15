#!/usr/bin/env python3
"""test-mini-finish-remote.py -- mini-finish against a REMOTE queue (queue_config.queue_is_remote()).
A stub queue API on this machine's non-loopback address serves /api/state, the run-dir tar (?scope=run), the upload route and a
check job's result. Proves:
  * --list reads /api/state with the bearer token and downloads nothing;
  * `mini-finish <job>` fetches the run dir into <home>/mini-finish-remote/<job>/, drops .git/hooks, replaces .git/config with a
    minimal one and drops trees/*/.git; the claim (finish/FINISH.json) and the brief live in the mirror;
  * a tar with ../x, an absolute name or a symlink leaving the target is refused and leaves nothing behind;
  * --check runs NO gate here: it uploads exactly one check job (runner by name, the --check-only arguments as runner_args, the
    candidate files as a tree, the spec's baseline as a tree), waits, reads the verdict from the fetched result;
  * FAIL calls neither the queue CLI's resolve nor clear-card; PASS (chain, steps left) runs validate, submit, resolve,
    clear-card in that order;
  * a partial mirror (the queue's _queue/skipped.txt) is reported at fetch time, and --check sends no check job for it
    unless MINI_FINISH_ALLOW_PARTIAL=1.
Fake token only. Run (sandbox only): python3 tests/test-mini-finish-remote.py [--bin PIPELINE_BIN]"""
import argparse
import http.server
import importlib.util
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
BIN = Path(ap.parse_args().bin)
FAILS = []


def ok(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else " " + str(extra)[:600]))
    if not cond:
        FAILS.append(name)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


sp = importlib.util.spec_from_file_location("mini_swe_runner_t", str(BIN / "mini-swe-runner.py"))
R = importlib.util.module_from_spec(sp)
sp.loader.exec_module(R)

T = Path(tempfile.mkdtemp(prefix="mfr-")).resolve()
HOMEDIR = T / "home"
MF_HOME = HOMEDIR / ".ollama-dispatch"
MF_HOME.mkdir(parents=True)
SRV_ROOT = "/srv/mini-work"   # the queue host's paths: they exist nowhere here

SEEN, UPLOADS = [], []
VALIDATION_MD = ("# VALIDATION\n\n## Fingerprint\n\n- spec: 00\n\n- runner: /srv/bin/mini-swe-runner.py sha=0123456789ab\n"
                 "- test cap: 30\n\nRESULT: PASS\n")
TARS = {}           # job id -> bytes of the run tar
CHECK = {"status": "done", "exit_code": 0, "out": ""}
ROWS = []


def tar_bytes(members):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as tf:
        for name, data in members:
            if isinstance(data, tuple):   # ("link", target)
                ti = tarfile.TarInfo(name)
                ti.type, ti.linkname = tarfile.SYMTYPE, data[1]
                tf.addfile(ti)
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755 if name.endswith(".sh") or "hooks" in name else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return b.getvalue()


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        SEEN.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/state":
            extra = [dict(id="dd0000000002", status="done", exit_code=0, label="x-val")] if len(UPLOADS) >= 2 else []
            return self._send(json.dumps({"jobs": ROWS + [dict(id="cc0000000001", status=CHECK["status"],
                                                               exit_code=CHECK["exit_code"], label="x-chk")] + extra}).encode())
        if self.path == "/api/jobs/dd0000000002/result":   # the validate job the continuation runs on the queue
            return self._send(tar_bytes([("job.json", b"{}"), ("log-tail.txt", b"RESULT PASS validate-spec V1-V8 C5 C6\n"),
                                         ("work/VALIDATION.md", VALIDATION_MD.encode())]), "application/gzip")
        if self.path.endswith("/result?scope=run"):
            jid = self.path.split("/")[3]
            return self._send(TARS[jid], "application/gzip") if jid in TARS else self._send(b"no", "text/plain", 404)
        if self.path == "/api/jobs/cc0000000001/result":
            return self._send(tar_bytes([("job.json", b"{}"), ("log-tail.txt", CHECK["out"].encode())]), "application/gzip")
        self._send(b"{}", code=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        SEEN.append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/api/jobs/upload":
            UPLOADS.append(body)
            return self._send(json.dumps({"id": "cc0000000001" if len(UPLOADS) == 1 else "dd0000000002", "run_dir": SRV_ROOT + "/chk"}).encode())
        if self.path in ("/api/runs/cc0000000001/clear", "/api/runs/dd0000000002/clear"):
            return self._send(b'{"cleared": true}')
        self._send(b"{}", code=404)

    def log_message(self, *a):
        pass


IP = lan_ip()
if IP.startswith("127."):
    print("SKIP: no non-loopback address")
    sys.exit(0)
srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]

FAKES = T / "fakes"
FAKES.mkdir()
FAKE_LOG = T / "fake.log"
for name in ("queue", "validate", "submit"):
    (FAKES / f"{name}.py").write_text(
        "import json, os, sys\n"
        f"open(os.environ['FAKE_LOG'], 'a').write(json.dumps({{'tool': '{name}', 'argv': sys.argv[1:]}}) + '\\n')\n"
        + ("print('job_id: c0ffee123456')\n" if name == "submit" else "print('ok')\n"))
SOCK = T / "me.sock"
SOCK.write_text("")
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "MINI_", "QUEUE_", "QEL_"))}
ENV.update(HOME=str(HOMEDIR), MINI_FINISH_HOME=str(MF_HOME), QUEUE_API=f"http://{IP}:{PORT}", QUEUE_API_TOKEN="fake-test-token",
           MINI_FINISH_POLL="0.05", CLAUDE_CODE_MESSAGING_SOCKET=str(SOCK), FAKE_LOG=str(FAKE_LOG),
           MINI_FINISH_QUEUE=shlex.join([sys.executable, str(FAKES / "queue.py")]),
           MINI_FINISH_VALIDATE=shlex.join([sys.executable, str(FAKES / "validate.py")]),
           MINI_FINISH_SUBMIT=shlex.join([sys.executable, str(FAKES / "submit.py")]))


def fin(*args):
    del SEEN[:]
    p = subprocess.run([sys.executable, str(BIN / "mini-finish"), *args], env=ENV, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def calls():
    if not FAKE_LOG.exists():
        return []
    return [json.loads(ln) for ln in FAKE_LOG.read_text().splitlines() if ln.strip()]


def run_tar(jid, spec, chain=False, extra=()):
    """The tar the queue would serve for job jid: spec.json, trees/t0 (baseline, with a .git), work/ (a git repo with a hook
    and a hostile config), _queue/."""
    runroot = f"{SRV_ROOT}/{jid}"
    spec = json.loads(json.dumps(spec).replace("@RUN@", runroot))
    mem = [("spec.json", json.dumps(spec).encode()), ("trees/t0/calc.js", R.CALC.encode()), ("trees/t0/.git/config", b"[core]\n"),
           ("trees/t0/.git/HEAD", b"ref: refs/heads/main\n"), ("work/TASK.md", b"# TASK\n"), ("work/calc.js", R.CALC.encode()),
           ("work/calc.test.js", R.GOOD_T.encode()), ("work/calc2.test.js", R.T2.encode()),
           ("work/.git/hooks/pre-commit", b"#!/bin/sh\ntouch HOOK-RAN\n"),
           ("work/.git/config", b"[core]\n\tfsmonitor = touch HOOK-RAN2\n"), ("work/.git/HEAD", b"ref: refs/heads/main\n"),
           ("_queue/job.json", b"{}"), ("_queue/done.json", json.dumps({"mode": "x"}).encode())]
    if chain:
        mem += [("ref/s1/calc.js", R.CALC_FIXED.encode()), ("ref/s2/calc.js", R.CALC_FIXED.encode())]
    TARS[jid] = tar_bytes(mem + list(extra))
    return runroot


def row(jid, label, **kw):
    r = {"id": jid, "label": label, "status": "failed", "runner": "/srv/bin/mini-swe-runner.py", "cwd": f"{SRV_ROOT}/{jid}/work",
         "task_file": "/srv/tasks/x.txt", "failure_class": "model", "model": "qwen-test", "runner_verdict": "FAIL stub"}
    r.update(kw)
    ROWS.append(r)


SINGLE = {"mode": "two-phase", "baseline_from": "@RUN@/trees/t0", "allowed": ["calc.js"], "test_file": "calc.test.js",
          "test_cmd": "node --test calc.test.js", "target_fn": "add", "timeout": 600, "fix_attempts": 1}
S1 = {"task": "GOAL-ONE\n", "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js", "allowed": ["calc.js"],
      "target_fn": "add"}
S2 = {"task": "GOAL-TWO\n", "test_file": "calc2.test.js", "test_cmd": "node --test calc2.test.js", "allowed": ["calc.js"],
      "target_fn": "other"}
CHAIN = {"baseline_from": "@RUN@/trees/t0", "timeout": 600, "fix_attempts": 1, "steps": [S1, S2]}

J1, J2 = "aaaaaaaa1111", "bbbbbbbb2222"
run_tar(J1, SINGLE)
run_tar(J2, CHAIN, chain=True)
row(J1, "calc-fix")
row(J2, "calc-chain", runner_verdict="FAIL at step-1")

print("--list")
rc, out = fin("--list")
ok("list exits 0 and names the job, not fetched", rc == 0 and J1 in out and "not fetched yet" in out, out)
ok("list read /api/state with the bearer token", ("GET", "/api/state", "Bearer fake-test-token") in SEEN, SEEN)
ok("list downloaded nothing", not any("scope=run" in p for _m, p, _a in SEEN) and not (MF_HOME / "mini-finish-remote").exists())

print("--fetch and claim")
rc, out = fin(J1)
M1 = MF_HOME / "mini-finish-remote" / J1
ok("claim run exits 0", rc == 0, out)
ok("run scope fetched with the bearer token", ("GET", f"/api/jobs/{J1}/result?scope=run", "Bearer fake-test-token") in SEEN, SEEN)
ok("mirror holds the work dir", (M1 / "work" / "calc.js").is_file() and (M1 / "spec.json").is_file())
ok("git hooks removed", not (M1 / "work" / ".git" / "hooks").exists())
ok("git config replaced by the minimal one",
   (M1 / "work" / ".git" / "config").read_text().startswith("[core]") and "fsmonitor" not in (M1 / "work" / ".git" / "config").read_text())
ok("baseline checkout metadata dropped", not (M1 / "trees" / "t0" / ".git").exists() and (M1 / "trees" / "t0" / "calc.js").is_file())
ok("the claim lives in the mirror", (M1 / "finish" / "FINISH.json").is_file())
ok("the brief points at the mirror", str(M1 / "work") in out, out)
ok("nothing from the tar ran", not list(T.rglob("HOOK-RAN*")))
ok("only the mirror root was written under the dispatch home", [p.name for p in MF_HOME.iterdir()] == ["mini-finish-remote"])

print("--check FAIL")
(M1 / "work" / "calc.js").write_text(R.CALC_FIXED)
CHECK.update(status="done", exit_code=1,
             out="[mini-runner] try-test calc.test.js: ACCEPTED\n[mini-runner] try-fix: calc.js\n[mini-runner]   suite: FAIL boom\n")
del UPLOADS[:]
if FAKE_LOG.exists():
    FAKE_LOG.unlink()
rc, out = fin(J1, "--check")
ok("FAIL exits 1", rc == 1, out)
ok("exactly one check job uploaded", len(UPLOADS) == 1, len(UPLOADS))
man = None
if UPLOADS:
    with tarfile.open(fileobj=io.BytesIO(UPLOADS[0])) as tf:
        man = json.loads(tf.extractfile("manifest.json").read())
        up_spec = json.loads(tf.extractfile("spec.json").read())
        names = tf.getnames()
ok("runner chosen by name", man and man["runner"] == "mini-swe-runner.py", man)
ra = (man or {}).get("runner_args") or []
ok("--check-only first, then the try args", ra[:1] == [["--check-only"]] and ["--mutation"] in ra
   and ["--try-test", {"tree": "cand", "rel": "calc.test.js", "prefix": ""}] in ra
   and ["--try-fix", {"tree": "cand", "rel": "calc.js", "prefix": "calc.js="}] in ra, ra)
ok("candidate files and the baseline travel as trees", {t["id"] for t in (man or {}).get("trees", [])} >= {"cand", "t0"}
   and any(n.startswith("trees/cand/overlay/calc.js") for n in names), (man or {}).get("trees"))
ok("the spec's baseline path points at the mirror (shipped as a tree)", up_spec.get("baseline_from") == str(M1 / "trees" / "t0")
   and any(r["path"] == ["baseline_from"] for r in man["refs"]), up_spec)
ok("job fields carry the model and a -chk label", man["job"].get("model") == "qwen-test" and "-chk-" in man["job"].get("label", ""), man["job"])
chk_dirs = sorted((M1 / "finish").glob("check-*"))
ok("no runner ran here (no scratch work dir, remote log)", chk_dirs and not (chk_dirs[-1] / "work").exists()
   and (chk_dirs[-1] / "check.log").read_text().startswith("$ remote check job cc0000000001"), chk_dirs)
ok("the red gate is reported", "suite: boom" in out or "suite" in out, out)
ok("FAIL called neither resolve nor clear-card", calls() == [], calls())
ok("check job fetched with the bearer token", ("GET", "/api/jobs/cc0000000001/result", "Bearer fake-test-token") in SEEN, SEEN)
_clr = ("POST", "/api/runs/cc0000000001/clear", "Bearer fake-test-token")
ok("the check job's card is cleared after its result was fetched, with the bearer token", _clr in SEEN
   and SEEN.index(_clr) > SEEN.index(("GET", "/api/jobs/cc0000000001/result", "Bearer fake-test-token")), SEEN)
ok("the failed job's own card is not cleared on FAIL", not any(m == "POST" and "/clear" in p and J1 in p for m, p, _a in SEEN), SEEN)

print("--check PASS on a chain with steps left: continuation, resolve, clear-card in that order")
rc, out = fin(J2)
M2 = MF_HOME / "mini-finish-remote" / J2
ok("chain claim exits 0", rc == 0, out)
(M2 / "work" / "calc.js").write_text(R.CALC_FIXED)
CHECK.update(status="done", exit_code=0,
             out="replay step 1: GREEN\n[mini-runner]   suite: ok\n[mini-runner]   fn_scope: ok\nRESULT PASS\n")
del UPLOADS[:]
if FAKE_LOG.exists():
    FAKE_LOG.unlink()
rc, out = fin(J2, "--check")
ok("PASS exits 0", rc == 0, out)
ok("one check job and one validate job uploaded for the chain", len(UPLOADS) == 2, len(UPLOADS))
if UPLOADS:
    with tarfile.open(fileobj=io.BytesIO(UPLOADS[0])) as tf:
        m2 = json.loads(tf.extractfile("manifest.json").read())
        sp2 = json.loads(tf.extractfile("spec.json").read())
    ok("replay refs shipped as a tree and --mutation sent", ["--mutation"] in m2["runner_args"]
       and any(r["path"] == ["replay", "refs"] for r in m2["refs"]) and sp2["replay"]["through"] == 1, (m2["runner_args"], sp2))
if len(UPLOADS) >= 2:
    with tarfile.open(fileobj=io.BytesIO(UPLOADS[1])) as tf:
        m3 = json.loads(tf.extractfile("manifest.json").read())
    ok("the continuation's validate job: runner flag --validate-spec, no local path", m3.get("runner_args") == [["--validate-spec"]]
       and m3["job"]["label"].endswith(tuple("0123456789")) and "-val-" in m3["job"]["label"], m3.get("runner_args"))
seq = [(c["tool"], c["argv"][:1]) for c in calls()]
_sp = [str(next((M2.parent).glob(J2 + "-c1/spec.json")))]
ok("remote: NO local mini-validate; submit, resolve, clear-card in that order", seq == [("submit", _sp), ("queue", ["resolve"]), ("queue", ["clear-card"])], seq)
_vm = next((M2.parent).glob(J2 + "-c1/VALIDATION.md"), None)
ok("the queue's VALIDATION.md landed next to the continuation spec, marked", _vm is not None and "- validated on the queue: job dd0000000002" in _vm.read_text(), _vm)
ok("the validate job's card is cleared", ("POST", "/api/runs/dd0000000002/clear", "Bearer fake-test-token") in SEEN)
ok("resolve and clear-card name the failed job", [c["argv"] for c in calls() if c["tool"] == "queue"] == [["resolve", J2], ["clear-card", J2]])
ok("FINISHED.json written in the mirror", (M2 / "finish" / "FINISHED.json").is_file())

print("--partial mirror (the run dir was over the queue's run result cap)")
J3 = "ffffffff6666"
run_tar(J3, SINGLE, extra=[("_queue/skipped.txt", b"# 1 file(s), 999 bytes, left out: over the run result cap of 128 MB\n"
                                                  b"999\twork/big-fixture.bin\n")])
row(J3, "calc-big")
rc, out = fin(J3)
M3 = MF_HOME / "mini-finish-remote" / J3
ok("partial: the claim run exits 0 and warns, naming the left-out file", rc == 0 and "WARNING" in out
   and "work/big-fixture.bin" in out and "run_max_mb" in out, out)
(M3 / "work" / "calc.js").write_text(R.CALC_FIXED)
CHECK.update(status="done", exit_code=1, out="[mini-runner]   suite: FAIL boom\n")
del UPLOADS[:]
if FAKE_LOG.exists():
    FAKE_LOG.unlink()
rc, out = fin(J3, "--check")
ok("partial: --check sends no check job", not UPLOADS and not any(m == "POST" for m, _p, _a in SEEN), SEEN)
ok("partial: --check fails and names the override", rc != 0 and "MINI_FINISH_ALLOW_PARTIAL" in out, (rc, out))
ok("partial: neither resolve nor clear-card", calls() == [], calls())
ENV["MINI_FINISH_ALLOW_PARTIAL"] = "1"
try:
    rc, out = fin(J3, "--check")
finally:
    del ENV["MINI_FINISH_ALLOW_PARTIAL"]
ok("partial with MINI_FINISH_ALLOW_PARTIAL=1: one check job is sent", len(UPLOADS) == 1, (len(UPLOADS), out))

print("--hostile tars")
for tag, members in (("dotdot", [("../x-escape", b"x")]), ("absolute", [(str(T / "abs-escape"), b"x")]),
                     ("symlink", [("work/link", ("link", "../../../../escape-target")), ("work/a", b"a")])):
    jid = {"dotdot": "cccccccc3333", "absolute": "dddddddd4444", "symlink": "eeeeeeee5555"}[tag]
    TARS[jid] = tar_bytes(members)
    row(jid, "evil-" + tag)
    rc, out = fin(jid)
    ok(f"{tag}: refused", rc == 1 and "cannot fetch" in out, out)
    ok(f"{tag}: no mirror, nothing outside", not (MF_HOME / "mini-finish-remote" / jid).exists()
       and not (MF_HOME / "mini-finish-remote" / "x-escape").exists() and not (T / "abs-escape").exists()
       and not any(p.name.startswith(".fetch-") for p in (MF_HOME / "mini-finish-remote").iterdir()))

print("--job ids from the queue are plain ids")
BAD = "../../evil0"
row(BAD, "evil-id")
rc, out = fin("--list")
ok("list survives a row with a path-like id and downloads nothing for it", rc == 0
   and not any(BAD in p for _m, p, _a in SEEN if "scope=run" in p), out)
_p = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import mini_finish_remote as M\n"
                     "try:\n    M.mirror_dir(sys.argv[2], sys.argv[3]); print('ACCEPTED')\n"
                     "except M.QR.RemoteError:\n    print('REFUSED')", str(BIN), str(MF_HOME), BAD],
                    env=ENV, capture_output=True, text=True, timeout=60)
ok("mirror_dir refuses a path-like id", _p.stdout.strip() == "REFUSED", _p.stdout + _p.stderr)
ok("nothing written outside the mirror root for it", {p.name for p in MF_HOME.iterdir()} <= {"mini-finish-remote", "mini-finish.jsonl"}
   and not (MF_HOME.parent / "evil0").exists(), sorted(p.name for p in MF_HOME.iterdir()))

print("FAILED: %s" % FAILS if FAILS else "ALL PASSED")
sys.exit(1 if FAILS else 0)
