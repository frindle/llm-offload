#!/usr/bin/env python3
"""test_mini_finish.py -- acceptance tests for ~/bin/mini-finish: Claude finishes a failed local mini job IN PLACE, in the
session that launched it, and the job's own gates (the real runner, --check-only, no model) decide.

  F1  refusals: not a mini job, not failed, spec class, a failed replay, superseded, continued (sidecar or live row), work dir
      gone, verify-mode spec, chain step 2 without per-step refs, another live session's job or claim (and --take), --check
      before a claim, ambiguous / bad / unknown ids, --dry-run alone; a gone launcher is claimable
  F2  single two-phase: the claim snapshots the local attempt and restores the frozen test, the brief names the failed gates,
      --check FAILs on the local attempt and keeps the claim, --dry-run PASS changes nothing (stray file noted), an edited
      frozen test fails, PASS writes FINISHED.json + the log, resolves + clears, prints landing with the local share; a
      finished job refuses a new claim and a rerun of --check only retries resolve + clear
  F3  chain failed at step 2/2 with saved step-refs: frozen test restored from the REVIEW packet, the real runner replays step 1
      and gates step 2; PASS lands (the job-written tests included)
  F4  chain failed at step 1/2: a failing mini-validate leaves nothing behind; PASS writes the continuation (replay 1..1),
      validates and submits it with --continues/--bundle/--label, and the continuation replays green under the real runner
  F5  test draft: the job's draft_accept_cmd through --draft-check; the accepted draft is kept for landing
  F6  diagnose: --try-diagnosis through a fake diag_check; tracked source edits fail the check
  F7  a 409 from the clear endpoint is reported (exit 2, never overridden) and the rerun clears; --list
  F8  queue events: finish_hint / failed_row from the sidecar
  F9  review fixes: the spec's mutation_gate in the check (single, and only the finished step of a chain); the runner refuses
      a real run in a run dir mini-finish holds (class infra, work dir untouched); a --take or claim never runs under a
      running check; with no runner log the frozen test comes from the phase-B REVIEW packet, then the baseline, else a
      warning; an unusable job_id from mini-submit (the queue row, else id unknown) and an uncreatable continuation dir;
      a FINISHED job retries resolve + clear with its spec gone or FINISHED.json unreadable, never under a running
      check, and a job finished between the first look and the lock is refused; a new file in a new directory
      in a single finish; a chain without a ref dir; a chain finish that deletes a file in scope

Everything lives under a temp dir: fake queue / mini-validate / mini-submit commands, a local HTTP stub for the clear endpoint,
CLAUDE_CODE_MESSAGING_SOCKET pointing at a fake socket path. Usage: python3 test_mini_finish.py [-k TAG]. Marker: ALL PASS.
"""
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

BIN = Path(__file__).resolve().parent
FINISH = BIN / "mini-finish"
RUNNER = BIN / "mini-swe-runner.py"
ONLY = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
fails = passed = 0


def chk(name, cond, detail=""):
    global fails, passed
    if cond:
        passed += 1
    else:
        fails += 1
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else f": {str(detail)[-3000:]}"), flush=True)


def want(tag):
    return ONLY is None or ONLY in tag


def load(name, path):
    sp = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


R = load("mini_swe_runner_fin_t", RUNNER)
CALC, CALC_FIXED, CALC_STEP2, GOOD_T, T2 = R.CALC, R.CALC_FIXED, R.CALC_STEP2, R.GOOD_T, R.T2
T = Path(tempfile.mkdtemp(prefix="minifinish-")).resolve()


def sha12(t):
    return hashlib.sha256(t if isinstance(t, bytes) else t.encode()).hexdigest()[:12]


# ---- the clear endpoint stub ------------------------------------------------------------------------------------------------
CLEARS, CLEAR_CODE, CLEARED = [], {}, set()


class ClearStub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        m = re.match(r"^/api/runs/([^/]+)/clear$", self.path)
        jid = m.group(1) if m else "?"
        CLEARS.append((jid, body))
        code = CLEAR_CODE.get(jid) or (404 if jid in CLEARED else 200)
        if code == 200:
            CLEARED.add(jid)
        out = json.dumps({"ok": code == 200, "id": jid, "error": None if code == 200 else "stub"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


SRV = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ClearStub)
threading.Thread(target=SRV.serve_forever, daemon=True).start()


def cleared(jid):
    return [b for i, b in CLEARS if i == jid]


# ---- fake tools ------------------------------------------------------------------------------------------------------------
FAKES = T / "fakes"
FAKES.mkdir()
FAKE_LOG = T / "fake.log"
(FAKES / "queue.py").write_text(
    "import json, os, sys\n"
    "log = os.environ['FAKE_LOG']\n"
    "open(log, 'a').write(json.dumps({'tool': 'queue', 'argv': sys.argv[1:]}) + '\\n')\n"
    "done = log + '.resolved'\n"
    "ids = open(done).read().split() if os.path.exists(done) else []\n"
    "if sys.argv[1:2] == ['resolve']:\n"
    "    if sys.argv[2] in ids:\n"
    "        print('[queue] job not found: ' + sys.argv[2]); sys.exit(1)\n"
    "    open(done, 'a').write(sys.argv[2] + '\\n'); print('[queue] resolved ' + sys.argv[2]); sys.exit(0)\n"
    "sys.exit(2)\n")
(FAKES / "validate.py").write_text(
    "import json, os, sys\n"
    "open(os.environ['FAKE_LOG'], 'a').write(json.dumps({'tool': 'validate', 'argv': sys.argv[1:]}) + '\\n')\n"
    "rc = int(os.environ.get('FAKE_VALIDATE_RC') or 0)\n"
    "print('mini-validate: PASS' if rc == 0 else 'mini-validate: FAIL s2 V4 ref green: FAIL stub')\n"
    "sys.exit(rc)\n")
(FAKES / "submit.py").write_text(  # FAKE_SUBMIT_MODE: noid (no job_id line), noid-row (the queue has the row), garbage
    "import json, os, sys\n"
    "open(os.environ['FAKE_LOG'], 'a').write(json.dumps({'tool': 'submit', 'argv': sys.argv[1:]}) + '\\n')\n"
    "mode = os.environ.get('FAKE_SUBMIT_MODE') or ''\n"
    "print('mini-submit: OK receipt /stub/receipt.json')\n"
    "if mode == 'noid-row':\n"
    "    st = os.environ['MINI_FINISH_STATE']\n"
    "    d = json.load(open(st))\n"
    "    d['jobs'].append({'id': 'c0ffee999999', 'status': 'queued', 'continues': sys.argv[sys.argv.index('--continues') + 1],\n"
    "                      'label': sys.argv[sys.argv.index('--label') + 1]})\n"
    "    json.dump(d, open(st, 'w'))\n"
    "elif mode == 'garbage':\n"
    "    print('job_id: [queue]')\n"
    "elif mode != 'noid':\n"
    "    print('job_id: c0ffee123456')\n")

SOCKS = T / "socks"
SOCKS.mkdir()
ME, OTHER, GONE = SOCKS / "1111.sock", SOCKS / "2222.sock", SOCKS / "3333.sock"
ME.write_text("")
MEN = str(ME)[len("/private"):] if str(ME).startswith("/private/tmp/") else str(ME)  # claims store norm_sock(ME): a TMPDIR under /private/tmp reads as /tmp
OTHER.write_text("")
for d in ("home", "logs", "livelogs"):
    (T / d).mkdir()
STATE = T / "state.json"
STATE.write_text(json.dumps({"jobs": []}))
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CLAUDE_PID", "MINI_", "QEL_", "DIAG_CHECK"))}
ENV.update(MINI_FINISH_HOME=str(T / "home"), MINI_FINISH_LOGS=str(T / "logs"), MINI_FINISH_LIVELOGS=str(T / "livelogs"),
           MINI_FINISH_STATE=str(STATE), MINI_RUNNER=str(RUNNER), MINI_FINISH_API=f"http://127.0.0.1:{SRV.server_address[1]}",
           MINI_FINISH_QUEUE=shlex.join([sys.executable, str(FAKES / "queue.py")]),
           MINI_FINISH_VALIDATE=shlex.join([sys.executable, str(FAKES / "validate.py")]),
           MINI_FINISH_SUBMIT=shlex.join([sys.executable, str(FAKES / "submit.py")]),
           CLAUDE_CODE_MESSAGING_SOCKET=str(ME), FAKE_LOG=str(FAKE_LOG))


def fin(*args, env=None, timeout=900):
    p = subprocess.run([sys.executable, str(FINISH), *args], env={**ENV, **(env or {})}, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def calls(tool=None):
    if not FAKE_LOG.exists():
        return []
    rows = [json.loads(ln) for ln in FAKE_LOG.read_text().splitlines() if ln.strip()]
    return [r["argv"] for r in rows if tool is None or r["tool"] == tool]


def reset_calls():
    for p in (FAKE_LOG, Path(str(FAKE_LOG) + ".resolved")):
        if p.exists():
            p.unlink()


# ---- fixtures ---------------------------------------------------------------------------------------------------------------
TASK_MD = "# TASK\n`add(a, b)` must return the sum.\n\n## Must contain\n\n- `sumFix`\n"
SINGLE = {"mode": "two-phase", "allowed": ["calc.js"], "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js",
          "target_fn": "add", "timeout": 600, "fix_attempts": 1}
S1 = {"task": "GOAL-ONE: add must return the sum.\n\n## Must contain\n\n- `sumFix`\n", "test_file": "calc.test.js",
      "test_cmd": "node --test calc.test.js", "allowed": ["calc.js"], "target_fn": "add"}
S2 = {"task": "GOAL-TWO: other must return 2.\n\n## Must contain\n\n- `otherFix`\n", "test_file": "calc2.test.js",
      "test_cmd": "node --test calc2.test.js", "allowed": ["calc.js"], "target_fn": "other"}
CHAIN = {"timeout": 600, "fix_attempts": 1, "steps": [S1, S2]}
LOCAL_SINGLE = CALC + "// sumFix\n"  # the literal, not the fix: still red
EDITED_T = GOOD_T.replace("add(1, 2), 3", "add(1, 2), -1")
LOCAL_S2 = CALC_FIXED.replace("function other() { return 1; }", "function other() { return 1; } // otherFix")
T2_EDITED = T2.replace("strictEqual(other(), 2)", "strictEqual(other(), 1)")
WEAK_T = GOOD_T.split("test('adds neg'")[0]
LL_SINGLE = ("[mini-runner] two-phase budgets: total=2400s phase_a=600s phase_b=1800s\n"
             f"[mini-runner] test frozen: calc.test.js sha={sha12(GOOD_T)} reach=['add'] tests=2\n"
             "[mini-runner] phase B attempt 1: RED: test_green: 2 failing\n"
             "RESULT FAIL phase-b RED test_green: 2 failing class=MODEL\n")
REVIEW_SINGLE = ("# REVIEW packet: FAIL phase-b RED\n\nallowed: calc.js\ntest: calc.test.js cmd: node --test calc.test.js\n\n"
                 "## Phase B gates\n\n| gate | | detail |\n|---|---|---|\n| test_green | FAIL | 2 failing: adds, adds neg |\n"
                 "| test_unchanged | FAIL | the test was edited |\n| fn_scope | ok |  |\n\n"
                 f"## Test file\n\n```\n{GOOD_T}\n```\n\n## Diff\n\n```\n(diff)\n```\n")
LL_CHAIN1 = ("[mini-runner] === step 1/2: calc.test.js ===\n[mini-runner] two-phase budgets: total=2400s\n"
             f"[mini-runner] test frozen: calc.test.js sha={sha12(GOOD_T)} reach=['add'] tests=2\n"
             "RESULT FAIL step-1 phase-b RED test_green: 2 failing class=MODEL\n")
LL_CHAIN2 = ("[mini-runner] === step 1/2: calc.test.js ===\n[mini-runner] two-phase budgets: total=2400s\n"
             f"[mini-runner] test frozen: calc.test.js sha={sha12(GOOD_T)} reach=['add'] tests=2\n"
             "[mini-runner] === step 2/2: calc2.test.js ===\n[mini-runner] two-phase budgets: total=2400s\n"
             f"[mini-runner] test frozen: calc2.test.js sha={sha12(T2)} reach=['other'] tests=2\n"
             "RESULT FAIL step-2 phase-b RED test_green: 1 failing class=MODEL\n")
REVIEW_CHAIN2 = ("# REVIEW packet: FAIL step-2\n\n| step | test | result |\n|---|---|---|\n| 1 | `calc.test.js` | PASS |\n"
                 "| 2 | `calc2.test.js` | FAIL |\n\n"
                 "## Step 1: `calc.test.js` (PASS)\n\n### Phase B gates\n\n| gate | | detail |\n|---|---|---|\n| test_green | ok |  |\n\n"
                 f"### Test file\n\n```\n{GOOD_T}\n```\n\n### Diff\n\n```\n(diff)\n```\n\n"
                 "## Step 2: `calc2.test.js` (FAIL)\n\n### Phase B gates\n\n| gate | | detail |\n|---|---|---|\n"
                 "| test_green | FAIL | o1 failing |\n\n"
                 f"### Test file\n\n```\n{T2}\n```\n\n### Diff\n\n```\n(diff)\n```\n")
DRAFT_CMD = ("if grep -q 'adds neg' {draft}; then echo '  s1 V3 red: ok'; echo '  s1 V4 ref green: ok'; echo ACCEPT; "
             "else echo '  s1 V3 red for the right reason: FAIL no test covers a negative operand'; echo REJECT; exit 3; fi")
DRAFT_SPEC = {**SINGLE, "phase_a_only": True, "draft_accept_cmd": DRAFT_CMD}
FAKE_DIAG = T / "fake_diag.py"
FAKE_DIAG.write_text("import os\ndef check(repo, report, spec=None):\n    t = open(os.path.join(repo, report)).read()\n"
                     "    return ('GOOD' in t, [] if 'GOOD' in t else ['no GOOD marker'])\n")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Job:
    pass


def mkjob(name, spec, tracked, work_files, *, untracked=None, jid=None, label=None, cls="model", status="failed",
          verdict="FAIL (mode=two-phase phase-b RED test_green)", livelog="", launched_by=ME, review=None, ref=None,
          run_files=None, step_refs=None, logs=None, task_md=None, **side):
    j = Job()
    j.root = T / name
    repo = j.root / "repo"
    repo.mkdir(parents=True)
    for f, t in {**tracked, ".harness.json": "{}", **(untracked or {})}.items():
        (repo / f).write_text(t)
    git(repo, "init", "-q")
    git(repo, "add", *tracked)
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    j.repo = repo
    j.spec_dir = j.root / "specs" / name
    j.spec_dir.mkdir(parents=True)
    j.spec = j.spec_dir / "spec.json"
    if task_md is not None:  # a job-local TASK.md next to spec.json, the way mini-spec writes it
        (j.spec_dir / "TASK.md").write_text(task_md)
        spec = {**spec, "task": str(j.spec_dir / "TASK.md")}
    j.spec.write_text(json.dumps({**spec, "baseline_from": str(repo)}, indent=1))
    for rel, t in (ref or {}).items():
        (j.spec_dir / "ref" / rel).parent.mkdir(parents=True, exist_ok=True)
        (j.spec_dir / "ref" / rel).write_text(t)
    j.run = j.root / "mini-work" / f"{name}-20261102120000"
    j.work = j.run / "work"
    shutil.copytree(repo, j.work, symlinks=True)
    for f, t in work_files.items():
        (j.work / f).write_text(t)
    for rel, t in (run_files or {}).items():
        (j.run / rel).write_text(t)
    if review is not None:
        (j.run / "REVIEW.md").write_text(review)
    for sname, files in (step_refs or {}).items():
        tmp = j.root / f"tmp-{sname}"
        tmp.mkdir()
        for f, t in files.items():
            (tmp / f).write_text(t)
        R.save_step_ref(tmp, j.run / "step-refs" / sname, list(files))
    j.id = jid or hashlib.sha1(name.encode()).hexdigest()[:12]
    j.label = label or name
    rec = {"id": j.id, "label": j.label, "bundle": "fin-test", "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "cwd": str(j.work),
           "status": status, "exit_code": 1, "launched_by": str(launched_by) if launched_by else None,
           "launched_by_session": None, "task_file": str(j.spec), "runner": str(RUNNER), "failure_class": cls,
           "failure_detail": f"mini-swe-runner RESULT: {verdict}", "runner_verdict": verdict, "terminal_reason": None,
           "continues": None, **side}
    (logs or (T / "logs")).mkdir(parents=True, exist_ok=True)
    ((logs or (T / "logs")) / f"{j.id}.done.json").write_text(json.dumps(rec, indent=1))
    if livelog:
        (T / "livelogs" / f"{j.id}-{j.label}.livelog").write_text(livelog)
    j.fdir = j.run / "finish"
    return j


def single_job(name, **kw):
    return mkjob(name, SINGLE, {"calc.js": CALC, "calc.test.js": GOOD_T}, {"calc.js": LOCAL_SINGLE, "calc.test.js": EDITED_T},
                 untracked={"TASK.md": TASK_MD}, livelog=LL_SINGLE, review=REVIEW_SINGLE, **kw)


def chain2_job(name, refs=True, **kw):
    return mkjob(name, CHAIN, {"calc.js": CALC}, {"calc.js": LOCAL_S2, "calc.test.js": GOOD_T, "calc2.test.js": T2_EDITED,
                                                  "TASK.md": S2["task"]},
                 verdict="FAIL (step-2 phase-b RED test_green: 1 failing)", livelog=LL_CHAIN2, review=REVIEW_CHAIN2,
                 step_refs={"s1": {"calc.js": CALC_FIXED, "calc.test.js": GOOD_T}} if refs else None, **kw)


def jread(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


# ---- F1 refusals ------------------------------------------------------------------------------------------------------------
if want("F1 refusals"):
    for i, (what, kw, msg) in enumerate((
            ("not a mini job", {"runner": "/x/bin/ollama-worker.py"}, "not a mini-swe-runner job"),
            ("not failed", {"status": "done"}, "is 'done', not failed"),
            ("spec class", {"cls": "spec"}, "class spec"),
            ("failed replay", {"verdict": "FAIL step-1 bad-spec replay RED test_green: 1 failing"}, "REPLAYING saved steps"),
            ("superseded", {"superseded_by": "abc999"}, "superseded by abc999"),
            ("continued (sidecar)", {"continued_by": "abc998"}, "already has a continuation (abc998)"),
            ("another live session's job", {"launched_by": OTHER}, "launched by another live session"))):
        j = single_job(f"ref-{i}", **kw)
        rc, out = fin(j.id)
        chk(f"F1 {what} refused", rc == 1 and "refused" in out and msg in out and not j.fdir.exists(), out)
    rc, out = fin(hashlib.sha1(b"ref-2").hexdigest()[:12])
    chk("F1 spec class points at mini-triage", "~/bin/mini-triage" in out, out)
    j = single_job("ref-gone")
    shutil.rmtree(j.work)
    rc, out = fin(j.id)
    chk("F1 work dir gone refused", rc == 1 and "nothing to finish in place" in out, out)
    j = single_job("ref-livecont")
    STATE.write_text(json.dumps({"jobs": [{"id": "zz0000000001", "continues": j.id, "status": "queued", "label": "x-c1"}]}))
    rc, out = fin(j.id)
    STATE.write_text(json.dumps({"jobs": []}))
    chk("F1 live continuation row refused", rc == 1 and "already has a continuation (zz0000000001)" in out, out)
    j = mkjob("ref-verify", {"mode": "verify", "allowed": ["calc.js"], "test_cmd": "true"}, {"calc.js": CALC}, {"calc.js": LOCAL_SINGLE})
    rc, out = fin(j.id)
    chk("F1 verify-mode spec refused", rc == 1 and "verify mode" in out, out)
    j = chain2_job("ref-norefs", refs=False)
    rc, out = fin(j.id)
    chk("F1 chain step 2 without step-refs refused", rc == 1 and "predates per-step refs" in out and "mini-triage" in out, out)
    j = single_job("ref-take", launched_by=OTHER)
    rc, out = fin(j.id, "--take")
    cl = jread(j.fdir / "FINISH.json")
    chk("F1 --take claims another session's job", rc == 0 and cl.get("claimed_by") == MEN and cl["claims"][-1]["take"] is True, out)
    j = single_job("ref-orphan", launched_by=GONE)
    rc, out = fin(j.id)
    chk("F1 a gone launcher's job is claimable", rc == 0 and (j.fdir / "FINISH.json").is_file(), out)
    j = single_job("ref-claimed")
    j.fdir.mkdir(parents=True)
    (j.fdir / "FINISH.json").write_text(json.dumps({"job": j.id, "claimed_by": str(OTHER), "ts": "2026-11-02T00:00:00Z", "claims": []}))
    rc, out = fin(j.id)
    rc2, out2 = fin(j.id, "--check")
    chk("F1 another live session's claim refused (claim and check)", rc == 1 and rc2 == 1 and "being finished by another live session" in out
        and "being finished by another live session" in out2, out + out2)
    rc, out = fin(j.id, "--take")
    chk("F1 --take takes over a claim", rc == 0 and jread(j.fdir / "FINISH.json").get("claimed_by") == MEN, out)
    j = single_job("ref-noclaim")
    rc, out = fin(j.id, "--check")
    chk("F1 --check before a claim refused", rc == 1 and "not claimed yet" in out, out)
    single_job("ref-amb1", jid="abcd00000001")
    single_job("ref-amb2", jid="abcd00000002")
    rc, out = fin("abcd0000")
    chk("F1 ambiguous prefix refused", rc == 1 and "ambiguous" in out, out)
    rc, out = fin("abcd00000002", "--check", "--dry-run")
    chk("F1 exact id wins (and is unclaimed)", rc == 1 and "not claimed yet" in out, out)
    rc, out = fin("../etc")
    rc2, out2 = fin("ffffffffffff")
    rc3, out3 = fin("abcd00000001", "--dry-run")
    chk("F1 bad id / unknown id / --dry-run alone", rc == 1 and "bad job id" in out and rc2 == 1 and "no job" in out2 and rc3 == 1
        and "goes with --check" in out3, out + out2 + out3)

# ---- F2 single --------------------------------------------------------------------------------------------------------------
if want("F2 single"):
    reset_calls()
    j = single_job("single-a")
    rc, out = fin(j.id)
    chk("F2 claim rc 0, in place", rc == 0 and "finish IN PLACE" in out and str(j.work) in out, out)
    chk("F2 frozen test restored from the baseline", "restored the frozen test calc.test.js" in out and "from the baseline" in out
        and (j.work / "calc.test.js").read_text() == GOOD_T, out)
    chk("F2 brief: failed gates from REVIEW", "test_green: 2 failing: adds, adds neg" in out and "test_unchanged" in out, out)
    chk("F2 brief: delta vs HEAD, must contain, next", "calc.js  (+1 -0)" in out and "`sumFix`" in out
        and f"~/bin/mini-finish {j.id} --check" in out and "no subagent, no API" in out, out)
    cl = jread(j.fdir / "FINISH.json")
    chk("F2 claim file", cl.get("claimed_by") == MEN and cl.get("files") == ["calc.js"] and cl.get("frozen") == sha12(GOOD_T)
        and cl.get("mode") == "single", cl)
    chk("F2 local attempt snapshot", (j.fdir / "local" / "calc.js").read_text() == LOCAL_SINGLE, "")
    rc, out = fin(j.id)
    chk("F2 second call: brief again, no new snapshot", rc == 0 and "brief again" in out and len(jread(j.fdir / "FINISH.json")["claims"]) == 2
        and (j.fdir / "local" / "calc.js").read_text() == LOCAL_SINGLE, out)
    rc, out = fin(j.id, "--check")
    chk("F2 check of the local attempt FAILs, claim kept", rc == 1 and "check FAIL" in out and "test_green" in out and "claim is kept" in out, out)
    chk("F2 FAIL: nothing resolved or cleared", not calls() and not cleared(j.id), (calls(), CLEARS))
    (j.work / "calc.js").write_text(CALC_FIXED)  # Claude finishes in place
    (j.work / "notes.txt").write_text("scratch\n")
    rc, out = fin(j.id, "--check", "--dry-run")
    chk("F2 --dry-run PASS", rc == 0 and "check PASS" in out and "--dry-run: nothing queued" in out, out)
    chk("F2 --dry-run: stray noted, nothing written or called", "notes.txt" in out and not (j.fdir / "FINISHED.json").exists()
        and not calls() and not cleared(j.id), out)
    (j.work / "notes.txt").unlink()
    (j.work / "calc.test.js").write_text(GOOD_T + "// tweak\n")
    rc, out = fin(j.id, "--check", "--dry-run")
    chk("F2 an edited frozen test fails before the runner", rc == 1 and "frozen test calc.test.js changed" in out
        and not list(j.fdir.glob("check-*/check.log")), out)
    (j.work / "calc.test.js").write_text(GOOD_T)
    rc, out = fin(j.id, "--check")
    fd = jread(j.fdir / "FINISHED.json")
    chk("F2 PASS: rc 0, FINISHED.json, local share 0.5", rc == 0 and fd.get("verdict") == "PASS" and fd.get("local_share") == 0.5
        and fd.get("finished_by") == MEN, (out, fd))
    chk("F2 PASS: resolved and cleared once, body {} (no override)", calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"],
        (calls(), CLEARS))
    log = (T / "home" / "mini-finish.jsonl").read_text() if (T / "home" / "mini-finish.jsonl").exists() else ""
    chk("F2 PASS: logged, landing + scorecard printed", f'"job": "{j.id}"' in log and "land:" in out
        and f"mini-scorecard ledger add --job {j.id}" in out and "local share 50%" in out, out)
    rc, out = fin(j.id)
    chk("F2 a finished job refuses a new claim", rc == 1 and "already finished" in out, out)
    rc, out = fin(j.id, "--check")
    chk("F2 rerun of --check retries resolve + clear only", rc == 0 and "retrying resolve + clear" in out and "no live row" in out
        and "nothing to clear" in out and len(list(j.fdir.glob("check-*"))) == 1, out)

# ---- F3 chain, failed at the last step ---------------------------------------------------------------------------------------
if want("F3 chain2"):
    reset_calls()
    j = chain2_job("chain-two")
    rc, out = fin(j.id)
    chk("F3 claim: step 2/2", rc == 0 and "[chain, step 2/2]" in out, out)
    chk("F3 frozen step-2 test restored from the REVIEW packet", "from the REVIEW packet" in out and (j.work / "calc2.test.js").read_text() == T2, out)
    chk("F3 brief: step-2 gates, delta vs the step-1 ref, must contain", "test_green: o1 failing" in out and "calc.js  (+1 -1)" in out
        and "`otherFix`" in out, out)
    rc, out = fin(j.id, "--check")
    chk("F3 check of the local attempt FAILs", rc == 1 and "check FAIL" in out and "test_green" in out, out)
    (j.work / "calc.js").write_text(CALC_STEP2)
    rc, out = fin(j.id, "--check")
    logs = sorted(j.fdir.glob("check-*/check.log"))
    lt = logs[-1].read_text() if logs else ""
    chk("F3 PASS: the real runner replayed step 1 and gated step 2", rc == 0 and "check PASS" in out and "replay step 1: GREEN" in lt
        and "replay step 2: GREEN" in lt, out + lt[-1500:])
    fd = jread(j.fdir / "FINISHED.json")
    chk("F3 FINISHED, no continuation, resolved + cleared", fd.get("step") == 2 and fd.get("continuation") is None
        and calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"], (fd, calls()))
    chk("F3 landing lists the earlier step's files and the job-written tests", "calc.js, calc.test.js, calc2.test.js" in out, out)
    chk("F3 the claim dir holds no runner scratch outside check-*", not (j.run / "step-refs" / "s2").exists(), "")

# ---- F4 chain, failed at step 1: continuation ----------------------------------------------------------------------------------
if want("F4 continuation"):
    reset_calls()
    j = mkjob("chain-one", CHAIN, {"calc.js": CALC}, {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T, "TASK.md": S1["task"]},
              verdict="FAIL (step-1 phase-b RED test_green: 2 failing)", livelog=LL_CHAIN1, label="calc-chain-one",
              ref={"s1/calc.js": CALC_FIXED, "s2/calc.js": CALC_STEP2})
    rc, out = fin(j.id)
    chk("F4 claim: step 1/2", rc == 0 and "[chain, step 1/2]" in out and "steps 2..2 go back to the local model" in out, out)
    (j.work / "calc.js").write_text(CALC_FIXED)
    cont = j.spec_dir.parent / "chain-one-c1"
    rc, out = fin(j.id, "--check", env={"FAKE_VALIDATE_RC": "1"})
    chk("F4 a failing mini-validate: exit 2, nothing resolved, no leftover dir", rc == 2 and "continuation failed" in out
        and not cont.exists() and not (j.fdir / "FINISHED.json").exists() and calls("queue") == [] and not cleared(j.id)
        and calls("submit") == [], out)
    chk("F4 the validate log is kept with the check", bool(list(j.fdir.glob("check-*/validate.log"))), "")
    reset_calls()
    rc, out = fin(j.id, "--check")
    sp = jread(cont / "spec.json")
    chk("F4 PASS queues the continuation", rc == 0 and "queued continuation c0ffee123456 calc-chain-one-c1" in out, out)
    chk("F4 continuation spec: replay 1..1, absolute ref, all steps", sp.get("replay") == {"refs": str(cont / "replay"), "through": 1,
        "job": j.id, "by": "mini-finish"} and sp.get("ref") == str(j.spec_dir / "ref") and len(sp.get("steps") or []) == 2, sp)
    chk("F4 continuation refs + TRIAGE.md", (cont / "replay" / "s1" / "calc.js").read_text() == CALC_FIXED
        and (cont / "replay" / "s1" / "calc.test.js").read_text() == GOOD_T and (cont / "TRIAGE.md").is_file(), "")
    chk("F4 validated, then submitted with --continues/--bundle/--label/--model", calls("validate") == [[str(cont / "spec.json")]]
        and calls("submit") == [[str(cont / "spec.json"), "--label", "calc-chain-one-c1", "--model", "qwen3.6-35b-a3b-vl-mtp-mxfp8",
                                 "--bundle", "fin-test", "--continues", j.id]], calls())
    fd = jread(j.fdir / "FINISHED.json")
    chk("F4 FINISHED records the continuation; resolved + cleared; no landing", (fd.get("continuation") or {}).get("job") == "c0ffee123456"
        and calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"] and "land:" not in out, (fd, out))
    cw = T / "chain-one" / "contcheck" / "work"
    cw.mkdir(parents=True)
    p = subprocess.run([sys.executable, str(RUNNER), "--check-only", "--cwd", str(cw), "--task-file", str(cont / "spec.json")],
                       capture_output=True, text=True, timeout=900, env={k: v for k, v in ENV.items() if not k.startswith("MINI_")})
    chk("F4 the continuation replays step 1 green under the real runner", p.returncode == 0 and "replay step 1: GREEN" in p.stdout
        and "check-only: 2 steps ok" in p.stdout, p.stdout[-1500:] + p.stderr[-500:])

# ---- F5 test draft ------------------------------------------------------------------------------------------------------------
if want("F5 draft"):
    reset_calls()
    j = mkjob("draft-a", DRAFT_SPEC, {"calc.js": CALC}, {"calc.test.js": WEAK_T}, untracked={"TASK.md": TASK_MD},
              verdict="FAIL (mode=test-draft rejected rc=3 draft=x)", livelog="RESULT FAIL mode=test-draft rejected rc=3 class=MODEL\n",
              run_files={"DRAFT-CHECK.txt": "  s1 V3 red for the right reason: FAIL no test covers a negative operand\nREJECT\n"})
    rc, out = fin(j.id)
    cl = jread(j.fdir / "FINISH.json")
    chk("F5 claim: draft, the test is the file to finish", rc == 0 and "[draft]" in out and cl.get("files") == ["calc.test.js"]
        and cl.get("frozen") is None and "negative operand" in out, out)
    rc, out = fin(j.id, "--check")
    chk("F5 check of the weak draft FAILs", rc == 1 and "check FAIL" in out, out)
    (j.work / "calc.test.js").write_text(GOOD_T)
    rc, out = fin(j.id, "--check")
    fd = jread(j.fdir / "FINISHED.json")
    chk("F5 PASS through draft_accept_cmd; the accepted draft is kept", rc == 0 and "check PASS" in out
        and (j.fdir / "draft_calc.test.js").read_text() == GOOD_T and fd.get("draft") == str(j.fdir / "draft_calc.test.js")
        and "land: the accepted draft" in out, out)
    chk("F5 resolved + cleared", calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"], calls())

# ---- F6 diagnose --------------------------------------------------------------------------------------------------------------
if want("F6 diagnose"):
    reset_calls()
    denv = {"DIAG_CHECK": str(FAKE_DIAG)}
    j = mkjob("diag-a", {"mode": "diagnose", "allowed": []}, {"calc.js": CALC}, {"DIAGNOSIS.md": "weak report\n"},
              task_md="# TASK\nDiagnose why add(1, 2) is not 3.\n",
              verdict="FAIL (mode=diagnose rejected: no GOOD marker)",
              livelog="[mini-runner] diagnose: REJECTED: no GOOD marker\nRESULT FAIL mode=diagnose rejected class=MODEL\n")
    rc, out = fin(j.id, env=denv)
    chk("F6 claim: diagnose, DIAGNOSIS.md to finish", rc == 0 and "[diagnose]" in out and jread(j.fdir / "FINISH.json").get("files") == ["DIAGNOSIS.md"], out)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check", "--dry-run", env=denv)
    chk("F6 a tracked source edit fails the read-only check", rc == 1 and "read-only" in out and "calc.js" in out and "no GOOD marker" in out
        and "not part of this finish" not in out, out)
    git(j.work, "checkout", "--", "calc.js")
    (j.work / "DIAGNOSIS.md").write_text("GOOD report: add subtracts\n")
    rc, out = fin(j.id, "--check", env=denv)
    chk("F6 PASS through diag_check, nothing to land, resolved + cleared", rc == 0 and "check PASS" in out and "nothing to land" in out
        and calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"], out)

# ---- F7 409 from clear, --list ----------------------------------------------------------------------------------------------------
if want("F7 clear409 list"):
    reset_calls()
    j = single_job("clear-409")
    CLEAR_CODE[j.id] = 409
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check")
    chk("F7 a 409 from clear: exit 2, reported, never overridden", rc == 2 and "HTTP 409" in out and "Not overridden" in out
        and (j.fdir / "FINISHED.json").is_file() and all(b == b"{}" for b in cleared(j.id)), out)
    CLEAR_CODE.pop(j.id)
    rc, out = fin(j.id, "--check")
    chk("F7 the rerun clears", rc == 0 and "retrying resolve + clear" in out and "cleared" in out and len(cleared(j.id)) == 2, out)
    ldir = T / "logs-list"
    a = single_job("list-a", logs=ldir)
    single_job("list-b", logs=ldir, cls="infra")
    c = single_job("list-c", logs=ldir)
    fin(c.id, env={"MINI_FINISH_LOGS": str(ldir)})
    rc, out = fin("--list", env={"MINI_FINISH_LOGS": str(ldir)})
    chk("F7 --list: finishable jobs, claims marked, the rest counted", rc == 0 and "2 finishable failed job(s)" in out
        and "(1 failed mini job(s) not finishable)" in out and a.id in out and re.search(rf"{c.id} .*claimed", out) is not None
        and "launcher=yours" in out, out)

# ---- F8 queue-event hint ----------------------------------------------------------------------------------------------------------
if want("F8 events"):
    os.environ["QEL_LOG_DIR"] = str(T / "logs-ev")
    os.environ["QEL_DIR"] = str(T / "qel")
    (T / "qel").mkdir(exist_ok=True)
    ok = single_job("ev-ok", logs=T / "logs-ev")
    bad = single_job("ev-spec", logs=T / "logs-ev", cls="spec")
    rp = single_job("ev-replay", logs=T / "logs-ev", verdict="FAIL step-2 bad-spec replay RED x")
    gone = single_job("ev-gone", logs=T / "logs-ev")
    shutil.rmtree(gone.work)
    QEL = load("qel_fin_t", BIN / "queue-event-log.py")
    chk("F8 finish_hint: a model-class mini failure with its work dir", QEL.finish_hint(ok.id, {}) == f"~/bin/mini-finish {ok.id}", "")
    chk("F8 finish_hint: none for spec class, a failed replay, a gone work dir", QEL.finish_hint(bad.id, {}) is None
        and QEL.finish_hint(rp.id, {}) is None and QEL.finish_hint(gone.id, {}) is None, "")
    chk("F8 finish_hint: the live row overrides the sidecar", QEL.finish_hint(ok.id, {"failure_class": "infra"}) is None, "")
    row = QEL.failed_row(ok.id, {"label": "ev-ok", "reason": "x"}, {"launched_by": "uds:" + str(ME)})
    chk("F8 failed_row carries launcher + finish", row.get("finish") == f"~/bin/mini-finish {ok.id}" and row.get("launched_by") == str(ME), row)
    DL = load("qed_fin_t", BIN / "queue-events-deliver.py")
    line = DL.fmt({"seq": 7, "state": "failed", "id": ok.id, "label": "ev-ok", "finish": f"~/bin/mini-finish {ok.id}"}, "yours")
    chk("F8 the delivered line carries finish=", line.endswith(f"finish=`~/bin/mini-finish {ok.id}`") and "#7 failed (yours)" in line, line)
    chk("F8 the header says: this session, no subagent/API, --check", "THIS session" in DL.HEADER and "no subagent" in DL.HEADER
        and "--check" in DL.HEADER, DL.HEADER)

# ---- F9 review fixes ------------------------------------------------------------------------------------------------------------
MUT_FAIL = {"cmd": "echo MUTATION killed=1 survived=5", "fail_below": 0.5}
MUT_PASS = {"cmd": "echo MUTATION killed=5 survived=0", "fail_below": 0.5}


def set_spec(j, **kw):
    sp = json.loads(j.spec.read_text())
    sp.update(kw)
    j.spec.write_text(json.dumps(sp, indent=1))


def last_log(j):
    logs = sorted(j.fdir.glob("check-*/check.log"))
    return logs[-1].read_text() if logs else ""


def tree(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file() and ".git" not in p.parts}


def chain1_job(name, ref=True, **kw):
    return mkjob(name, CHAIN, {"calc.js": CALC}, {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T, "TASK.md": S1["task"]},
                 verdict="FAIL (step-1 phase-b RED test_green: 2 failing)", livelog=LL_CHAIN1, label=f"calc-{name}",
                 ref={"s1/calc.js": CALC_FIXED, "s2/calc.js": CALC_STEP2} if ref else None, **kw)


if want("F9 mutation single"):
    reset_calls()
    j = mkjob("mut-single", {**SINGLE, "mutation_gate": MUT_FAIL}, {"calc.js": CALC, "calc.test.js": GOOD_T},
              {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T}, untracked={"TASK.md": TASK_MD}, livelog=LL_SINGLE,
              review=REVIEW_SINGLE)
    rc, out = fin(j.id)
    chk("F9 brief: the check runs the spec's mutation_gate", rc == 0 and "mutation_gate: the check runs it" in out
        and "fail_below=0.5" in out, out)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check", "--dry-run")
    chk("F9 single: a green fix the mutation gate scores below fail_below FAILs", rc == 1 and "check FAIL" in out
        and "mutation" in out and "< fail_below 0.5" in out and not (j.fdir / "FINISHED.json").exists(), out)
    set_spec(j, mutation_gate=MUT_PASS)
    rc, out = fin(j.id, "--check", "--dry-run")
    lt = last_log(j)
    chk("F9 single: ...and PASSes once the gate is met (its row in the log)", rc == 0 and "check PASS" in out
        and re.search(r"^\[mini-runner\]   mutation: ok custom on calc\.js:\d+-\d+: score=1\.00", lt, re.M) is not None,
        out + lt[-1500:])

if want("F9 mutation chain"):
    reset_calls()
    j = mkjob("mut-chain", {**CHAIN, "mutation_gate": MUT_FAIL}, {"calc.js": CALC},
              {"calc.js": LOCAL_S2, "calc.test.js": GOOD_T, "calc2.test.js": T2_EDITED, "TASK.md": S2["task"]},
              verdict="FAIL (step-2 phase-b RED test_green: 1 failing)", livelog=LL_CHAIN2, review=REVIEW_CHAIN2,
              step_refs={"s1": {"calc.js": CALC_FIXED, "calc.test.js": GOOD_T}})
    rc, out = fin(j.id)
    chk("F9 chain brief names the mutation gate", rc == 0 and "mutation_gate: the check runs it" in out, out)
    (j.work / "calc.js").write_text(CALC_STEP2)
    rc, out = fin(j.id, "--check", "--dry-run")
    lt = last_log(j)
    s1 = lt.split("replay step 1:", 1)[-1].split("replay step 2:", 1)[0]
    chk("F9 chain: the mutation gate runs on the finished step only, and fails it", rc == 1 and "check FAIL" in out
        and "< fail_below 0.5" in out and "replay step 1: GREEN" in lt and "mutation:" not in s1, out + lt[-2500:])
    set_spec(j, mutation_gate=MUT_PASS)
    rc, out = fin(j.id, "--check", "--dry-run")
    lt = last_log(j)
    chk("F9 chain: ...and PASSes once the gate is met", rc == 0 and "check PASS" in out and "replay step 2: GREEN" in lt
        and re.search(r"^\[mini-runner\]   mutation: ok custom on calc\.js", lt, re.M) is not None, out + lt[-2500:])

if want("F9 runner guard"):
    j = single_job("guard-a")
    chk("F9 finish_guard: nothing before a claim", R.finish_guard(j.work) is None, "")
    rc, out = fin(j.id)
    g = R.finish_guard(j.work)
    chk("F9 finish_guard: a claimed run dir is held", rc == 0 and g is not None and g.startswith("finish-claimed: job " + j.id)
        and "being finished in place" in g, (out, g))
    (j.work / "calc.js").write_text(CALC_FIXED)
    before = tree(j.work)
    if g:  # only with the guard up; past it a real run reads the endpoint config, pointed at a missing file here regardless
        p = subprocess.run([sys.executable, str(RUNNER), "--cwd", str(j.work), "--task-file", str(j.spec)], capture_output=True,
                           text=True, timeout=120, env={**{k: v for k, v in ENV.items() if not k.startswith("MINI_")},
                                                        "DARKBLOOM_LOCAL_JSON": str(T / "no-such-local.json")})
        chk("F9 a real run in a claimed run dir refuses before touching it (bad-cwd, class=INFRA)", p.returncode != 0
            and re.search(r"^RESULT FAIL bad-cwd finish-claimed: job " + j.id + r" .* class=INFRA$", p.stdout, re.M) is not None
            and "endpoint config" not in p.stdout and tree(j.work) == before, p.stdout[-1500:] + p.stderr[-500:])
        p = subprocess.run([sys.executable, str(RUNNER), "--check-only", "--cwd", str(T / "guard-co" / "work"), "--task-file",
                            str(j.spec)], capture_output=True, text=True, timeout=600,
                           env={k: v for k, v in ENV.items() if not k.startswith("MINI_")})
        chk("F9 --check-only is not held (it builds in its own dir)", "finish-claimed" not in p.stdout, p.stdout[-800:])
    u = T / "guard-unit" / "run"
    (u / "work").mkdir(parents=True)
    (u / "finish").mkdir()
    (u / "finish" / "FINISH.json").write_text(json.dumps({"job": "feedfacecafe"}))
    (u / "finish" / "FINISHED.json").write_text("{}")
    g = R.finish_guard(u / "work") or ""
    chk("F9 finish_guard: a finished run dir is still held (land or move its work)", "feedfacecafe" in g and "was finished in place" in g, g)

if want("F9 lock"):
    import fcntl
    j = single_job("lock-a")
    j.fdir.mkdir(parents=True)
    held = {"job": j.id, "claimed_by": str(OTHER), "ts": "2026-11-02T00:00:00Z", "claims": []}
    (j.fdir / "FINISH.json").write_text(json.dumps(held))
    fh = open(j.fdir / ".check.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a check of this job is running
    rc, out = fin(j.id, "--check", "--take")
    rc2, out2 = fin(j.id, "--take")
    chk("F9 a --take (with or without --check) under a running check is refused; the claim is not rewritten", rc == 1 and rc2 == 1
        and "already running" in out and "already running" in out2 and jread(j.fdir / "FINISH.json") == held, out + out2)
    fh.close()
    rc, out = fin(j.id, "--take")
    chk("F9 ...and goes through once the check ends", rc == 0 and jread(j.fdir / "FINISH.json").get("claimed_by") == MEN, out)

if want("F9 frozen"):
    j = mkjob("frozen-review", SINGLE, {"calc.js": CALC, "calc.test.js": WEAK_T}, {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T},
              untracked={"TASK.md": TASK_MD}, review=REVIEW_SINGLE)
    rc, out = fin(j.id)
    cl = jread(j.fdir / "FINISH.json")
    chk("F9 no runner log: the frozen test comes from the phase-B REVIEW packet, not the baseline", rc == 0
        and cl.get("frozen") == sha12(GOOD_T) and (j.work / "calc.test.js").read_text() == GOOD_T
        and "inferred from the REVIEW packet" in out and "restored the frozen test" not in out, out)
    j = mkjob("frozen-base", SINGLE, {"calc.js": CALC, "calc.test.js": GOOD_T}, {"calc.js": LOCAL_SINGLE, "calc.test.js": EDITED_T},
              untracked={"TASK.md": TASK_MD})
    rc, out = fin(j.id)
    chk("F9 no runner log or packet: the baseline's supplied test, and it says so", rc == 0
        and jread(j.fdir / "FINISH.json").get("frozen") == sha12(GOOD_T) and "inferred from the baseline's supplied test" in out
        and (j.work / "calc.test.js").read_text() == GOOD_T, out)
    j = mkjob("frozen-unknown", SINGLE, {"calc.js": CALC}, {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T},
              untracked={"TASK.md": TASK_MD})
    rc, out = fin(j.id)
    cl = jread(j.fdir / "FINISH.json")
    chk("F9 nothing to infer it from: a warning, and the test is part of the finish", rc == 0 and cl.get("frozen") is None
        and "calc.test.js" in (cl.get("files") or []) and "whether phase A froze one is unknown" in out, out)
    rej = ("# REVIEW packet: FAIL phase-a rejected\n\n## Phase A REJECTED (no fix attempted)\n\n- weak\n\n"
           f"## Test file (rejected)\n\n```\n{WEAK_T}\n```\n\n## Model's final message\n\n```\n(none)\n```\n")
    j = mkjob("frozen-rejected", SINGLE, {"calc.js": CALC}, {"calc.js": LOCAL_SINGLE, "calc.test.js": WEAK_T},
              untracked={"TASK.md": TASK_MD}, review=rej)
    rc, out = fin(j.id)
    cl = jread(j.fdir / "FINISH.json")
    chk("F9 a phase-A rejection packet: never frozen, no warning", rc == 0 and cl.get("frozen") is None
        and "was never frozen" in out and "WARNING" not in out, out)

if want("F9 continuation id"):
    reset_calls()
    j = chain1_job("cid-garbage")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check", env={"FAKE_SUBMIT_MODE": "garbage"})
    fd = jread(j.fdir / "FINISHED.json")
    cont = j.spec_dir.parent / "cid-garbage-c1"
    chk("F9 an unusable job_id after an rc-0 submit: queued, id unknown, dir kept, resolved + cleared", rc == 0
        and "printed no usable job_id" in out and "queued continuation (id unknown)" in out and cont.is_dir()
        and (fd.get("continuation") or {}).get("job") is None and (fd.get("continuation") or {}).get("dir") == str(cont)
        and calls("queue") == [["resolve", j.id]] and cleared(j.id) == [b"{}"], (out, fd))
    rc, out = fin(j.id, "--check")
    chk("F9 ...and its retry names the unknown id", rc == 0 and "continuation (id unknown)" in out, out)
    reset_calls()
    j = chain1_job("cid-row")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check", env={"FAKE_SUBMIT_MODE": "noid-row"})
    STATE.write_text(json.dumps({"jobs": []}))
    chk("F9 no job_id line: the queue's own row gives the id", rc == 0 and "queued continuation c0ffee999999" in out
        and (jread(j.fdir / "FINISHED.json").get("continuation") or {}).get("job") == "c0ffee999999", out)
    reset_calls()
    j = chain1_job("cid-ro")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    os.chmod(j.spec_dir.parent, 0o555)
    try:
        rc, out = fin(j.id, "--check")
    finally:
        os.chmod(j.spec_dir.parent, 0o755)
    chk("F9 an uncreatable continuation dir: exit 2, nothing resolved, the claim kept", rc == 2
        and "cannot create the continuation dir" in out and not (j.fdir / "FINISHED.json").exists() and calls("queue") == []
        and not cleared(j.id) and (j.fdir / "FINISH.json").is_file(), out)

if want("F9 finished retry"):
    reset_calls()
    j = single_job("fin-nospec")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check")
    chk("F9 setup: finished", rc == 0 and (j.fdir / "FINISHED.json").is_file(), out)
    j.spec.unlink()
    rc, out = fin(j.id, "--check")
    rc2, out2 = fin(j.id)
    chk("F9 a FINISHED job with its spec gone: --check still retries resolve + clear, a claim still refuses", rc == 0
        and "retrying resolve + clear" in out and rc2 == 1 and "already finished" in out2, out + out2)
    (j.fdir / "FINISHED.json").write_text("{not json")
    rc, out = fin(j.id, "--check")
    chk("F9 ...and with FINISHED.json unreadable", rc == 0 and "retrying resolve + clear" in out, out)
    ldir = T / "logs-fin"
    k = single_job("fin-list", logs=ldir)
    fin(k.id, env={"MINI_FINISH_LOGS": str(ldir)})
    (k.work / "calc.js").write_text(CALC_FIXED)
    fin(k.id, "--check", env={"MINI_FINISH_LOGS": str(ldir)})
    k.spec.unlink()
    rc, out = fin("--list", env={"MINI_FINISH_LOGS": str(ldir)})
    chk("F9 --list counts a FINISHED job with its spec gone as not finishable", rc == 0 and "0 finishable failed job(s)" in out
        and "(1 failed mini job(s) not finishable)" in out, out)

if want("F9 finished lock"):
    import fcntl
    reset_calls()
    j = single_job("fin-lock")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check")
    chk("F9 setup: finished (lock)", rc == 0 and (j.fdir / "FINISHED.json").is_file(), out)
    reset_calls()
    fh = open(j.fdir / ".check.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the check that wrote FINISHED.json is still resolving
    rc, out = fin(j.id, "--check")
    chk("F9 a FINISHED retry under a running check is refused; nothing resolved", rc == 1 and "already running" in out
        and calls("queue") == [], out)
    fh.close()
    rc, out = fin(j.id, "--check")
    chk("F9 ...and retries once it ends", rc == 0 and "retrying resolve + clear" in out, out)
    race = T / "race.py"
    race.write_text(
        "import importlib.machinery, importlib.util, json, sys\n"
        "ld = importlib.machinery.SourceFileLoader('mini_finish_race', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))\n"
        "ld.exec_module(m)\n"
        "orig = m.take_lock\n"
        "def late(c):\n"
        "    c.fdir.mkdir(parents=True, exist_ok=True)\n"
        "    (c.fdir / 'FINISHED.json').write_text(json.dumps({'ts': 'race', 'verdict': 'PASS'}))  # a check ended first\n"
        "    return orig(c)\n"
        "m.take_lock = late\n"
        "sys.exit(m.main(sys.argv[2:]))\n")

    def fin_race(*args):
        q = subprocess.run([sys.executable, str(race), str(FINISH), *args], env=ENV, capture_output=True, text=True, timeout=300)
        return q.returncode, q.stdout + q.stderr
    reset_calls()
    j = single_job("race-claim")
    rc, out = fin_race(j.id)
    chk("F9 a job finished between the first look and the lock: a claim refuses and writes no claim", rc == 1
        and "already finished" in out and not (j.fdir / "FINISH.json").exists(), out)
    j = single_job("race-check")
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    reset_calls()
    rc, out = fin_race(j.id, "--check")
    chk("F9 ...and a --check refuses: no check run, nothing resolved or cleared", rc == 1 and "already finished" in out
        and not list(j.fdir.glob("check-*")) and calls("queue") == [] and not cleared(j.id), out)

if want("F9 new dir"):
    reset_calls()
    j = mkjob("newdir", {**SINGLE, "allowed": ["calc.js", "lib/helper.js"]}, {"calc.js": CALC, "calc.test.js": GOOD_T},
              {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T}, untracked={"TASK.md": TASK_MD}, livelog=LL_SINGLE,
              review=REVIEW_SINGLE)
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    (j.work / "lib").mkdir()
    (j.work / "lib" / "helper.js").write_text("module.exports = { one: () => 1 };\n")
    rc, out = fin(j.id, "--check", "--dry-run")
    lt = last_log(j)
    chk("F9 a new file in a new directory reaches the try-fix gates", "no try-fix result" not in out and "] try-fix: " in lt
        and "check PASS" in out, out + lt[-1500:])

if want("F9 noref"):
    reset_calls()
    j = chain1_job("noref", ref=False)
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check")
    chk("F9 a chain without a ref dir: checked, but not continued or resolved (exit 2)", rc == 2 and "no validated ref dir" in out
        and not (j.fdir / "FINISHED.json").exists() and calls() == [] and not cleared(j.id)
        and not (j.spec_dir.parent / "noref-c1").exists(), out)

if want("F9 chain delete"):
    reset_calls()
    s1 = {**S1, "allowed": ["calc.js", "legacy.js"]}
    j = mkjob("chain-del", {**CHAIN, "steps": [s1, S2]}, {"calc.js": CALC, "legacy.js": "module.exports = 1;\n"},
              {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T, "TASK.md": S1["task"]},
              verdict="FAIL (step-1 phase-b RED test_green: 2 failing)", livelog=LL_CHAIN1, label="calc-chain-del",
              ref={"s1/calc.js": CALC_FIXED, "s1/legacy.js": "module.exports = 1;\n", "s2/calc.js": CALC_STEP2,
                   "s2/legacy.js": "module.exports = 1;\n"})
    fin(j.id)
    (j.work / "calc.js").write_text(CALC_FIXED)
    (j.work / "legacy.js").unlink()  # the finish removes a file the step may edit
    rc, out = fin(j.id, "--check")
    lt = last_log(j)
    man = R.ref_manifest(j.spec_dir.parent / "chain-del-c1" / "replay" / "s1")
    chk("F9 a chain finish that deletes a file in scope: replayed green, the deletion recorded", rc == 0 and "check PASS" in out
        and "replay step 1: GREEN" in lt and "legacy.js" in (man.get("deleted") or []), (out, lt[-1500:], man))

# ---- F10 auto: a mode-auto job finished against its derived two-phase spec ------------------------------------------------
if want("F10 auto"):
    reset_calls()
    AUTO_TASK = "Problem: add(1, 2) returns the wrong value; it must return 3.\n"
    LL_AUTO = ("[mini-runner] auto derive: target calc.js:add test calc.test.js :: node --test calc.test.js\n" + LL_SINGLE.replace(
        "RESULT FAIL phase-b RED test_green: 2 failing class=MODEL",
        "RESULT FAIL mode=auto phase=fix phase-b RED test_green: 2 failing target=calc.js:add class=MODEL"))
    name = "auto-a"
    derived = {**SINGLE, "baseline_from": str(T / name / "repo"), "task": str(T / name / "mini-work" / f"{name}-20261102120000" / "TASK.fix.md")}
    j = mkjob(name, {"mode": "auto", "allowed": [], "name": name}, {"calc.js": CALC},
              {"calc.js": LOCAL_SINGLE, "calc.test.js": GOOD_T, "TASK.md": TASK_MD},
              verdict="FAIL mode=auto phase=fix phase-b RED test_green: 2 failing target=calc.js:add", livelog=LL_AUTO,
              review=REVIEW_SINGLE, task_md=AUTO_TASK, run_files={"spec.auto.json": json.dumps(derived), "TASK.fix.md": TASK_MD})
    rc, out = fin(j.id)
    cl = jread(j.fdir / "FINISH.json")
    chk("F10 an auto job that failed in its fix stage is claimed as single, against spec.auto.json",
        rc == 0 and f"spec: {j.run / 'spec.auto.json'}" in out and cl.get("mode") == "single" and cl.get("frozen") == sha12(GOOD_T), out)
    chk("F10 brief: the model-written test is flagged, with the Problem and the packet", "AUTO job" in out
        and str(j.spec_dir / "TASK.md") in out and "Check the test's assertions against the Problem FIRST" in out
        and "REVIEW-AUTO.md" in out, out)
    chk("F10 brief: the Problem, the target function and the test inline (one read)", "--- Problem (" in out
        and "add(1, 2) returns the wrong value" in out and "--- calc.js: add (work dir" in out
        and "    1  function add(a, b) { return a - b; }" in out and "function other" not in out.split("--- calc.js: add")[1].split("--- test")[0]
        and "--- test calc.test.js (" in out and "frozen)" in out and "require('./calc')" in out, out)
    (j.work / "calc.js").write_text(CALC_FIXED)
    rc, out = fin(j.id, "--check")
    fd = jread(j.fdir / "FINISHED.json")
    chk("F10 check PASS: finished, resolved, and `mini-auto land` is the landing step", rc == 0 and "check PASS" in out
        and fd.get("verdict") == "PASS" and f"~/bin/mini-auto land {j.id}" in out and "copy them into the baseline" not in out
        and calls("queue") == [["resolve", j.id]], (out, fd, calls()))
    j2 = mkjob("auto-diag", {"mode": "auto", "allowed": [], "name": "auto-diag"}, {"calc.js": CALC}, {"calc.js": CALC},
               verdict="FAIL mode=auto phase=diagnose diag_check: no root cause", task_md=AUTO_TASK)
    rc, out = fin(j2.id)
    chk("F10 an auto job that failed before its fix stage is refused: fix it directly (or resubmit with hints)",
        rc == 1 and "failed before its fix stage (phase=diagnose)" in out and "fix it directly" in out
        and "--diagnose skip" in out and not j2.fdir.exists(), out)
    j3 = mkjob("auto-badspec", {"mode": "auto", "allowed": [], "name": "auto-badspec"}, {"calc.js": CALC}, {"calc.js": CALC},
               verdict="FAIL mode=auto phase=fix x", task_md=AUTO_TASK, run_files={"spec.auto.json": "{not json"})
    rc, out = fin(j3.id)
    chk("F10 an unreadable derived spec is refused", rc == 1 and "derived two-phase spec" in out, out)

SRV.shutdown()
if fails == 0:
    shutil.rmtree(T, ignore_errors=True)
print(f"\n{passed} passed, {fails} failed" + ("" if fails else "\nALL PASS") + ("" if fails == 0 else f"\nkept: {T}"))
sys.exit(1 if fails else 0)
