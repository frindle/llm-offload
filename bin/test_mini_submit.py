#!/usr/bin/env python3
"""test_mini_submit.py -- acceptance tests for the mini-swe submit gate (~/bin/mini-submit, mini_submit_receipt.py,
the ollama-queue.py enqueue receipt refusal, mini-validate runner-sha staleness, the runner's --try-fix-chain and cwd_guard).

  T1  direct enqueue with --cwd == baseline_from is refused; mini-submit rejects --cwd; a receipt cwd mismatch is refused;
      a workdir inside baseline_from fails C2 (and cwd_guard itself catches cwd inside baseline_from)
  T2  a mini-spec-style draft (relative task, no ref copy, no red_ok/target_fn/fn_scope/fix_steps/fix_wall) fails one item at a
      time with an exact fix; after the printed fixes are applied it passes. Default: mini-validate fills the C4 fields, so one
      validation is enough; T2 --no-fill: C4 names them and its printed patch is applied, then a second validation
  T3  a runner with an injected chained diff_reach bug fails C6 before enqueue; a runner sha change makes C3 stale; a failing
      --self-test blocks C0
  T4  prior model-class failure + no TRIAGE.md is refused with the mini-triage command; an infra-only family is auto-triaged
  T5  label rules
  T6  cmd_enqueue refusals (missing / stale receipt, changed spec bytes, non-empty cwd); non-mini runners unaffected

No network, no GPU, no real enqueue (stub queue), everything under a temp dir (MINI_SUBMIT_HOME / FAILURE_LEDGER / MINI_LIVELOG_DIR
point there). Usage: python3 test_mini_submit.py [-k SUBSTR]. Marker: ALL PASS.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
REAL_HOME = os.environ.get('HOME', '')
sys.path.insert(0, str(BIN))
import mini_submit_receipt as MR  # noqa: E402  (written by this change)

T = Path(tempfile.mkdtemp(prefix="minisubmit-")).resolve()
HOME_Q = T / "home_q"  # HOME for the ollama-queue.py module load; the runner copy lives in HOME_Q/bin
(HOME_Q / "bin").mkdir(parents=True)
RUN_OK = HOME_Q / "bin" / "mini-swe-runner.py"
shutil.copy2(BIN / "mini-swe-runner.py", RUN_OK)
fails = passed = 0
ONLY = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None


def chk(name, cond, detail=""):
    global fails, passed
    if cond:
        passed += 1
    else:
        fails += 1
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else (f": {detail}" if detail else "")), flush=True)


def want(tag):
    return ONLY is None or ONLY in tag


# ------------------------------------------------------------------------------------------------ fixture (3 toy functions)
BASE_M = '''"""m: three toy functions."""


def inc(x):
    raise NotImplementedError("m stub: inc")


def dbl(x):
    raise NotImplementedError("m stub: dbl")


def neg(x):
    raise NotImplementedError("m stub: neg")
'''
INC = '''def inc(x):
    """Add one."""
    if x is None:
        raise ValueError("inc: None")
    y = x + 1
    return y
'''
DBL = '''def dbl(x):
    return x * 2
'''
NEG = '''def neg(x):
    return -x
'''


def stub(n):
    return f'def {n}(x):\n    raise NotImplementedError("m stub: {n}")\n'


def m_text(done):
    parts = ['"""m: three toy functions."""\n']
    for n, body in (("inc", INC), ("dbl", DBL), ("neg", NEG)):
        parts.append(body if n in done else stub(n))
    return "\n\n".join(parts)


TEST = '''"""Frozen test, step {n}: m.{fn}."""
import importlib
import os
import socket
import sys


def _no_net(*_a, **_k):
    raise RuntimeError("network access in a unit test")


socket.socket.connect = _no_net

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
m = importlib.import_module("m")  # alias assigned ONLY here


def test_{fn}_basic():
    got = m.{fn}(3)
    assert got == {a}, f"{fn}(3): {{got!r}}"
    got = m.{fn}(0)
    assert got == {b}, f"{fn}(0): {{got!r}}"


def test_{fn}_negative():
    got = m.{fn}(-2)
    assert got == {c}, f"{fn}(-2): {{got!r}}"
    got = m.{fn}(10)
    assert got == {d}, f"{fn}(10): {{got!r}}"


TESTS = [test_{fn}_basic, test_{fn}_negative]

if __name__ == "__main__":
    order = TESTS[::-1] if os.environ.get("TEST_ORDER") == "reverse" else TESTS
    failed = 0
    for t in order:
        try:
            t()
            print(f"RESULT {{t.__name__}} PASS")
        except Exception as e:
            failed += 1
            print(f"RESULT {{t.__name__}} FAIL {{type(e).__name__}}: {{e}}")
    print(f"{{len(order) - failed}} passed, {{failed}} failed")
    sys.exit(1 if failed else 0)
'''
VALS = {"inc": (4, 1, -1, 11), "dbl": (6, 0, -4, 20), "neg": (-3, 0, 2, -10)}


def vcs(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a], cwd=cwd, check=True, capture_output=True)


def build(root, steps=3):
    """A valid, complete steps spec: repo/ (git, frozen tests committed), spec/ (spec.json, TASK-sN.md, ref/sN/m.py)."""
    root = Path(root)
    repo, sd = root / "repo", root / "spec"
    repo.mkdir(parents=True)
    sd.mkdir(parents=True)
    (repo / "m.py").write_text(BASE_M)
    fns = ["inc", "dbl", "neg"][:steps]
    for i, fn in enumerate(fns, 1):
        a, b, c, d = VALS[fn]
        (repo / f"test_m_s{i}.py").write_text(TEST.format(n=i, fn=fn, a=a, b=b, c=c, d=d))
    vcs(repo, "init", "-q")
    vcs(repo, "add", "-A")
    vcs(repo, "commit", "-qm", "base")
    ref, st = sd / "ref", []
    for i, fn in enumerate(fns, 1):
        (ref / f"s{i}").mkdir(parents=True)
        (ref / f"s{i}" / "m.py").write_text(m_text(fns[:i]))
        (sd / f"TASK-s{i}.md").write_text(f"# TASK step {i}/{steps}: implement `{fn}(x)` in m.py\n\nEdit ONLY m.py. The test test_m_s{i}.py is FROZEN.\n"
                                          f"Check: python3 test_m_s{i}.py\n\nMust contain: `def {fn}(x)`\n")
        st.append({"task": str(sd / f"TASK-s{i}.md"), "test_file": f"test_m_s{i}.py", "test_cmd": f"python3 test_m_s{i}.py",
                   "target_fn": fn, "red_ok": [], "fn_scope": "enforce", "fix_steps": 40, "fix_wall": 800, "timeout": 1500})
    spec = {"mode": "two-phase", "baseline_from": str(repo), "allowed": ["m.py"], "suite_cmd": "python3 -m py_compile m.py",
            "stub_error": "m stub:", "min_tests": 2, "fix_attempts": 2, "test_steps": 10, "ref": str(ref), "steps": st}
    sp = sd / "spec.json"
    sp.write_text(json.dumps(spec, indent=1))
    return sp, spec


# ------------------------------------------------------------------------------------------------ runners + stub queue
def make_bug_runner():
    """Copy of the real runner with the 2026-10-24 chained diff_reach shape: the changed lines are taken against git HEAD instead of the
    step's own start text, so step N is charged for step N-1's lines (which its test never runs)."""
    src = (BIN / "mini-swe-runner.py").read_text()
    anchor = "lines = changed_lines(cwd, f, base_texts.get(f))"
    assert src.count(anchor) == 1, "diff_reach injection point drifted: update make_bug_runner()"
    patched = src.replace(anchor, "lines = changed_lines(cwd, f, _head_text(cwd, f))")
    patched = patched.replace("def diff_reach(cwd, reach, files, base_texts):",
                              "def _head_text(cwd, f):\n    r = subprocess.run(['git', 'show', 'HEAD:' + f], cwd=cwd, capture_output=True, text=True)\n"
                              "    return r.stdout if r.returncode == 0 else None\n\n\ndef diff_reach(cwd, reach, files, base_texts):", 1)
    p = T / "runner_bug" / "mini-swe-runner.py"
    p.parent.mkdir()
    p.write_text(patched)
    return p


STUBQ = T / "stubq.py"
STUBQ.write_text('import json, os, sys\nopen(os.environ["STUBQ_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\nprint(json.dumps({"id": "abc123def456"}))\n')


def make_env(tag, runner):
    h = T / tag / "msh"
    (h / "mini-selftest").mkdir(parents=True, exist_ok=True)
    e = {**os.environ, "MINI_SUBMIT_HOME": str(h), "MINI_RUNNER": str(runner), "MINI_SUBMIT_QUEUE": str(STUBQ),
         "STUBQ_LOG": str(T / tag / "stubq.log"), "FAILURE_LEDGER": str(T / tag / "failures.jsonl"),
         "MINI_LIVELOG_DIR": str(T / tag / "livelogs"), "PYTHONDONTWRITEBYTECODE": "1"}
    (T / tag / "livelogs").mkdir(parents=True, exist_ok=True)
    seed_selftest(e, runner)
    return e


def seed_selftest(e, runner):
    d = Path(e["MINI_SUBMIT_HOME"]) / "mini-selftest"
    d.mkdir(parents=True, exist_ok=True)
    (d / MR.sha_file(runner)).write_text("seeded by test_mini_submit\n")


# A model the vet gate (model_profile.vet_model, used by mini-submit and enqueue) knows offline: the
# documented launch model, an alias in model_profiles.yaml. A made-up name is refused as unknown.
MODEL = "qwen3.8:27b-q4_K_M"


def submit(e, spec, label="resell-ocr-model-store", extra=(), model=MODEL):
    p = subprocess.run([sys.executable, str(BIN / "mini-submit"), str(spec), "--label", label, "--model", model, *extra],
                       capture_output=True, text=True, env=e, timeout=1800)
    return p.returncode, p.stdout, p.stderr


def fail_of(out):
    """(check id, reason, fix) of the first FAIL in mini-submit output, else None."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("mini-submit: FAIL "):
            cid, _, reason = ln[len("mini-submit: FAIL "):].partition(" ")
            fix = lines[i + 1].strip()[len("fix: "):] if i + 1 < len(lines) and lines[i + 1].strip().startswith("fix: ") else ""
            return cid, reason, fix
    return None


def validate(e, spec, *extra):
    return subprocess.run([sys.executable, str(BIN / "mini-validate"), str(spec), *extra], capture_output=True, text=True, env=e, timeout=1800)


def good_job(tag, steps, runner):
    """A fixture job validated against `runner`: (spec path, env)."""
    sp, _ = build(T / tag / "job", steps)
    e = make_env(tag, runner)
    v = validate(e, sp)
    chk(f"[{tag}] fixture validates with the real mini-validate ({steps} step(s))", v.returncode == 0, (v.stdout + v.stderr)[-800:])
    return sp, e


def load_q(enforce):
    os.environ["HOME"] = str(HOME_Q)
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    ld = importlib.machinery.SourceFileLoader("queue_ms", str(BIN / "ollama-queue.py"))
    q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_ms", ld))
    old = sys.argv
    sys.argv = [str(BIN / "ollama-queue.py")]
    try:
        ld.exec_module(q)
    finally:
        sys.argv = old
    q.MINI_RECEIPT_ENFORCE = enforce
    return q


def q_enqueue(q, runner, cwd, task_file, label="resell-ocr-model-store"):
    """Run `ollama-queue.py enqueue ...` in-process; (exit code, stderr text)."""
    err, old = io.StringIO(), sys.argv
    sys.argv = ["ollama-queue.py", "enqueue", "--runner", str(runner), "--cwd", str(cwd), "--task-file", str(task_file), "--model", MODEL,
                "--host", "auto", "--task-kind", "coding", "--allow-no-verify", "--allow-unisolated", "--label", label]
    code = None
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            q.main()
    except SystemExit as se:
        code = se.code
    finally:
        sys.argv = old
    if isinstance(code, str):
        err.write(code)
        code = 1
    return code, err.getvalue()


def t2_draft(fill):
    """T2: a mini-spec-style draft fixed one printed item at a time. fill=True: mini-validate writes the C4 fields itself (one
    validation); fill=False (--no-fill): C4 names them, its printed patch is applied, and the spec edit makes C3 stale once more."""
    tag = "T2" if fill else "T2-nofill"
    sp, spec = build(T / tag / "job", 2)
    e = make_env(tag, RUN_OK)
    sd = sp.parent
    hidden = sd / "_refsrc"
    shutil.move(str(sd / "ref"), str(hidden))
    spec.pop("ref")
    for i, st in enumerate(spec["steps"], 1):
        st["task"] = f"TASK-s{i}.md"  # relative: lives in the spec dir, not in baseline_from
        for k in ("red_ok", "target_fn", "fn_scope", "fix_steps", "fix_wall"):
            st.pop(k)
    sp.write_text(json.dumps(spec, indent=1))
    seq, ok, vouts = [], False, []
    for _ in range(8):
        rc, out, err = submit(e, sp, extra=["--dry-run"])
        f = fail_of(out)
        if rc == 0:
            ok = "DRY-RUN ok" in out
            break
        if not f:
            chk(f"{tag} unexpected non-check failure", False, (out + err)[-600:])
            break
        seq.append(f)
        cid, reason, fix = f
        if not fix:
            chk(f"{tag} {cid} printed no fix", False, reason)
            break
        if fix.startswith("mkdir -p"):
            chk(f"{tag} the ref fix names the missing s<N> dir", f"{sd}/ref/s1" in fix, fix)
            shutil.move(str(hidden), str(sd / "ref"))
        elif cid == "C3":
            chk(f"{tag} the C3 fix is the exact mini-validate command", fix == f"{BIN / 'mini-validate'} {sp}", fix)
            v = validate(e, sp, *([] if fill else ["--no-fill"]))
            vouts.append(v.stdout + v.stderr)
            if v.returncode != 0:
                chk(f"{tag} mini-validate on the fixed spec", False, (v.stdout + v.stderr)[-600:])
                break
        else:
            r = subprocess.run(fix, shell=True, capture_output=True, text=True)
            if r.returncode != 0:
                chk(f"{tag} the printed {cid} fix runs", False, fix[:200] + r.stderr[-200:])
                break
    ids = [c for c, _r, _f in seq]
    final = json.loads(sp.read_text())
    if fill:
        chk("T2 FAIL sequence: relative task, missing ref, no VALIDATION, then pass (mini-validate filled the C4 keys: one validation)",
            ids == ["C1", "C1", "C3"] and ok, f"{ids} ok={ok}")
        vo = vouts[0] if vouts else ""
        for k in ("red_ok", "baseline_green", "fn_scope", "target_fn", "fix_steps", "fix_wall"):
            chk(f"T2 mini-validate printed its {k} fill", f"s1 {k}: (missing) -> " in vo, vo[:900])
        vm = (sd / "VALIDATION.md").read_text() if (sd / "VALIDATION.md").is_file() else ""
        chk("T2 VALIDATION.md lists the fills", "## Spec fields filled before validation" in vm and "- s2 target_fn: (missing) -> [\"dbl\"]" in vm, vm[:1200])
        chk("T2 the filled target_fn is the function each step's ref edits",
            [s.get("target_fn") for s in final["steps"]] == [["inc"], ["dbl"]], json.dumps(final["steps"])[:400])
        chk("T2 the filled sizing is the V6 formula (fix_steps 30, fix_wall 600)",
            all(s.get("fix_steps") == 30 and s.get("fix_wall") == 600 for s in final["steps"]), json.dumps(final["steps"])[:400])
        v = validate(e, sp)
        chk("T2 a second mini-validate fills nothing", v.returncode == 0 and "filled" not in v.stdout, (v.stdout + v.stderr)[-600:])
    else:
        chk("T2 --no-fill FAIL sequence: relative task, missing ref, no VALIDATION, missing C4 keys, stale after the C4 edit, then pass",
            ids == ["C1", "C1", "C3", "C4", "C3"] and ok, f"{ids} ok={ok}")
        chk("T2 --no-fill leaves the spec alone", not any("filled" in vo for vo in vouts), vouts[0][:600] if vouts else "")
        c4 = next((s for s in seq if s[0] == "C4"), None)
        if c4:
            for k in ("red_ok", "target_fn", "fn_scope", "fix_steps", "fix_wall"):
                chk(f"T2 C4 names {k}", k in c4[1], c4[1][:300])
            chk("T2 C4 fix carries the VALIDATION.md formula values (fix_steps 30, fix_wall 600) and the inferred target_fn",
                "'fix_steps'" in c4[2] and "30" in c4[2] and "600" in c4[2] and "inc" in c4[2], c4[2][:400])
    chk(f"{tag} the fixed spec has absolute tasks, red_ok, target_fn, fn_scope, fix_steps, fix_wall on every step",
        all(Path(s["task"]).is_absolute() and "red_ok" in s and s.get("target_fn") and s.get("fn_scope") in ("warn", "enforce", "fail", "off")
            and s.get("fix_steps", 0) >= 30 and s.get("fix_wall") for s in final["steps"]), json.dumps(final["steps"])[:400])


AUTO_CALC = '''"""calc: order pricing."""


def clamp(x, lo, hi):
    return max(lo, min(x, hi))


def bulk_total(qty, unit):
    if qty > 10:
        return round(qty * unit * 0.95, 2)
    return qty * unit
'''


def t7_auto():
    """T7 mode auto: no ref/frozen test; C1 checks the worktree/task/hints, C3 is the kill switch + must-not-offload refusal."""
    root = T / "T7"
    repo, sd = root / "shop", root / "spec"
    (repo / "pkg").mkdir(parents=True)
    sd.mkdir(parents=True)
    (repo / "calc.py").write_text(AUTO_CALC)
    (repo / "pkg" / "util.py").write_text("def ident(x):\n    return x\n")
    vcs(repo, "init", "-q")
    vcs(repo, "add", "-A")
    vcs(repo, "commit", "-qm", "base")
    (sd / "TASK.md").write_text("Orders of exactly 10 items miss the 5% bulk discount: bulk_total(10, 2) returns 20 but should return 19.0.\n")
    base = {"mode": "auto", "allowed": [], "baseline_from": str(repo), "task": str(sd / "TASK.md"), "files": ["calc.py"], "fn": "bulk_total"}
    e = make_env("T7", RUN_OK)
    e.update(MINI_SPEC_LIB=str(BIN / "mini-spec"), MINI_GRAPH_LOOKUP="off", MINI_GRAPH_WARN="off")

    def put(**kw):
        sp = sd / "spec.json"
        d = {**base, **kw}
        sp.write_text(json.dumps({k: v for k, v in d.items() if v is not None}, indent=1))
        return sp

    sp = put()
    rc, out, err = submit(e, sp, label="shop-bulk-discount", extra=["--dry-run"])
    chk("T7 auto spec (file + fn hint) passes C0-C8 in a dry run", rc == 0 and "DRY-RUN ok" in out, out[-900:] + err[-300:])
    chk("T7 ...C1/C3 run the auto checks, C4/C6 do not apply",
        "C1 ok auto: baseline" in out and "C3 ok auto: no must-not-offload" in out and "C4 ok not a two-phase spec" in out
        and "C6 ok plain spec" in out, out[-900:])
    chk("T7 ...the dry run leaves nothing behind (no receipt, no work dir)",
        not (sd / "SUBMIT-OK.json").exists() and not any((Path(e["MINI_SUBMIT_HOME"]) / "mini-work").glob("shop-bulk-discount-*")))
    sp = put(files=None, fn=None)
    rc, out, err = submit(e, sp, label="shop-bulk-discount", extra=["--dry-run"])
    chk("T7 auto spec without hints (diagnosis locates the code) passes the checks", rc == 0 and "DRY-RUN ok" in out, out[-600:])
    sp = put(files=["calc.py"])
    rc, out, err = submit(e, sp, label="shop-bulk-discount")
    log = [json.loads(ln) for ln in Path(e["STUBQ_LOG"]).read_text().splitlines()] if Path(e["STUBQ_LOG"]).is_file() else []
    argv = log[-1] if log else []
    wd = argv[argv.index("--cwd") + 1] if "--cwd" in argv else ""
    chk("T7 a real auto submit enqueues (stub queue) with a fresh empty work dir and a receipt",
        rc == 0 and "job_id: abc123def456" in out and wd.endswith("/work") and Path(wd).is_dir() and not any(Path(wd).iterdir())
        and argv[argv.index("--task-file") + 1] == str(sp) and (sd / "SUBMIT-OK.json").is_file(), out[-600:] + str(argv))
    chk("T7 ...the run dir holds no check-only leftovers", Path(wd).is_dir() and sorted(c.name for c in Path(wd).parent.iterdir()) == ["work"],
        str(sorted(c.name for c in Path(wd).parent.iterdir())) if Path(wd).is_dir() else "no work dir")
    q = load_q(True)
    chk("T7 ...and the queue's receipt check authorises it", q._mini_receipt_refusal(str(RUN_OK), str(sp), wd) is None)
    # the same label twice in one second: the second submit gets <label>-<stamp>-2, not a C2 "File exists" failure
    mw, now = Path(e["MINI_SUBMIT_HOME"]) / "mini-work", time.time()
    taken = [mw / f"shop-bulk-discount-{time.strftime('%Y%m%d%H%M%S', time.localtime(now + d))}" for d in range(4)]
    for t in taken:
        (t / "work").mkdir(parents=True, exist_ok=True)
    rc, out, err = submit(e, put(files=["calc.py"]), label="shop-bulk-discount")
    log = [json.loads(ln) for ln in Path(e["STUBQ_LOG"]).read_text().splitlines()]
    wd2 = log[-1][log[-1].index("--cwd") + 1] if "--cwd" in log[-1] else ""
    chk("T7 a second submit of one label in the same second gets its own run dir (-2), not a C2 collision",
        rc == 0 and wd2 != wd and Path(wd2).parent in [Path(f"{t}-2") for t in taken], out[-600:] + wd2)
    for t in taken + [Path(wd2).parent]:
        shutil.rmtree(t, ignore_errors=True)

    def fails_with(cid, needle, label="shop-bulk-discount", env=None, **kw):
        s = put(**kw)
        rc, out, err = submit(env or e, s, label=label, extra=["--dry-run"])
        f = fail_of(out)
        return rc == 1 and f is not None and f[0] == cid and needle in f[1], (f, out[-500:])

    (sd / "TASK-login.md").write_text("Users with an expired password cannot login: the session check rejects them.\n")
    ok, d = fails_with("C3", "must-not-offload", task=str(sd / "TASK-login.md"))
    chk("T7 a must-not-offload description (login/password) is refused at C3", ok, str(d))
    ok, d = fails_with("C3", "auto mode is OFF", env={**e, "MINI_AUTO": "off"})
    chk("T7 MINI_AUTO=off refuses at C3", ok, str(d))
    ok, d = fails_with("C3", "must-not-offload", files=["calc.py"], fn="refresh_token")
    chk("T7 a must-not-offload fn hint is refused at C3", ok, str(d))
    ok, d = fails_with("C1", "absolute path", task="TASK.md")
    chk("T7 a relative task is refused at C1 with a patch to the absolute path", ok and str(sd / "TASK.md") in (d[0] or ("", "", ""))[2], str(d))
    (sd / "EMPTY.md").write_text("  \n")
    ok, d = fails_with("C1", "missing or empty", task=str(sd / "EMPTY.md"))
    chk("T7 an empty description is refused at C1", ok, str(d))
    ok, d = fails_with("C1", "not tracked files", files=["calc.py", "nope.py"])
    chk("T7 a file hint that is not a tracked file is refused at C1", ok, str(d))
    ok, d = fails_with("C1", "auto:", files=["pkg"], fn=None)
    chk("T7 a directory hint is accepted (not refused at C1)", not ok, str(d))
    ok, d = fails_with("C1", "not the root of a git worktree", baseline_from=str(repo / "pkg"), files=None, fn=None)
    chk("T7 baseline_from below the worktree root is refused at C1", ok, str(d))
    ok, d = fails_with("C1", "diagnose=skip needs", diagnose="skip", fn=None)
    chk("T7 diagnose=skip without an fn hint is refused at C1", ok, str(d))
    ok, d = fails_with("C1", "diagnose must be", diagnose="sometimes")
    chk("T7 an unknown diagnose value is refused at C1", ok, str(d))
    sp = put(diagnose="skip")
    rc, out, err = submit(e, sp, label="shop-bulk-discount", extra=["--dry-run"])
    chk("T7 diagnose=skip with one file + fn hint passes", rc == 0 and "DRY-RUN ok" in out and "diagnose=skip" in out, out[-600:])


def main():
    bug = make_bug_runner()
    q0 = load_q(False)
    chk("MINI_RECEIPT_ENFORCE ships False", q0.MINI_RECEIPT_ENFORCE is False)
    msub = importlib.machinery.SourceFileLoader("mini_submit_mod", str(BIN / "mini-submit"))
    ms = importlib.util.module_from_spec(importlib.util.spec_from_loader("mini_submit_mod", msub))
    msub.exec_module(ms)

    # ------------------------------------------------------------------ T5 labels (pure)
    if want("T5"):
        for lab in ("webhook-fix-u2b", "demand-u1", "project-fix-u3", "Bad_Label", "a" * 49, "fix-task-job", "x-y"):
            chk(f"T5 refuse label {lab[:30]!r}", ms.check_label(lab) is not None)
        for lab in ("broker-guard-removal-dormant", "resell-ocr-model-store", "webhook-retry-backoff-u2b"):
            chk(f"T5 accept label {lab!r}", ms.check_label(lab) is None, str(ms.check_label(lab)))

    # ------------------------------------------------------------------ pure helpers: budget_failures / classify / parse_runner_result
    if want("T4") or want("unit"):
        import failure_ledger as FL
        rows = [{"job_id": "m1", "tags": ["model-nonconvergence-other"], "detail": "RESULT FAIL phase=fix test_green=FAIL class=MODEL"},
                {"job_id": "i1", "tags": ["runner-infra"], "detail": "x"},
                {"job_id": "i2", "tags": ["model-nonconvergence-other"], "detail": "mini-swe-runner RESULT: bad-cwd cwd is base"},
                {"job_id": "i3", "tags": [], "detail": "RESULT FAIL APIConnectionError class=INFRA"},
                {"job_id": "g1", "tags": ["runner-gate:diff_reach"], "detail": "mini-swe-runner diff_reach=FAIL"}]
        chk("budget_failures drops runner-infra / class=INFRA / bad-cwd rows, keeps model and runner-gate rows",
            [r["job_id"] for r in FL.budget_failures(rows)] == ["m1", "g1"], str([r["job_id"] for r in FL.budget_failures(rows)]))
        chk("budget_failures is pure (input untouched) and tolerates None", len(rows) == 5 and FL.budget_failures(None) == [])
        sig, tags = FL.classify({"failure_detail": "mini-swe-runner RESULT: diff_reach=FAIL x", "terminal_reason": "nonconvergence"}, {}, {})
        chk("classify keeps runner-gate:<x> as a tag", "runner-gate:diff_reach" in tags, str(tags))
        chk("a runner-gate row is NOT budget-free", len(FL.budget_failures([{"tags": tags, "detail": "mini-swe-runner diff_reach=FAIL"}])) == 1)
        chk("parse_runner_result: ' class=INFRA' -> infra", q0.parse_runner_result("RESULT FAIL APIConnectionError boom class=INFRA")[1] == "infra")
        chk("parse_runner_result: model class stays model", q0.parse_runner_result("RESULT FAIL mode=two-phase test_green=FAIL class=MODEL")[1] == "model")
        chk("parse_runner_result: bad-spec stays spec", q0.parse_runner_result("RESULT FAIL bad-spec nope class=SPEC")[1] == "spec")

    # ------------------------------------------------------------------ G: 1-step job validated with the real runner copy
    need_g = any(want(t) for t in ("T1", "T3", "T4", "T6"))
    if need_g:
        gsp, ge = good_job("G", 1, RUN_OK)
        gsd = gsp.parent
        gspec = json.loads(gsp.read_text())
        # --- T1
        if want("T1"):
            rc, out, err = submit(ge, gsp, extra=["--cwd", "/tmp/should-not-be-allowed"])
            chk("T1 mini-submit rejects --cwd (rc 2)", rc == 2 and "no --cwd" in err, f"rc={rc} {err[-200:]}")
            ginside = dict(ge)
            ginside["MINI_SUBMIT_HOME"] = str(Path(gspec["baseline_from"]) / "inside-home")
            seed_selftest(ginside, RUN_OK)
            rc, out, err = submit(ginside, gsp, extra=["--dry-run"])
            f = fail_of(out)
            chk("T1 a workdir inside baseline_from fails C2", rc == 1 and f and f[0] == "C2" and "baseline_from" in f[1], out[-400:])
            sandbox = T / "cwdguard"
            sandbox.mkdir()
            R = importlib.util.module_from_spec(importlib.util.spec_from_file_location("runner_ok", RUN_OK))
            R.__spec__.loader.exec_module(R)
            inside = Path(gspec["baseline_from"]) / "sub" / "work"
            chk("T1 runner cwd_guard catches cwd INSIDE baseline_from", R.cwd_guard(inside, gspec, str(gsp)) is not None)
            chk("T1 runner cwd_guard still accepts an unrelated cwd", R.cwd_guard(sandbox / "w", gspec, str(gsp)) is None)
            # a real (stub-queue) submit: the receipt + enqueue argv
            rc, out, err = submit(ge, gsp)
            chk("T1 clean submit passes every check and enqueues via the stub queue", rc == 0 and "job_id: abc123def456" in out, f"rc={rc}\n{out[-900:]}{err[-300:]}")
            log = [json.loads(ln) for ln in Path(ge["STUBQ_LOG"]).read_text().splitlines()] if Path(ge["STUBQ_LOG"]).is_file() else []
            argv = log[0] if log else []
            wd = argv[argv.index("--cwd") + 1] if "--cwd" in argv else ""
            chk("T1 enqueue argv: enqueue --runner <runner> --cwd <fresh workdir> --task-file SPEC --label L",
                argv[:1] == ["enqueue"] and argv[argv.index("--runner") + 1] == str(RUN_OK) and argv[argv.index("--task-file") + 1] == str(gsp)
                and argv[argv.index("--label") + 1] == "resell-ocr-model-store" and "/mini-work/resell-ocr-model-store-" in wd and wd.endswith("/work"), str(argv))
            chk("T1 the workdir is empty, outside the spec dir and baseline_from",
                Path(wd).is_dir() and not any(Path(wd).iterdir()) and str(gsd) not in wd and gspec["baseline_from"] not in wd)
            rec = json.loads((gsd / "SUBMIT-OK.json").read_text())
            chk("T1 receipt has spec_sha, cwd, runner_sha, label, ts, checks",
                all(k in rec for k in ("spec_sha", "cwd", "runner_sha", "label", "ts", "checks")) and rec["cwd"] == wd
                and [c["id"] for c in rec["checks"]] == [f"C{i}" for i in range(9)], str(rec)[:400])
            q = load_q(True)
            code, msg = q_enqueue(q, RUN_OK, gspec["baseline_from"], gsp)
            chk("T1 direct enqueue with --cwd == baseline_from is refused (exit 2, names mini-submit)",
                code == 2 and "REFUSING enqueue: mini-swe jobs go through ~/bin/mini-submit SPEC --label L" in msg and "cwd mismatch" in msg, f"{code} {msg[-300:]}")
            other = T / "other-empty"
            other.mkdir()
            code, msg = q_enqueue(q, RUN_OK, other, gsp)
            chk("T1 a receipt cwd mismatch (different empty dir) is refused", code == 2 and "cwd mismatch" in msg, f"{code} {msg[-300:]}")
            chk("T1 the matching receipt authorises the enqueue (helper returns None)", q._mini_receipt_refusal(str(RUN_OK), str(gsp), wd) is None)
            # --- T6 (uses the receipt just written)
            if want("T6") or True:
                rpath = gsd / "SUBMIT-OK.json"
                good = rpath.read_text()
                rpath.unlink()
                code, msg = q_enqueue(q, RUN_OK, wd, gsp)
                chk("T6 missing receipt refused", code == 2 and "REFUSING enqueue" in msg and "missing" in msg, f"{code} {msg[-300:]}")
                old = json.loads(good)
                old["ts"] = time.time() - 16 * 60
                rpath.write_text(json.dumps(old))
                code, msg = q_enqueue(q, RUN_OK, wd, gsp)
                chk("T6 receipt older than 15 min refused", code == 2 and "ts stale" in msg, f"{code} {msg[-300:]}")
                rpath.write_text(good)
                orig = gsp.read_text()
                gsp.write_text(orig.replace('"min_tests": 2', '"min_tests": 3'))
                code, msg = q_enqueue(q, RUN_OK, wd, gsp)
                chk("T6 changed spec bytes refused", code == 2 and "spec_sha" in msg, f"{code} {msg[-300:]}")
                gsp.write_text(orig)
                (Path(wd) / "stray.txt").write_text("x")
                code, msg = q_enqueue(q, RUN_OK, wd, gsp)
                chk("T6 non-empty cwd refused", code == 2 and "not empty" in msg, f"{code} {msg[-300:]}")
                (Path(wd) / "stray.txt").unlink()
                chk("T6 back to the good state: authorised again", q._mini_receipt_refusal(str(RUN_OK), str(gsp), wd) is None)
                chk("T6 non-mini runners are never checked",
                    q._mini_receipt_refusal(str(HOME_Q / "bin" / "code-review-agent.py"), str(gsp), "/nonexistent") is None)
                chk("T6 enforcement is a module flag (False by default, so the receipt is not consulted)", load_q(False).MINI_RECEIPT_ENFORCE is False)
                r = subprocess.run([sys.executable, str(BIN / "test-queue-runner-allowlist-symlink.py")], capture_output=True, text=True, env={**os.environ, "HOME": REAL_HOME})
                chk("T6 existing test-queue-runner-allowlist-symlink.py still passes", r.returncode == 0 and "ALL PASS" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
        # --- T3b/c
        if want("T3"):
            touched = T / "runner_touch" / "mini-swe-runner.py"
            touched.parent.mkdir()
            touched.write_text(RUN_OK.read_text() + "\n# touched: a different runner sha\n")
            et = dict(ge, MINI_RUNNER=str(touched))
            seed_selftest(et, touched)
            rc, out, err = submit(et, gsp, extra=["--dry-run"])
            f = fail_of(out)
            chk("T3 a runner sha change makes C3 stale", rc == 1 and f and f[0] == "C3" and "stale" in f[1] and "mini-validate" in f[2], out[-500:])
            chk("T3 stale C3 never reached the queue", not Path(ge["STUBQ_LOG"]).read_text().count("\n") > 1)
            badst = T / "runner_selftest_fail" / "mini-swe-runner.py"
            badst.parent.mkdir()
            badst.write_text("import sys\nprint('self-test FAILED: boom')\nsys.exit(1)\n")
            eb = make_env("selftest_fail", badst)
            (Path(eb["MINI_SUBMIT_HOME"]) / "mini-selftest" / MR.sha_file(badst)).unlink()
            rc, out, err = submit(eb, gsp, extra=["--dry-run"])
            f = fail_of(out)
            chk("T3 a failing --self-test blocks C0", rc == 1 and f and f[0] == "C0" and "--self-test" in f[2], out[-400:])
            chk("T3 a failed self-test is not cached", not (Path(eb["MINI_SUBMIT_HOME"]) / "mini-selftest" / MR.sha_file(badst)).exists())
            rc, out, err = submit(eb, gsp, extra=["--dry-run", "--override", "i know"])
            f = fail_of(out)
            chk("T3 C0 is not overridable", rc == 1 and f and f[0] == "C0")
            okst = T / "runner_selftest_ok" / "mini-swe-runner.py"
            okst.parent.mkdir()
            okst.write_text("import sys\nprint('self-test ok')\n")
            eo = make_env("selftest_ok", okst)
            (Path(eo["MINI_SUBMIT_HOME"]) / "mini-selftest" / MR.sha_file(okst)).unlink()
            rc, out, err = submit(eo, gsp, extra=["--dry-run"])
            chk("T3 a passing --self-test is cached per runner sha", (Path(eo["MINI_SUBMIT_HOME"]) / "mini-selftest" / MR.sha_file(okst)).is_file(), out[-300:])
        # --- T4
        if want("T4"):
            ledger = Path(ge["FAILURE_LEDGER"])
            lv = Path(ge["MINI_LIVELOG_DIR"])
            if (gsd / "SUBMIT-OK.json").exists():
                (gsd / "SUBMIT-OK.json").unlink()
            row = {"job_id": "aaaa11112222", "label": "ocr-prior-attempt", "task_file": str(gsp), "class": "model", "tags": ["model-nonconvergence-other"],
                   "detail": "mini-swe-runner RESULT FAIL mode=two-phase test_green=FAIL class=MODEL", "ts": "2026-10-24T10:00:00Z", "verdict": "FAIL"}
            ledger.write_text(json.dumps(row) + "\n")
            rc, out, err = submit(ge, gsp, label="resell-ocr-retry-store", extra=["--dry-run"])
            f = fail_of(out)
            chk("T4 prior model-class failure + no TRIAGE.md is refused", rc == 1 and f and f[0] == "C8", out[-500:])
            chk("T4 the fix is the exact mini-triage command", f and f[2].endswith(f"mini-triage aaaa11112222 --write {gsd}"), f and f[2])
            rc, out, err = submit(ge, gsp, label="resell-ocr-retry-store", extra=["--dry-run", "--override", "triaged by hand"])
            chk("T4 --override skips C8", rc == 0 and "C8 SKIPPED" in out, out[-400:])
            infra = {"job_id": "bbbb33334444", "label": "ocr-prior-attempt", "task_file": str(gsp), "class": "model", "tags": ["runner-infra"],
                     "detail": "mini-swe-runner RESULT: bad-cwd cwd /x contains the baseline_from /y class=INFRA", "ts": "2026-10-24T11:00:00Z", "verdict": "FAIL"}
            ledger.write_text(json.dumps(infra) + "\n")
            (lv / "bbbb33334444-ocr.livelog").write_text(f"mini-runner --task-file {gsp}\nRESULT FAIL bad-cwd cwd contains baseline_from class=SPEC\n")
            rc, out, err = submit(ge, gsp, label="resell-ocr-retry-store", extra=["--dry-run"])
            tri = gsd / "TRIAGE.md"
            chk("T4 an infra-only family is accepted", rc == 0 and "DRY-RUN ok" in out, out[-500:])
            chk("T4 ...and TRIAGE.md was auto-written for it", tri.is_file() and "bbbb33334444" in tri.read_text(), tri.read_text() if tri.is_file() else "no TRIAGE.md")
            tri.unlink(missing_ok=True)
            # family stem: a sibling job dir <family><letter> with a model failure also demands TRIAGE.md
            sib = T / "G-sib" / "ocr-job-u1b"
            shutil.copytree(gsd, T / "G-sib" / "ocr-job-u1", symlinks=True) if False else None
            ledger.write_text(json.dumps({**row, "task_file": str(gsd.parent / (gsd.name + "x") / "spec.json")}) + "\n")
            rc, out, err = submit(ge, gsp, label="resell-ocr-retry-store", extra=["--dry-run"])
            chk("T4 unrelated task_file rows are ignored", rc == 0, out[-300:])
            del sib

    # ------------------------------------------------------------------ T2: a mini-spec-style draft, fixed one printed item at a time
    if want("T2"):
        for fill in (True, False):
            t2_draft(fill)

    # ------------------------------------------------------------------ T3a: injected chained diff_reach bug vs the same 3-step job on the real runner
    if want("T3"):
        sp_ok, e_ok = good_job("H3-ok", 3, RUN_OK)
        rc, out, err = submit(e_ok, sp_ok, extra=["--dry-run"])
        chk("T3 control: the 3-step job passes C0-C8 on the real runner", rc == 0 and "DRY-RUN ok" in out and "C6 ok" in out, out[-900:])
        sp_bad, e_bad = good_job("H3-bug", 3, bug)
        rc, out, err = submit(e_bad, sp_bad, extra=["--dry-run"])
        f = fail_of(out)
        chk("T3 injected chained diff_reach bug: C5 and C3 pass, C6 fails with 'runner-gate suspect'",
            rc == 1 and "C5 ok" in out and f and f[0] == "C6" and "runner-gate suspect" in f[1], out[-900:])
        chk("T3 ...and diff_reach is the gate named", f and "diff_reach" in f[1], f and f[1][:400])
        chk("T3 ...before anything was enqueued", not Path(e_bad["STUBQ_LOG"]).exists())

    if want("T7"):
        t7_auto()

    shutil.rmtree(T, ignore_errors=True)
    print(f"\n{passed} passed, {fails} failed")
    print("ALL PASS" if not fails else "SOME CHECKS FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
