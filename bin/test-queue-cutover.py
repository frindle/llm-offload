#!/usr/bin/env python3
"""test-queue-cutover.py -- queue-cutover (export, switch-clients, fallback) against a fake launchctl and the REAL queue API
Handler on 127.0.0.1 port 0 (standby import for export, drain + export routes for fallback). Made-up data, fake token.

Proves: --dry-run changes nothing; export refuses while a job runs; export stops the 9 jobs (plists stay), uploads, checks
the returned hash, is idempotent, and starts the jobs again when the upload fails; switch-clients backs up and edits the
config once and lists QUEUE_API users by name; fallback drains, waits, pulls the export, restores state and config,
bootstraps the jobs, stops (drain on, jobs stopped) while the container still runs a job, and is re-runnable; the
token never appears in the output or the log. Usage: test-queue-cutover.py [--api PATH]. Sandbox only.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []
TOK = "fake-cutover-token-0123"
PRE = "example.test."
JOBS = ("ollama-queue-daemon", "ollama-queue-api", "queue-event-log", "cpu-prefetch", "dispatch-ack-reconcile",
        "dispatch-escalation-watcher", "verify-orphan-reap", "log-rotate", "docsum-ui")

FAKE_LC = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ["LC_STATE"]
try:
    d = json.load(open(st))
except Exception:
    d = {"loaded": [], "calls": []}
a = sys.argv[1:]
d["calls"].append(a[:1] + a[1:2])
rc = 0
if a[0] == "print":
    rc = 0 if a[1].split("/")[-1] in d["loaded"] else 113
elif a[0] == "bootout":
    lab = a[1].split("/")[-1]
    if lab in d["loaded"]:
        d["loaded"].remove(lab)
    else:
        rc = 3
elif a[0] == "bootstrap":
    lab = os.path.basename(a[2])[:-6]
    if lab not in d["loaded"]:
        d["loaded"].append(lab)
json.dump(d, open(st, "w"))
sys.exit(rc)
'''


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): sha(p) for p in sorted(Path(root).rglob("*")) if p.is_file()
            and not p.name.endswith(".lock")
            and not any(part.startswith(".standby") or part.startswith(".export") for part in p.relative_to(root).parts)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("tqs_h", str(HERE / "test-queue-standby.py"))
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    tmp = Path(tempfile.mkdtemp(prefix="qa-cutover-"))
    mac_s, mac_d, con_s, con_d = tmp / "mac_s", tmp / "mac_d", tmp / "con_s", tmp / "con_d"
    agents, work = tmp / "agents", tmp / "work"
    for d in (mac_s / "ollama-queue-logs", mac_d / "cpu-jobs", con_s, con_d, agents):
        d.mkdir(parents=True)
    (mac_s / "ollama-queue-state.json").write_text(json.dumps({"jobs": [{"id": "bbbb00000001", "status": "done"}]}))
    (mac_s / "ollama-queue-logs" / "bbbb00000001.log").write_text("made-up\n")
    (mac_d / "hosts.json").write_text('{"example": {"url": "http://example.invalid"}}')
    (mac_d / "queue-events.jsonl").write_text('{"seq": 7}\n')
    import sqlite3
    c = sqlite3.connect(str(mac_d / "cpu-jobs" / "jobs.sqlite"))
    c.execute("create table jobs(id text primary key, status text)")
    c.execute("insert into jobs values('c1','done')")
    c.commit(); c.close()
    for j in JOBS:
        (agents / (PRE + j + ".plist")).write_text("<plist/>")
    (agents / (PRE + "extra.plist")).write_text("<plist><key>QUEUE_API</key><string>secret-not-printed</string></plist>")
    lc = tmp / "launchctl"
    lc.write_text(FAKE_LC)
    lc.chmod(0o755)
    lcs = tmp / "lc-state.json"
    lcs.write_text(json.dumps({"loaded": [PRE + j for j in JOBS], "calls": []}))
    cfg = tmp / "cfg" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[queue]\napi_url = "http://127.0.0.1:7684"\n\n[other]\nx = 1\n')
    cfg0 = cfg.read_text()

    # the container: real Handler, its own dirs
    for k, v in (("QUEUE_STATE_DIR", con_s), ("QUEUE_DISPATCH_DIR", con_d), ("CPU_LANE_DIR", con_d / "cpu-jobs"),
                 ("CPU_RUNNER_TOKEN_FILE", tmp / "cpu-token"), ("CPU_LANE_LLM_LOCAL_JSON", tmp / "no-local.json"),
                 ("QEL_DIR", tmp / "qel"), ("QUEUE_BACKTEST_DIR", tmp / "bt"), ("QUEUE_API_TOKEN", TOK)):
        os.environ[k] = str(v)
    os.environ["QUEUE_STANDBY"] = "1"
    api = H.load_api(a.api)
    api.QUEUE_API_TOKEN_FILE = tmp / "cfg" / "queue-api-token"
    srv = H.serve(api)
    url = "http://127.0.0.1:%d" % srv.server_address[1]

    def run(*args, token=TOK, env_extra=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp), "QUEUE_STATE_DIR": str(mac_s), "QUEUE_DISPATCH_DIR": str(mac_d),
               "QUEUE_API_TOKEN": token, "QC_HOME": str(work), "QC_LAUNCHCTL": str(lc), "QC_LAUNCH_AGENTS": str(agents),
               "QC_CONFIG": str(cfg), "QC_UID": "501", "LC_STATE": str(lcs), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, str(HERE / "queue-cutover"), "--url", url, *args], capture_output=True, text=True,
                           env=env, timeout=300)
        return p.returncode, p.stdout + p.stderr

    def loaded():
        return sorted(json.loads(lcs.read_text())["loaded"])

    def mutating_calls():
        return [c[0] for c in json.loads(lcs.read_text())["calls"] if c[0] in ("bootout", "bootstrap")]

    # ---- dry run ---------------------------------------------------------------------------------------------------
    rc, out = run("--dry-run", "export")
    check("dry-run export: exit 0, says what it would do", (rc, "would boot out" in out, "would upload" in out), (0, True, True))
    check("dry-run export: nothing stopped, nothing written", (len(loaded()), mutating_calls(), work.exists()), (9, [], False))
    # ---- refuse while running --------------------------------------------------------------------------------------
    keep = (mac_s / "ollama-queue-state.json").read_text()
    (mac_s / "ollama-queue-state.json").write_text(json.dumps({"jobs": [{"id": "bbbb00000009", "status": "running"}]}))
    rc, out = run("export")
    check("export refuses while a job runs", (rc, "jobs are running" in out, mutating_calls()), (1, True, []))
    (mac_s / "ollama-queue-state.json").write_text(keep)
    # ---- export ------------------------------------------------------------------------------------------------------
    rc, out = run("export")
    check("export: exit 0", rc, 0)
    check("export: all 9 jobs stopped, plists kept", (loaded(), all((agents / (PRE + j + ".plist")).exists() for j in JOBS)), ([], True))
    ct, mt = tree(con_s), tree(mac_s)
    check("export: the container holds the Mac's files", (sorted(set(ct) ^ set(mt)), [k for k in mt if ct.get(k) != mt[k]],
          (con_d / "hosts.json").read_text() == (mac_d / "hosts.json").read_text()), ([], [], True))
    check("export: logged, token not in output or log", (TOK in out, TOK in (work / "queue-cutover.log").read_text()), (False, False))
    check("export: output says the hash matched", "hash matches" in out, True)
    rc, out = run("export")
    check("export again (jobs already stopped): exit 0, no new bootout", (rc, mutating_calls().count("bootout")), (0, 9))
    # ---- upload failure restarts the jobs ----------------------------------------------------------------------------
    rc, out = run("export", token="wrong-token")
    check("bad token: export fails and the Mac jobs are started again", (rc, len(loaded())), (1, 9))
    rc, out = run("export")
    check("export after the failure: ok again", (rc, loaded()), (0, []))
    # ---- switch-clients --------------------------------------------------------------------------------------------
    rc, out = run("--dry-run", "switch-clients")
    check("dry-run switch-clients: config untouched", (rc, cfg.read_text()), (0, cfg0))
    rc, out = run("switch-clients")
    txt = cfg.read_text()
    baks = sorted(work.glob("config.toml.bak-*"))
    check("switch-clients: api_url set, rest kept, backup holds the old file",
          (rc, 'api_url = "%s"' % url in txt, "[other]" in txt, len(baks) == 1 and baks[0].read_text() == cfg0), (0, True, True, True))
    check("switch-clients: lists the QUEUE_API plist by name, not its value", ("extra.plist" in out, "secret-not-printed" in out), (True, False))
    rc, out = run("switch-clients")
    check("switch-clients again: nothing to do, no second backup", (rc, "already" in out, len(list(work.glob("config.toml.bak-*")))), (0, True, 1))
    # ---- fallback ----------------------------------------------------------------------------------------------------
    os.environ["QUEUE_STANDBY"] = ""                      # the container went live
    (con_s / "ollama-queue-state.json").write_text(json.dumps({"jobs": [
        {"id": "bbbb00000001", "status": "done"}, {"id": "cccc00000002", "status": "done"}]}))
    rc, out = run("--dry-run", "fallback")
    check("dry-run fallback: lists the steps, changes nothing", (rc, "step 5" in out, loaded(), (con_s / "queue-drain.json").exists()),
          (0, True, [], False))
    (con_s / "ollama-queue-state.json").write_text(json.dumps({"jobs": [
        {"id": "bbbb00000001", "status": "done"}, {"id": "cccc00000002", "status": "running"}]}))
    rc, out = run("fallback", "--wait", "0")
    check("fallback while the container runs a job: stops, drain on, Mac jobs still stopped",
          (rc, "still running" in out, (con_s / "queue-drain.json").exists(), loaded()), (1, True, True, []))
    (con_s / "ollama-queue-state.json").write_text(json.dumps({"jobs": [
        {"id": "bbbb00000001", "status": "done"}, {"id": "cccc00000002", "status": "done"}]}))
    rc, out = run("fallback", "--wait", "0")
    check("fallback: exit 0", rc, 0)
    got = json.loads((mac_s / "ollama-queue-state.json").read_text())
    check("fallback: the Mac state is the container's", [j["id"] for j in got["jobs"]], ["bbbb00000001", "cccc00000002"])
    check("fallback: the old Mac state is kept as a backup", len(list(mac_s.glob(".standby-backup-*"))) >= 1, True)
    check("fallback: client config restored", cfg.read_text(), cfg0)
    check("fallback: the 9 jobs are bootstrapped again", len(loaded()), 9)
    check("fallback: token not in output or log", (TOK in out, TOK in (work / "queue-cutover.log").read_text()), (False, False))
    rc, out = run("fallback", "--wait", "0")
    check("fallback again: already done, nothing touched", (rc, "already completed" in out, mutating_calls().count("bootstrap"),
          mutating_calls().count("bootout")), (0, True, 18, 18))
    # ---- unit: the config edit ---------------------------------------------------------------------------------------
    spec2 = importlib.util.spec_from_loader("qc", loader=importlib.machinery.SourceFileLoader("qc", str(HERE / "queue-cutover")))
    qc = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(qc)
    check("set_api_url: no file", qc.set_api_url("", "http://x"), '\n[queue]\napi_url = "http://x"\n')
    check("set_api_url: [queue] without the key", qc.api_url_in(qc.set_api_url("[queue]\nfoo = 1\n", "http://x")), "http://x")
    check("set_api_url: key in another section is not touched",
          qc.set_api_url('[a]\napi_url = "keep"\n', "http://x").count('api_url = "keep"'), 1)

    bad = RESULTS.count(False)
    print("%d checks, %d failed" % (len(RESULTS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    import importlib.machinery  # noqa: E402,F401
    sys.exit(main())
