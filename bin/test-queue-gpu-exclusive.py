#!/usr/bin/env python3
"""test-queue-gpu-exclusive.py -- behavioural tests for `ollama-queue.py enqueue-gpu`.

WHAT IS UNDER TEST (2026-10-05). A non-LLM shell job that needs one lane's GPU to
itself (first use: a Strata trial on the Unraid RTX 3080, which the Unraid Ollama
pre-gate also uses). The rules:
  * enqueue-gpu refuses a request with no bundle, an auto/studio/Darkbloom/Bonsai/URL
    host, an unknown host, or a timeout outside 60s..6h. The spec file is 0600. A
    duplicate live label is refused. The row is never fit-routed.
  * routing: the named host's URL, with no fit/size lookup.
  * launch: only when the lane has no running job and no unresolved gate/regate is
    pinned to it. A Studio gate or a Studio job does not block it.
  * the bundle commitment never picks a gpu-exclusive row (running or pending), and
    a gpu-exclusive launch never consumes the commitment's backfill slot.
  * orphan recovery settles it done/failed from the runner's result sidecar and never
    requeues it.
  * the runner (gpu-exclusive-runner.py --self-test): evict-first, fail closed,
    timeout/stop cleanup, exit codes never 3/5.

Everything runs on temp state with DISPATCH_VERIFY_SANDBOX=1. No real enqueue, no
real Ollama, no GPU.

Usage: test-queue-gpu-exclusive.py [--queue PATH] [--runner PATH]
The revert test passes the pre-change backup as --queue and must FAIL.
"""
import argparse
import importlib.machinery
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"

BIN = Path.home() / "bin"
RESULTS = []
UNRAID_URL = "http://203.0.113.193:11434"
HOSTS = {"studio": {"url": "http://127.0.0.1:11434", "usable_bytes": 40 * 1024 ** 3},
         "unraid": {"url": UNRAID_URL, "usable_bytes": 9 * 1024 ** 3}}


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def guarded(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        RESULTS.append(False)
        print(f"FAIL {name}: raised {type(e).__name__}: {e}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(BIN / "ollama-queue.py"))
    ap.add_argument("--runner", default=str(BIN / "gpu-exclusive-runner.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="q-gpujob-"))
    q = load(a.queue, "oq_gpujob")
    q.STATE_PATH, q.LOCK_PATH = tmp / "state.json", tmp / "state.lock"
    q.LIVE_LOG_DIR = tmp / "livelogs"
    if hasattr(q, "GPU_JOB_DIR"):
        q.GPU_JOB_DIR = tmp / "gpu-jobs"
    fake_w = types.SimpleNamespace(KNOWN_OLLAMA_HOSTS=HOSTS, BIG_HOST_NAME="studio",
                                   SMALL_HOST_NAME="unraid")
    q.worker = lambda: fake_w
    # Darkbloom/Bonsai unconfigured so _lane_name resolves through the host table only.
    q._darkbloom_url = lambda: None
    q._bonsai_url = lambda: None

    def boom(*_a, **_k):
        raise AssertionError("fit/size lookup must not run for a gpu-exclusive job")

    def gpu_row(jid="g1", label="gpu-strata-trial", status="pending"):
        job, _spec = q.build_gpu_job(jid, label, "strata-trial", "unraid",
                                     "ssh box 'run'", 7300, hosts=HOSTS, job_dir=tmp / "gpu-jobs")
        job["status"] = status
        return job

    # --- 1. enqueue validation ---------------------------------------------------------
    def s1():
        def refused(**kw):
            base = dict(job_id="x", label="l", bundle="b", host="unraid", cmd="true",
                        timeout_s=600, hosts=HOSTS, job_dir=tmp)
            base.update(kw)
            try:
                q.build_gpu_job(**base)
                return False
            except ValueError:
                return True
        check("1 refuse: no bundle", refused(bundle=""), True)
        check("1 refuse: host auto", refused(host="auto"), True)
        check("1 refuse: host studio (Darkbloom lane: not ours to evict)", refused(host="studio"), True)
        check("1 refuse: explicit URL", refused(host=UNRAID_URL), True)
        check("1 refuse: unknown host", refused(host="nosuch"), True)
        check("1 refuse: timeout too short / too long",
              (refused(timeout_s=5), refused(timeout_s=99999)), (True, True))
        check("1 refuse: empty cmd", refused(cmd="  "), True)
        check("1 accept: unraid, bundled", refused(), False)
        j = gpu_row()
        check("1 row: placeholder model, allowlisted runner, kind, bundle, fixed lane",
              (j["model"], j["runner"] in q.ALLOWED_RUNNERS, j["job_kind"], j["bundle"],
               j["host_pref"], j["fit_checked"]),
              (q.GPU_JOB_MODEL, True, q.GPU_JOB_KIND, "strata-trial", "unraid", True))
        check("1 row: is recognised as gpu-exclusive", q._is_gpu_exclusive_job(j), True)
        check("1 a plain runner row is NOT gpu-exclusive",
              q._is_gpu_exclusive_job({"runner": q.GPU_JOB_RUNNER}), False)
        check("1 row: no gate (ungateable runner job)", q._is_ungateable_job(j), True)
        check("1 row: never a 'long job' a Studio gate would preempt", q._is_long_job(j), False)
    guarded("1 enqueue validation", s1)

    # --- 2. enqueue end to end on temp state -------------------------------------------
    def s2():
        args = argparse.Namespace(label="gpu-strata-trial", bundle="strata-trial", host="unraid",
                                  cmd="ssh box 'secret-free run'", timeout=7300, on_abort="true",
                                  vram_check=None, vram_max_used_mib=1024, summary="trial",
                                  front=False)
        q.STATE_PATH.write_text(json.dumps({"jobs": [
            {"id": "s1", "label": "auto-author-x", "status": "pending", "bundle": "x"}]}))
        q.cmd_enqueue_gpu(args)
        st = json.loads(q.STATE_PATH.read_text())
        row = st["jobs"][-1]
        check("2 enqueued as the last row, pending", (row["label"], row["status"]),
              ("gpu-strata-trial", "pending"))
        tf = Path(row["task_file"])
        check("2 spec file is 0600", stat.S_IMODE(tf.stat().st_mode), 0o600)
        spec = json.loads(tf.read_text())
        check("2 spec carries cmd/timeout/on_abort, evict on",
              (spec["cmd"], spec["timeout_s"], spec["on_abort"], spec["evict"]),
              ("ssh box 'secret-free run'", 7300, "true", True))
        check("2 the command is NOT copied onto the queue row",
              "secret-free" in json.dumps(row), False)
        check("2 live log path set", bool(row.get("live_log_path")), True)
        try:
            q.cmd_enqueue_gpu(args)
            dup = None
        except SystemExit as e:
            dup = e.code
        check("2 duplicate live label refused", dup, q._DUPLICATE_LABEL_RC)
    guarded("2 enqueue end to end", s2)

    # --- 3. routing ----------------------------------------------------------------------
    def s3():
        j = gpu_row()
        q._model_size_cached = boom
        check("3 candidate lanes: exactly the named host's URL, no size lookup",
              q._candidate_lanes(j, fake_w), [UNRAID_URL])
        check("3 fit routing: host unchanged, not fit-routed",
              q._apply_fit_routing(j, fake_w)[0], "unraid")
    guarded("3 routing", s3)

    # --- 4. launch decision ---------------------------------------------------------------
    def s4():
        j = gpu_row()
        d = q.gpu_exclusive_launch_decision
        check("4 free lane, nothing waiting -> launch", d(j, [j])[0], True)
        busy = {"id": "p", "label": "gate-abc", "status": "running", "lane": "unraid",
                "host_pref": "unraid"}
        check("4 a job RUNNING on unraid -> wait (never preempt)", d(j, [j, busy])[0], False)
        pg = {"id": "g", "label": "gate-abc123", "status": "pending", "host_pref": "unraid"}
        check("4 a PENDING unraid pre-gate -> wait (gates first)", d(j, [pg, j])[0], False)
        held_gate = dict(pg, status="held")
        check("4 a HELD unraid gate -> wait", d(j, [held_gate, j])[0], False)
        rg = {"id": "r", "label": "regate-abc123", "status": "pending", "host_pref": "studio-db"}
        check("4 a Studio regate does not block the unraid job", d(j, [rg, j])[0], True)
        st_run = {"id": "s", "label": "auto-author-x", "status": "running", "lane": "studio-db",
                  "host_pref": "studio"}
        check("4 a running Studio job does not block it", d(j, [st_run, j])[0], True)
        done_gate = dict(pg, status="done")
        check("4 a finished gate does not block it", d(j, [done_gate, j])[0], True)
        other = gpu_row("g2", "gpu-other", status="running")
        other["lane"] = "unraid"
        check("4 a second gpu job waits for the first", d(j, [other, j])[0], False)
    guarded("4 launch decision", s4)

    # --- 5. commitment / focus wiring in cmd_run -------------------------------------------
    def s5():
        import ast
        import textwrap

        src = inspect.getsource(q.cmd_run)

        def _has_gpu_exclusive_skip_in_running_key_assign():
            """Return True iff the ast.If node whose body assigns _running_key
            contains a negated call to _is_gpu_exclusive_job(_j)."""
            tree = ast.parse(textwrap.dedent(src))
            # Find the function body
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "cmd_run":
                    for child in ast.walk(node):
                        if isinstance(child, ast.If):
                            # Check if this If's body contains an assignment
                            # to _running_key = _pk(...)
                            assigns_running_key = False
                            for stmt in ast.walk(child):
                                if isinstance(stmt, ast.Assign):
                                    for target in stmt.targets:
                                        if isinstance(target, ast.Name) and target.id == "_running_key":
                                            assigns_running_key = True
                            if assigns_running_key:
                                # Look in the test for: not _is_gpu_exclusive_job(_j)
                                for sub in ast.walk(child.test):
                                    if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
                                        if isinstance(sub.operand, ast.Call):
                                            callee = sub.operand.func
                                            if isinstance(callee, ast.Name) and callee.id == "_is_gpu_exclusive_job":
                                                return True
            return False

        check("5 running-key loop skips gpu-exclusive rows",
              _has_gpu_exclusive_skip_in_running_key_assign(), True)
        check("5 commitment candidate order excludes gpu-exclusive rows",
              "if not _is_gpu_exclusive_job(_oj)]" in src, True)
        i_gx = src.find("gpu_exclusive_launch_decision(job, state[\"jobs\"])")
        i_fs = src.find("elif focus_skips_job(job, _pk(job)")
        check("5 launch loop: gpu rows take the lane decision INSTEAD of the focus skip",
              0 < i_gx < i_fs, True)
        check("5 a gpu launch never consumes the backfill slot",
              bool(__import__("re").search(
                  r"_backfill_ok and _commit_key is not None[^:]*?"
                  r"and not _is_gpu_exclusive_job\(job\)[^:]*?\):", src)), True)
        # behavioural: the commitment step with the filtered candidate keys
        jobs = [gpu_row(status="running") | {"lane": "unraid", "pid": 1}]
        pk = lambda r: r.get("bundle")
        running_key = next((pk(r) for r in jobs if r["status"] == "running"
                            and not q._is_gpu_exclusive_job(r)), None)
        cands = [pk(r) for r in jobs if r["status"] == "pending" and not q._is_gpu_exclusive_job(r)]
        check("5 with only a gpu job running, nothing is offered to the commitment",
              (running_key, cands), (None, []))
    guarded("5 commitment wiring", s5)

    # --- 6. orphan recovery ------------------------------------------------------------------
    def s6():
        j = gpu_row(status="running")
        rp = q.gpu_job_result_path(j)
        check("6 result path sits next to the spec", rp.name, "g1.result.json")
        rd = lambda rec: (lambda _p: rec)
        R = q.gpu_job_recovery
        check("6 finished exit 0 -> done", R(j, rd({"phase": "finished", "exit": 0, "rc": 0}))[0], "done")
        check("6 finished rc 1 -> failed", R(j, rd({"phase": "finished", "exit": 1, "rc": 1}))[0], "failed")
        check("6 died mid-run -> failed, never pending",
              R(j, rd({"phase": "running"}))[0], "failed")
        check("6 no sidecar -> failed, never pending", R(j, rd(None))[0], "failed")
        j2 = gpu_row(status="running")
        j2["pid"] = 4242
        q._settle_orphaned_gpu_job(j2, 4242, read=rd(None))
        check("6 settle: failed, pid/lane cleared, not requeued",
              (j2["status"], j2["pid"], j2["lane"]), ("failed", None, None))
        src = inspect.getsource(q.cmd_run)
        check("6 both recovery sites route gpu rows to the settle (never the requeue path)",
              src.count("_settle_orphaned_gpu_job("), 2)
    guarded("6 orphan recovery", s6)

    # --- 7. the runner itself ---------------------------------------------------------------
    def s7():
        r = subprocess.run([sys.executable, a.runner, "--self-test"], capture_output=True,
                           text=True, timeout=180)
        check("7 gpu-exclusive-runner.py --self-test passes",
              (r.returncode, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""),
              (0, "SELF_TEST_OK"))
    guarded("7 runner", s7)

    # --- 8. CLI surface ----------------------------------------------------------------------
    def s8():
        r = subprocess.run([sys.executable, a.queue, "enqueue-gpu", "--help"],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "DISPATCH_VERIFY_SANDBOX": "0"})
        check("8 `enqueue-gpu --help` exists and names --bundle", "--bundle" in r.stdout, True)
        r2 = subprocess.run([sys.executable, a.queue, "enqueue-gpu", "--cmd", "true", "--label",
                             "x", "--bundle", "b"], capture_output=True, text=True, timeout=60)
        check("8 under the verify sandbox the real enqueue-gpu is refused (rc 3)",
              r2.returncode, 3)
    guarded("8 cli", s8)

    # --- 9. `results` for a finished gpu-exclusive job (2026-10-05) -------------------------
    # The Strata trial 36d2d7751cb5 read `[PENDING ] ... kind=?` forever: a gpu job is never
    # gated, so there is no verdict to wait for. Drive the real reader on a done.json shaped
    # exactly like that job's (no job_kind field -- older sidecars do not carry one).
    def s9():
        ld = tmp / "logs9"
        ld.mkdir()
        (ld / "abc123def456.done.json").write_text(json.dumps({
            "id": "abc123def456", "label": "gpu-strata-trial-3080", "bundle": "strata-trial",
            "model": "gpu-exclusive", "status": "done", "exit_code": 0, "task_kind": None,
            "runner": str(BIN / "gpu-exclusive-runner.py"), "host_pref": "unraid",
            "persisted_at": "2026-10-05T22:15:32Z"}))
        res = q._load_job_result("abc123def456", ld)
        check("9 finished gpu job is NOT tagged PENDING in `results`",
              q._verdict_tag(res), "NO-GATE")
        kind = q._result_kind(res) if hasattr(q, "_result_kind") else res.get("task_kind")
        check("9 its kind column names it (not '?')", kind, "gpu-excl")
    guarded("9 results tag", s9)

    n_ok = sum(RESULTS)
    print(f"\n{n_ok}/{len(RESULTS)} checks passed")
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
