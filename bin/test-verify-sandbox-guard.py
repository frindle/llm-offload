#!/usr/bin/env python3
"""test-verify-sandbox-guard.py -- code under verification can never reach the REAL queue.

chat-frontend-plan s6-routes (2026-10-04): the slice's tests drove a route whose
default `enqueue` shelled out to ~/bin/ollama-queue.py enqueue; only missing
--model/--task-file stopped a real job. The guard (2026-10-05):
  * every verify/test runner exports DISPATCH_VERIFY_SANDBOX=1 into the child env;
  * ollama-queue.py main() refuses every non-read-only subcommand under it (exit 3,
    logged), and _Locked.save() refuses to write the REAL state file under it.

NEVER enqueues a real job: every main() call has cmd_enqueue/cmd_status replaced by
a recording stub, STATE_PATH/LOCK_PATH/the refusal log point into a temp dir, and the
one real-CLI subprocess passes a task file that does not exist (so even a missing
guard dies on the task-file check before any state is touched).

The REVERT test loads a copy of ollama-queue.py with the guard call deleted and shows
the stubbed enqueue IS reached under the marker -- i.e. the guard is what bites.
"""
import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
QUEUE = BIN / "ollama-queue.py"
MARK = "DISPATCH_VERIFY_SANDBOX"
GUARD_CALL = "    refuse_if_verify_sandboxed(args.cmd)\n"
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def load_queue(src_path, tmp, tag):
    spec = importlib.util.spec_from_file_location(f"oq_{tag}", src_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.STATE_PATH = Path(tmp) / "state.json"
    m.LOCK_PATH = Path(tmp) / "state.lock"
    m.VERIFY_SANDBOX_LOG = Path(tmp) / "refusals.log"
    return m


def run_main(m, argv, sandbox):
    """main() with recording stubs for enqueue/status. Returns (exit_code, calls)."""
    calls = []
    m.cmd_enqueue = lambda a: calls.append("enqueue")
    m.cmd_status = lambda a: calls.append("status")
    old_argv, old_env = sys.argv, os.environ.get(MARK)
    sys.argv = ["ollama-queue.py"] + argv
    if sandbox:
        os.environ[MARK] = "1"
    else:
        os.environ.pop(MARK, None)
    code = 0
    try:
        m.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old_argv
        if old_env is None:
            os.environ.pop(MARK, None)
        else:
            os.environ[MARK] = old_env
    return code, calls


ENQ = ["enqueue", "--model", "stub-model", "--task-file", "/nonexistent/TASK.md"]

with tempfile.TemporaryDirectory() as tmp:
    q = load_queue(QUEUE, tmp, "live")
    # A. guard bites: refused, stub never reached, refusal logged
    code, calls = run_main(q, ENQ, sandbox=True)
    check("A sandboxed enqueue exits VERIFY_SANDBOX_RC", code, 3)
    check("A stubbed enqueue NOT reached under sandbox", calls, [])
    log = (Path(tmp) / "refusals.log")
    check("A refusal is logged", log.is_file() and "cli:enqueue" in log.read_text(), True)
    # B. control: without the marker the same argv reaches the (stub) enqueue
    code, calls = run_main(q, ENQ, sandbox=False)
    check("B unsandboxed enqueue reaches cmd_enqueue (control)", calls, ["enqueue"])
    # C. read-only subcommands still work under the sandbox
    code, calls = run_main(q, ["status"], sandbox=True)
    check("C sandboxed status allowed", calls, ["status"])
    check("C '0' is NOT sandboxed", (os.environ.__setitem__(MARK, "0"), q.verify_sandboxed(),
                                     os.environ.pop(MARK))[1], False)
    # E. state-save guard (in-process importer path) -- on a temp "real" path
    st = Path(tmp) / "state.json"
    st.write_text('{"jobs": []}')
    q._DEFAULT_STATE_PATH = st
    os.environ[MARK] = "1"
    try:
        with q._Locked() as lk:
            lk.save({"jobs": [{"id": "x"}]})
        raised = False
    except RuntimeError as e:
        raised = "REFUSED state write" in str(e)
    check("E sandboxed save of the default state path raises", raised, True)
    check("E state file untouched", st.read_text(), '{"jobs": []}')
    q._DEFAULT_STATE_PATH = Path(tmp) / "elsewhere.json"     # test redirected STATE_PATH
    with q._Locked() as lk:
        lk.save({"jobs": [{"id": "y"}]})
    check("E redirected STATE_PATH still writable under sandbox", '"y"' in st.read_text(), True)
    os.environ.pop(MARK, None)

    # D. REVERT: the guard call deleted -> the sandboxed enqueue reaches the stub
    src = QUEUE.read_text()
    check("D guard call present exactly once in main()", src.count(GUARD_CALL), 1)
    rev = Path(tmp) / "oq_reverted.py"
    rev.write_text(src.replace(GUARD_CALL, ""))
    qr = load_queue(rev, tmp, "reverted")
    code, calls = run_main(qr, ENQ, sandbox=True)
    check("D REVERT: without the guard the sandboxed enqueue IS reached", calls, ["enqueue"])
    # save-guard revert
    s2 = src.replace("if verify_sandboxed() and Path(STATE_PATH) == Path(_DEFAULT_STATE_PATH):",
                     "if False:")
    check("D save guard line present", s2 != src, True)
    rev2 = Path(tmp) / "oq_reverted2.py"
    rev2.write_text(s2)
    qr2 = load_queue(rev2, tmp, "reverted2")
    qr2._DEFAULT_STATE_PATH = qr2.STATE_PATH
    os.environ[MARK] = "1"
    with qr2._Locked() as lk:
        lk.save({"jobs": [{"id": "z"}]})
    os.environ.pop(MARK, None)
    check("D REVERT: without the save guard the sandboxed write lands",
          '"z"' in st.read_text(), True)

    # F. end-to-end through pgrun (the shared verify runner) and the REAL CLI.
    # A code-under-test shell that calls the real enqueue; the task file does not
    # exist, so even a broken guard could not enqueue.
    sys.path.insert(0, str(BIN))
    import pgrun
    env = dict(os.environ, DISPATCH_VERIFY_SANDBOX_LOG=str(Path(tmp) / "e2e.log"))
    env.pop(MARK, None)
    cut = f"{sys.executable} {QUEUE} " + " ".join(ENQ)
    rc, so, se, _to = pgrun.run_group(cut, cwd=tmp, env=env, timeout=120)
    check("F pgrun child -> real CLI enqueue refused rc=3", rc, 3)
    check("F refusal names the marker", MARK in se, True)
    rc, so, se, _to = pgrun.run_group(f"echo ${MARK}", cwd=tmp, timeout=30)
    check("F pgrun sets the marker with env=None", so.strip(), "1")
    rc, so, se, _to = pgrun.run_group(f"echo ${MARK}", cwd=tmp, env={"PATH": os.environ["PATH"]},
                                      timeout=30)
    check("F pgrun sets the marker with an explicit env", so.strip(), "1")
    # control: the same CLI call NOT under the sandbox dies on an argument check before any
    # state is touched (the model vet refuses the stub model; without the vet, the task-file
    # check), proving F's rc=3 came from the guard, not from the bad argument.
    p = subprocess.run(cut, shell=True, capture_output=True, text=True, env=env, timeout=120)
    check("F control: unsandboxed call dies on an argument check, not rc 3",
          (p.returncode != 3, "REFUSING enqueue: unknown model" in p.stderr
           or "task file does not exist" in p.stderr), (True, True))

# G. worker shell env (model run_bash + verify) carries the marker
wsrc = (BIN / "ollama-worker.py").read_text()
fn = next(n for n in ast.parse(wsrc).body
          if isinstance(n, ast.FunctionDef) and n.name == "_shell_env")
ns = {"os": os, "Path": Path}
exec(compile(ast.Module([fn], []), "w", "exec"), ns)
check("G worker _shell_env sets the marker", ns["_shell_env"]().get(MARK), "1")

# H. verify-relevance's verify runner (preflight + gate mutation runs)
spec = importlib.util.spec_from_file_location("vr", BIN / "verify-relevance.py")
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)
with tempfile.TemporaryDirectory() as tmp:
    rc, out, _ = vr._run_verify(f"echo ${MARK}", Path(tmp), 30)
    check("H verify-relevance _run_verify child sees marker", out.strip(), "1")
    vr._PGRUN = None
    rc, out, _ = vr._run_verify(f"echo ${MARK}", Path(tmp), 30)
    check("H verify-relevance fallback path sees marker", out.strip(), "1")

# I. the mini pipeline (the live verify/test runners) sets the marker itself: the runner's
# test runs and agent shell, and mini-validate's runs. A spec env cannot clear it. The
# retired ollama-dispatch-* tools are gone and are not listed (never recreate them).
def _load(path, tag):
    sp = importlib.util.spec_from_loader(tag, SourceFileLoader(tag, str(path)))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


class _Stop(Exception):
    pass


ECHO = f"echo sbx=${MARK}"
_old = os.environ.pop(MARK, None)          # the runners must set it, not inherit it
try:
    with tempfile.TemporaryDirectory() as tmp:
        mr = _load(BIN / "mini-swe-runner.py", "mini_runner_sbx")
        rc, out, _ = mr.run_cmd(ECHO, tmp, {})
        check("I mini-swe-runner run_cmd child sees marker", out.strip(), "sbx=1")
        rc, out, _ = mr.run_cmd(ECHO, tmp, {"env": {MARK: "0"}})
        check("I mini-swe-runner run_cmd: a spec env cannot clear it", out.strip(), "sbx=1")
        seen = {}

        def _fake_popen(argv, **kw):      # never launches mini: record the agent env, stop
            seen.update(kw.get("env") or {})
            raise _Stop()
        mr._popen = _fake_popen
        try:
            mr.run_mini("m", "task", Path(tmp, "t.json"), tmp, {"PATH": os.environ["PATH"], MARK: "0"}, "", 60,
                        types.SimpleNamespace(rows=[], trip=None))
        except _Stop:
            pass
        check("I mini-swe-runner run_mini agent shell env carries marker", seen.get(MARK), "1")
        mv = _load(BIN / "mini-validate", "mini_validate_sbx")
        rc, out = mv.run(ECHO, tmp, env={MARK: "0"})
        check("I mini-validate run child sees marker (spec env cannot clear it)", out.strip(), "sbx=1")
finally:
    if _old is not None:
        os.environ[MARK] = _old

SITES = {
    "ollama-queue.py": 1,             # _preflight_verify
    "dispatch-self-heal.py": 1,
    "bakeoff-score.py": 1,
    "verify-quality.py": 1,
    "pgrun.py": 1,                    # run_group (behavioural check in F)
}
for f, n in SITES.items():
    check(f"I {f} VERIFY-SANDBOX sites >= {n}",
          (BIN / f).read_text().count("VERIFY-SANDBOX") >= n, True)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
