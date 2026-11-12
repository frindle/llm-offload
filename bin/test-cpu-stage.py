#!/usr/bin/env python3
"""test-cpu-stage.py -- the pipeline's CPU-lane wrapper (cpu_stage.run_stage), Phase 6, 2026-10-08.

Everything runs against stubbed seams in a temp HOME / temp CPU_LANE_DIR: the real runner, the real
queue API and the real cpu-jobs store are NEVER contacted (cpu_stage._runner_online/_submit are
replaced; the store is a temp dir).

  1  KILL SWITCH   CPU_LANE=0 and ~/.ollama-dispatch/cpu-lane.disabled each force the caller's local path:
                   the remote seam is never touched and NO lane marker is registered
  2  FALLBACK      runner unreachable / _submit raising / ineligible tree -> the caller's local_fn result,
                   the reason is logged, a local marker is registered and cleared
  3  STALE         a worktree change while the runner "ran" the stage discards the remote result and
                   re-runs locally (reason STALE SNAPSHOT); an unchanged tree keeps the remote result
  4  HAPPY PATH    exit code / stdout / stderr pass through unchanged; the snapshot hash travels in the
                   job env and label; local_fn is not called
  5  NO LANE HOLD  while a stage runs (remote row OR local marker) outstanding_by_bundle lists the bundle and
                   ollama-queue.bundle_commit_status says "waiting"; afterwards nothing is outstanding
  6  CALL SITES    every pipeline file that owns a CPU stage routes it through cpu_stage with a local fallback

Usage: test-cpu-stage.py [--bin DIR]   (DIR holds cpu_stage.py, cpu_lane.py, ollama-queue.py ...)
Exit 0 + "CPU_STAGE_TEST_OK" = all pass.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path.home() / "bin"))
A = ap.parse_args()
BIN = Path(A.bin).resolve()

TMP = Path(tempfile.mkdtemp(prefix="test-cpu-stage-"))
os.environ["HOME"] = str(TMP / "home")
os.environ["CPU_LANE_DIR"] = str(TMP / "cpu-jobs")
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("CPU_LANE", None)
(TMP / "home").mkdir()
sys.path.insert(0, str(BIN))
import cpu_lane          # noqa: E402
import cpu_stage as cs   # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   <<< " + str(detail)))


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_repo(name, lock=True):
    r = TMP / name
    r.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=r)
    sh("git", "config", "user.email", "t@t", cwd=r)
    sh("git", "config", "user.name", "t", cwd=r)
    (r / "package.json").write_text('{"name":"t","private":true}\n')
    if lock:
        (r / "package-lock.json").write_text('{"name":"t","lockfileVersion":3,"packages":{}}\n')
    (r / "verify.sh").write_text('#!/bin/bash\necho VERIFY_OK\n')
    (r / ".gitignore").write_text("node_modules\n")
    sh("git", "add", "-A", cwd=r)
    sh("git", "commit", "-qm", "base", cwd=r)
    return str(r)


calls = {"submit": 0, "marker_begin": 0, "marker_end": 0, "online": 0}
state = {"online": True, "submit": None, "env": None, "label": None, "cmd": None, "markers": []}


def fake_online():
    calls["online"] += 1
    return state["online"], "http://stub", object()


def fake_submit(cj, api, wt, cmd, timeout_s, stage, bundle_id, tools, env, cwd_rel, label, claim_timeout_s, owner=None):
    calls["submit"] += 1
    state["env"], state["label"], state["cmd"], state["tools"] = env, label, cmd, tools
    state["owner"] = owner
    if state["submit"] is None:
        raise RuntimeError("stub: no submit behaviour set")
    return state["submit"](wt)


def fake_marker(stage, bundle_id, timeout_s):
    calls["marker_begin"] += 1
    state["markers"].append((stage, bundle_id))
    return lambda code=None: calls.__setitem__("marker_end", calls["marker_end"] + 1)


_REAL_SUBMIT = cs._submit
cs._runner_online, cs._submit, cs._marker = fake_online, fake_submit, fake_marker


def res(exit_code=0, out="", err="", timed_out=False):
    return types.SimpleNamespace(exit_code=exit_code, stdout_tail=out, stderr_tail=err, timed_out=timed_out,
                                 job_id="jobX", timings={"run_s": 1.5}, ran_on="runner")


def reset():
    for k in calls:
        calls[k] = 0
    state.update(online=True, submit=None, markers=[])
    os.environ.pop("CPU_LANE", None)
    try:
        cs.DISABLE_FILE.unlink()
    except OSError:
        pass


LOCAL = lambda: (3, "LOCAL-OUT", "LOCAL-ERR", False)   # noqa: E731
wt = make_repo("wt")

# ---- 1. kill switch ----------------------------------------------------------------------------
reset()
os.environ["CPU_LANE"] = "0"
state["submit"] = lambda w: res(0, "REMOTE")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-env", "b1", LOCAL)
check("1 kill switch env CPU_LANE=0: local path, remote never touched",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and calls["submit"] == 0 and calls["online"] == 0, (r, calls))
check("1 kill switch env: NO lane marker (pre-lane behaviour exactly)", calls["marker_begin"] == 0, calls)
check("1 kill switch env: reason says so", "kill switch" in r.why, r.why)
reset()
cs.DISABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
cs.DISABLE_FILE.write_text("")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-file", "b1", LOCAL)
check("1 kill switch FILE: local path, remote never touched, no marker",
      r.ran_on == "local" and calls["submit"] == 0 and calls["online"] == 0 and calls["marker_begin"] == 0, (r, calls))
reset()
state["submit"] = lambda w: res(0, "REMOTE")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-off", "b1", LOCAL)
check("1 control: switch off -> remote used", r.ran_on == "runner" and r.stdout == "REMOTE", r)

# ---- 2. fallback -------------------------------------------------------------------------------
reset()
state["online"] = False
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-offline", "b2", LOCAL)
check("2 runner unreachable: caller's local result (exit/out/err untouched)",
      r.ran_on == "local" and (r.exit_code, r.stdout, r.stderr) == (3, "LOCAL-OUT", "LOCAL-ERR"), r)
check("2 runner unreachable: reason logged, NO marker by default (bundle keeps its lane = pre-lane behaviour)",
      "no CPU runner online" in r.why and calls["marker_begin"] == 0, (r.why, calls))
os.environ["CPU_LANE_LOCAL_MARKER"] = "1"
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-offline-opt", "b2", LOCAL)
check("2 opt-in CPU_LANE_LOCAL_MARKER=1: marker registered AND cleared around the local run",
      calls["marker_begin"] == 1 and calls["marker_end"] == 1 and state["markers"] == [("fb-offline-opt", "b2")], calls)
os.environ.pop("CPU_LANE_LOCAL_MARKER")
check("2 runner unreachable: never submitted", calls["submit"] == 0, calls)
reset()


def boom(w):
    raise RuntimeError("cpu runner unavailable: upload put http 500")


state["submit"] = boom
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-submit", "b2", LOCAL)
check("2 submit failure: falls back to local with the error as reason",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and "upload put http 500" in r.why, r)
reset()
r = cs.run_stage(make_repo("nolock", lock=False), "bash verify.sh", 60, "fb-inel", "b2", LOCAL)
check("2 ineligible tree (package.json without lockfile): local, never submitted",
      r.ran_on == "local" and "ineligible" in r.why and calls["submit"] == 0, (r.why, calls))
nonode = TMP / "nonode"
nonode.mkdir()
sh("git", "init", "-q", "-b", "main", cwd=nonode)
(nonode / "a.py").write_text("x=1\n")
sh("git", "add", "-A", cwd=nonode)
sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b", cwd=nonode)
reset()
r = cs.run_stage(str(nonode), "python3 a.py", 60, "fb-nonode", "b2", LOCAL)
check("2 non-node tree stays local (runner python/toolchain differs)", r.ran_on == "local" and calls["submit"] == 0, r.why)
w2 = make_repo("hostpath")
Path(w2, "verify.sh").write_text("#!/bin/bash\ncat /Users/someone/secret.txt\n")
reset()
r = cs.run_stage(w2, "bash verify.sh", 60, "fb-host", "b2", LOCAL)
check("2 verify naming a host-only path stays local", r.ran_on == "local" and "host-only path" in r.why, r.why)
w3 = make_repo("netneed")
Path(w3, "verify.sh").write_text("#!/bin/bash\nnpx --yes some-unlisted-tool --run\n")
reset()
r = cs.run_stage(w3, "bash verify.sh", 60, "fb-npx", "b2", LOCAL)
check("2 `npx --yes <tool not in the lockfile>` (needs network) stays local",
      r.ran_on == "local" and "needs network" in r.why, r.why)

# ---- 3. stale snapshot -------------------------------------------------------------------------
reset()


def mutate_then_return(w):
    Path(w, "late-write.txt").write_text("a writer that ignored the tree lock\n")
    return res(0, "REMOTE-STALE-OUT")


state["submit"] = mutate_then_return
r = cs.run_stage(make_repo("stale"), "bash verify.sh", 60, "stale", "b3", LOCAL)
check("3 worktree changed while the runner ran: remote result DISCARDED, local re-run",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and "STALE SNAPSHOT" in r.why, r)
check("3 stale: local re-run happened (no marker by default)", calls["marker_begin"] == 0, calls)
reset()
state["submit"] = lambda w: res(0, "REMOTE-OK")
r = cs.run_stage(make_repo("fresh"), "bash verify.sh", 60, "fresh", "b3", LOCAL)
check("3 control: unchanged tree keeps the remote result", r.ran_on == "runner" and r.stdout == "REMOTE-OK", r)

# ---- 4. happy path -----------------------------------------------------------------------------
reset()
w = make_repo("happy")
state["submit"] = lambda x: res(7, "VERIFY-OUT\nVERIFY_OK\n", "an err line\n")
seen_local = []
r = cs.run_stage(w, "bash verify.sh", 60, "happy", "b4", lambda: (seen_local.append(1), (0, "", "", False))[1])
snap = cs.snapshot(w)
check("4 remote exit code / stdout / stderr pass through unchanged",
      (r.exit_code, r.stdout, r.stderr, r.timed_out, r.ran_on) == (7, "VERIFY-OUT\nVERIFY_OK\n", "an err line\n", False, "runner"), r)
check("4 local_fn is NOT called and no marker is registered by us on the remote path",
      not seen_local and calls["marker_begin"] == 0, (seen_local, calls))
check("4 HEAD+diff hash travels in the job env and label",
      state["env"].get("DISPATCH_SNAPSHOT") == snap and snap in (state["label"] or ""), (state["env"], state["label"], snap))
check("4 job env carries the caller's env + the sandbox marker is passed by callers",
      state["env"].get("DISPATCH_VERIFY_SANDBOX") == "1", state["env"])
check("4 shipped tools include the relevance tools under bin/ and the ollama-queue basename stub",
      all(k in state["tools"] for k in ("bin/verify-relevance.py", "bin/pgrun.py", "bin/cpu_vr_measure.py")),
      sorted(state["tools"]))
check("4 the remote command links $HOME/bin to the shipped tools", "$JOB_TOOLS/bin" in state["cmd"], state["cmd"])
Path(w, "new.txt").write_text("x\n")
check("4 snapshot changes with an untracked file", cs.snapshot(w) != snap)
sh("git", "checkout", "--", ".", cwd=w) if False else None
reset()
state["submit"] = lambda x: res(None, "", "", True)
r = cs.run_stage(make_repo("tmo"), "bash verify.sh", 5, "timeout", "b4", LOCAL)
check("4 remote timeout is reported as timed_out (callers map it to their own timeout handling)",
      r.ran_on == "runner" and r.timed_out, r)
reset()
big = cs.GZB64_SUFFIX and None
import base64, gzip   # noqa: E402,E401
payload = json.dumps({"verdict": "relevant", "x": "y" * 500000})
b64 = base64.b64encode(gzip.compress(payload.encode())).decode()
state["submit"] = lambda x: res(0, b64)
r = cs.run_stage(make_repo("gz"), "echo", 5, "gz", "b4", LOCAL, gzb64=True,
                 check=lambda rr: None if json.loads(rr.stdout)["verdict"] == "relevant" else "bad")
check("4 gzip+base64 transport decodes a large JSON record intact", r.ran_on == "runner" and json.loads(r.stdout)["x"] == "y" * 500000, r.why)
state["submit"] = lambda x: res(0, "not-base64-!!")
r = cs.run_stage(make_repo("gz2"), "echo", 5, "gz2", "b4", LOCAL, gzb64=True)
check("4 undecodable remote record -> local fallback", r.ran_on == "local" and "undecodable" in r.why, r)
reset()
state["submit"] = lambda x: res(0, "{}")
r = cs.run_stage(make_repo("veto"), "echo", 5, "veto", "b4", LOCAL, check=lambda rr: "no verdict")
check("4 check() veto -> local fallback", r.ran_on == "local" and "vetoed" in r.why, r)

# ---- 5. no lane hold ---------------------------------------------------------------------------
reset()
os.environ["CPU_LANE_DIR"] = str(TMP / "cpu-jobs")
store = cpu_lane.Store(cpu_lane.BASE)
loader = importlib.machinery.SourceFileLoader("oq_cs", str(BIN / "ollama-queue.py"))
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_cs", loader))
sys.modules["oq_cs"] = q
loader.exec_module(q)
pk = lambda j: j.get("bundle")   # noqa: E731
BK = "bundle-nolanehold"
saw = {}
# real markers via the REAL cpu_dispatch marker (temp store)
del cs._marker
cs._marker = __import__("cpu_dispatch")._marker


def local_watch():
    out = cpu_lane.Store(cpu_lane.BASE).outstanding_by_bundle()
    saw["local"] = (BK in out, q.bundle_commit_status(BK, [], pk, {}, {}, cpu_wait=out.get(BK))[0])
    return 0, "ok", "", False


state["online"] = False
os.environ["CPU_LANE_LOCAL_MARKER"] = "1"
r = cs.run_stage(wt, "bash verify.sh", 60, "nolane-local", BK, local_watch)
os.environ.pop("CPU_LANE_LOCAL_MARKER")
check("5 local-fallback stage in flight: bundle listed outstanding AND commit status 'waiting' (opt-in: holds no GPU lane)",
      saw.get("local") == (True, "waiting"), saw)
check("5 ...and nothing is outstanding after it ends", BK not in store.outstanding_by_bundle(), store.outstanding_by_bundle())


def remote_watch(w):
    jid = store.create({"cmd": "x", "timeout_s": 60}, label="l", stage="nolane-remote", bundle_id=BK)
    out = cpu_lane.Store(cpu_lane.BASE).outstanding_by_bundle()
    saw["remote"] = (BK in out, q.bundle_commit_status(BK, [], pk, {}, {}, cpu_wait=out.get(BK))[0])
    if jid:
        store.cancel(jid)
    return res(0, "R")


state["online"] = True
state["submit"] = remote_watch
r = cs.run_stage(wt, "bash verify.sh", 60, "nolane-remote", BK, LOCAL)
check("5 remote stage in flight: bundle outstanding AND commit status 'waiting'",
      saw.get("remote") == (True, "waiting"), saw)
check("5 control: a RUNNING row of the bundle keeps it 'working' (the lane hold is unchanged for real work)",
      q.bundle_commit_status(BK, [{"bundle": BK, "status": "running"}], pk, {}, {}, cpu_wait=["j"])[0] == "working")

# ---- 7. relevance-stage eligibility + runner parity (2026-10-09, rt-card-bonus) -----------------------
import shutil, tarfile, io      # noqa: E402
reset()
wr = make_repo("relrepo")
# the harness manifest is git-excluded, exactly as the real dispatch worktrees do (info/exclude)
Path(wr, ".git", "info", "exclude").write_text(".dispatch-harness.json\n")
Path(wr, ".dispatch-harness.json").write_text('{"creation_task": true, "target": "lib/new.ts"}\n')
Path(wr, "lib").mkdir()
Path(wr, "lib", "new.ts").write_text("export const x = 1;\n")      # untracked creation target


def fake_runner(w):
    """Emulate the runner: clone HEAD, apply the shipped patch, unpack tools, run the cmd verbatim."""
    import cpu_job
    d = Path(tempfile.mkdtemp(prefix="fakerunner-", dir=str(TMP)))
    sh("git", "clone", "-q", w, str(d / "checkout"))
    _p, patch, _k = cpu_job.make_payload(w)
    if patch:
        (d / "patch").write_bytes(patch)
        sh("git", "apply", "--binary", str(d / "patch"), cwd=str(d / "checkout"))
    (d / "tools").mkdir()
    with tarfile.open(fileobj=io.BytesIO(cpu_job.make_tools_tgz(state["tools"])), mode="r:gz") as tf:
        tf.extractall(d / "tools")
    (d / "home").mkdir()
    env = dict(os.environ, HOME=str(d / "home"), JOB_TOOLS=str(d / "tools"))
    p = subprocess.run(["bash", "-c", state["cmd"]], cwd=str(d / "checkout"), env=env,
                       capture_output=True, text=True)
    return res(p.returncode, p.stdout, p.stderr)


state["submit"] = fake_runner
import tempfile      # noqa: E402
r = cs.run_stage(wr, 'test -f .dispatch-harness.json && echo HARNESS_PRESENT', 60, "rel-harness", "b7", LOCAL)
check("7 the git-excluded .dispatch-harness.json is shipped and restored on the runner "
      "(verify-relevance --applied read it: missing => rc=3 'no tracked diff' for every creation task)",
      r.ran_on == "runner" and "HARNESS_PRESENT" in r.stdout, (r, r.stdout))
reset()
state["submit"] = fake_runner
r = cs.run_stage(wr, 'python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$HOME/bin/vr-args.json"',
                 60, "rel-args", "b7", LOCAL, tools={"bin/vr-args.json": b'{"a": 1}'})
check("7 a cmd naming its OWN shipped $HOME/bin/vr-args.json is eligible and runs on the runner "
      "(was 'host-only path' -> a 17 min local mutation run)",
      r.ran_on == "runner" and r.stdout.strip() == "1", (r.why, r.stdout, r.stderr))
reset()
state["submit"] = fake_runner
r = cs.run_stage(wr, 'cat "$HOME/bin/not-shipped.json"', 60, "rel-args2", "b7", LOCAL,
                 tools={"bin/vr-args.json": b'{}'})
check("7 ...but a $HOME/bin path that is NOT shipped is still host-only (stays local)",
      r.ran_on == "local" and "host-only path" in r.why, r.why)
# a veto after real runner work names the wasted wall in the reason (no silent doubling)
reset()
state["submit"] = lambda w: res(3, "", "x")
r = cs.run_stage(wr, "true", 60, "rel-veto", "b7", LOCAL, check=lambda rr: "bad record")
check("7 a vetoed remote result is logged as remote work discarded + local re-run",
      r.ran_on == "local" and "vetoed after" in r.why and "bad record" in r.why, r.why)
# gate relevance: verify-relevance's clean abstain (rc=3 'nothing to mutate') is a verdict, not a veto
import importlib.util   # noqa: E402
_sp = importlib.util.spec_from_file_location("gate_on_complete_t", str(BIN / "gate-on-complete.py"))
_g = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(_g)
_cap = {}
_real_run_stage = cs.run_stage
cs.run_stage = lambda *a, **k: (_cap.update(k), types.SimpleNamespace(exit_code=0, stdout="", stderr="", timed_out=False))[1]
_g._relevance_run(["x"], str(wr), "bash verify.sh", None, "")
cs.run_stage = _real_run_stage
_chk = _cap["check"]
R = lambda rc, so, se: types.SimpleNamespace(exit_code=rc, stdout=so, stderr=se, timed_out=False)   # noqa: E731
check("7 gate check: rc=3 'nothing to mutate' (clean abstain) is accepted, not re-run locally",
      _chk(R(3, "", "the reference impl produced no tracked diff; nothing to mutate\n")) is None)
check("7 gate check: empty stdout with another rc, and non-JSON, are still vetoed",
      bool(_chk(R(1, "", "boom"))) and bool(_chk(R(0, "garbage", ""))) and _chk(R(0, '{"verdict": "x"}', "")) is None)

# ---- 8. capability-driven eligibility (2026-10-09): the runner reports node/python/modules/sqlite3 ---------
import sqlite3 as _sq3      # noqa: E402
import cpu_dispatch as _cd  # noqa: E402
os.environ["CPU_LANE_HOST_PYTHON"] = "3.14"
CAPS = {"node": 26, "python": "3.14", "py_modules": ["flask", "requests", "paramiko"], "sqlite3": True}

wm = make_repo("mockmod")
Path(wm, "verify.test.ts").write_text('import { mock } from "node:test";\nmock.module("x", { exports: {} });\n')
sh("git", "add", "-A", cwd=wm)
ok0, why0 = cs.eligibility(wm, "bash verify.sh")
check("8 mock.module + legacy/no caps: refused, reason names runner node 22 (as before)",
      (not ok0) and "runner node 22 API differs from host node" in why0, why0)
ok1, why1 = cs.eligibility(wm, "bash verify.sh", caps=dict(CAPS, node=22))
check("8 mock.module + runner reporting node 22: still refused with the REAL version",
      (not ok1) and "runner node 22 API" in why1, why1)
ok2, why2 = cs.eligibility(wm, "bash verify.sh", caps=CAPS)
check("8 mock.module + runner reporting node 26: eligible", ok2, why2)


def py_repo(name, body, verify=None):
    d = TMP / name
    d.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=d)
    (d / "target-mod.py").write_text(body)
    (d / "test_fixture.py").write_text("import importlib.util\nimportlib.util.spec_from_file_location('t', 'target-mod.py')\n")
    (d / "verify.sh").write_text(verify or '#!/bin/bash\npython3 ./test_fixture.py && echo VERIFY_OK\n')
    sh("git", "add", "-A", cwd=d)
    sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b", cwd=d)
    return str(d)


wp = py_repo("pyok", "import os, json\nfrom flask import Flask\nimport requests\ntry:\n    import paramiko\nexcept ImportError:\n    pass\n")
check("8 python bundle + legacy/no caps: stays local, reason names the missing runner caps",
      cs.eligibility(wp, "bash verify.sh") == (False, cs._NONODE_NO_CAPS), cs.eligibility(wp, "bash verify.sh"))
check("8 short_reason keeps the cause (new and legacy texts)",
      cs.short_reason("ineligible: " + cs._NONODE_NO_CAPS) == "not a node project: the runner reports no python caps (offline or pre-1.3 image)"
      and cs.short_reason(cs._LEGACY_NONODE) == "not a node project: runner python/toolchain differs from the host"
      and cs.short_reason(cs._NONODE_NO_PY).endswith("runner parity cannot be checked"),
      [cs.short_reason(x) for x in (cs._NONODE_NO_CAPS, cs._LEGACY_NONODE, cs._NONODE_NO_PY)])
check("8 python bundle + runner python 3.14 carrying flask/requests/paramiko: eligible",
      cs.eligibility(wp, "bash verify.sh", caps=CAPS) == (True, ""), cs.eligibility(wp, "bash verify.sh", caps=CAPS))
okm, whym = cs.eligibility(wp, "bash verify.sh", caps=dict(CAPS, python="3.11"))
check("8 python minor mismatch: refused, both versions in the reason",
      (not okm) and "3.11" in whym and "3.14" in whym, whym)
okx, whyx = cs.eligibility(wp, "bash verify.sh", caps=dict(CAPS, py_modules=["flask"]))
check("8 module the runner lacks: refused, names the missing modules",
      (not okx) and "paramiko" in whyx and "requests" in whyx and "flask" not in whyx, whyx)
check("8 stdlib-only python bundle is eligible", cs.eligibility(py_repo("pystd", "import os, re, sqlite3\n"), "bash verify.sh", caps=CAPS)[0])
okn, whyn = cs.eligibility(py_repo("pyfn", "import os\ndef f():\n    import numpy\n"), "bash verify.sh", caps=CAPS)
check("8 a function-local third-party import is seen (numpy): refused", (not okn) and "numpy" in whyn, whyn)
boot = '#!/bin/bash\n.venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1 || rm -rf .venv\npython3 ./test_fixture.py\n'
check("8 the scaffold's venv pip bootstrap does not make a python bundle 'needs network'",
      cs.eligibility(py_repo("pyboot", "import os\n", boot), "bash verify.sh", caps=CAPS)[0])
okp, whyp = cs.eligibility(py_repo("pypip", "import os\n", "#!/bin/bash\npip install numpy\npython3 ./test_fixture.py\n"), "bash verify.sh", caps=CAPS)
check("8 any OTHER pip install still needs network -> refused", (not okp) and "pip" in whyp, whyp)
wq = make_repo("sqlprisma")
Path(wq, "verify.sh").write_text("#!/bin/bash\nsqlite3 --version\necho VERIFY_OK\n")
Path(wq, "prisma").mkdir()
Path(wq, "prisma", "schema.prisma").write_text("// changed\n")       # untracked change under prisma/
os.environ.pop("CPU_RUNNER_HAS_SQLITE", None)
check("8 sqlite3 in verify.sh + prisma change: refused for a runner without sqlite3, allowed when it reports it",
      (not cs.eligibility(wq, "bash verify.sh")[0]) and cs.eligibility(wq, "bash verify.sh", caps=CAPS)[0]
      and not cs.eligibility(wq, "bash verify.sh", caps=dict(CAPS, sqlite3=False))[0])
# combine_caps: any legacy runner in the fleet pulls everything down to legacy
cc = _cd.combine_caps([{"runner": "a", "caps": CAPS}, {"runner": "b"}])
check("8 combine_caps: a legacy runner in the mix => node 22, no python, no modules, no sqlite3",
      cc["node"] == 22 and cc["python"] is None and cc["py_modules"] == [] and cc["sqlite3"] is False, cc)
cc2 = _cd.combine_caps([{"caps": CAPS}, {"caps": dict(CAPS, node=24, py_modules=["flask"])}])
check("8 combine_caps: weakest node, common module set", cc2["node"] == 24 and cc2["py_modules"] == ["flask"] and cc2["python"] == "3.14", cc2)
check("8 combine_caps: no runners -> None (unknown == legacy)", _cd.combine_caps([]) is None)

# end to end through run_stage: the python bundle runs on the runner only when the runner says it can
cs._runner_caps = lambda api, cj: state.get("caps")
reset()
state["submit"] = lambda w: res(0, "REMOTE")
state["caps"] = None
r = cs.run_stage(wp, "bash verify.sh", 60, "py-legacy", "b8", LOCAL)
check("8 run_stage python bundle, legacy runner: local, never submitted, today's reason",
      r.ran_on == "local" and calls["submit"] == 0 and "not a node project" in r.why, (r.why, calls))
reset()
state["submit"] = lambda w: res(0, "REMOTE")
state["caps"] = CAPS
r = cs.run_stage(wp, "bash verify.sh", 60, "py-caps", "b8", LOCAL)
check("8 run_stage python bundle, runner reports python 3.14 + modules: runs on the runner",
      r.ran_on == "runner" and r.stdout == "REMOTE" and calls["submit"] == 1, (r.why, calls))
reset()
state["online"] = False
state["caps"] = CAPS
r = cs.run_stage(wt, "bash verify.sh", 60, "mm-offline", "b8", LOCAL)
check("8 runner unreachable: still the loud local fallback ('no CPU runner online')",
      r.ran_on == "local" and "no CPU runner online" in r.why, r.why)
reset()

# 2026-10-14 voicemail-ui-s4-apilist: the OLD scaffold bootstrap (`python3 -m venv .venv && pip install`, no
# `|| rm -rf .venv`) left an empty isolated venv offline -> fastapi missing on the runner, refimpl red. Emulate the
# runner for real: PIP_NO_INDEX=1 and python3 is ITSELF a venv (/opt/venv on the runner) carrying a module its base
# interpreter lacks -- a nested --system-site-packages venv alone reaches only the base, not the baked set.
OLDBOOT = ('#!/usr/bin/env bash\ncd "$(dirname "$0")" || exit 1\nPY=.venv/bin/python\nif [ ! -x "$PY" ]; then\n'
           '  python3 -m venv .venv >/dev/null 2>&1 && .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1\n'
           'fi\n[ -x "$PY" ] || PY=python3\n"$PY" -c "import bakedmod" && echo VERIFY_OK\n')
NEWBOOT = ('#!/usr/bin/env bash\ncd "$(dirname "$0")" || exit 1\nPY=.venv/bin/python\n'
           '.venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1 || rm -rf .venv\n'
           '[ -x "$PY" ] || PY=python3\n"$PY" -c "import bakedmod" && echo VERIFY_OK\n')
_CAPSV = dict(CAPS, py_modules=["bakedmod"])
_BAKED = TMP / "bakedvenv"
subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(_BAKED)], check=True)
_bsp = subprocess.run([str(_BAKED / "bin" / "python"), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                      capture_output=True, text=True, check=True).stdout.strip()
Path(_bsp, "bakedmod.py").write_text("X = 1\n")


def _venv_repo(name, verify, req=True):
    d = py_repo(name, "import bakedmod\n", verify)
    if req:
        Path(d, "requirements.txt").write_text("bakedmod\n")
        Path(d, ".gitignore").write_text(".venv\n")
        sh("git", "add", "-A", cwd=d)
        sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "req", cwd=d)
    return d


def _venv_run(d, tag):
    reset()
    state["submit"], state["caps"] = fake_runner, _CAPSV
    saved = {k: os.environ.get(k) for k in ("PIP_NO_INDEX", "PATH")}
    os.environ["PIP_NO_INDEX"] = "1"
    os.environ["PATH"] = str(_BAKED / "bin") + os.pathsep + os.environ.get("PATH", "")
    try:
        return cs.run_stage(d, "bash verify.sh", 120, tag, "b8v", LOCAL)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


_vo = _venv_run(_venv_repo("venvold", OLDBOOT), "venv-old")
check("8 old venv bootstrap offline: the runner's venv sees the baked modules (VERIFY_OK on the runner)",
      _vo.ran_on == "runner" and "VERIFY_OK" in (_vo.stdout or ""), (_vo.ran_on, _vo.why, _vo.stdout, _vo.stderr))
_vn = _venv_run(_venv_repo("venvnew", NEWBOOT), "venv-new")
check("8 newer `|| rm -rf .venv` bootstrap offline: still VERIFY_OK on the runner",
      _vn.ran_on == "runner" and "VERIFY_OK" in (_vn.stdout or ""), (_vn.ran_on, _vn.why, _vn.stdout, _vn.stderr))
os.environ["CPU_LANE_VENV_SITE"] = "0"
try:
    _vk = _venv_run(_venv_repo("venvks", OLDBOOT), "venv-ks")
finally:
    os.environ.pop("CPU_LANE_VENV_SITE", None)
check("8 kill switch CPU_LANE_VENV_SITE=0: the old bootstrap is red again offline (the bug, reproduced)",
      _vk.ran_on == "runner" and "VERIFY_OK" not in (_vk.stdout or ""), (_vk.ran_on, _vk.stdout, _vk.stderr))
check("8 venv prelude only for a python project with requirements.txt",
      cs.venv_prelude(_venv_repo("venvnoreq", OLDBOOT, req=False)) == "" and cs.venv_prelude(wt) == ""
      and "--system-site-packages" in cs.venv_prelude(_venv_repo("venvreq2", OLDBOOT)))
reset()

_fa = py_repo("pyfastform", "from fastapi import FastAPI, Form\napp = FastAPI()\n")
_fcaps = dict(CAPS, py_modules=["fastapi"])
_okf, _whyf = cs.eligibility(_fa, "bash verify.sh", caps=_fcaps)
check("8 fastapi Form route + runner without python-multipart: refused, names the runtime dep",
      (not _okf) and "python-multipart" in _whyf, _whyf)
check("8 ... and eligible once the runner carries python_multipart",
      cs.eligibility(_fa, "bash verify.sh", caps=dict(CAPS, py_modules=["fastapi", "python_multipart"]))[0])
check("8 fastapi WITHOUT Form/File routes needs no multipart",
      cs.eligibility(py_repo("pyfastplain", "from fastapi import FastAPI\napp = FastAPI()\n"), "bash verify.sh", caps=_fcaps)[0])

# the queue store: caps are recorded, expire, and an OLD runners table is migrated in place
_old = TMP / "old-lane"
_old.mkdir()
_c = _sq3.connect(str(_old / "jobs.sqlite"))
_c.executescript("CREATE TABLE runners(runner_id TEXT PRIMARY KEY, last_seen REAL, claims INTEGER DEFAULT 0);"
                 "INSERT INTO runners VALUES('legacy', 1.0, 3);")
_c.commit()
_c.close()
_now = [1000.0]
_st = cpu_lane.Store(_old, clock=lambda: _now[0])
_st.claim("new", 30, caps=CAPS)
_st.claim("legacy", 30)
_by = {x["runner"]: x for x in _st.summary()["runners"]}
check("8 store: old runners table migrated; caps recorded for the reporting runner, none for the legacy one",
      _by["new"].get("caps") == CAPS and "caps" not in _by["legacy"], _by)
_now[0] += cpu_lane.CAPS_FRESH_S + 5
_st.claim("new", 30)          # a later claim WITHOUT caps (e.g. a downgraded image) must not keep stale caps
check("8 store: caps expire after CAPS_FRESH_S without a re-report (a downgraded image cannot inherit them)",
      "caps" not in {x["runner"]: x for x in _st.summary()["runners"]}["new"])
_st.claim("new", 30, caps="not-a-dict")
check("8 store: a malformed caps value is ignored", "caps" not in {x["runner"]: x for x in _st.summary()["runners"]}["new"])

# ---- 6. call sites ------------------------------------------------------------------------------
site = {
    "gate-on-complete.py": ["cpu_stage", "_relevance_run(", "\"relevance\""],
}
for f, needles in site.items():
    t = (BIN / f).read_text()
    miss = [n for n in needles if n not in t]
    check(f"6 call site {f} routes its CPU stage through cpu_stage", not miss, miss)
t = (BIN / "gate-on-complete.py").read_text()
check("6 gate: measure_relevance no longer runs verify-relevance with a bare subprocess.run",
      "r = subprocess.run(cmd, capture_output=True, text=True,\n                           timeout=RELEVANCE_BUDGET_S + 900)\n        rec = json.loads" not in t)

# ---- 9  ROUTING: the Mac takes a CPU-lane stage ONLY when Unraid is saturated (bounded wait) or down/ineligible
os.environ.pop("CPU_LANE_SATURATION_WAIT_S", None)
check("9 routing: runner alive + inside the saturation window => keep waiting (busy is not a reason to go local)",
      cs.routing_decision(True, 5, 1200) == "wait" and cs.routing_decision(True, 1199, 1200) == "wait")
check("9 routing: runner alive but waited past the saturation threshold => local:saturated",
      cs.routing_decision(True, 1201, 1200) == "local:saturated")
check("9 routing: runner heartbeat stale => local:runner-down immediately, regardless of wait",
      cs.routing_decision(False, 0, 1200) == "local:runner-down")
check("9 default saturation wait is bounded and far above the old 60s claim timeout",
      600 <= cs.saturation_wait_s() <= 3600 and cs.effective_claim_timeout(60) == cs.saturation_wait_s())
os.environ["CPU_LANE_SATURATION_WAIT_S"] = "45"
check("9 CPU_LANE_SATURATION_WAIT_S overrides; the caller's larger value is a floor",
      cs.effective_claim_timeout(60) == 60 and cs.effective_claim_timeout(10) == 45)
os.environ.pop("CPU_LANE_SATURATION_WAIT_S", None)
_seen = {}
class _FakeCJ:
    @staticmethod
    def submit_cpu_job(*a, **kw):
        _seen.update(kw)
        return types.SimpleNamespace(exit_code=0)
_REAL_SUBMIT(_FakeCJ, "http://x", "/wt", "true", 10, "verify", "b", {}, {}, None, "l", 60)
check("9 _submit hands cpu_job the saturation wait (not the caller's 60s) and a runner-down probe",
      _seen.get("claim_timeout_s") == cs.saturation_wait_s() and callable(_seen.get("should_abort")), _seen)
_orig_ro = cs._runner_online
cs._runner_online = lambda: (False, "http://x", None)
check("9 the probe reports a stale runner", bool(cs._runner_down_probe()()))
cs._runner_online = lambda: (True, "http://x", None)
check("9 the probe is silent while the runner heartbeat is fresh", cs._runner_down_probe()() is None)
cs._runner_online = _orig_ro
# cpu_job: a truthy should_abort while pending cancels the job and falls back (RuntimeError with fallback=False)
_cj = cs._runner_online and __import__("cpu_job")
_calls = []
def _fake_http(api, tok, method, path, body=None, raw=None, timeout=30):
    _calls.append((method, path))
    if path == "/api/cpu/health":
        return 200, {}
    if method == "POST" and path == "/api/cpu/jobs":
        return 201, {"id": "j1"}
    if method == "GET":
        return 200, {"status": "pending"}
    return 200, {}
_oh, _omp = _cj._http, _cj.make_payload
_cj._http, _cj.make_payload = _fake_http, (lambda wt, mode: (b"p", None, "bundle"))
wab = make_repo("abortwt")
try:
    _cj.submit_cpu_job(wab, "true", 10, api="http://x", token="t", claim_timeout_s=9999, poll_s=0.01,
                       fallback=False, should_abort=lambda: "runner offline")
    _err = ""
except RuntimeError as e:
    _err = str(e)
finally:
    _cj._http, _cj.make_payload = _oh, _omp
check("9 cpu_job: a stale-runner abort cancels the pending job and falls back long before the claim timeout",
      "runner offline" in _err and ("DELETE", "/api/cpu/jobs/j1") in _calls, (_err, _calls))

# ---- 10  PRISMA: the scaffold's dead `npx --yes prisma` branches must not push a prisma-less repo to the Mac
wnp = make_repo("noprisma")
Path(wnp, "verify.sh").write_text("#!/bin/bash\nif [ -f prisma/schema.prisma ]; then npx --yes prisma generate; fi\necho VERIFY_OK\n")
sh("git", "add", "-A", cwd=wnp)
check("10 verify.sh naming `npx --yes prisma` in a repo with NO prisma schema is runner-eligible",
      cs.eligibility(wnp, "bash verify.sh", caps=CAPS)[0], cs.eligibility(wnp, "bash verify.sh", caps=CAPS))
wyp = make_repo("withprisma")
Path(wyp, "verify.sh").write_text("#!/bin/bash\nnpx --yes prisma generate\necho VERIFY_OK\n")
Path(wyp, "prisma").mkdir()
Path(wyp, "prisma", "schema.prisma").write_text("// s\n")
okw, whw = cs.eligibility(wyp, "bash verify.sh", caps=CAPS)
check("10 same verify.sh WITH a prisma schema and prisma absent from the lockfile still stays local (needs network)",
      (not okw) and "prisma" in whw, whw)

# ---- 11  SATURATION: runners report slots; the queue derives busy / saturated (Mac-only-when-saturated signal)
_sat_dir = TMP / "sat"
_sst = cpu_lane.Store(_sat_dir)
def _mk(n):
    j = _sst.create({"cmd": "true", "cwd": ".", "timeout_s": 60}, "l%d" % n, "verify", "bs")
    _sst.put_blob(j, "payload", __import__("io").BytesIO(b"x"), 1)
    _sst.ready(j)
    return j
for _i in range(2):
    _mk(_i)
_sst.claim("r1", 30, slots=2)
_r1 = lambda: {x["runner"]: x for x in _sst.summary()["runners"]}["r1"]
check("11 one of two slots busy: not saturated", (_r1().get("slots"), _r1().get("busy"), _r1().get("saturated")) == (2, 1, False), _r1())
_sst.claim("r1", 30, slots=2)
check("11 both slots busy: saturated (Mac may take work); same runner id holds several claims", (_r1().get("busy"), _r1().get("saturated")) == (2, True), _r1())
_sst.claim("old", 30)
check("11 a runner that never reports slots has no saturation fields (legacy)",
      "saturated" not in {x["runner"]: x for x in _sst.summary()["runners"]}["old"])

# ---- 12  OWNER provenance: a CPU row must say WHO submitted it (bare 12-hex worktree hashes were unattributable)
_ab = "/Users/x/.ollama-dispatch/ab-sampling/v2/wt/d698b2cdbee3"
check("12 owner_label: ab-sampling replay worktree", "ab-sampling v2 d698b2cdbee3" in cs.owner_label(_ab, "relevance"), cs.owner_label(_ab))
check("12 owner_label: slice worktree names the slice", cs.owner_label("/w/wt-slice-rt-amazon-s1-extract", "x") == "slice rt-amazon-s1-extract", cs.owner_label("/w/wt-slice-rt-amazon-s1-extract"))
os.environ["CPU_LANE_OWNER"] = "my-tool: thing"
check("12 owner_label: env CPU_LANE_OWNER wins", cs.owner_label(_ab) == "my-tool: thing")
os.environ.pop("CPU_LANE_OWNER")
check("12 owner_label never raises on junk", isinstance(cs.owner_label(None), str))
reset()
state["submit"] = lambda w: res(0, "REMOTE")
r = cs.run_stage(wt, "bash verify.sh", 60, "own-stage", "b12", LOCAL)
check("12 run_stage hands the owner to the runner submit", bool(state.get("owner")) and Path(str(wt)).name in state["owner"], state.get("owner"))
_ost = cpu_lane.Store(TMP / "own")
_oj = _ost.create({"cmd": "true", "cwd": ".", "timeout_s": 60, "owner": "preflight: ab-sampling v2 d698b2cdbee3"}, "relevance@abc", "relevance", "d698b2cdbee3")
_ost.put_blob(_oj, "payload", __import__("io").BytesIO(b"x"), 1)
_ost.ready(_oj)
check("12 cpu-lane summary exposes owner on pending rows", _ost.summary()["pending"][0].get("owner") == "preflight: ab-sampling v2 d698b2cdbee3", _ost.summary()["pending"])
_oj2 = _ost.create({"cmd": "true"}, "old", "verify", "bx")
_ost.put_blob(_oj2, "payload", __import__("io").BytesIO(b"x"), 1)
_ost.ready(_oj2)
check("12 spec without owner (old producer) -> owner '' (fail-open)", [x["owner"] for x in _ost.summary()["pending"] if x["label"] == "old"] == [""])
os.environ["CPU_LANE_LOCAL_RUN_DIR"] = str(TMP / "lrd"); os.environ.pop("CPU_LANE_LOG_OFF", None)
_end = cs.local_run_begin("audit-trivial", "d698b2cdbee3", "why", 60, owner="OWN-X")
check("12 local_running records carry the owner", [x.get("owner") for x in cs.local_running()] == ["OWN-X"], cs.local_running())
_end()

# ---- 13 BUNDLE KEY: the key a stage hands the CPU lane is the queue row's REAL bundle tag --------------
for _v in ("DISPATCH_BUNDLE", "OLLAMA_DISPATCH_VIA_QUEUE"):
    os.environ.pop(_v, None)
_qs = TMP / "q13-state.json"; _ql = TMP / "q13-logs"; _ql.mkdir(exist_ok=True)
_qs.write_text(json.dumps({"jobs": [
    {"id": "live1", "label": "psab-A2-76d747232905", "bundle": "prose-sticky-ab-20261013"},
    {"id": "solo1", "label": "solo-fix-s1-x", "bundle": None}]}))
(_ql / "gone1.done.json").write_text(json.dumps({"id": "gone1", "label": "psab-B1-76d747232905", "bundle": "prose-sticky-ab-20261013"}))
cs.QUEUE_STATE, cs.QUEUE_LOGS = _qs, _ql
check("13 bundle_key: live queue row's bundle wins over the label", cs.bundle_key(job_id="live1", wt="/w/wt-psab-A2-76d747232905-f0194bda") == "prose-sticky-ab-20261013", cs.bundle_key(job_id="live1"))
check("13 bundle_key: pruned row -> done.json sidecar bundle", cs.bundle_key(job_id="gone1") == "prose-sticky-ab-20261013", cs.bundle_key(job_id="gone1"))
os.environ["OLLAMA_DISPATCH_VIA_QUEUE"] = "live1"
check("13 bundle_key: $OLLAMA_DISPATCH_VIA_QUEUE resolves the worker's own row", cs.bundle_key(wt="/w/wt-x") == "prose-sticky-ab-20261013")
os.environ.pop("OLLAMA_DISPATCH_VIA_QUEUE")
check("13 bundle_key: exact live label -> its bundle", cs.bundle_key(label="psab-A2-76d747232905") == "prose-sticky-ab-20261013")
os.environ["DISPATCH_BUNDLE"] = "env-tag"
check("13 bundle_key: $DISPATCH_BUNDLE beats the row lookup", cs.bundle_key(job_id="live1") == "env-tag")
check("13 bundle_key: explicit beats $DISPATCH_BUNDLE", cs.bundle_key(job_id="live1", explicit="exp") == "exp")
os.environ.pop("DISPATCH_BUNDLE")
_nb = cs.bundle_key(job_id="solo1", wt="/w/wt-solo-fix-s1-x")
check("13 bundle_key: bundle-less job still falls back to a sane label-derived key", bool(_nb) and "solo-fix" in _nb, _nb)
check("13 bundle_key: no info at all -> None, never raises", cs.bundle_key() is None)
cs.QUEUE_STATE = TMP / "nope.json"
check("13 bundle_key: torn/missing queue state is fail-open", cs.bundle_key(job_id="live1", wt="/w/wt-abc-1") not in (None, "prose-sticky-ab-20261013"))
cs.QUEUE_STATE = _qs
# the gate's relevance stage must submit the job's bundle (the real symptom: a fake per-job dashboard group)
_gsrc = (BIN / "gate-on-complete.py").read_text()
import re as _re13
check("13 gate main() stamps payload job_id BEFORE measure_relevance (so the lane key is the queue bundle)",
      _re13.search(r'payload\.setdefault\("job_id", a\.job_id\)\s*\n\s*measure_relevance\(payload, cwd,', _gsrc) is not None)
_gl = importlib.machinery.SourceFileLoader("gate_on_complete_t13", str(BIN / "gate-on-complete.py"))
_gm = types.ModuleType("gate_on_complete_t13"); _gm.__file__ = str(BIN / "gate-on-complete.py")
try:
    _gl.exec_module(_gm)
    _cap = {}
    _orig_rs = cs.run_stage
    cs.run_stage = lambda wt, cmd, t, stage, bundle_id, local_fn, **k: (_cap.update(b=bundle_id, stage=stage) or cs.StageResult(0, "{}", "", False, "local"))
    try:
        _gm._relevance_run(["true"], str(wt), "bash verify.sh", None, "", "prose-sticky-ab-20261013")
        check("13 gate _relevance_run submits the explicit queue bundle as the lane key", _cap.get("b") == "prose-sticky-ab-20261013" and _cap.get("stage") == "relevance", _cap)
    finally:
        cs.run_stage = _orig_rs
except SystemExit:
    pass
# the authoring/slicer entrypoints export the plan's bundle so every stage + child inherits it

# ---- 14  SUB-PACKAGE PARITY BLOCK: the scaffold's `if [ -d '/Users/..sidecar/node_modules' ]` guard (WARN else) is
# host-conditional, not host-only -- unless the tests load a dep only the sub-package declares (2026-10-14).
SUBBLK = ("#!/bin/bash\n# --- sub-package node_modules (env parity) ---\n# c\nif [ ! -e sidecar/node_modules ]; then\n"
          "  if [ -d '/Users/someone/Desktop/GitHub Projects/r/sidecar/node_modules' ]; then\n"
          "    ln -s '/Users/someone/Desktop/GitHub Projects/r/sidecar/node_modules' sidecar/node_modules\n  else\n"
          "    echo \"  WARN: sub-package deps '/Users/someone/Desktop/GitHub Projects/r/sidecar/node_modules' not installed\"\n"
          "  fi\nelse\n  echo ok\nfi\n\necho VERIFY_OK\n")
def subpkg_repo(name, src, verify=SUBBLK):
    w = make_repo(name)
    Path(w, "sidecar", "src").mkdir(parents=True)
    Path(w, "sidecar", "package.json").write_text('{"name":"s","dependencies":{"playwright-core":"^1"}}\n')
    Path(w, "sidecar", "src", "t.js").write_text(src)
    Path(w, "verify.test.ts").write_text("import { createRequire } from 'node:module';\nconst require = createRequire(import.meta.url);\nrequire('./sidecar/src/t.js');\n")
    Path(w, "verify.sh").write_text(verify)
    sh("git", "add", "-A", cwd=w)
    return w
_w = subpkg_repo("sp_ok2", "const fs = require('node:fs');\n")
check("14 (no dep) eligible", cs.eligibility(_w, "bash verify.sh", caps=CAPS) == (True, ""), cs.eligibility(_w, "bash verify.sh", caps=CAPS))
_w = subpkg_repo("sp_need", "const { chromium } = require('playwright-core');\n")
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS)
check("14 test loading a sub-package-only dep stays local (host-only path)", (not _o) and "host-only" in _y, _y)
CAPS_SP = dict(CAPS, subpkg_install=True)
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS_SP)
check("14 sub-dep needed, NO sub-lock, runner has subpkg_install -> stays local", (not _o) and "host-only" in _y, _y)
Path(_w, "sidecar", "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}\n')
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS_SP)
check("14 sub-dep needed + sidecar package-lock.json + subpkg_install cap -> runner-eligible", (_o, _y) == (True, ""), _y)
check("14 subpkg_lock_dirs names the sidecar", cs.subpkg_lock_dirs(_w, Path(_w, "verify.sh").read_text()) == ["sidecar"])
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS)
check("14 sub-lock but runner WITHOUT subpkg_install cap (legacy image) -> stays local", (not _o) and "host-only" in _y, _y)
os.environ["CPU_LANE_SUBPKG_GUARD"] = "0"
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS_SP)
_d = cs.subpkg_lock_dirs(_w, Path(_w, "verify.sh").read_text())
del os.environ["CPU_LANE_SUBPKG_GUARD"]
check("14 kill switch beats the sub-lock path", (not _o) and "host-only" in _y and _d == [], (_y, _d))
_w = subpkg_repo("sp_kill", "const fs = require('node:fs');\n")
os.environ["CPU_LANE_SUBPKG_GUARD"] = "0"
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS)
del os.environ["CPU_LANE_SUBPKG_GUARD"]
check("14 kill switch CPU_LANE_SUBPKG_GUARD=0 restores the old host-only verdict", (not _o) and "host-only" in _y, _y)
_w = subpkg_repo("sp_other", "const fs = require('node:fs');\n", SUBBLK + "cat /Users/someone/secret.txt\n")
_o, _y = cs.eligibility(_w, "bash verify.sh", caps=CAPS)
check("14 a host path OUTSIDE the guard block still forces local", (not _o) and "host-only" in _y, _y)
print()
print("CPU_STAGE_TEST_OK" if all(RESULTS) else "CPU_STAGE_TEST_FAILED (%d of %d failed)" % (RESULTS.count(False), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
