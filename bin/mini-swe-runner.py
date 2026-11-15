#!/usr/bin/env python3
"""mini-swe-runner.py -- queue runner that hands one coding task to mini-swe-agent.

Launched only by ~/bin/ollama-queue.py (add to ALLOWED_RUNNERS; select with
`queue-submit --runner ~/bin/mini-swe-runner.py`). The queue passes ONLY
--model --host --num-ctx --cwd --task-file; --host/--num-ctx are lane placeholders (the
agent talks to the live Darkbloom endpoint + key from ~/.darkbloom/local.json, read in code,
never printed). Running as a queue job makes the work visible to bloom-control, so the
Qwen pair is held instead of idle-restoring Gemma.

Spec (task file, JSON):
  {"allowed": ["app.py"],           # files the task may change (required)
   "baseline_from": "/path/wt",     # optional: copy this worktree into --cwd first
   "task": "TASK.md",               # task text file inside cwd, or an absolute path copied in as TASK.md
   "verify": "verify.sh",           # oracle run after the agent (default verify.sh)
   "timeout": 900}

Two-phase mode ("mode": "two-phase"; no verify.sh/refimpl needed, see docs/mini-swe-agent.md):
  "test_file": "test_x.py", "test_cmd": "python3 -m pytest -q test_x.py"   (required)
  optional: "symptom" (regex the baseline failure must match), "target_fn" (function name or list
  the test must invoke at baseline), "suite_cmd", "typecheck_cmd", "env" {}, "min_asserts" 2, "min_tests" 2,
  "test_timeout", "test_steps" 30, "test_wall" 600 and "fix_wall" 900 (seconds: mini's own wall_time_limit_seconds per attempt;
  the runner's kill timer backstops it at cap+60), "test_attempts" 2, "fix_steps" 45, "fix_attempts" 2,
  "gate_reserve" 240, "cmd_timeout" 300, "test_cmd_timeout" 150 (the phase A gate's cap on one test_cmd run; a run still going
  at the cap is rejected as a hang), "allow_error_fail", "drop" [files removed after prepare],
  "stub_error" (string or list: the message a seeded stub throws, e.g. "not implemented: parseCards"; its
  appearance in the baseline failure counts as a valid red, i.e. the stub's error stands in for the assertion failure).
  "update_tests" (list of tracked existing test files, top level or per step): tests that assert the old behaviour. Phase B may edit them
  (scope/untracked/chain-guard allow them; they are NOT `allowed`); precheck needs each to exist, be tracked, be a test file, differ from
  test_file/allowed and appear by name in suite_cmd; gate `update_tests`: each keeps >= its baseline number of test cases. single_shot
  cannot edit tests, so such a spec runs the mini loop. mini-spec reports candidates (IMPACT.md) but never fills the key.
  "update_tests_strict" (auto sets it with the update_tests it derives): the file names need not appear in suite_cmd, single_shot
  may run (it leaves the tests alone), and gate `update_flip` checks each edited test function: it fails on the fix without the edit,
  passes with it, keeps its assertions, and nothing else in the file changes but added imports and new tests.
  "suite_narrow" (a command): the suite gate runs it instead of suite_cmd when the whole suite times out at baseline (auto sets it to
  the existing tests nearest the change for a whole-pytest-run suite). "bg_baseline" (default true): the baseline suite/typecheck run
  in the background on a copy of the work dir while phase A works; false runs them first, in the work dir.
  Phase A: qwen writes test_file; gates: fails at baseline on an assert (for `symptom`), real
  asserts, no vacuous bounds/skips, no mocking of the target, the target code demonstrably runs
  (V8 coverage / python profile hook), a no-op edit of the target keeps it red. Phase B (fresh
  context: TASK.md + the test): fix; gates: test unchanged + green, reverting the allowed files
  turns it red again, suite/typecheck add no failures vs baseline, Must-contain literals, scope.
  Writes REVIEW.md next to the work dir for the Opus review.

Diagnose mode ("mode": "diagnose", "allowed": [], "task", "locate" [regex], "must_mention" [literal], "diag_attempts" 3): read-only; the model
writes DIAGNOSIS.md, ~/bin/diag_check.py gates it, a fresh mini run edits it on rejection (see docs/mini-swe-agent.md "Diagnose mode").

Gates after the agent: verify.sh must pass, changed tracked files must be within "allowed",
no untracked files outside .venv/TASK.md/verify.sh/fixtures, and a final message starting
"MISSING:" is reported as MISSING (not a pass). The Opus diff review is still required.
Prints a one-line RESULT to stdout (the queue's live log). Exit 0 = all gates green,
1 = anything else. Exit 3 = PAUSED (the provider stayed unavailable past MINI_OUTAGE_MAX_S, default 7 days; or the process was restarted; nothing graded; the queue re-launches the run when
Darkbloom serves again). Never exits 5 (queue reads it as unconverged).

Self-test: mini-swe-runner.py --self-test (no network, no agent).
"""
import argparse
import ast
import bisect
import codecs
import difflib
import glob
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import stat
import tarfile
import tempfile
import textwrap
import subprocess
import sys
import threading
import time
import types
import unicodedata
import warnings
from pathlib import Path
import urllib.request
from urllib.parse import unquote
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config

HOME = Path.home()
# launchd (queue daemon) PATH lacks Homebrew: node/npm/tsc gates and the agent's shell need it.
os.environ["PATH"] = os.pathsep.join(dict.fromkeys(
    [p for p in os.environ.get("PATH", "").split(os.pathsep) if p] + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
MINI = os.environ.get("MINI_BIN") or str(HOME / ".ollama-dispatch/mini-venv/bin/mini")
OVERLAY = HOME / "bin/configs/mini-local.yaml"
DIAG_OVERLAY = HOME / "bin/configs/mini-diagnose.yaml"  # diagnose stage only: a read-only instance template over OVERLAY's fix workflow
TEST_OVERLAY = HOME / "bin/configs/mini-testwrite.yaml"  # two-phase phase A only: a write-the-failing-test template (no fix workflow)
POLISH_OVERLAY = HOME / "bin/configs/mini-diagnose-polish.yaml"  # the diagnose form-repair attempt: edit one section, investigate nothing
MINI_EXT = Path(__file__).resolve().parent / "mini_ext"  # mini_ctx.CtxLitellmModel (overlay model_class)
LOCAL_JSON = Path(os.environ.get("DARKBLOOM_LOCAL_JSON", HOME / ".darkbloom/local.json"))


def _read_local():
    """The Darkbloom record (base_url with /v1, api_key): local.json, with DARKBLOOM_BASE_URL / DARKBLOOM_API_KEY laid over
    it. A missing file is an error only when the environment names no endpoint (the Mac, as before)."""
    try:
        rec = json.loads(LOCAL_JSON.read_text())
    except OSError:
        if not (os.environ.get("DARKBLOOM_API_KEY") or os.environ.get("DARKBLOOM_BASE_URL")):
            raise
        rec = {}
    return queue_config.darkbloom_apply_env(rec, v1=True)


IGNORE_UNTRACKED = ("verify.test.ts", "test-data", "node_modules", ".venv", "TASK.md", "verify.sh", "test_fixture.py", "refimpl.py", "__pycache__", ".pytest_cache")
RC_OK, RC_FAIL, RC_PAUSED = 0, 1, 3  # 3 = the queue's PAUSED exit: it parks the row and re-launches it when Darkbloom serves again
WALL_GRACE = 60  # runner kill timer = WALL_BACKSTOP x mini's wall cap + this
WALL_BACKSTOP = 4  # mini's wall cap counts only the time the provider was serving; a run still alive at 4x the cap is hung
_OUTAGE, _GRACE = [0.0], [120]  # seconds the provider was unavailable (reported by the model client), and the SIGALRM grace
DEFAULT_MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
READ_HINT = ("Keep observations small: find code with `grep -n`, then read it by line range, at most 60 lines per read "
             "(`nl -ba FILE | sed -n '120,179p'`). Never cat a whole large file, and pipe long command output through tail.\n\n")


# ---- process control: children, secrets, deadlines --------------------------------------------
# Every child (mini, test/suite commands) runs in its own session, so the queue's SIGTERM to OUR process
# group would orphan them. Track their pgids; on SIGTERM/deadline kill them, delete the key file, report, exit.
_CHILDREN, _SECRET_FILES, _DEADLINE = set(), set(), [None]


def _kill_children():
    for sig in (signal.SIGTERM, signal.SIGKILL):
        live = []
        for pg in list(_CHILDREN):
            try:
                os.killpg(pg, sig)
                live.append(pg)
            except OSError:
                _CHILDREN.discard(pg)
        if not live or sig == signal.SIGKILL:
            break
        for _ in range(30):  # up to 3 s for a graceful exit before SIGKILL
            time.sleep(0.1)
            if not any(_pg_alive(pg) for pg in live):
                break


def _pg_alive(pg):
    try:
        os.killpg(pg, 0)
        return True
    except OSError:
        return False


def _drop_secrets():
    for p in list(_SECRET_FILES):
        shutil.rmtree(p, ignore_errors=True)
        _SECRET_FILES.discard(p)


_SCRATCH_DIRS = set()  # throwaway copies of the work dir (the background baselines' copy): removed on every exit path


def _drop_scratch():
    for p in list(_SCRATCH_DIRS):
        subprocess.run(["chmod", "-R", "u+w", str(p)], capture_output=True)
        shutil.rmtree(p, ignore_errors=True)
        _SCRATCH_DIRS.discard(p)


def _abort(why):
    """Signal/deadline exit: kill the child trees, drop the key file, one RESULT line, drop scratch copies. Restores nothing in the work dir."""
    _kill_children()
    _drop_secrets()
    try:
        print("RESULT " + tag_result(f"FAIL {why}"), flush=True)
        _drop_scratch()
    finally:
        os._exit(RC_FAIL)


class ProviderDown(Exception):
    """A mini run ended because the provider stayed unavailable (exit_status ProviderUnavailable): not the model's failure."""


class ModelGone(Exception):
    """A mini run ended because the endpoint kept answering without listing the run's model on /models (exit_status
    ModelNotServed, mini_ctx MINI_MODEL_ABSENT_MAX_S): infrastructure, failed fast instead of waiting days for it to return."""


def _now():
    """time.time() minus the provider outage so far: budgets and deadlines measure the time Darkbloom was serving."""
    return time.time() - _OUTAGE[0]


def note_outage(delta):
    """The provider was unavailable for `delta` more seconds: give the total-deadline alarm that time back."""
    _OUTAGE[0] += delta
    if _DEADLINE[0]:
        _DEADLINE[0] += delta
        signal.alarm(max(1, int(_DEADLINE[0] - time.time() + _GRACE[0])))


def install_signal_handlers(total=None, grace=120):
    _GRACE[0] = grace
    signal.signal(signal.SIGTERM, lambda *_: _abort("preempted"))
    signal.signal(signal.SIGINT, lambda *_: _abort("preempted"))
    if total:
        _DEADLINE[0] = time.time() + total
        signal.signal(signal.SIGALRM, lambda *_: _abort(f"timeout-total {int(total)}s"))
        signal.alarm(int(total + grace))  # backstop; phases and commands are also capped by remaining()


def remaining(cap=None):
    left = (_DEADLINE[0] - time.time()) if _DEADLINE[0] else None
    vals = [v for v in (cap, left) if v is not None]
    return min(vals) if vals else None


def _popen(argv, **kw):
    p = subprocess.Popen(argv, start_new_session=True, **kw)
    _CHILDREN.add(p.pid)
    return p


# The agent's shell commands inherit mini's environment, so the endpoint key must never be in it. mini is
# started through this launcher instead: it reads the key from a 0600 file (in a 0700 temp dir) and deletes
# it, sets litellm.api_key (litellm's OpenAI path falls back to it when no api_key kwarg/env is given), drops
# the variables, then runs the real `mini` script in-process. Nothing key-bearing reaches config or traj.
LAUNCH = r'''import os, runpy, sys, types
p = os.environ.pop("MINI_KEY_FILE", None); k = ""
os.environ.pop("OPENAI_API_KEY", None)
if p:
    try:
        with open(p) as fh: k = fh.read().strip()
    finally:
        try: os.unlink(p)
        except OSError: pass
try:
    import litellm
except ImportError:  # self-test stub runs without litellm installed
    litellm = sys.modules["litellm"] = types.ModuleType("litellm")
litellm.api_key = k or None
sys.path.insert(0, @MINI_EXT@)  # mini_ext (CtxLitellmModel); sys.path, not an env var: the agent's shell inherits env
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def launch_argv(argv):
    """[interpreter of the mini script, -c LAUNCH, mini, args...]."""
    try:
        first = Path(argv[0]).read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        first = ""
    interp = shlex.split(first[2:]) if first.startswith("#!") else [sys.executable]
    return [*interp, "-c", LAUNCH.replace("@MINI_EXT@", repr(str(MINI_EXT))), *argv]


def write_key_file(key):
    d = tempfile.mkdtemp(prefix="mini-k-")  # 0700
    _SECRET_FILES.add(d)
    p = Path(d, "k")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return d, str(p)


def no_think_for(spec, phase):
    """Spec key `no_think` (default off): True/False for every call, or {"A": bool, "B": bool, "single": bool} per phase
    (A = test writing, B = mini fix loop, single = single-shot repair). Missing phase = off."""
    v = spec.get("no_think", False)
    return bool(v.get(phase, False)) if isinstance(v, dict) else bool(v)


NO_THINK_CFG = "model.model_kwargs.extra_body.chat_template_kwargs.enable_thinking=false"  # Darkbloom/vllm-mlx honours it (3 vs 126 completion tokens, probed 2026-10-16)


def build_argv(model, task_text, traj, cwd=None, steps=None, wall=None, no_think=False, overlay=None):
    """overlay: an extra config merged over OVERLAY (later -c wins), e.g. DIAG_OVERLAY; skipped when the file is missing."""
    extra = ["-c", str(overlay)] if overlay and Path(overlay).is_file() else []
    pin = ["-c", f"environment.cwd={Path(cwd).resolve()}"] if cwd else []  # upstream environment config: commands start here
    lim = ["-c", f"agent.step_limit={int(steps)}"] if steps else []
    lim += ["-c", f"agent.wall_time_limit_seconds={int(wall)}"] if wall else []  # mini stops itself at the cap: exit_status TimeExceeded (a LimitsExceeded subclass; the step cap gives plain LimitsExceeded)
    lim += ["-c", NO_THINK_CFG] if no_think else []  # overrides the overlay's enable_thinking: true for this run only
    return [MINI, "-m", f"openai/{model}", "-c", "mini.yaml", "-c", str(OVERLAY), *extra, *pin, *lim,
            "--exit-immediately", "-o", str(traj), "-t", task_text]


def untracked_violations(status_lines, extra_ok=()):
    bad = []
    for ln in status_lines:
        if ln.startswith("??") and ln[3:].strip().rstrip("/") not in extra_ok and not any(k in ln for k in IGNORE_UNTRACKED):
            bad.append(ln[3:])
    return bad


def untracked_files(cwd):
    return {ln[3:].strip() for ln in git(cwd, "status", "--short", "--untracked-files=all").splitlines() if ln.startswith("??")}


def clean_new_untracked(cwd, before, keep=()):
    """Delete untracked files that are NEW since `before` (the attempt-start set) and not in `keep` (allowed files, the test) or IGNORE_UNTRACKED.
    Pre-existing untracked files are never touched. Returns the removed paths."""
    gone = []
    for f in sorted(untracked_files(cwd) - set(before)):
        if f in keep or any(k in f for k in IGNORE_UNTRACKED):
            continue
        p = Path(cwd, f)
        if p.is_file() or p.is_symlink():
            p.unlink()
            gone.append(f)
    return gone


def scope_violations(changed, allowed):
    return [f for f in changed if f not in allowed and f != "refimpl.py"]  # runner deletes refimpl itself


CLAIM_RE = re.compile(r"\b(exists?|existing|already|imports?|imported)\b", re.I)
NEGATE_RE = re.compile(r"(does not|doesn't|not yet|no longer) exist|\b(create|creates|new file|add|adds)\b", re.I)
PATH_RE = re.compile(r"`([\w./-]+/[\w.-]+\.\w+)`")
IMPORT_RE = re.compile(r"from\s+(\.*)(\w[\w.]*)\s+import")  # `from .. import x` names no module: no match


def preflight_missing(cwd, text):
    """Files/modules TASK.md claims exist (backticked path, or 'from ..mod import') but are absent in cwd."""
    root, missing = Path(cwd), []
    for ln in text.splitlines():
        if not CLAIM_RE.search(ln) or NEGATE_RE.search(ln):
            continue
        for p in PATH_RE.findall(ln):
            # `./x.ts` / `../lib/x.ts` is a module specifier relative to some file, not a repo-root path: it exists when that
            # tail is somewhere in the tree (a bfmr spec quoting `./bfmrJoin.ts` from lib/ was refused as missing)
            tail = re.sub(r"^(\.\.?/)+", "", p)
            found = (root / p).exists() if tail == p else any(
                h for h in root.rglob(Path(tail).name) if h.as_posix().endswith("/" + tail) and ".git" not in h.parts)
            if not found and p not in missing:
                missing.append(p)
        for dots, mod in IMPORT_RE.findall(ln):
            leaf = mod.strip(".").split(".")[-1]
            if not dots or not leaf.isidentifier():
                continue  # absolute imports may be third-party; an empty leaf made rglob("/__init__.py") raise (isort r2)
            if not any(root.rglob(leaf + ".py")) and not any(root.rglob(leaf + "/__init__.py")) and leaf not in missing:
                missing.append(leaf)
    return missing


def traj_usage(traj):
    """[(created, prompt_tokens, completion_tokens)] per assistant reply in the (incrementally written) trajectory."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return []
    out = []
    for m in msgs:
        r = (m.get("extra") or {}).get("response") or {}
        u = r.get("usage") or {}
        if m.get("role") == "assistant" and r.get("created") and u.get("completion_tokens") is not None:
            out.append((r["created"], u.get("prompt_tokens") or 0, u["completion_tokens"]))
    return out


def tok_report(rows, start, seen, base=0):
    """Lines in the worker's '(X tok/s' format so the dashboard's tok/s parser (`([\\d.]+) tok/s`) picks them up. X is the
    WALL rate: completion tokens / seconds since the previous reply, i.e. it includes prompt prefill and tool time, not decode speed
    (measured: ~75% of a step is prefill, 1.02 s per 1k prompt tokens vs 14.4 s per 1k completion tokens). The line says so."""
    lines, prev = [], (rows[seen - 1][0] if seen else start)
    for i in range(seen, len(rows)):
        created, pt, ct = rows[i]
        lines.append(f"[mini-runner] step {base + i + 1}: {pt} in / {ct} out ({ct / max(created - prev, 1):.1f} tok/s wall incl. prefill+tools, {max(created - prev, 1):.0f}s since prev reply)")
        prev = created
    return lines


GIT_TIMEOUT_S = 60  # one git command; a healthy one here takes well under a second
GIT_TRIES = 3


class GitStall(RuntimeError):
    """A git command did not return in GIT_TIMEOUT_S on any of GIT_TRIES tries. Seen 2026-11-02: the run's first
    `git rev-parse HEAD` blocked for good in open() of the worktree's gitdir file (a repo under ~/Desktop) while a
    fresh git on the same repo answered at once. The job's live log stopped at the budgets line until the 40-minute
    alarm. Infrastructure, not the model's failure."""


_RUN_CTX = {}   # cwd / task_file of this run, for incident records


def _incident(kind, **fields):
    """Best-effort entry in the shared infra incident log (infra_incidents.py beside this file; summary with
    `infra_incidents.py summary`). Never raises and never blocks on a protected path: the log is under
    ~/.ollama-dispatch."""
    try:
        import importlib.util as _iu
        sp = _iu.spec_from_file_location("infra_incidents", str(Path(__file__).resolve().parent / "infra_incidents.py"))
        m = _iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        m.record(kind, "runner", **{**_RUN_CTX, **fields})
    except Exception:
        pass


def _scratch_incidents(fn):
    """Run a self-test with the incident log on a scratch file (stage 24, 2026-11-06). mini-submit's C0 preflight runs --self-test
    on the Mac with the real HOME, and the model-gone fixture's child runner recorded its `model-not-served` in the live
    ~/.ollama-dispatch/infra-incidents.jsonl as if a job had met it. infra_incidents.py honours INFRA_INCIDENTS_PATH and every
    self-test child gets {**os.environ, ...}, so one setting here covers this process and its children."""
    import tempfile as _tf
    prev = os.environ.get("INFRA_INCIDENTS_PATH")
    with _tf.TemporaryDirectory(prefix="mini-selftest-incidents-") as d:
        os.environ["INFRA_INCIDENTS_PATH"] = str(Path(d, "infra-incidents.jsonl"))
        try:
            return fn()
        finally:
            if prev is None:
                os.environ.pop("INFRA_INCIDENTS_PATH", None)
            else:
                os.environ["INFRA_INCIDENTS_PATH"] = prev


def run_git(cwd, *args, check=False):
    """subprocess.run(["git", *args], capture_output=True, text=True) with a ceiling: a git that does not answer in
    GIT_TIMEOUT_S is killed and run again (a fresh git answered at once in the 2026-11-02 stall), up to GIT_TRIES times; then
    GitStall. Each retry is noted in the live log so a stall is visible while it happens."""
    argv = ["git", *args]
    for n in range(1, GIT_TRIES + 1):
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=GIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # still inside the kernel call: the kill lands when it returns; never wait on it again
            print(f"[mini-runner] `{' '.join(argv)}` in {cwd} did not answer in {GIT_TIMEOUT_S}s (try {n}/{GIT_TRIES})"
                  + ("; running it again" if n < GIT_TRIES else ""), flush=True)
            _incident("git-timeout", cmd=" ".join(argv[:4]), path=str(cwd), attempt=n, of=GIT_TRIES, timeout_s=GIT_TIMEOUT_S)
            continue
        if check and p.returncode:
            raise subprocess.CalledProcessError(p.returncode, argv, out, err)
        return subprocess.CompletedProcess(argv, p.returncode, out, err)
    raise GitStall(f"`{' '.join(argv)}` in {cwd} did not answer in {GIT_TIMEOUT_S}s on {GIT_TRIES} tries")


def git(cwd, *args):
    return run_git(cwd, *args).stdout


def is_bytecode(ln):
    return ln.endswith((".pyc", ".pyo")) or "/__pycache__/" in "/" + ln


def changed_files(cwd, *extra):
    """git diff --name-only minus compiled bytecode (a tracked __pycache__/*.pyc is not the agent's change)."""
    return [ln for ln in git(cwd, "diff", "--name-only", *extra).splitlines() if ln and not is_bytecode(ln)]


def revert_bytecode(cwd):
    pyc = [ln for ln in git(cwd, "diff", "--name-only").splitlines() if ln and is_bytecode(ln)]
    if pyc:
        run_git(cwd, "checkout", "--", *pyc)


def snapshot(cwd):
    """{path: bytes|None} of every tracked file that differs from HEAD (None = deleted): the tree a chain step starts from."""
    return {c: (Path(cwd, c).read_bytes() if Path(cwd, c).is_file() else None) for c in changed_files(cwd)}


def step_changed(cwd, spec):
    """changed_files() relative to where this step started (earlier steps' edits are not this step's changes)."""
    ch = changed_files(cwd)
    if spec.get("_task_written"):
        ch = [c for c in ch if c != "TASK.md"]
    sb = spec.get("_step_base")
    if not sb:
        return ch
    out = [c for c in ch if not (c in sb and (Path(cwd, c).read_bytes() if Path(cwd, c).is_file() else None) == sb[c])]
    return out + [c for c in sb if c not in ch and c != "TASK.md"]  # reverted to HEAD, which differs from the step base


def restore_paths(cwd, spec, paths):
    """Undo stray edits: back to the step's starting content (a chain step's base) or HEAD."""
    sb = spec.get("_step_base") or {}
    for c in paths:
        if c in sb:
            if sb[c] is None:
                Path(cwd, c).unlink(missing_ok=True)
            else:
                Path(cwd, c).write_bytes(sb[c])
        else:
            run_git(cwd, "checkout", "--", c)


def detach_git(cwd, src):
    """A copied worktree's .git is a `gitdir:` pointer, so reset/checkout in the copy would rewrite the SOURCE
    worktree's index. Give the copy its own repo: same HEAD, objects borrowed read-only via alternates."""
    rh, rc = run_git(src, "rev-parse", "HEAD"), run_git(src, "rev-parse", "--git-common-dir")
    head, common = rh.stdout.strip(), rc.stdout.strip()
    if not head or not common:  # an empty answer would leave a .git with a blank HEAD and fail later, far from the cause
        bad = rh if not head else rc
        raise GitStall(f"`{' '.join(bad.args)}` in {src} gave no answer (exit {bad.returncode}: {(bad.stderr or '').strip()[:200]})")
    objs = (Path(src) / common).resolve() / "objects"
    (Path(cwd) / ".git").unlink()
    run_git(cwd, "init", "-q", check=True)
    Path(cwd, ".git/objects/info/alternates").write_text(f"{objs}\n")
    Path(cwd, ".git/HEAD").write_text(f"{head}\n")
    run_git(cwd, "read-tree", head, check=True)


def cwd_guard(cwd, spec, task_file):
    """Message if cwd equals/contains the task file, spec file or baseline_from (prepare() wipes cwd), else None."""
    cr = Path(os.path.expanduser(str(cwd))).resolve()
    cands = []
    t = spec.get("task")
    if isinstance(t, str) and Path(os.path.expanduser(t)).is_absolute():
        cands.append(("task file", t))
    if task_file:
        cands.append(("spec file", task_file))
    if spec.get("baseline_from"):
        cands.append(("baseline_from", spec["baseline_from"]))
    for label, p in cands:
        pr = Path(os.path.expanduser(str(p))).resolve()
        if pr == cr or cr in pr.parents:
            return (f"cwd {cr} contains the {label} {pr}; the runner rebuilds cwd, "
                    "so use a separate empty work dir")
    bf = spec.get("baseline_from")
    if bf:  # cwd INSIDE baseline_from: prepare() would copy the tree into itself / clobber the source
        br = Path(os.path.expanduser(str(bf))).resolve()
        if br in cr.parents:
            return (f"cwd {cr} is inside the baseline_from {br}; the runner rebuilds cwd, "
                    "so use a separate empty work dir outside it")
    return None


def finish_guard(cwd):
    """Message if `mini-finish` holds this run dir (<run>/finish/FINISH.json): Claude is finishing (or finished) a failed job IN PLACE
    in cwd, and prepare() would wipe that work. None otherwise. Only a real run is guarded: --check-only builds in its own dir."""
    fdir = Path(os.path.expanduser(str(cwd))).resolve().parent / "finish"
    if not (fdir / "FINISH.json").is_file():
        return None
    try:
        jid = json.loads((fdir / "FINISH.json").read_text()).get("job") or "?"
    except (OSError, ValueError, AttributeError):
        jid = "?"
    what = ("was finished in place (FINISHED.json): land or move that work first" if (fdir / "FINISHED.json").is_file()
            else "is being finished in place by Claude (mini-finish)")
    return (f"finish-claimed: job {jid} {what} in {cwd}; a run here would wipe it. Move {fdir} aside only if that work is "
            f"no longer wanted")


SETUP_HEARTBEAT_S = 60  # a live-log line this often while the work dir is being prepared
SETUP_LIMIT_S = 900     # past this, setup is stuck on the disk: an INFRA exit, not a model timeout (the budget clock runs)
SETUP_SLOW_S = 120      # a setup that finished but took this long is logged as an incident (healthy: a few seconds)


def _setup_watch(what, t0, done):
    """prepare()'s watchdog thread. A copy blocked in a stalled open() never returns and no exception reaches it, so
    the thread itself ends the run (_abort exits the whole process); until then the live log shows where setup is."""
    while not done.wait(SETUP_HEARTBEAT_S):
        el = time.time() - t0
        if el >= SETUP_LIMIT_S:
            print(f"[mini-runner] {what}: no progress in {el:.0f}s; the disk is not answering (infrastructure, not the model)", flush=True)
            _incident("disk-stall", what=what, seconds=round(el))
            _abort(f"disk-stall {what} did not finish in {SETUP_LIMIT_S}s")
        print(f"[mini-runner] still {what} ({el:.0f}s)", flush=True)


def prepare(cwd, spec):
    """Reset cwd to the spec's baseline (copy + its own git when baseline_from is set). Setup is visible in the live log:
    a start line, a heartbeat every SETUP_HEARTBEAT_S and a done line; git calls are capped (run_git) and the whole of it
    by SETUP_LIMIT_S."""
    src = spec.get("baseline_from")
    what = f"preparing the work dir from {src}" if src else "resetting the work dir"
    t0, done = time.time(), threading.Event()
    print(f"[mini-runner] {what}", flush=True)
    threading.Thread(target=_setup_watch, args=(what, t0, done), daemon=True).start()
    try:
        _prepare(cwd, spec)
    finally:
        done.set()
    el = time.time() - t0
    print(f"[mini-runner] work dir ready ({el:.0f}s)", flush=True)
    if el >= SETUP_SLOW_S:
        _incident("setup-slow", what=what, seconds=round(el))


def _prepare(cwd, spec):
    src = spec.get("baseline_from")
    if src:
        # scratch dir: wipe leftovers from earlier runs (read-only files, existing symlinks) so copytree starts clean
        subprocess.run(["chmod", "-R", "u+w", str(cwd)], capture_output=True)
        for child in Path(cwd).iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        shutil.copytree(src, cwd, dirs_exist_ok=True, symlinks=True,
                        ignore=shutil.ignore_patterns(".venv", "__pycache__"))
        link = Path(cwd) / ".venv"
        if not link.exists() and (Path(src) / ".venv").exists():
            link.symlink_to(Path(src) / ".venv")
        if (Path(cwd) / ".git").is_file():
            detach_git(cwd, src)
    keep = list(spec.get("keep", [])) + ["TASK.md", "verify.sh", "verify.test.ts", "test_fixture.py"]
    # untracked baseline files (verify helpers like check_literals.py, dispatch-env.ts) must survive `git clean`
    for ln in git(cwd, "ls-files", "--others", "--exclude-standard").splitlines():
        top = ln.split("/")[0]
        if top not in keep and top not in ("refimpl.py", "ANSWER.md", ".venv", "node_modules"):
            keep.append(top)
    stash = Path(cwd).parent / (Path(cwd).name + ".keep")
    shutil.rmtree(stash, ignore_errors=True)
    for k in keep:  # oracle files may be tracked-and-modified; checkout would revert them
        src_k = Path(cwd) / k
        if src_k.is_dir():
            shutil.copytree(src_k, stash / k, symlinks=True)
        elif src_k.exists():
            (stash / k).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_k, stash / k)
    run_git(cwd, "reset", "-q")
    run_git(cwd, "checkout", "--", ".")
    run_git(cwd, "clean", "-fdq", "-e", ".venv", "-e", "node_modules", *sum((["-e", k] for k in keep), []))
    if stash.exists():
        subprocess.run(["chmod", "-R", "u+w", str(cwd), str(stash)], capture_output=True)  # kept files may be read-only
        shutil.copytree(stash, cwd, dirs_exist_ok=True, symlinks=True)
        shutil.rmtree(stash, ignore_errors=True)
    for junk in ("refimpl.py", "ANSWER.md", "AUTO-TASK.md", "auto-harness-check.py"):  # answer key + harness-authoring leftovers
        jp = Path(cwd) / junk
        if jp.exists():
            jp.unlink()
    tp = Path(os.path.expanduser(spec.get("task", "TASK.md")))
    if tp.is_absolute():  # job-local TASK.md (e.g. next to spec.json) replaces the baseline's
        dst = Path(cwd) / "TASK.md"
        if dst.exists():
            dst.chmod(0o644)
        shutil.copy2(tp, dst)
        dst.chmod(0o644)
        ans = answers_path(spec)  # answers to earlier needs_info questions (mini-answer): the same frozen test, one more fact
        if ans is not None and ans.is_file() and ans.read_text(errors="replace").strip():
            with open(dst, "a") as fh:
                fh.write("\n\n## Answers to your earlier questions\n\n" + re.sub(r"<!--.*?-->\s*", "", ans.read_text(errors="replace"), flags=re.S).strip() + "\n")
    (Path(cwd) / "NEEDS.md").unlink(missing_ok=True)  # a rerun starts clean
    (Path(cwd).parent / (Path(cwd).name + ".needs-info.json")).unlink(missing_ok=True)


def verify_sound(cwd, spec):
    """Prove the oracle before spending model time: verify must FAIL on the baseline and PASS with the reference impl."""
    vcmd = ["bash", spec.get("verify", "verify.sh")]
    base = subprocess.run(vcmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    if base.returncode == 0:
        return "verify passes on the untouched baseline (no evidence of the bug)"
    ref = Path(spec.get("baseline_from", "")) / "refimpl.py"
    if not ref.exists():
        return None if spec.get("no_ref_ok") else "no refimpl.py in baseline: cannot prove the task is satisfiable (spec no_ref_ok to skip)"
    tmp = Path(tempfile.mkdtemp(prefix="mini-sound-"))
    try:
        work = tmp / "w"
        shutil.copytree(cwd, work, symlinks=True)
        shutil.copy2(ref, work / "refimpl.py")
        r = subprocess.run(["python3", "refimpl.py", str(work)], cwd=work, capture_output=True, text=True)
        if r.returncode != 0:
            return "refimpl.py failed to apply: " + r.stderr.strip()[-200:]
        v = subprocess.run(vcmd, cwd=work, capture_output=True, text=True, timeout=600)
        if v.returncode != 0:
            fails = [ln.strip() for ln in (v.stdout + v.stderr).splitlines() if "FAIL" in ln or "not ok" in ln][:6]
            return "verify FAILS with the reference impl applied (broken oracle): " + " | ".join(fails)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- mandatory pre-run check ------------------------------------------------------------------

SHELL_SKIP = {"cd", "export", "set", "true", "false", "test", "[", "exit", "echo", "source", ".", "pushd", "popd", "unset", "wait", ":"}
WRAPPERS = {"timeout", "env", "time", "nice", "nohup", "command"}
RUNTIMES = {"node", "npm", "npx", "python", "python3", "pytest", "tsc", "tsx", "bun", "deno"}


SHELL_KW = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "in", "select", "function", "time", "{", "}", "(", ")", "!", "[["}
KW_LEAD = {"then", "else", "elif", "do", "if", "while", "until", "!"}  # keywords that are followed by a real command on the same segment
_UNRESOLVED = re.compile(r"[$`*?]")


def cmd_heads(cmd):
    """[(dir, argv)] for each simple command of a shell string (dir = where it runs, tracking `cd`);
    a `cd X` yields (dir, ['cd', X]) with dir already moved. Shell syntax (keywords, `!`, subshell parens, braces)
    is skipped; a head or cd target that cannot be resolved statically ($VAR, $(...), globs) is yielded with
    argv[0] == '?' so the caller can WARN instead of FAIL. After an unresolvable cd, later dirs are '?'-prefixed (unknown)."""
    out, cur = [], "."
    for seg in re.split(r"&&|\|\||;|\n|\|", cmd or ""):
        seg = seg.strip()
        while seg and seg[0] in "({!" and (len(seg) == 1 or seg[0] != "!" or seg[1] == " "):
            seg = seg[1:].lstrip()
        seg = seg.rstrip(") }").rstrip() if seg.count("(") < seg.count(")") or seg.endswith("}") else seg
        try:
            w = shlex.split(seg)
        except ValueError:
            continue
        i = 0
        while i < len(w) and (w[i] in KW_LEAD or re.match(r"[A-Za-z_]\w*=", w[i])):
            i += 1
        if i < len(w) and w[i] in SHELL_KW:
            continue  # for/done/fi/case/esac/in/function ...: structure, not a command
        while i < len(w) and w[i] in WRAPPERS:
            i += 1
            while i < len(w) and (w[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", w[i]) or re.match(r"[A-Za-z_]\w*=", w[i])):
                i += 1
        if i >= len(w):
            continue
        if w[i] == "cd" and len(w) > i + 1:
            if _UNRESOLVED.search(w[i + 1]) or cur is None:
                cur = None
                out.append((None, ["?", "cd " + w[i + 1]]))
            else:
                cur = os.path.normpath(os.path.join(cur, os.path.expanduser(w[i + 1])))
                out.append((cur, ["cd", w[i + 1]]))
        elif w[i] not in SHELL_SKIP:
            if _UNRESOLVED.search(w[i]) or cur is None:
                out.append((cur, ["?", w[i]]))
            else:
                out.append((cur, w[i:]))
    return out


def _defines(text, fn):
    n = re.escape(fn)
    return bool(re.search(
        rf"\b(?:def|function\*?|class)\s+{n}\b|\b(?:const|let|var)\s+{n}\b|\b{n}\s*[:=]\s*(?:async\s*)?(?:function\b|\(|[\w$]+\s*=>)"
        rf"|^\s*(?:(?:public|private|protected|static|async|get|set|export)\s+)*{n}\s*\([^)]*\)\s*(?::[^{{\n]+)?\{{", text, re.M))


def _binds_top(text, name):  # same logic as diag_check.binds_top (copied, not imported)
    """True when a module-level statement assigns `name`: python `NAME = ...` / `NAME: T = ...`, JS `[export] const|let|var NAME =`."""
    n = re.escape(name)
    return bool(re.search(rf"^(?:{n}\s*(?::[^=\n]+)?=(?!=)|(?:export\s+)?(?:const|let|var)\s+{n}\s*(?::[^=\n]+)?=(?!=))", text, re.M))


# ---- function spans (shared with mini-spec) --------------------------------------------------------------------------

JS_KW = {"if", "for", "while", "switch", "catch", "function", "return", "with", "else", "do", "try", "new", "typeof", "await", "super"}

JS_DEFS = [
    re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[(<]", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
               r"(?:function\b|\([^)]*\)\s*(?::[^=\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)", re.M),
    re.compile(r"^[ \t]+(?:(?:public|private|protected|static|async|get|set)\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{\n]+)?\{", re.M),
    re.compile(r"^[ \t]*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)", re.M),
]


_RE_PREV = set("(,=:[!&|?{};") | {""}


def _skip_regex(text, i):
    """Index just past the regex literal starting at text[i] == '/', or None when it is not (or not a closed, one-line) regex."""
    j, n, cls = i + 1, len(text), False
    while j < n and text[j] != "\n":
        c = text[j]
        if c == "\\":
            j += 1
        elif c == "[":
            cls = True
        elif c == "]":
            cls = False
        elif c == "/" and not cls:
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def _js_scan(text, start, strings=True):
    """Index of the `}` closing the function body whose signature contains/ends at `start`, or None. Parens are tracked so braces in
    the parameter list (destructuring, defaults) are not the body; an arrow `=>` followed by a non-brace is an expression body that
    ends at the statement end. strings=False is the fallback: plain brace counting (quotes, regexes and comments not skipped)."""
    n, i = len(text), start
    if text[start:start + 1] == "<":  # generics: <T extends {a: 1}>
        d = 0
        while i < n:
            d += (text[i] == "<") - (text[i] == ">")
            i += 1
            if d == 0:
                break
    paren, prev = 0, ""
    expr = text[max(0, start - 1):start + 1] == "=>"
    if expr:
        i = start + 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i < n and text[i] != "{":
            return _expr_end(text, i)
    depth = 0
    type_depth = 0
    while i < n:
        c = text[i]
        if strings:
            if c in "'\"`":
                q, i = c, i + 1
                while i < n and text[i] != q:
                    i += 2 if text[i] == "\\" else 1
                i += 1
                prev = "x"
                continue
            if text.startswith("//", i):
                i = text.find("\n", i)
                if i < 0:
                    return None
                continue
            if text.startswith("/*", i):
                i = text.find("*/", i)
                if i < 0:
                    return None
                i += 2
                continue
            if c == "/" and (prev in _RE_PREV or prev == ">" and text[max(0, i - 2):i] == "=>"
                             or re.search(r"\b(?:return|typeof|case|in|of|delete|void|throw|else|do)\s*$", text[max(0, i - 12):i])):
                j = _skip_regex(text, i)
                if j:
                    i, prev = j, "x"
                    continue
        if c == "(" and depth == 0:
            paren += 1
        elif c == ")" and depth == 0 and paren:
            paren -= 1
        elif c == "{" and not paren:
            if type_depth or (depth == 0 and prev and prev in "<:|&,"):
                type_depth += 1
            else:
                depth += 1
        elif c == "}" and not paren:
            if type_depth:
                type_depth -= 1
            elif depth:
                depth -= 1
                if depth == 0:
                    return i
        elif c == "=" and text.startswith("=>", i) and depth == 0 and not paren:  # `(x) => expr` after the params: expression body?
            j = i + 2
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] != "{":
                return _expr_end(text, j)
        if not c.isspace():
            prev = c
        i += 1
    return None


def _expr_end(text, i):
    """Index of the last char of the expression starting at text[i]: stops at a depth-0 `;`/`,`/closing bracket, or a newline at
    depth 0 that neither follows nor precedes a continuation operator."""
    n, d, prev = len(text), 0, ""
    while i < n:
        c = text[i]
        if c in "'\"`":
            q = c
            i += 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
            prev = "x"
        elif c in "([{":
            d += 1
            prev = c
        elif c in ")]}":
            if d == 0:
                return i - 1
            d -= 1
            prev = c
        elif d == 0 and c in ";,":
            return i - 1
        elif d == 0 and c == "\n":
            m = re.match(r"\s*(\S)", text[i:])
            if prev not in "=+-*/&|?:,.<>(" and not (m and m.group(1) in ".?:+-*/&|"):
                return i - 1
        elif not c.isspace():
            prev = c
        i += 1
    return n - 1


def _js_end(text, start):
    """1-based line where the function whose signature contains/ends at `start` ends, or None when it cannot be told (callers then show
    a fixed window from the start and say so). Strict scan first, then plain brace counting."""
    for strings in (True, False):
        e = _js_scan(text, start, strings)
        if e is not None:
            return text.count("\n", 0, e) + 1
    return None


def functions(path, text):
    """{name: (first_line, last_line_or_None)} of functions (not classes or plain variables) defined in a file.
    Python: ast (methods included). JS/TS: definition regexes, each confirmed by the runner's own _defines() so a
    candidate is exactly what mini-precheck will accept."""
    out = {}
    if lang_of(path) == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(n.name, (min([n.lineno] + [d.lineno for d in n.decorator_list]), getattr(n, "end_lineno", None)))  # span starts at the first decorator
        return out
    for rx in JS_DEFS:
        for m in rx.finditer(text):
            name = m.group(1)
            if name in JS_KW or name in out or not _defines(text, name):
                continue
            first = text.count("\n", 0, m.start(1)) + 1
            out[name] = (first, _js_end(text, m.end() - 1))
    return dict(sorted(out.items(), key=lambda kv: kv[1][0]))


IMPORT_START = re.compile(r"^\s*(?:import\b|from\s+\S+\s+import\b|(?:const|let|var)\b.*=\s*(?:await\s+)?require\(|require\(|(?:export\s.*)?from\s+['\"]|['\"]use strict['\"])")
SLICE_CAP, SLICE_NEAR = 150, 60


def _import_done(txt):
    """Is the (possibly multi-line) import/require statement in txt complete?"""
    if re.match(r"\s*(?:export\b.*?)?(?:import\b[^'\"]*?)?\s*\bfrom\s*['\"]", txt, re.S) or re.match(r"\s*import\s*['\"]", txt):
        return True
    if txt.count("(") > txt.count(")") or txt.count("{") > txt.count("}") or txt.count("[") > txt.count("]"):
        return False
    first = txt.lstrip()
    if re.match(r"(?:import|export)\b", first) and re.search(r"^\s*(?:import|export)\b.*\{[^}]*$|,\s*$", txt, re.S):
        return False  # `import x,` / `import {` ... still waiting for `from '...'`
    return not txt.rstrip().endswith((",", "=", "(", "{", "[", "+", "||", "&&"))


def _import_lines(lines, before):
    """[(lineno, text)] of the import/require lines (multi-line `import {..} from` included, through the closing line) in lines[:before]."""
    out, i = [], 0
    while i < min(before, len(lines)):
        if IMPORT_START.match(lines[i]):
            j = i
            while j + 1 < len(lines) and not _import_done("\n".join(lines[i:j + 1])) and j - i < 40:
                j += 1
            out += [(k + 1, lines[k]) for k in range(i, j + 1)]
            i = j
        i += 1
    return out


def py_all_defs(text):
    """[(name, qualname, first, last, stub)] of every function and method in python source, nested ones too, in line order.
    qualname joins the enclosing classes and functions with dots (`Outer.method`); first is the first decorator's line; stub:
    the body (after a docstring) is only `pass`, `...` or `raise NotImplementedError`. [] when the text does not parse.
    functions() keeps one span per name; this one keeps them all (stage 26, scrapy2)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []

    def stub(body):
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis)
                   or (isinstance(s, ast.Raise) and s.exc is not None and "NotImplementedError" in ast.unparse(s.exc)) for s in body)

    def walk(stmts, owners):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([s.lineno] + [d.lineno for d in s.decorator_list])
                out.append((s.name, ".".join(owners + [s.name]), first, getattr(s, "end_lineno", None), stub(s.body)))
                walk(s.body, owners + [s.name])
            elif isinstance(s, ast.ClassDef):
                walk(s.body, owners + [s.name])
            else:
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(s, field, None)
                    if isinstance(sub, list):
                        walk(sub, owners)
                for h in list(getattr(s, "handlers", None) or []) + list(getattr(s, "cases", None) or []):
                    walk(h.body, owners)

    walk(tree.body, [])
    return sorted(out, key=lambda d: (d[2], d[1]))


def _def_rank(d, task):
    """Sort key of one py_all_defs row for _slice_targets: the definition whose owners (enclosing classes or functions) TASK.md
    names most often first, then a concrete body before a stub, then the earlier one."""
    owners = d[1].split(".")[:-1]
    hits = max((len(re.findall(rf"(?<!\w){re.escape(o)}(?!\w)", task)) for o in owners), default=0)
    return (-hits, d[4], d[2])


def _slice_targets(f, text, hit, defs, cwd):
    """fn_slice's rows for the target names: [(label, first, last)] and notes. A python name several classes define (scrapy2: `pop`
    in QueueProtocol, ScrapyPriorityQueue and DownloaderAwarePriorityQueue; functions() kept the first, the Protocol stub) shows the
    definition TASK.md names most (by its owner), a concrete body before a stub, plus a runner-up that ties with it on both, each
    labelled with its qualified name; the others are listed by line."""
    alldefs = py_all_defs(text) if lang_of(f) == "py" else []
    try:
        task = Path(cwd, "TASK.md").read_text(errors="replace").replace("\\_", "_")
    except OSError:
        task = ""
    rows, also = [], []
    for fn in hit:
        ds = [d for d in alldefs if d[0] == fn]
        if len(ds) < 2:
            rows.append((fn, defs[fn][0], defs[fn][1]))
            continue
        ranked = sorted(ds, key=lambda d: _def_rank(d, task))
        top = _def_rank(ranked[0], task)[:2]
        pick = [ranked[0]] + [d for d in ranked[1:2] if _def_rank(d, task)[:2] == top]
        rows += [(d[1], d[2], d[3]) for d in sorted(pick, key=lambda d: d[2])]
        rest = [d for d in ds if d not in pick]
        also.append(f"`{fn}` is also defined at: " + ", ".join(f"{d[1]}:{d[2]}-{d[3]}" for d in rest[:12]))
    return rows, also


def _py_class_spans(text, f="x.py"):
    """[(name, first, last)] of every class in python source (first: its first decorator's line), for fn_scope_check: a target_fn
    that names a class (a2a: ClientTransport) covers the class's whole body, the methods the fix adds there included. [] for
    another language or text that does not parse."""
    if lang_of(f) != "py":
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    return [(n.name, min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno) for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and getattr(n, "end_lineno", None)]


def fn_slice(cwd, allowed, target_fn, anchors=()):
    """Prompt text with the part of the code the model needs, read from the CURRENT files (call it again on a retry): per allowed
    file that defines a target function -- its import/require lines, each target function line-numbered (at most 150 lines, or +-60
    around the reach_anchor line when the function is longer), and a one-line outline `name:first-last` of its other functions.
    The model still edits the real file; the slice only saves it the first read."""
    fns, anchors, blocks = as_list(target_fn), as_list(anchors), []
    for f in allowed:
        p = Path(cwd, f)
        if not p.is_file() or lang_of(f) is None:
            continue
        text = p.read_text(errors="replace")
        defs = functions(f, text)
        hit = [fn for fn in fns if fn in defs]
        if not hit:
            continue
        lines = text.splitlines()
        parts = [f"### `{f}` ({len(lines)} lines)"]
        imps = _import_lines(lines, min(defs[hit[0]][0] - 1, 80))
        if imps:
            parts.append("Imports:\n" + "\n".join(f"{n:>5}\t{t}" for n, t in imps))
        rows, also = _slice_targets(f, text, hit, defs, cwd)
        for fn, a, b in rows:
            unknown = b is None  # end not determinable: show a fixed window and say so
            if unknown:
                b = min(len(lines), a + SLICE_NEAR - 1)
            x, y = a, b
            if b - a + 1 > SLICE_CAP and not unknown:
                al = next((n for n in (anchor_line("\n".join(lines[a - 1:b]), lit) for lit in anchors) if n), None)
                if al:
                    mid = a + al - 1
                    x = max(a, mid - SLICE_NEAR)
                    y = min(b, x + 2 * SLICE_NEAR)
                    x = max(a, y - 2 * SLICE_NEAR)
                else:
                    y = a + SLICE_CAP - 1
            parts.append(f"`{fn}`: current lines {x}-{y} shown; edit the real file `{f}`; after an edit view only the changed range."
                         + (f" (the function spans {a}-{b})" if (x, y) != (a, b) else "")
                         + (f" (could not find where the function ends; showing {SLICE_NEAR} lines from its start -- read further in the real file if needed)" if unknown else "") + "\n"
                         + "\n".join(f"{n:>5}\t{lines[n - 1]}" for n in range(x, y + 1)))
        parts += also
        other = [f"{n}:{a}-{b or a}" for n, (a, b) in defs.items() if n not in hit][:40]
        if other:
            parts.append("Other functions in the file (name:first-last): " + ", ".join(other))
        blocks.append("\n\n".join(parts))
    return "--- CURRENT CODE (slice) ---\n" + "\n\n".join(blocks) + "\n--- end slice ---\n\n" if blocks else ""


def _tool_errors(cmd, root, env, label, deep=False, warns=None):
    """Binaries a command string needs: on the PATH the agent will get, and (deep) the first one not exit 127."""
    errs, path, first = [], env.get("PATH", ""), True
    for d, argv in cmd_heads(cmd):
        if argv[0] == "?":  # not statically resolvable ($VAR, $(...), glob, or after such a cd): never a hard failure
            if warns is not None:
                warns.append(f"{label}: `{argv[1]}` cannot be resolved statically; not checked")
            first = False
            continue
        run_dir = Path(root, d)
        if argv[0] == "cd":
            if not run_dir.is_dir():
                errs.append(f"{label}: `cd {argv[1]}` -> directory {run_dir} does not exist")
            continue
        exe = argv[0]
        found = (str(run_dir / exe) if os.access(run_dir / exe, os.X_OK) else None) if "/" in exe else shutil.which(exe, path=path)
        if not found:
            errs.append(f"{label}: `{exe}` not found" + (f" in {run_dir}" if "/" in exe else f" on PATH ({path})"))
            first = False
            continue
        if deep and first and Path(exe).name in RUNTIMES | {"python3.11", "python3.12", "python3.13"}:
            try:
                rc = subprocess.run([found, "--version"], cwd=run_dir if run_dir.is_dir() else root, env=env, capture_output=True, timeout=15).returncode
            except (OSError, subprocess.TimeoutExpired):
                rc = 127
            if rc == 127:
                errs.append(f"{label}: `{exe} --version` exits 127")
        first = False
        if deep and Path(exe).name.startswith("python") and "-m" in argv[1:2]:
            mod = argv[2] if len(argv) > 2 else ""
            if mod:
                r = subprocess.run([found, "-c", "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 3)", mod],
                                   cwd=run_dir if run_dir.is_dir() else root, env=env, capture_output=True)
                if r.returncode != 0:
                    errs.append(f"{label}: `{exe} -m {mod}`: module {mod} is not importable with that interpreter")
    return errs


TESTFILE_RE = re.compile(r"(^|/)test[_-][^/]*$|(_test\.py|\.(test|spec)\.[cm]?[jt]sx?)$|(^|/)(test|tests|__tests__)/")


def precheck(spec, cwd=None, sound=None, later=False):
    """Everything provable about a spec before any model call -> (errors, warnings). cwd = the prepared scratch dir
    (adds oracle soundness and the TASK.md preflight); without it files are read from baseline_from. later=True
    (a chain step after the first, checked before the chain ran): what earlier steps create is only a warning."""
    err, warn = [], []
    src = spec.get("baseline_from")
    root = Path(cwd or (os.path.expanduser(src) if src else "."))
    allowed = list(spec.get("allowed") or [])
    if spec.get("test_file") and spec["test_file"] in allowed:
        err.append(f"test_file {spec['test_file']} is listed in allowed: the frozen test must not be editable (single-shot would also swap it away)")
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
    ut = spec.get("update_tests")
    if ut is not None and not (isinstance(ut, list) and all(isinstance(x, str) for x in ut)):
        err.append("update_tests must be a list of file paths")
    else:
        inrepo = git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
        for f in ut or []:
            if not Path(root, f).is_file():
                err.append(f"update_tests {f} does not exist in {root}")
            elif inrepo and not git(root, "ls-files", "--", f).strip():
                err.append(f"update_tests {f} is not tracked: only existing tests can be updated (a new test goes in test_file)")
            if not TESTFILE_RE.search(f):
                err.append(f"update_tests {f} is not a test file (test_*/test-*, *_test.py, *.test.*, *.spec.*, or under test/tests/__tests__)")
            if f == spec.get("test_file"):
                err.append(f"update_tests {f} is the frozen test_file")
            if f in allowed:
                err.append(f"update_tests {f} is also in allowed: tests the model may edit are never allowed files")
            if Path(f).name not in (spec.get("suite_cmd") or "") and not spec.get("update_tests_strict"):  # strict: update_flip runs each edit
                err.append(f"update_tests {f}: its file name must appear in suite_cmd, so the suite proves the edited test passes")
    if src:
        sp = Path(os.path.expanduser(src))
        if not sp.is_dir() or git(sp, "rev-parse", "--is-inside-work-tree").strip() != "true":
            err.append(f"baseline_from {src} is not a git worktree")
        else:
            behind = git(sp, "rev-list", "--count", "HEAD..origin/main").strip()
            if behind.isdigit() and int(behind) > 0:
                warn.append(f"baseline_from HEAD is {behind} commit(s) behind origin/main")
    fm = spec.get("fix_mode", "mini")
    if fm not in ("mini", "single_shot"):
        err.append(f'fix_mode {fm!r} is not "mini" or "single_shot"')
    for k, lo, hi in (("best_of", 1, 5), ("single_shot_retries", 0, 5)):
        if k in spec and not (isinstance(spec[k], int) and not isinstance(spec[k], bool) and lo <= spec[k] <= hi):
            err.append(f"{k} must be an integer {lo}..{hi}")
    nt = spec.get("no_think", False)
    if not (isinstance(nt, bool) or (isinstance(nt, dict) and set(nt) <= {"A", "B", "single"} and all(isinstance(v, bool) for v in nt.values()))):
        err.append('no_think must be true/false or an object with keys among "A", "B", "single" and true/false values')
    if fm == "single_shot" and len(as_list(spec.get("target_fn"))) != 1:
        warn.append("fix_mode single_shot needs exactly one target_fn; the run will use the mini loop")
    mg = spec.get("mutation_gate")
    if mg not in (None, False, True):
        if not isinstance(mg, dict):
            err.append("mutation_gate must be true/false or an object")
        else:
            bad = set(mg) - {"warn_below", "fail_below", "timeout", "min_mutants", "required", "tool", "cmd", "format", "mutmut_selector", "mutmut_mode", "mutmut_pythonpath"}
            if bad:
                err.append(f"mutation_gate unknown keys {sorted(bad)}")
            for k in ("warn_below", "fail_below"):
                if k in mg and not (isinstance(mg[k], (int, float)) and not isinstance(mg[k], bool) and 0 <= mg[k] <= 1):
                    err.append(f"mutation_gate.{k} must be a number 0..1")
            if mg.get("tool", "auto") not in ("auto", "stryker", "mutmut"):
                err.append('mutation_gate.tool must be "auto", "stryker" or "mutmut"')
            if mg.get("format", "line") not in ("stryker", "mutmut", "line"):
                err.append('mutation_gate.format must be "stryker", "mutmut" or "line"')
            if (mg.get("fail_below") is not None and mg.get("warn_below") is not None and mg["fail_below"] > mg["warn_below"]):
                warn.append("mutation_gate.fail_below is above warn_below")
    if mg and len(as_list(spec.get("target_fn"))) == 0:
        warn.append("mutation_gate needs a target_fn (the mutants are limited to its lines); the gate will be skipped")
    fsm = spec.get("fn_scope", "fail")  # 2026-10-24 gates (G3/G1)
    if fsm not in ("fail", "enforce", "warn", "off"):
        err.append(f'fn_scope {fsm!r} is not "fail", "enforce", "warn" or "off"')
    elif fsm == "warn" and not str(spec.get("fn_scope_reason") or "").strip():
        err.append('fn_scope "warn" needs a spec.fn_scope_reason (why the fix may edit outside target_fn); the default is "fail"')
    scm = spec.get("source_checks", "fail")
    if scm not in ("fail", "allow"):
        err.append(f'source_checks {scm!r} is not "fail" or "allow"')
    elif scm == "allow" and not str(spec.get("source_checks_reason") or "").strip():
        err.append('source_checks "allow" needs a spec.source_checks_reason (why only the code\'s text can show this change, '
                   'e.g. a call wired into a daemon loop no test can drive); the default is "fail"')
    if spec.get("new_names") is not None and not (isinstance(spec["new_names"], list) and all(isinstance(x, str) and x.isidentifier() for x in spec["new_names"])):
        err.append("new_names must be a list of identifiers (the module-level names the TASK adds)")
    if spec.get("diag_lines") is not None and not _line_list(spec["diag_lines"]):
        err.append("diag_lines must be a list of at most 40 line numbers (of the first allowed file, inside target_fn)")
    for e in spec.get("red_ok") or []:
        if not (isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("match"), str) and e["match"].strip()):
            err.append('red_ok entries must be {"type": "<ExceptionName>", "match": "<regex naming the new symbol/path>"}')
        elif e["type"] in HARD_BAD_RED:
            err.append(f"red_ok type {e['type']} is never a right-reason red (the test is broken)")
        else:
            try:
                re.compile(e["match"])
            except re.error as x:
                err.append(f"red_ok match {e['match']!r} is not a regex: {x}")
    if spec.get("baseline_green") is not None and not isinstance(spec["baseline_green"], list):
        err.append("baseline_green must be a list of test names allowed to pass at baseline")
    if spec.get("no_hacks") not in (None, True, False, "off", "on"):
        err.append('no_hacks must be true/"on" (default) or "off"')
    if spec.get("lint") not in (None, True, False, "off", "on"):
        err.append('lint must be true/"on" (default) or "off"')
    tf, tc = spec.get("test_file"), spec.get("test_cmd")
    if tf and not Path(root, tf).parent.is_dir():
        err.append(f"test_file directory {Path(tf).parent} does not exist in {root}")
    fns = spec.get("target_fn") or []
    fns = [fns] if isinstance(fns, str) else list(fns)
    if fns:
        texts = [Path(root, f).read_text(errors="replace") for f in allowed if Path(root, f).is_file()]
        if not texts:
            err.append(f"target_fn {fns} set but none of the allowed files exist: {allowed}")
        else:
            gone = [fn for fn in fns if not any(_defines(t, fn) for t in texts)]
            if gone:
                err.append(f"target_fn {gone} not defined in the allowed files {allowed}")
        for lit in as_list(spec.get("reach_anchor")):
            n = sum(t.count(lit) for t in texts)
            if n != 1:
                err.append(f'reach_anchor "{lit}" {"missing" if n == 0 else f"ambiguous ({n} matches)"} in the allowed files {allowed}')
        bo = in_browser_only(root, allowed, fns)
        if bo:
            err.append(f"target_fn {bo} only run inside page.evaluate(); node coverage cannot reach them. Target the caller instead")
    if tc:
        err += _tool_errors(tc, root, env, "test_cmd", deep=True, warns=warn)
    for k in ("suite_cmd", "typecheck_cmd"):
        if spec.get(k):
            warn += _tool_errors(spec[k], root, env, k, warns=warn)
    if cwd:
        tp = Path(os.path.expanduser(spec.get("task", "TASK.md")))
        tpath = Path(cwd, "TASK.md" if tp.is_absolute() else tp)  # an absolute task is copied in as TASK.md
        if not tpath.is_file() and tp.is_absolute():
            tpath = tp
        if tpath.is_file():
            gone = preflight_missing(cwd, tpath.read_text(errors="replace"))
            if spec.get("phase_a_only"):  # the test is what the model is about to write
                gone = [g for g in gone if g != spec.get("test_file")]
            if gone:
                err.append(f"preflight: TASK.md references missing in baseline: {gone}")
        if sound is None:
            sound = bool(spec.get("verify")) and not spec.get("skip_soundness")
        if sound:
            bad = verify_sound(cwd, spec)
            if bad:
                err.append(f"verify-unsound: {bad}")
    if later:
        warn += [e for e in err if e.startswith("target_fn")]
        err = [e for e in err if not e.startswith("target_fn")]
    return err, warn


def merged_steps(spec):
    """Full two-phase specs for a `steps` chain: the shared keys with each step's overrides on top."""
    shared = {k: v for k, v in spec.items() if k not in ("steps", "mode")}
    return [{**shared, **st, "mode": "two-phase"} for st in (spec.get("steps") or [])]


def is_inline_task(t):
    return "\n" in t or not re.fullmatch(r"[~\w./+-]+", t)


def validate_steps(steps, raw=None):
    """steps = merged specs; raw = the spec's own `steps` list: each raw step must carry its own task (a top-level
    `task` shared by the chain would otherwise hide a step that has none)."""
    bad, seen = [], {}
    if not steps:
        return ["steps must be a non-empty list"]
    for i, r in enumerate(raw or [], 1):
        if i > 1 and (not isinstance(r, dict) or not r.get("task")):  # step 1 may fall back to the shared task / TASK.md
            bad.append(f"step-{i}: needs its own 'task' (path or inline text) in steps[{i - 1}]; a top-level task is not inherited")
    for i, sp in enumerate(steps, 1):
        for k in ("allowed", "test_file", "test_cmd"):
            if not sp.get(k):
                bad.append(f"step-{i}: needs {k!r}")
        if i > 1 and not sp.get("task") and not raw:
            bad.append(f"step-{i}: needs its own 'task' (path or inline text)")
        t = sp.get("task")
        if isinstance(t, str) and not is_inline_task(t) and Path(os.path.expanduser(t)).is_absolute() and not Path(os.path.expanduser(t)).is_file():
            bad.append(f"step-{i}: task file {t} does not exist")
        if sp.get("test_file") in seen:
            bad.append(f"step-{i}: test_file {sp['test_file']} already used by step-{seen[sp['test_file']]}")
        seen.setdefault(sp.get("test_file"), i)
    return bad


def precheck_all(spec, cwd=None):
    """precheck() for a plain spec or every step of a chain (used by bin/mini-precheck)."""
    if not spec.get("steps"):
        return precheck(spec, cwd)
    steps = merged_steps(spec)
    err, warn = [f"bad-spec {b}" for b in validate_steps(steps, spec.get("steps"))], []
    if err:
        return err, warn
    for i, sp in enumerate(steps, 1):
        e, w = precheck(sp, cwd, sound=False, later=i > 1)
        err += [f"step-{i}: {x}" for x in e]
        warn += [f"step-{i}: {x}" for x in w]
    return err, warn


def report_precheck(err, warn):
    for w in warn:
        print(f"[mini-runner] precheck WARN: {w}", flush=True)
    for e in err:
        print(f"[mini-runner] precheck FAIL: {e}", flush=True)
    return "; ".join(err)[:500]


# ---- mini invocation -------------------------------------------------------------------------

# ---- fail-fast: end a hopeless attempt early (called from run_mini's poll thread) --------------------------------------
POLL_SECS = 5
FF_TEST_STEP, FF_FIX_STEP, FF_REPEAT = 30, 15, 3
# Phase B "no allowed file changed" step threshold scales with the allowed file's size: (min lines, step), largest first; FF_FIX_STEP below all.
FF_FIX_SCALE = ((400, 35), (200, 25))
FF_STAGE_GRACE = 10   # a write STAGED toward an allowed file (see staged_write) buys this many steps past the threshold, not forever
FF_STAGE_LINES = 8    # a heredoc / script body this long that writes a file counts as staging even when it does not name an allowed file


def ff_fix_step(texts):
    """Step by which an allowed file must differ from the attempt-start snapshot: 15, 25 if the biggest allowed file is >200 lines, 35 if >400."""
    n = max([len(t.splitlines()) for t in (texts.values() if isinstance(texts, dict) else texts) if t] or [0])
    return next((st for lim, st in FF_FIX_SCALE if n > lim), FF_FIX_STEP)


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", re.S)
_REDIR_RE = re.compile(r"(?:^|[\s\d])>{1,2}[ \t]*(?![&>]|/dev/null)([^\s&|;<>()]+)")
_WRITE_RES = (re.compile(r"\btee\b"), re.compile(r"\b(?:cp|mv|install)\s"), re.compile(r"\bsed\s+(?:-\w+\s+)*-i|\bperl\s+(?:-\w+\s+)*-\w*i"),
              re.compile(r"open\([^)]*,\s*['\"][wax]b?\+?['\"]|write_text\(|write_bytes\(|writeFile(?:Sync)?\(|appendFile(?:Sync)?\("))


def staged_write(command, allowed):
    """True iff this command WRITES a file toward an allowed file: it names an allowed file (or its basename) and writes (redirect, tee, cp/mv,
    sed -i, python/node file writes) -- an in-place edit, a `head/tail ALLOWED > /tmp/x` or `cp ALLOWED ALLOWED.bak` staging step -- or it is a
    heredoc/script body of >= FF_STAGE_LINES lines redirected into a file (a rewrite staged under /tmp). Pure reads (cat, nl, sed -n, grep) are not."""
    c = command or ""
    stripped = _HEREDOC_RE.sub(lambda m: "<<" + m.group(2) + "\n", c)  # redirects inside the body (`=>`, `>`) are code, not shell
    writes = bool(_REDIR_RE.search(stripped)) or any(r.search(c) for r in _WRITE_RES)
    if not writes:
        return False
    if any(f in c or Path(f).name in c for f in allowed):
        return True
    return any(m.group(3).count("\n") + 1 >= FF_STAGE_LINES for m in _HEREDOC_RE.finditer(c)) and bool(_REDIR_RE.search(stripped) or _WRITE_RES[0].search(c))


def traj_steps(traj):
    """[(command, output)] per completed agent step in the (incrementally written) trajectory; [] while it is unreadable."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return []
    outs = {m.get("tool_call_id"): m for m in msgs if m.get("role") == "tool"}
    steps = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for ac in (m.get("extra") or {}).get("actions") or []:
            o = outs.get(ac.get("tool_call_id"))
            if o is not None:
                steps.append((ac.get("command") or "", o.get("content") if isinstance(o.get("content"), str) else json.dumps(o.get("content"))))
    return steps


def _squash(s):
    return " ".join(str(s).split())


_RUNNER_WORDS = {"node", "python", "python3", "pytest", "npm", "npx", "jest", "vitest", "mocha", "go", "cargo", "bash", "sh", "make", "tsx", "bun", "deno", "yarn", "pnpm", "uv", "tox"}


def _cmd_core(c):
    """A command with its `cd X &&` prefix(es) dropped, python3 -> python, `-m` and flags removed: the words that say WHAT ran."""
    c = re.sub(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", _squash(c))
    return [{"python3": "python"}.get(w, w) for w in c.split(" ") if w and w != "-m" and not w.startswith("-")]


def ran_test_cmd(steps, spec):
    """True iff some step looks like it ran the spec's test_cmd: same core words ignoring cd prefix, flags, -m, python/python3, or
    (when the test file's basename is in the command) any known test-runner word. Lenient on purpose -- never a false 'not run'."""
    cmd, tf = _squash(spec.get("test_cmd", "")), Path(spec.get("test_file", "")).name
    want = _cmd_core(cmd)
    for c, _ in steps:
        if cmd and cmd in _squash(c):
            return True
        have = _cmd_core(c)
        if want and all(w in have for w in want):
            return True
        if tf and tf in c and _RUNNER_WORDS & set(have):
            return True
    return False


def failfast_reason(steps, phase, spec, cwd, base=None):
    """Why this attempt should end now, or None. Phase A: the test file must exist (differ from its skeleton) by step 30 (the phase
    cap; accepted past runs wrote as late as step 16 and ran the test as late as step 18, so there is no earlier 'must run' rule).
    Phase B: an allowed file must differ from `base` ({file: text at the START OF THIS ATTEMPT}) by step ff_fix_step(base) (15; 25 if the
    allowed file is >200 lines; 35 if >400); a write staged toward an allowed file (staged_write) counts as progress for FF_STAGE_GRACE
    more steps. Either phase: the same command 3 times in a row with identical output."""
    if len(steps) >= FF_REPEAT:
        last = steps[-FF_REPEAT:]
        if len({(_squash(c), o) for c, o in last}) == 1:
            return f"fail-fast: the same command ran {FF_REPEAT} times in a row with identical output (`{_squash(last[0][0])[:120]}`)"
    if phase == "A" and len(steps) >= FF_TEST_STEP:
        tp = Path(cwd, spec["test_file"])
        skel = (base or {}).get(spec["test_file"])
        if not tp.is_file() or (skel is not None and tp.read_text(errors="replace") == skel):
            return f"fail-fast: no test written to `{spec['test_file']}` after {len(steps)} steps"
    if phase == "B" and base is not None and os.environ.get("MINI_FAILFAST_STEPS") == "1" and len(steps) >= ff_fix_step(base):
        def now(f):
            p = Path(cwd, f)
            return p.read_text(errors="replace") if p.is_file() else None
        if all(now(f) == t for f, t in base.items()):
            staged = any(staged_write(c, list(base)) for c, _ in steps)
            if not staged or len(steps) >= ff_fix_step(base) + FF_STAGE_GRACE:
                return f"killed by fail-fast at step {len(steps)} (no allowed-file edit)" + (" (a staged rewrite was never copied into it)" if staged else "")
    return None


def make_watch(phase, spec, cwd, base=None, test_sha=None):
    """Poll-thread watch. Phase B with test_sha (G6): an edit of the frozen test ends the attempt at once as TEST-SUSPECT."""
    tp = Path(cwd, spec["test_file"]) if test_sha and spec.get("test_file") else None

    def watch(traj):
        if tp is not None:
            try:
                if tp.exists() and sha(tp) != test_sha:
                    return ("TEST-SUSPECT: the frozen test was edited. Never edit the test; if you believe it is wrong, stop and "
                            "say why in your final message (the edit is saved as test-proposal.diff for review)")
            except OSError:
                pass
        return failfast_reason(traj_steps(traj), phase, spec, cwd, base)
    return watch


# ---- draft nudge: a short runner line on the latest observation while the file the phase must write is still unwritten ----------
# 2026-11-05 real-issue bench (sqlmeta r1 and loguru r1 phase A, cfnlint r1 and sqlmeta r2 diagnose): attempts read code until the
# step cap or the fail-fast and never wrote the file their prompt told them to draft early; that budget line sat thousands of tokens
# back by then. The runner only sets MINI_DRAFT_* for one run; mini_ctx (DRAFT NUDGE) checks the file before each model call and
# appends the line to the newest observation of the copy sent to the API. No new rule: the gates and the fail-fasts are unchanged.
# MINI_DRAFT_NUDGE=off in the runner's environment turns it off (A/B runs).
def draft_nudge(path, first, every, cap, say, name=None, mark="", alt=(), heads=(), sha=""):
    """MINI_DRAFT_* settings for one run_mini call; {} when MINI_DRAFT_NUDGE is off. heads: `## <name>` headings the file must
    have to count as written (mini_ctx MINI_DRAFT_HEADS); sha: the sha256 of a rejected version that does not count as written
    while the file still has exactly those bytes (MINI_DRAFT_SHA)."""
    if (os.environ.get("MINI_DRAFT_NUDGE") or "").strip().lower() in ("off", "0", "no", "false"):
        return {}
    d = {"MINI_DRAFT_FILE": str(Path(path).absolute()), "MINI_DRAFT_NAME": name or Path(path).name, "MINI_DRAFT_STEP": str(int(first)),
         "MINI_DRAFT_EVERY": str(int(every)), "MINI_DRAFT_CAP": str(int(cap)), "MINI_DRAFT_SAY": " ".join(str(say).split())}
    if mark:
        d["MINI_DRAFT_MARK"] = mark
    if alt:
        d["MINI_DRAFT_ALT"] = ",".join(str(Path(x).absolute()) for x in alt)
    if heads:
        d["MINI_DRAFT_HEADS"] = "|".join(heads)
    if sha:
        d["MINI_DRAFT_SHA"] = sha
    return d


def test_draft(cwd, spec, tf, skel):
    """Phase A's nudge: from the step the prompt names (max(8, n // 3)) and every n // 5 steps after, until the fail-fast step n."""
    n = min(int(spec.get("test_steps", FF_TEST_STEP)), FF_TEST_STEP)
    say = (f"Write the complete first version of `{tf}` now{' (replace every placeholder with a real test)' if skel else ''}: you do "
           "not need to know how to fix the bug, only what TASK.md expects the code to do. Run it, then improve it from its output. "
           f"The attempt is ended at step {n} if `{tf}` still has no real test in it.")
    return draft_nudge(Path(cwd, tf), max(8, n // 3), max(4, n // 5), n, say, name=tf, mark=SKELETON_MARK if skel else "")


class Usage:
    def __init__(self):
        self.rows, self.secs, self.ctx, self.trip = [], 0, [], None  # trip: why the last run_mini ended early

    def summary(self):
        pt, ct = sum(r[1] for r in self.rows), sum(r[2] for r in self.rows)
        return (f"[worker] USAGE SUMMARY: prompt_tokens={pt} completion_tokens={ct} tok_s={ct / max(self.secs, 1):.1f} "
                f"decode_tok_s=None tok_s_basis=wall-incl-prefill calls={len(self.rows)}")


def run_mini(model, task_text, traj, cwd, env, key, budget, usage, steps=None, tag="", wall=None, watch=None, no_think=False, overlay=None,
             draft=None):
    """One fresh-context mini run. Returns (log, timed_out). Kills the whole process group at the budget (or wall+60, the backstop
    for mini's own wall_time_limit_seconds).
    watch(traj) -> reason|None is called from the poll thread; a reason kills the run early and lands in usage.trip.
    The key goes to the launcher through a 0600 file, never through the environment the agent's commands inherit.
    draft: this run's MINI_DRAFT_* settings (draft_nudge); any MINI_DRAFT_* inherited from the caller's environment is dropped."""
    Path(traj).unlink(missing_ok=True)
    usage.trip = None
    budget = remaining(budget)
    if budget is not None and budget <= 1:
        print(f"[mini-runner] no time left for run {tag or ''}".rstrip(), flush=True)
        return "", True
    env = {k: v for k, v in env.items() if k not in ("OPENAI_API_KEY", "MINI_KEY_FILE") and not k.startswith("MINI_DRAFT_")}
    env.update(draft or {})
    env["DISPATCH_VERIFY_SANDBOX"] = "1"  # the agent's shell: ollama-queue.py refuses every mutating subcommand (no real jobs from a run)
    kdir = None
    if key:
        kdir, env["MINI_KEY_FILE"] = write_key_file(key)
    proc = _popen(launch_argv(build_argv(model, task_text, traj, cwd, steps, wall, no_think, overlay)), cwd=cwd, env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
    fired = threading.Event()

    def kill():
        fired.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    t0, stop, base, chunks = int(time.time()), threading.Event(), len(usage.rows), []
    out_s, timer, outage_seen = [0.0], [None], [0.0]

    def arm():  # kill at the budget, or at WALL_BACKSTOP x the wall cap: both extended by the time the provider was down
        lim = min(budget, WALL_BACKSTOP * wall + WALL_GRACE) if wall else budget
        timer[0] = threading.Timer(max(1, lim + out_s[0] - (time.time() - t0)), kill)
        timer[0].start()

    arm()

    def poll():
        seen = 0
        while not stop.wait(POLL_SECS):
            rows = traj_usage(traj)
            for ln in tok_report(rows, t0, seen, base):
                print(ln, flush=True)
            seen = len(rows)
            try:
                why = watch(traj) if watch else None
            except Exception as e:  # a watcher bug must never take the run down
                why = None
                print(f"[mini-runner] watch error: {e}", flush=True)
            if why and usage.trip is None:
                usage.trip = why
                print(f"[mini-runner] {tag or 'run'} ended early: {why}", flush=True)
                kill()
                return

    threading.Thread(target=poll, daemon=True).start()
    for line in proc.stdout:  # stream so the queue's live log shows the agent working
        chunks.append(line)
        c = CREDIT_LINE.search(line)
        m = None if c else OUTAGE_LINE.search(line)
        if c or m:
            if c:  # "credited Ns": what mini_ctx took off its own clock for this wait, each second once
                delta = float(c.group(1))
            else:  # an older mini_ctx: "outage so far Ns" counts up within one episode and restarts after a success
                so_far = float(m.group(1))
                delta = so_far - outage_seen[0] if so_far >= outage_seen[0] else so_far
                outage_seen[0] = so_far
            delta = credit_cap(delta, time.time() - t0, out_s[0])
            if delta > 0:
                out_s[0] += delta
                note_outage(delta)
                timer[0].cancel()
                arm()
        print(line.replace(key, "<key>") if key else line, end="", flush=True)
    proc.wait()
    timer[0].cancel()
    stop.set()
    if out_s[0]:
        print(f"[mini-runner] {tag or 'run'}: provider unavailable {int(out_s[0])}s (excluded from the wall/total budgets)", flush=True)
    _CHILDREN.discard(proc.pid)
    if kdir:
        shutil.rmtree(kdir, ignore_errors=True)
        _SECRET_FILES.discard(kdir)
    ex = exit_status(traj)
    if ex == "ProviderUnavailable":
        raise ProviderDown(f"{tag or 'run'}: the provider stayed unavailable")
    if ex == "ModelNotServed":
        raise ModelGone(f"{tag or 'run'}: the endpoint stopped listing the model on /models")
    if usage.trip is None and ex in LIMIT_WHY:
        usage.trip = f"limit: the previous attempt ended because {LIMIT_WHY[ex]} ({ex}); be faster and more direct"
    rows = traj_usage(traj)
    usage.rows += rows
    usage.secs += max((rows[-1][0] - t0) if rows else 0, 0)
    if tag:
        peak = max((r[1] for r in rows), default=0)
        line = f"{tag}: steps={len(rows)} peak_prompt={peak} last_prompt={rows[-1][1] if rows else 0} tokens"
        usage.ctx.append(line)
        print(f"[mini-runner] context {line}", flush=True)
    return "".join(chunks), fired.is_set()


# mini_ctx's wait lines. Each ends "; credited Ns": the seconds it took off its own clock for that wait, each second once
# (the outage loop: the failed attempt and the nap before the retry; the relay: a hold while the call waits, see mini_ctx
# RELAY WAIT and PAUSE). OUTAGE_LINE is the inference an older mini_ctx (no suffix) needs. CREDIT_SLACK_S: credits may run
# ahead of the run's own time by one nap (credited before it is slept, at most 120 s) and the 0.1 s rounding.
OUTAGE_LINE = re.compile(r"^\[provider-outage\].*outage so far (\d+(?:\.\d+)?)s of")
CREDIT_LINE = re.compile(r"^\[provider-(?:outage|hold)\].*; credited (\d+(?:\.\d+)?)s$")
CREDIT_SLACK_S = 150


def credit_cap(delta, elapsed, credited):
    """The part of a wait line's credit a run takes: all of it while the run's credits stay within its own time so far
    (`elapsed`) + CREDIT_SLACK_S, so a stray line cannot stop the clocks for good."""
    return max(0.0, min(delta, elapsed + CREDIT_SLACK_S - credited))

LIMIT_WHY = {"TimeExceeded": "the wall-time limit was reached", "LimitsExceeded": "the step limit was reached"}


def exit_status(traj):
    """mini's exit_status for a trajectory (Submitted, LimitsExceeded, TimeExceeded, ...) or None."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return None
    return next((m["extra"].get("exit_status") for m in reversed(msgs) if m.get("role") == "exit" and isinstance(m.get("extra"), dict)), None)


def final_message(traj):
    """The agent's last words (exit submission, else last assistant text), for REVIEW.md."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return ""
    for m in reversed(msgs):
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        if m.get("role") in ("exit", "assistant") and isinstance(c, str) and c.strip():
            return c.strip()[-2000:]
    return ""


def said_missing(log, traj=None):
    """True when the agent SUBMITTED a final message reporting MISSING: (the text after the log's last COMPLETE_TASK marker).
    With a trajectory, a run that never submitted (LimitsExceeded, TimeExceeded, killed) is never MISSING: its last marker is
    the prompt's own, so a tool output printing 'MISSING: x' was read as the agent's report (setform 2fed9f58b143: an import
    probe printed 'MISSING: bloom_ledger', phase A stopped after one attempt with missing=True)."""
    if traj is not None and exit_status(traj) != "Submitted":
        return False
    return "MISSING:" in log.split("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")[-1] if "COMPLETE_TASK" in log else False


# ---- two-phase gates ------------------------------------------------------------------------

ASSERT_RE = re.compile(r"\bassert(?:\.\w+)?\s*\(|^\s*assert\s|\bexpect\s*\(|\bself\.assert\w+\s*\(|\bpytest\.raises\s*\(", re.M)
VACUOUS = [
    (re.compile(r"\bassert(?:\.ok)?\s*\(\s*(?:true|1|!0)\s*[,)]"), "assert(true)"),
    (re.compile(r"\bexpect\s*\(\s*(?:true|1)\s*\)"), "expect(true)"),
    (re.compile(r"^\s*assert\s+(?:True|1|not\s+False)\s*(?:,|$)", re.M), "assert True"),
    (re.compile(r"(?:\.length|\.size|\blen\([^\n]*?\))\s*>=\s*0\b"), ">= 0 bound"),
    (re.compile(r"\bassert\.\w+\s*\(\s*([\w.]+)\s*,\s*\1\s*[,)]"), "value compared to itself"),
    (re.compile(r"^\s*assert\s+([\w.]+)\s*==\s*\1\s*$", re.M), "value compared to itself"),
    (re.compile(r"\b(?:test|it|describe)\.(?:skip|todo)\b|\{\s*(?:skip|todo)\s*:\s*true|pytest\.mark\.(?:skip|xfail)|unittest\.skip"), "skipped test"),
]
MOCK_RE = re.compile(r"mock|patch|monkeypatch|stub|spyOn|sinon|require\.cache", re.I)
CRASH_RE = re.compile(r"SyntaxError|UnboundLocalError|NameError: name|Cannot find module|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|ImportError|ERROR collecting|"
                      r"no tests ran|test timed out|timed out after|TIMEOUT \d+s")
# stage 25: the CRASH_RE markers that count wherever they stand once per-test lines parsed (crash_hit); pytest's captured-log line
# (`LEVEL    logger:file.py:NN msg`, which fail_lines skips); a word that names an exception type (fb_red_problem)
RUNLEVEL_CRASH_RE = re.compile(r"ERROR collecting|no tests ran|test timed out|timed out after|TIMEOUT \d+s")
_CAPLOG_RE = re.compile(r"^(?:CRITICAL|ERROR|WARNING)\s{2,}[\w.\-]+:[\w.\-]+:\d+\s")
_EXC_WORD_RE = re.compile(r"\b[A-Za-z_]\w*(?:Error|Exception)\b")
FAILASSERT_RE = re.compile(r"AssertionError|ERR_ASSERTION|^E\s+assert|^E\s+AssertionError", re.M)
REASONLINE_RE = re.compile(r"^\s*(AssertionError|TypeError|ReferenceError|RangeError|SyntaxError|Error)\b.*:|^E\s+(assert|\w*Error)", re.M)
FAILLINE_RE = re.compile(r"error TS\d+|^\s*not ok \d+ - |^\s*✖ |^FAILED |^ERROR |: error:", re.M)


def lang_of(path):
    s = Path(path).suffix
    return "py" if s == ".py" else "node" if s in (".js", ".ts", ".mjs", ".cjs", ".mts", ".cts", ".jsx", ".tsx") else None


def _stems(targets):
    """Names a test would import a target by: file stem, plus the directory for index/__init__ files."""
    out = []
    for t in targets:
        if not lang_of(t):
            continue
        s = Path(t).stem
        out.append(s)
        if s in ("index", "__init__") and Path(t).parent.name:
            out.append(Path(t).parent.name)
    return out


def _is_bound_expr_py(n):
    """len(x) / x.size / x.count() style expression (a size that may legitimately be zero)."""
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len":
        return True
    if isinstance(n, ast.Attribute) and n.attr in ("size", "length", "count"):
        return True
    return isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("count", "__len__")


def _num(n):
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub) and isinstance(n.operand, ast.Constant):
        return -n.operand.value if isinstance(n.operand.value, (int, float)) else None
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) else None


def _vacuous_cmp_py(n):
    """`len(x) >= 0`, `0 <= len(x)`, `len(x) <= 5` (upper bound alone allows zero), `x == x`."""
    if not isinstance(n, ast.Compare) or len(n.ops) != 1:
        return None
    left, op, right = n.left, n.ops[0], n.comparators[0]
    if isinstance(op, (ast.Eq, ast.Is)) and ast.dump(left) == ast.dump(right):
        return "value compared to itself"
    if _num(left) is not None and _is_bound_expr_py(right):  # normalise `0 <= len(x)` to `len(x) >= 0`
        left, right = right, left
        op = {ast.LtE: ast.GtE(), ast.Lt: ast.Gt(), ast.GtE: ast.LtE(), ast.Gt: ast.Lt()}.get(type(op), op)
    if _is_bound_expr_py(left) and _num(right) is not None:
        v = _num(right)
        if (isinstance(op, ast.GtE) and v <= 0) or (isinstance(op, ast.Gt) and v < 0):
            return ">= 0 bound"
        if isinstance(op, (ast.LtE, ast.Lt)):
            return "upper bound only (zero allowed)"
    return None


def _truthy_const_py(n):
    if isinstance(n, ast.Constant):
        return bool(n.value) and not isinstance(n.value, type(Ellipsis))
    if isinstance(n, ast.Tuple):
        return len(n.elts) > 0  # assert (x, "msg") is always true
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and isinstance(n.operand, ast.Constant):
        return not n.operand.value
    if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
        return any(_truthy_const_py(v) for v in n.values)
    return False


def _dotted(n):
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def _stem_hit(dotted, stems):
    """py_static's stem_in for callers outside it: does a dotted module path (or file path) name one of the target stems?"""
    return any(s in (dotted or "").replace("/", ".").replace("-", "_").split(".") for s in stems) or \
        any(re.search(rf"(?:^|[/.]){re.escape(s)}(?:\.py)?$", dotted or "") for s in stems)


def _attr_after_stem(dotted, stems):
    """'calc.requests.get' -> 'requests': the module attribute a string patch target replaces (None when it names the module)."""
    parts = dotted.replace("/", ".").split(".")
    idx = [i for i, p in enumerate(parts) if p in stems or p.replace("_", "-") in stems or p.replace("-", "_") in stems]
    return parts[idx[-1] + 1] if idx and idx[-1] + 1 < len(parts) else None


_SETTING = re.compile(r"_?[A-Z][A-Z0-9_]*")
_CONFIG_CALLS = {"os.environ.get", "os.getenv", "environ.get", "getenv", "int", "float", "str", "bool", "Path", "pathlib.Path", "Path.home",
                 "pathlib.Path.home", "os.path.expanduser", "os.path.expandvars", "os.path.join", "os.path.abspath", "os.path.dirname",
                 "os.path.realpath"}


def _config_expr(v):
    """A module-level settings value: a scalar literal, an environment lookup, another setting, or casts / path helpers over those.
    Containers are not settings: a dict/list/set at module level is state or a lookup table the code under test reads."""
    if isinstance(v, ast.Constant):
        return True
    if isinstance(v, ast.Name):
        return v.id == "__file__" or bool(_SETTING.fullmatch(v.id))
    if isinstance(v, ast.Attribute):  # Path(__file__).parent
        return v.attr in ("parent", "name", "stem") and _config_expr(v.value)
    if isinstance(v, ast.Subscript):
        return _dotted(v.value) in ("os.environ", "environ") and _config_expr(v.slice)
    if isinstance(v, ast.BinOp):
        return _config_expr(v.left) and _config_expr(v.right)
    if isinstance(v, ast.BoolOp):
        return all(_config_expr(x) for x in v.values)
    if isinstance(v, ast.UnaryOp):
        return _config_expr(v.operand)
    if isinstance(v, ast.JoinedStr):
        return all(isinstance(x, ast.Constant) or (isinstance(x, ast.FormattedValue) and _config_expr(x.value)) for x in v.values)
    if isinstance(v, ast.Call):
        args_ok = all(_config_expr(x) for x in v.args) and all(_config_expr(k.value) for k in v.keywords)
        if (_dotted(v.func) or "") in _CONFIG_CALLS:
            return args_ok
        if isinstance(v.func, ast.Attribute):  # Path(...).expanduser(), os.getenv(...).lower(), "~/x".strip()
            return v.func.attr in ("expanduser", "resolve", "absolute", "lower", "upper", "strip", "rstrip")                 and _config_expr(v.func.value) and args_ok
    return False


def _py_dep_names(cwd, targets, stems):
    """Names in a python target's module namespace that are not code under test, so a test may point them at fakes (the phase A
    prompt allows faking network, clock, env and paths): what the target imports from outside the targets (requests, time,
    subprocess ...) and its UPPER_CASE settings built from literals or the environment. A def or class of the target never is.
    2026-11-02: a draft that set `aw.requests` (the HTTP client the real code calls) was told only 'overwrites aw.requests', while
    its real offence, replacing four functions of the target, went unreported."""
    out, defined = set(), set()
    for tgt in targets:
        if lang_of(tgt) != "py" or cwd is None:
            continue
        try:
            body = list(ast.parse(Path(cwd, tgt).read_text(errors="replace")).body)
        except (OSError, SyntaxError, ValueError):
            continue
        while body:  # top level, plus top-level try/if blocks (optional imports, platform switches)
            n = body.pop(0)
            if isinstance(n, ast.Try):
                body += n.body + n.orelse + n.finalbody + [s for h in n.handlers for s in h.body]
            elif isinstance(n, ast.If):
                body += n.body + n.orelse
            elif isinstance(n, ast.Import):
                out |= {a.asname or a.name.split(".")[0] for a in n.names if not _stem_hit(a.name, stems)}
            elif isinstance(n, ast.ImportFrom) and not n.level and not _stem_hit(n.module or "", stems):
                out |= {a.asname or a.name for a in n.names if a.name != "*" and a.name not in stems}
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                if len(tg) == 1 and isinstance(tg[0], ast.Name) and _SETTING.fullmatch(tg[0].id) and _config_expr(n.value):
                    out.add(tg[0].id)
    return out - defined


def _py_top(tree):
    """A module's top-level statements, plus those in top-level try/if blocks (optional imports, platform switches)."""
    body, out = list(tree.body), []
    while body:
        n = body.pop(0)
        if isinstance(n, ast.Try):
            body += n.body + n.orelse + n.finalbody + [s for h in n.handlers for s in h.body]
        elif isinstance(n, ast.If):
            body += n.body + n.orelse
        else:
            out.append(n)
    return out


def _py_offers(stmts, names):
    """(all, star): of `names` (what a module has from the target), those it binds, and those `from it import *` passes on
    (its __all__ when that is a literal list, else the ones without a leading underscore)."""
    star = None
    for n in stmts:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets) and \
                isinstance(n.value, (ast.List, ast.Tuple)):
            star = {e.value for e in n.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return names, ({x for x in names if not x.startswith("_")} if star is None else names & star)


def _py_reexports(cwd, targets):
    """Dotted names (`optuna.samplers.PartialFixedSampler`) by which a python test reaches a target's own code through a package
    that re-exports it: what each enclosing package's __init__.py takes from the target (`from ._partial_fixed import
    PartialFixedSampler`, or `*`), and from those packages in turn, up the tree; only the target's own defs, classes and
    assignments count, never what it imports. 2026-11-05 bench optuna: a test importing the sampler the way the issue and the
    docs do, `from optuna.samplers import PartialFixedSampler`, was rejected as never importing the code under test."""
    out = set()
    for tgt in targets:
        if lang_of(tgt) != "py" or cwd is None:
            continue
        try:
            stmts = _py_top(ast.parse(Path(cwd, tgt).read_text(errors="replace")))
        except (OSError, SyntaxError, ValueError):
            continue
        own = {n.name for n in stmts if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        own |= {t.id for n in stmts if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name) and t.id != "__all__"}
        parts = list(Path(tgt).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        top = 1 if len(parts) > 1 and parts[0] in ("src", "lib") else 0  # src layout: the import path starts below src/
        offers = {".".join(parts[top:]): _py_offers(stmts, own)}
        for i in range(len(parts) - 1, top, -1):  # the enclosing packages, innermost first
            pkg = parts[top:i]
            try:
                pstmts = _py_top(ast.parse(Path(cwd, *parts[:i], "__init__.py").read_text(errors="replace")))
            except (OSError, SyntaxError, ValueError):
                break  # not a package (a namespace dir, or no __init__.py): nothing above it re-exports through it
            got = set()
            for n in pstmts:
                if not isinstance(n, ast.ImportFrom):
                    continue
                if n.level:
                    if n.level - 1 > len(pkg):
                        continue
                    src = ".".join(pkg[:len(pkg) - (n.level - 1)] + ([n.module] if n.module else []))
                else:
                    src = n.module or ""
                if src not in offers:
                    continue
                names, star = offers[src]
                for a in n.names:
                    if a.name == "*":
                        got |= star
                    elif a.name in names:
                        got.add(a.asname or a.name)
            offers[".".join(pkg)] = _py_offers(pstmts, got)
        out |= {f"{m}.{x}" for m, (names, _s) in offers.items() for x in names}
    return out


_PY_TEST_FILE =re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test|conftest)\.py$|(?:^|/)tests?/[^/]*\.py$")


def _py_precedent_fakes(cwd, stems):
    """Attributes of a python target module that the repo's own committed tests replace (`aw.deluge_login = lambda: None`,
    monkeypatch.setattr(aw, 'x', ...), patch('aw.x')): the repo treats those as collaborators of the code under test, so a phase A
    test that fakes the same ones follows the repo's test style, which the phase A prompt asks for, and is not mocking it. Read
    from the committed tree (git HEAD), never the work tree, so a draft cannot make its own precedent; the caller removes the
    functions under test. 2026-11-03 bench-dupes4: a test faking deluge_login, get_all_torrents, set_torrent_label and
    record_activity of arr-webhook.py, as the repo's test_queue_dupes.py does for the same function, failed the job as mocking."""
    if cwd is None or not stems:
        return set()
    try:
        ls = subprocess.run(["git", "-C", str(cwd), "ls-tree", "-r", "HEAD"], capture_output=True, text=True, timeout=30)
        shas = [ln.split()[2] for ln in ls.stdout.splitlines()
                if "\t" in ln and ln.split()[1] == "blob" and _PY_TEST_FILE.search(ln.split("\t", 1)[1])][:2000]
        if ls.returncode or not shas:
            return set()
        raw = subprocess.run(["git", "-C", str(cwd), "cat-file", "--batch"], input=("\n".join(shas) + "\n").encode(),
                             capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError, IndexError):
        return set()
    names = {v for s in stems for v in (s, s.replace("-", "_"))}
    found, i = set(), 0
    while i < len(raw):
        nl = raw.find(b"\n", i)
        if nl < 0:
            break
        head = raw[i:nl].split()
        if len(head) < 3 or head[1] != b"blob":  # "<sha> missing"
            i = nl + 1
            continue
        size = int(head[2])
        body, i = raw[nl + 1:nl + 1 + size], nl + 2 + size
        if size > 1_000_000:
            continue
        text = body.decode("utf-8", "replace")
        if not any(v in text for v in names):
            continue
        facts = py_static(text, stems)
        if facts and facts["imports_target"]:
            found |= facts["replaced"]
    return found


def py_static(text, stems, ok_attrs=frozenset(), reexp=frozenset()):
    """ast-based facts about a python test. None if it does not parse (regex fallback). ok_attrs: attributes of a target module the
    test may set (its imported dependencies and settings, from _py_dep_names); replacing anything else in it is mocking. reexp:
    dotted names a package re-exports the target's code by (_py_reexports); importing or using one imports the target."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    alias = {}  # local name -> canonical dotted name (pytest, unittest.mock, unittest.mock.patch, <target>)
    imports_target, targets_bound = False, set()

    def stem_in(dotted):
        return any(s in (dotted or "").replace("/", ".").replace("-", "_").split(".") for s in stems) or \
            any(re.search(rf"(?:^|[/.]){re.escape(s)}(?:\.py)?$", dotted or "") for s in stems)

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                if stem_in(a.name):
                    imports_target = True
                    targets_bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            for a in n.names:
                alias[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
                if stem_in(mod) or a.name in stems or (not n.level and f"{mod}.{a.name}" in reexp):
                    imports_target = True
                    if a.name in stems:
                        targets_bound.add(a.asname or a.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and stem_in(n.value):
            imports_target = True  # importlib / spec_from_file_location / runpy / subprocess on the target
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            if any(isinstance(c, ast.Constant) and isinstance(c.value, str) and stem_in(c.value) for c in ast.walk(n.value)):
                targets_bound.add(n.targets[0].id)  # mod = importlib.import_module("calc") / load(...)

    def canon(node):
        d = _dotted(node)
        if not d:
            return None
        head, _, rest = d.partition(".")
        base = alias.get(head, head)
        return f"{base}.{rest}" if rest else base

    if not imports_target and reexp:  # `import optuna` ... optuna.samplers.PartialFixedSampler(...)
        imports_target = any(canon(n) in reexp for n in ast.walk(tree) if isinstance(n, ast.Attribute))
    asserts, vac, skipped, mocks = 0, [], [], []
    replaced = set()  # attribute names of a target module the test sets or patches (allowed or not): _py_precedent_fakes reads it
    for n in ast.walk(tree):
        if isinstance(n, ast.Assert):
            asserts += 1
            why = "assert True" if _truthy_const_py(n.test) else _vacuous_cmp_py(n.test)
            if why:
                vac.append(why)
        elif isinstance(n, ast.Call):
            c = canon(n.func) or ""
            leaf = c.rsplit(".", 1)[-1]
            if leaf.startswith("assert") or leaf in ("raises", "warns") and "pytest" in c:
                asserts += 1
                a0 = n.args[0] if n.args else None
                if leaf in ("assertTrue", "assert_", "assertIsNotNone") and a0 is not None and (_truthy_const_py(a0) or _vacuous_cmp_py(a0)):
                    vac.append(f"{leaf}(constant)" if _truthy_const_py(a0) else _vacuous_cmp_py(a0))
                elif leaf in ("assertEqual", "assertEquals", "assertIs", "assert_equal", "assertListEqual", "assertDictEqual") and len(n.args) >= 2 \
                        and ast.dump(n.args[0]) == ast.dump(n.args[1]):
                    vac.append("value compared to itself")
                elif leaf == "assertGreaterEqual" and len(n.args) >= 2 and _is_bound_expr_py(n.args[0]) and (_num(n.args[1]) or 0) <= 0 and _num(n.args[1]) is not None:
                    vac.append(">= 0 bound")
                elif leaf in ("assertLessEqual", "assertLess") and n.args and _is_bound_expr_py(n.args[0]):
                    vac.append("upper bound only (zero allowed)")
            if leaf in ("skip", "skipTest", "skipIf", "skipUnless", "xfail", "importorskip") and ("pytest" in c or "unittest" in c or leaf == "skipTest"):
                if leaf != "importorskip":
                    skipped.append(c)
            # mocking / replacing the target
            mocky = leaf in ("patch", "object", "dict", "multiple", "setattr", "delattr", "setitem") and (
                "mock" in c or "monkeypatch" in c or "mocker" in c or c.endswith("patch.object") or alias.get(c.split(".")[0], "").endswith("patch"))
            if c in ("setattr", "delattr") or mocky:
                a0 = n.args[0] if n.args else None
                a1 = n.args[1] if len(n.args) > 1 else None
                dep = isinstance(a1, ast.Constant) and isinstance(a1.value, str) and a1.value in ok_attrs and "delattr" not in c
                if isinstance(a0, ast.Name) and a0.id in targets_bound and isinstance(a1, ast.Constant) and isinstance(a1.value, str) \
                        and "delattr" not in c:
                    replaced.add(a1.value)
                if isinstance(a0, ast.Name) and a0.id in targets_bound and not dep:
                    mocks.append(f"{c}({ast.unparse(a0)}, {ast.unparse(a1) + ', ' if isinstance(a1, ast.Constant) else ''}...)")
                elif isinstance(a0, ast.Constant) and isinstance(a0.value, str) and stem_in(a0.value.rsplit(".", 1)[0]):
                    attr = _attr_after_stem(a0.value, stems)
                    if attr and "delattr" not in c:
                        replaced.add(attr)
                    if attr not in ok_attrs:
                        mocks.append(f"{c}({ast.unparse(a0)}, ...)")
            if c in ("sys.modules.setdefault", "sys.modules.__setitem__", "sys.modules.update") or \
                    (leaf == "dict" and mocky and n.args and _dotted(n.args[0]) == "sys.modules"):
                if leaf in ("setdefault", "__setitem__"):
                    keys = [a.value for a in n.args[:1] if isinstance(a, ast.Constant)]
                else:
                    pos = n.args[:1] if leaf == "update" else n.args[1:2]
                    keys = [k.value for a in pos if isinstance(a, ast.Dict) for k in a.keys if isinstance(k, ast.Constant)] + \
                        [kw.arg for kw in n.keywords if kw.arg]
                hit = next((k for k in keys if isinstance(k, str) and stem_in(k)), None)
                if hit:
                    mocks.append(f"replaces sys.modules[{hit!r}]")
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.Delete)):
            for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]):
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in targets_bound:
                    if isinstance(n, ast.Assign):
                        replaced.add(t.attr)
                    if not (isinstance(n, ast.Assign) and t.attr in ok_attrs):  # `aw.requests = Fake()` fakes a dependency
                        mocks.append(f"overwrites {ast.unparse(t)}")
                # assigning sys.modules["calc"] installs a fake; `del sys.modules["calc"]` only forces a fresh import of the
                # real module (2026-11-02: a sound test that faked requests/time.sleep and then re-imported was rejected)
                if isinstance(t, ast.Subscript) and not isinstance(n, ast.Delete) and _dotted(t.value) == "sys.modules" \
                        and isinstance(t.slice, ast.Constant) and stem_in(str(t.slice.value)):
                    mocks.append(f"replaces sys.modules[{t.slice.value!r}]")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in n.decorator_list:
                c = canon(d.func if isinstance(d, ast.Call) else d) or ""
                if re.search(r"(?:^|\.)(?:skip|skipIf|skipUnless|xfail)$", c) and ("pytest" in c or "unittest" in c):
                    skipped.append(c)
    return {"asserts": asserts, "vacuous": vac, "skipped": skipped, "mocks": mocks, "imports_target": imports_target, "parser": "python-ast",
            "replaced": replaced}


# Node side: acorn from the repo's node_modules (TS sources are type-stripped by node first). Prints JSON facts,
# or {"error": ...} when no parser is available (regex fallback).
JS_ANALYZER = r'''
const fs = require("fs"), path = require("path"), Module = require("module");
const [from, isTs, stemsJson] = process.argv.slice(1);
const stems = JSON.parse(stemsJson);
let src = fs.readFileSync(0, "utf8"), acorn = null;
for (const where of [path.join(from, "x.js"), process.env.MINI_ACORN && path.join(process.env.MINI_ACORN, "x.js")].filter(Boolean)) {
  try { acorn = Module.createRequire(where)("acorn"); break; } catch {}
}
const out = (o) => { process.stdout.write(JSON.stringify(o)); process.exit(0); };
if (!acorn) out({ error: "no-parser" });
if (isTs === "1") { try { src = Module.stripTypeScriptTypes(src); } catch (e) { out({ error: "strip: " + e.message }); } }
let ast;
for (const sourceType of ["module", "script"]) {
  try { ast = acorn.parse(src, { ecmaVersion: "latest", sourceType, allowHashBang: true, allowAwaitOutsideFunction: true, allowReturnOutsideFunction: true }); break; } catch (e) { if (sourceType === "script") out({ error: "parse: " + e.message }); }
}
const T = (n) => src.slice(n.start, n.end);
const specStem = (s) => { const b = path.basename(String(s)).replace(/\.(?:[mc]?[jt]sx?|json)$/, ""); return b; };
const isTarget = (s) => typeof s === "string" && (stems.includes(specStem(s)) || stems.some((t) => new RegExp("(^|/)" + t + "(/index)?(\\.[mc]?[jt]sx?)?$").test(s)));
const ASSERT_MODS = /^(node:)?assert(\/strict)?$|^chai$|^power-assert$/;
const assertNS = new Set(["assert"]), assertFn = new Map(), expectFns = new Set(["expect"]), tgt = new Set();
let importsTarget = false, asserts = 0;
const vac = [], skipped = [], mocks = [];
function walk(n, f, parent) { if (!n || typeof n.type !== "string") return; f(n, parent); for (const k in n) { if (k === "start" || k === "end") continue; const v = n[k]; if (Array.isArray(v)) v.forEach((c) => c && typeof c.type === "string" && walk(c, f, n)); else if (v && typeof v.type === "string") walk(v, f, n); } }
const reqSpec = (n) => { // require('x') / require('x').strict / await import('x')
  if (!n) return null;
  if (n.type === "AwaitExpression") n = n.argument;
  if (n.type === "MemberExpression" && !n.computed && n.property.name === "strict") n = n.object;
  if (n.type === "ImportExpression" && n.source.type === "Literal") return n.source.value;
  if (n.type === "CallExpression" && n.callee.type === "Identifier" && n.callee.name === "require" && n.arguments[0] && n.arguments[0].type === "Literal") return n.arguments[0].value;
  return null;
};
const bindPattern = (id, spec) => {
  const am = ASSERT_MODS.test(spec), tm = isTarget(spec);
  if (tm) importsTarget = true;
  if (id.type === "Identifier") { if (am) assertNS.add(id.name); if (tm) tgt.add(id.name); }
  else if (id.type === "ObjectPattern") for (const p of id.properties) {
    if (p.type !== "Property" || p.value.type !== "Identifier") continue;
    const key = p.key.name || p.key.value;
    if (am) { if (key === "strict" || key === "assert") assertNS.add(p.value.name); else if (key === "expect") expectFns.add(p.value.name); else assertFn.set(p.value.name, key); }
    if (tm) tgt.add(p.value.name);
  }
};
walk(ast, (n) => {
  if (n.type === "ImportDeclaration") {
    const spec = n.source.value;
    if (isTarget(spec)) importsTarget = true;
    for (const s of n.specifiers) {
      const imported = s.imported ? (s.imported.name || s.imported.value) : null;
      if (ASSERT_MODS.test(spec)) { if (!imported || imported === "default" || imported === "strict" || imported === "assert") assertNS.add(s.local.name); else if (imported === "expect") expectFns.add(s.local.name); else assertFn.set(s.local.name, imported); }
      if (/^(vitest|@jest\/globals|expect)$/.test(spec) && imported === "expect") expectFns.add(s.local.name);
      if (isTarget(spec)) tgt.add(s.local.name);
    }
  } else if (n.type === "VariableDeclarator" && n.init) {
    const spec = reqSpec(n.init);
    if (spec != null) bindPattern(n.id, spec);
    else if (n.id.type === "Identifier" && n.init.type === "Identifier" && assertNS.has(n.init.name)) assertNS.add(n.id.name);
    else if (n.id.type === "Identifier" && n.init.type === "MemberExpression" && !n.init.computed && n.init.object.type === "Identifier" && assertNS.has(n.init.object.name)) assertFn.set(n.id.name, n.init.property.name);
  } else if (n.type === "CallExpression" || n.type === "ImportExpression") {
    const s = reqSpec(n); if (s != null && isTarget(s)) importsTarget = true;
  }
});
const truthy = (n) => n && ((n.type === "Literal" && !!n.value && !(n.regex)) || (n.type === "UnaryExpression" && n.operator === "!" && n.argument.type === "Literal" && !n.argument.value)
  || (n.type === "ArrayExpression") || (n.type === "ObjectExpression") || (n.type === "LogicalExpression" && n.operator === "||" && (truthy(n.left) || truthy(n.right))));
const isLen = (n) => n && ((n.type === "MemberExpression" && !n.computed && /^(length|size|byteLength)$/.test(n.property.name)) || (n.type === "CallExpression" && n.callee.type === "MemberExpression" && /^(count|size)$/.test(n.callee.property.name || "")));
const num = (n) => n && n.type === "Literal" && typeof n.value === "number" ? n.value : n && n.type === "UnaryExpression" && n.operator === "-" && n.argument.type === "Literal" && typeof n.argument.value === "number" ? -n.argument.value : null;
const FLIP = { "<=": ">=", "<": ">", ">=": "<=", ">": "<" };
function cmpVac(n) {
  if (!n || n.type !== "BinaryExpression") return null;
  let { left, right, operator: op } = n;
  if (/^(===|==)$/.test(op) && T(left) === T(right)) return "value compared to itself";
  if (num(left) !== null && isLen(right)) { [left, right] = [right, left]; op = FLIP[op] || op; }
  if (isLen(left) && num(right) !== null) {
    const v = num(right);
    if ((op === ">=" && v <= 0) || (op === ">" && v < 0)) return ">= 0 bound";
    if (op === "<=" || op === "<") return "upper bound only (zero allowed)";
  }
  return null;
}
const EQ = /^(equal|strictEqual|deepEqual|deepStrictEqual|is|eql|equals)$/;
walk(ast, (n, parent) => {
  if (n.type !== "CallExpression") return;
  const c = n.callee, a0 = n.arguments[0], a1 = n.arguments[1];
  let m = null; // assertion method name
  if (c.type === "Identifier" && assertNS.has(c.name)) m = "ok";
  else if (c.type === "Identifier" && assertFn.has(c.name)) m = assertFn.get(c.name);
  else if (c.type === "MemberExpression" && !c.computed) {
    const o = c.object;
    if (o.type === "Identifier" && assertNS.has(o.name)) m = c.property.name;
    else if (o.type === "MemberExpression" && !o.computed && (o.property.name === "assert" || (o.property.name === "strict" && o.object.type === "Identifier" && assertNS.has(o.object.name)))) m = c.property.name;
  }
  if (m) {
    asserts++;
    if ((m === "ok" || m === "assert" || m === "isTrue" || m === "true") && (truthy(a0) || cmpVac(a0))) vac.push(truthy(a0) ? "assert(true)" : cmpVac(a0));
    else if (EQ.test(m) && a0 && a1 && (T(a0) === T(a1) || (a0.type === "Literal" && a1.type === "Literal"))) vac.push("value compared to itself");
  }
  if (c.type === "Identifier" && expectFns.has(c.name) && parent && parent.type === "MemberExpression") {
    asserts++;
    if (truthy(a0) || (a0 && a0.type === "Literal")) vac.push("expect(true)");
    else if (cmpVac(a0)) vac.push(cmpVac(a0));
  }
  // skipped tests: test.skip / it.todo / describe.skip / t.skip() / { skip: ... } options
  const pname = c.type === "MemberExpression" && !c.computed ? c.property.name : null;
  if (pname && /^(skip|todo)$/.test(pname) && c.object.type === "Identifier" && /^(test|it|describe|suite|t|context)$/.test(c.object.name)) skipped.push(T(c));
  if (c.type === "Identifier" && /^(test|it|describe|suite)$/.test(c.name))
    for (const arg of n.arguments) if (arg.type === "ObjectExpression") for (const p of arg.properties)
      if (p.type === "Property" && /^(skip|todo)$/.test(p.key.name || p.key.value) && !(p.value.type === "Literal" && !p.value.value)) skipped.push("{" + (p.key.name || p.key.value) + ": ...}");
  if (pname === "skip" && c.object.type === "Identifier" && n.arguments.length === 0) skipped.push(T(c) + "()");
  // mocks that name or receive the target
  const cname = c.type === "Identifier" ? c.name : pname;
  const specArg = a0 && a0.type === "Literal" ? a0.value : a0 && a0.type === "TemplateLiteral" ? T(a0).slice(1, -1) : null;
  if (/^(module|mock|doMock|unstable_mockModule|replaceEsm|replace|proxyquire|rewire|esmock|mockRequire|setMock)$/.test(cname || "") && isTarget(specArg)) mocks.push(T(n).slice(0, 120));
  const objArg = a0 && a0.type === "Identifier" && tgt.has(a0.name);
  if (objArg && /^(method|spyOn|stub|replace|replaceGetter|replaceSetter|getter|setter|property|mock|fake|defineProperty|assign|set|defineProperties|setPrototypeOf)$/.test(cname || "")) mocks.push(T(n).slice(0, 120));
});
walk(ast, (n) => {
  const t = n.type === "AssignmentExpression" ? n.left : n.type === "UnaryExpression" && n.operator === "delete" ? n.argument : null;
  if (!t || t.type !== "MemberExpression") return;
  if (t.object.type === "Identifier" && tgt.has(t.object.name)) mocks.push("overwrites " + T(t));
  if (n.type === "AssignmentExpression" && T(t.object) === "require.cache" && stems.some((s) => T(t.property).includes(s))) mocks.push("replaces " + T(t).slice(0, 80));
});
out({ asserts, vacuous: vac, skipped, mocks, imports_target: importsTarget, parser: "acorn" });
'''


def js_static(text, stems, from_dir, is_ts):
    if not shutil.which("node"):
        return None
    try:
        r = subprocess.run(["node", "-e", JS_ANALYZER, str(from_dir), "1" if is_ts else "0", json.dumps(stems)],
                           input=text, capture_output=True, text=True, timeout=30)
        res = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    return None if "error" in res else res


def test_static(text, targets, min_asserts=2, test_path=None, cwd=None, test_rules=True, source_checks=True, fns=()):
    """Problems with the test source itself (empty list = fine). Real parsers first (python ast; acorn from the
    repo's node_modules for JS/TS), regex fallback otherwise. fns: the functions under test (spec target_fn), which a test may
    never replace even where the repo's committed tests do (_py_precedent_fakes)."""
    if FROZEN_MARK in text:  # mini-spec's template, not filled in yet: its placeholder asserts would count as real ones
        left = [i for i, ln in enumerate(text.splitlines(), 1) if FROZEN_MARK in ln]
        return [f"unfilled frozen-test template: replace every {FROZEN_MARK} placeholder ({len(left)} left, first at line {left[0]}) "
                "with a real call and its exact expected value"]
    stems = _stems(targets)
    lang = lang_of(test_path) if test_path else next((lang_of(t) for t in targets if lang_of(t)), None)
    facts, prec = None, set()
    if lang == "py":
        prec = _py_precedent_fakes(cwd, stems) - set(as_list(fns))
        facts = py_static(text, stems, _py_dep_names(cwd, targets, stems) | prec, _py_reexports(cwd, targets))
    elif lang == "node":
        start = Path(cwd or ".") / (Path(test_path).parent if test_path else ".")
        facts = js_static(text, stems, start.resolve(), bool(test_path and re.search(r"\.[mc]?tsx?$", test_path)))
    if facts is None:
        return test_static_regex(text, targets, min_asserts)
    bad = []
    if stems and not facts["imports_target"]:
        bad.append(f"test never imports the code under test ({', '.join(stems)})")
    if facts["vacuous"]:
        bad.append("vacuous: " + ", ".join(sorted(set(facts["vacuous"]))))
    if facts["skipped"]:
        bad.append("vacuous: skipped test (" + ", ".join(sorted(set(facts["skipped"])))[:120] + ")")
    if facts["asserts"] - len(facts["vacuous"]) < min_asserts:
        bad.append(f"fewer than {min_asserts} real assertions")
    if facts["mocks"]:  # every offender: naming only the first left a draft fixing the wrong line twice
        ms = list(dict.fromkeys(m[:80] for m in facts["mocks"]))
        bad.append("mocks the code under test: " + ", ".join(ms[:5]) + (f" (+{len(ms) - 5} more)" if len(ms) > 5 else "")
                   + ". Let the real code run and fake only what it calls out to (its network client, clock, env, files)"
                   + (f" or what the repo's own tests already fake ({', '.join(sorted(prec)[:8])})" if prec else ""))
    if lang == "py" and test_rules:
        code = [Path(t).name for t in targets if lang_of(t) or Path(t).suffix in ("", ".sh", ".bash", ".zsh")]
        bad += py_test_rules(text, stems, code, source_checks)
    return bad


def _assert_aliases(text):
    names = {"assert"}
    names |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['\"](?:node:)?assert(?:/strict)?['\"]\s*\)", text))
    names |= set(re.findall(r"import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+['\"](?:node:)?assert(?:/strict)?['\"]", text))
    names |= {x for t in re.findall(r"\bstrict\s+as\s+(\w+)|\bstrict\s*:\s*(\w+)", text) for x in t if x}
    fns = set()
    for grp in re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\(\s*['\"](?:node:)?assert(?:/strict)?['\"]", text) + \
            re.findall(r"import\s*\{([^}]*)\}\s*from\s*['\"](?:node:)?assert(?:/strict)?['\"]", text):
        for part in grp.split(","):
            bits = re.split(r"\s*(?::|\bas\b)\s*", part.strip())
            if bits and bits[-1]:
                fns.add((bits[-1], bits[0]))
    return names, fns


def test_static_regex(text, targets, min_asserts=2):
    """Regex fallback (no parser available)."""
    bad = []
    stems = _stems(targets)
    names, fns = _assert_aliases(text)
    alt = "|".join(re.escape(n) for n in sorted(names))
    ok_fns = "|".join(re.escape(a) for a, orig in fns if orig in ("ok", "assert")) or "(?!x)x"
    eq_fns = "|".join(re.escape(a) for a, orig in fns if "qual" in orig) or "(?!x)x"
    bound = r"(?:\.length|\.size|\blen\([^\n]*?\))"
    vacuous = VACUOUS + [
        (re.compile(rf"\b(?:{alt})(?:\.ok)?\s*\(\s*(?:true|1|!0)\s*[,)]|\b(?:{ok_fns})\s*\(\s*(?:true|1|!0)\s*[,)]"), "assert(true)"),
        (re.compile(rf"\b(?:{alt})\.\w+\s*\(\s*([\w.]+)\s*,\s*\1\s*[,)]|\b(?:{eq_fns})\s*\(\s*([\w.]+)\s*,\s*\2\s*[,)]"), "value compared to itself"),
        (re.compile(rf"{bound}\s*<=?\s*\d+|\b\d+\s*>=?\s*{bound}"), "upper bound only (zero allowed)"),
        (re.compile(rf"\b0\s*<=\s*{bound}|-1\s*<\s*{bound}"), ">= 0 bound"),
    ]
    spans = set()
    vac = []
    for rx, why in vacuous:
        for m in rx.finditer(text):
            if m.start() not in spans:
                spans.add(m.start())
                vac.append(why)
    # same-line form, or a require(/import( call whose string specifier sits on a later line (formatter-wrapped)
    if stems and not any(re.search(rf"(require\(|import\b|from\b)[^\n]*\b{re.escape(s)}\b", text)
                         or re.search(rf"\b(?:require|import)\s*\(\s*['\"`][^'\"`\n]*\b{re.escape(s)}\b", text) for s in stems):
        bad.append(f"test never imports the code under test ({', '.join(stems)})")
    if vac:
        bad.append("vacuous: " + ", ".join(sorted(set(vac))))
    n_asserts = len(ASSERT_RE.findall(text)) + sum(len(re.findall(rf"(?<![.\w]){re.escape(a)}\s*\(", text)) for a, _ in fns)
    if alt != "assert":
        n_asserts += len(re.findall(rf"\b(?:{'|'.join(re.escape(n) for n in names - {'assert'})})(?:\.\w+)?\s*\(", text))
    if n_asserts - len(vac) < min_asserts:
        bad.append(f"fewer than {min_asserts} real assertions")
    patch_alias = set(re.findall(r"import\s+(?:patch|mock)\s+as\s+(\w+)", text))
    mock_re = re.compile(r"mock|patch|monkeypatch|stub|spyOn|sinon|require\.cache|proxyquire|rewire|esmock|defineProperty"
                         + "".join(rf"|\b{re.escape(p)}\b" for p in patch_alias), re.I)
    for ln in text.splitlines():
        if re.match(r"\s*(?://|#|\*|/\*)", ln):  # comment-only line: not a mock
            continue
        if mock_re.search(ln) and any(re.search(rf"\b{re.escape(s)}\b", ln) for s in stems) and not re.match(r"\s*(?:import|from)\b|\s*(?:const|let|var)\s.*=\s*require\(", ln):
            bad.append(f"mocks the code under test: {ln.strip()[:120]}")
            break
    for s in stems:  # monkeypatching an export of the target (`amazon.fetchX = ...`, setattr(mod, ...))
        for name in re.findall(rf"(?:const|let|var)\s+(\w+)\s*=\s*require\(['\"][^'\"]*\b{re.escape(s)}(?:\.\w+)?['\"]\)", text) + \
                re.findall(rf"import\s+[\w.]*\b{re.escape(s)}\s+as\s+(\w+)|^\s*(?:from\s+[\w.]+\s+)?import\s+({re.escape(s)})\b", text, re.M):
            name = name if isinstance(name, str) else next(n for n in name if n)
            if re.search(rf"\b{name}\.\w+\s*=[^=]|\b{name}\[[^\]]+\]\s*=[^=]|setattr\(\s*{name}\b|defineProperty\(\s*{name}\b", text):
                bad.append(f"overwrites an export of the code under test ({name})")
    return bad


def test_count(out):
    m = re.findall(r"^(?:# |ℹ )tests (\d+)", out, re.M)
    if m:
        return int(m[-1])
    # the frozen-test template's summary `N tests, M failed`: N is the count (summing the `M failed` alone made a green run
    # count 0 tests, below the baseline's count, and test_green failed on a passing test)
    m = re.findall(r"^(\d+) tests?, \d+ failed\b", out, re.M)
    if m:
        return int(m[-1])
    n = [int(x) for x in re.findall(r"(\d+) (?:passed|failed|error)", out)]
    return sum(n) if n else None


# ---- 2026-10-24 gates (workflow v2, ~/.ollama-dispatch/mini-swe-workflow-design.md) ------------------------------------
# G1/G2 per-test red classification, G4 no_hacks anti-gaming scan, G5 frozen-test static rules, G7 failure class tags.

_TYPE_RE = re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Warning|Failure))\b:?\s?(.*)$", re.S)
_PT_TEMPLATE = re.compile(r"^RESULT (?P<name>(?!PASS\b|FAIL\b)[A-Za-z_]\w*) (?P<st>PASS|FAIL)\b[ \t]*(?P<rest>.*)$", re.M)
_PT_OKFAIL = re.compile(r"^(?P<st>OK|PASS|FAIL)[ \t]+(?P<name>test\w*)\b:?[ \t]*(?P<rest>.*)$", re.M)
_PT_PYTEST = re.compile(r"^(?P<st>PASSED|FAILED|ERROR) (?P<name>\S+::\S+?)(?: - (?P<rest>.*))?$", re.M)
_PT_TAP = re.compile(r"^(?P<ind>[ \t]*)(?P<st>not ok|ok) \d+ - (?P<name>.+?)(?:\s+#\s*(?:SKIP|TODO)\b.*)?$", re.M)
_PT_SPEC = re.compile(r"^(?P<ind>[ \t]*)(?P<st>\u2714|\u2716|\ufe63|\u26a0) (?P<name>.+?) \((?P<ms>\d+(?:\.\d+)?)ms\)(?P<tail>.*)$")
_PT_SPEC_ERR = re.compile(r"^(?P<t>[A-Za-z_$][\w$]*)(?: \[(?P<code>[A-Z0-9_]+)\])?(?::\s*(?P<m>.*))?$")
_PT_STACK = re.compile(r"^at (?:async )?\S.*(?:\(.*:\d+:\d+\)|:\d+:\d+)(?: \{)?$")  # a trailing ` {` opens the error's props
HARD_BAD_RED = {"UnboundLocalError", "NameError", "SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError",
                "RecursionError", "TabError"}
SHAPE_BAD_RED = re.compile(r"'NoneType' object|object is not subscriptable|object is not callable|takes \d+ positional|missing \d+ required")


def _split_type(rest):
    """'AttributeError: msg' -> ('AttributeError', 'msg'); pytest's 'assert 1 == 2' -> ('AssertionError', ...)."""
    rest = (rest or "").strip()
    m = _TYPE_RE.match(rest)
    if m:
        return m.group(1).rsplit(".", 1)[-1], m.group(2).strip()
    if rest.startswith("assert"):
        return "AssertionError", rest
    return (rest.split(":", 1)[0].strip() or "unknown") if rest else "unknown", rest


def _pytest_failure_type(out, name):
    """(type, msg) of a FAILED pytest test from its failure section: the first `E` line of the section's last `E` block (the final
    exception of a chain). None when the section is not in the output (--tb=no/line). Section titles use `Class.test` for `Class::test`."""
    m = re.search(rf"^_+ {re.escape(name.replace('::', '.'))} _+$", out, re.M)
    if not m:
        return None
    end = re.search(r"^(?:_+ [^_\s]\S* _+|={3,}.*)$", out[m.end():], re.M)  # next test's title or the summary banner, never a `_ _ _` frame rule
    groups, cur = [], []
    for ln in out[m.end(): m.end() + end.start() if end else len(out)].splitlines():
        e = re.match(r"^E\s+(.*)$", ln)
        if e:
            cur.append(e.group(1))
        elif cur:
            groups.append(cur)
            cur = []
    groups += [cur] if cur else []
    return _split_type(groups[-1][0]) if groups else None


def parse_test_results(out):
    """Per-test outcomes [(name, 'PASS'|'FAIL', exc_type|None, msg)] from a test run's output, or [] when it has no per-test lines.
    Formats: the frozen-test template (`RESULT <name> PASS|FAIL <Type>: <msg>`), `OK <name>` / `FAIL <name>: <Type>: <msg>`
    scripts, `pytest -rA` summary lines, and node TAP (`ok N - name` / `not ok N - name` + the YAML block's error name/code)."""
    out = out or ""
    for rx in (_PT_TEMPLATE, _PT_OKFAIL):
        rows = []
        for m in rx.finditer(out):
            st = "PASS" if m.group("st") in ("PASS", "OK") else "FAIL"
            t, msg = _split_type(m.group("rest")) if st == "FAIL" else (None, "")
            rows.append((m.group("name"), st, t, msg))
        if rows:
            return _dedupe_results(rows)
    rows = []
    for m in _PT_PYTEST.finditer(out):
        st = "PASS" if m.group("st") == "PASSED" else "FAIL"
        rest, name = m.group("rest"), m.group("name").split("::", 1)[-1]
        t, msg = (_split_type(rest) if rest else ("ERROR" if m.group("st") == "ERROR" else "unknown", "")) if st == "FAIL" else (None, "")
        if m.group("st") == "FAILED" and (not rest or rest.rstrip().endswith("...")):
            # pytest cuts the short summary to the terminal width ('AssertionE...'): read the type from the test's failure section
            got = _pytest_failure_type(out, name)
            cut = (rest or "").strip()[:-3]
            if got:
                t, msg = got
            elif len(cut) >= 4 and "AssertionError".startswith(cut):
                t = "AssertionError"
        rows.append((name, st, t, msg))
    if rows:
        return _dedupe_results(rows)
    ms = list(_PT_TAP.finditer(out))
    for i, m in enumerate(ms):
        if m.group("ind"):  # nested subtest lines: the top-level test line carries the verdict
            continue
        if m.group("st") == "ok":
            rows.append((m.group("name").strip(), "PASS", None, ""))
            continue
        block = out[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(out)]
        code = re.search(r"^\s*code: '?(\w+)'?", block, re.M)
        nm = re.search(r"^\s*name: '?(\w+)'?", block, re.M)
        err = re.search(r"^\s*error: [|>]?-?\s*'?(.*)$", block, re.M)
        if err and err.group(1).strip().rstrip("'") == "test failed" and code and code.group(1) == "ERR_TEST_FAILURE":
            # node <23's stand-in for a test file that crashed while loading (its own error is printed above it), the TAP
            # twin of the spec reporter's "'test failed'": no per-test verdict, so the caller runs the whole-output checks
            return []
        t = "AssertionError" if (code and code.group(1) == "ERR_ASSERTION") or (nm and nm.group(1) == "AssertionError") else \
            (nm.group(1) if nm else (code.group(1) if code else "Error"))
        rows.append((m.group("name").strip(), "FAIL", t, (err.group(1).strip() if err else "")[:300]))
    if rows:
        return _dedupe_results(rows)
    return _dedupe_results(_parse_node_spec(out))


def _spec_error(body):
    """(type, message) of one failure in node's spec report: the lines under its `\u2716 name (Nms)` line, e.g.
    `AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:` + `20 !== 19`, up to the stack trace."""
    txt = [ln.strip() for ln in body]
    k = next((i for i, t in enumerate(txt) if t), None)
    if k is None or txt[k] == "'test failed'":  # node's stand-in for a file that crashed while loading: its own error is above
        return None
    rest = []
    for t in txt[k + 1:]:
        if _PT_STACK.match(t):
            break
        if t:
            rest.append(t)
    m = _PT_SPEC_ERR.match(txt[k])
    if m and (m.group("m") is not None or m.group("t").endswith("Error")):
        typ = "AssertionError" if m.group("code") == "ERR_ASSERTION" else m.group("t")
        msg = " ".join([m.group("m") or "", *rest])
    else:  # a thrown non-Error value (`'plain string'`)
        typ, msg = "Error", " ".join([txt[k], *rest])
    return typ, re.sub(r"\s+", " ", msg).strip()[:300]


def _parse_node_spec(out):
    """node --test's spec reporter (its default since node 23, TTY or not): the top-level `\u2714 name (Nms)` /
    `\u2716 name (Nms)` lines, each red's error from the `\u2716 failing tests:` section (a suite or parent test takes its
    first failing child's). Skipped and todo tests are left out. [] when a red's error cannot be read (a test file that
    failed to load prints no section), so the caller falls back to the whole-output checks, as before this parser."""
    lines = (out or "").splitlines()
    cut = next((i for i, ln in enumerate(lines) if ln.strip() == "\u2716 failing tests:"), len(lines))
    errs, i = {}, cut + 1
    while i < len(lines):
        m = _PT_SPEC.match(lines[i])
        if m and not m.group("ind") and lines[i - 1].startswith("test at "):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("test at "):
                j += 1
            e = _spec_error(lines[i + 1:j])
            if e:
                errs.setdefault(m.group("name"), e)
            i = j
            continue
        i += 1
    rows, head = [], lines[:cut]
    for k, ln in enumerate(head):
        m = _PT_SPEC.match(ln)
        if not m or m.group("ind") or m.group("st") in ("\ufe63", "\u26a0") or re.search(r"#\s*(?:SKIP|TODO)\b", m.group("tail")):
            continue
        name = m.group("name")
        if m.group("st") == "\u2714":
            rows.append((name, "PASS", None, ""))
            continue
        e = errs.get(name)
        if e is None:
            start = next((x for x in range(k - 1, -1, -1) if head[x] == f"\u25b6 {name}"), None)
            for c in (head[start + 1:k] if start is not None else []):
                cm = _PT_SPEC.match(c)
                if cm and cm.group("st") == "\u2716" and cm.group("name") in errs:
                    e = errs[cm.group("name")]
                    break
        if e is None:
            return []
        rows.append((name, "FAIL", e[0], e[1]))
    return rows


def _dedupe_results(rows):
    seen, out = {}, []
    for r in rows:  # a test printed twice (e.g. retried harness) keeps its LAST verdict
        seen[r[0]] = r
    for r in rows:
        if seen.get(r[0]) is r:
            out.append(r)
    return out


# ---- stage 27 (2026-11-07): the crash the issue reports ----
# pyrato r1 9d4a4c405ac4: the issue's traceback ends in `ValueError: cannot reshape array of size 0 into shape (0,1323)`; the
# model's test reproduced it on the tests' own input as `... shape (0,3969)`, and G1 and the fallback rejected that red as a
# wrong-reason ValueError. A non-assertion red is the right reason when TASK.md's Problem section (the issue as reported, not the
# diagnosis) shows the same exception type with the same message once numbers are masked; a message the issue cut short (`...`)
# matches by its prefix. Short or one-word messages (`KeyError: 'abc'`) never match; HARD_BAD_RED and SHAPE_BAD_RED come first.
_REPORTED_ERR_RE = re.compile(r"^[ \t>|]*(?:E[ \t]+)?(?:[A-Za-z_]\w*\.)*([A-Z]\w*(?:Error|Exception))[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.M)
_REPORTED_MIN = 12  # characters of the masked message, which must also hold a space


def _norm_err(msg):
    """An exception message as reported_red compares it: its first non-blank line, hex and decimal numbers masked as N,
    whitespace collapsed, a trailing period dropped."""
    line = next((ln for ln in str(msg or "").splitlines() if ln.strip()), "")
    line = re.sub(r"\d+", "N", re.sub(r"0x[0-9a-fA-F]+", "N", line))
    return re.sub(r"\s+", " ", line).strip().rstrip(".")


def _problem_text(task):
    """TASK.md's Problem section (the issue as reported), else its requirement text (requirement_text)."""
    m = re.search(r"(?ms)^##[ \t]+Problem\b[^\n]*\n(.*?)(?=^##[ \t]+(?:Diagnosis|Entry point|Required change|Must keep|Test notes|"
                  r"Existing tests|Scope)\b|\Z)", task or "")
    return m.group(1) if m else requirement_text(task or "")


def _reported_errors(task):
    """[(type, masked message, cut)] for each `Type: message` line of the Problem section and each inline code span holding
    one; cut when the issue ended the message with ... (a prefix match)."""
    text, found = _problem_text(task), []
    for chunk in [text] + re.findall(r"`([^`\n]+)`", text):
        for m in _REPORTED_ERR_RE.finditer(chunk):
            msg, cut = m.group(2), False
            if msg.endswith(("...", "…")):
                msg, cut = msg.rstrip(".…").rstrip(), True
            found.append((m.group(1), _norm_err(msg), cut))
    return list(dict.fromkeys(found))


def reported_red(typ, msg, spec):
    """True when a baseline red is the exception the issue itself reports (see the block comment above)."""
    task = (spec or {}).get("_red_task")
    if not task:
        return False
    t, m = str(typ or "").rsplit(".", 1)[-1], _norm_err(msg)
    if len(m) < _REPORTED_MIN or " " not in m:
        return False
    return any(rt == t and len(rm) >= _REPORTED_MIN and " " in rm and (m == rm or (cut and m.startswith(rm)))
               for rt, rm, cut in _reported_errors(task))


def red_reason_problem(typ, msg, spec):
    """None when a baseline FAIL of this type/message is a right-reason red, else why not (G1)."""
    typ, msg = typ or "unknown", msg or ""
    if typ in HARD_BAD_RED:
        return f"{typ} (the test itself is broken; no fix to the code can repair it)"
    if typ in ("TypeError", "AttributeError") and protocol_red(typ, msg, spec):
        return None  # stage 26: the missing protocol TASK.md asks for (see _PROTO_RED)
    if typ in ("TypeError", "AttributeError") and SHAPE_BAD_RED.search(msg):
        return f"{typ}: {msg[:120]} (the test uses a value of the wrong shape, e.g. a helper that returns None)"
    if reported_red(typ, msg, spec):
        return None  # stage 27 (pyrato r1 9d4a4c405ac4): the crash the issue reports, reproduced
    if typ == "AssertionError":
        return None
    if any(x and x in msg for x in as_list(spec.get("stub_error"))):
        return None
    for e in spec.get("red_ok") or []:
        if isinstance(e, dict) and e.get("type") == typ and re.search(str(e.get("match", "")), msg):
            return None
    if spec.get("allow_error_fail"):
        return None
    return (f"{typ}: {msg[:120]} is not an AssertionError and matches no spec.red_ok entry. When TASK.md adds a method, attribute, "
            "parameter or protocol, assert it before the test uses it (python: `assert hasattr(Cls, \"name\")`, `assert \"param\" in "
            "inspect.signature(fn).parameters`; JS: `assert.strictEqual(typeof obj.name, \"function\")`) so the current code fails on "
            "that assert, not on the call; a red_ok list in the test file does nothing (for the spec's author: if this IS the intended "
            f"red, add {json.dumps(red_ok_suggestion(typ, msg))} to spec.red_ok)")


def red_ok_suggestion(typ, msg):
    """A concrete spec.red_ok entry for a red of this type: it matches the last quoted name in the message (the missing key or
    attribute: KeyError 'sandbox' -> "sandbox"), else the message's first word of 3+ characters."""
    q = re.findall(r"['\"]([^'\"\n]{1,60})['\"]", msg or "")
    w = q[-1] if q else next(iter(re.findall(r"[A-Za-z_][\w.]{2,}", msg or "")), "")
    return {"type": typ, "match": re.escape(w)}


# ---- stage 26 (2026-11-07): protocol reds ----
# A test of a protocol TASK.md adds fails at baseline with the interpreter's own TypeError, not an AssertionError: a2a's `async
# with transport` on a ClientTransport without __aenter__/__aexit__ raised "'_TestTransport' object does not support the
# asynchronous context manager protocol", and G1 rejected all three tests, then the fallback too. That red is the right reason when
# the object's class is one the task's files define (or a subclass of one that the test defines) and TASK.md names the protocol:
# one of its dunders, or its plain-words name. The fix that adds the protocol turns exactly that error into the test's own assertions.
_BIN_DUNDER = {"+": "add", "-": "sub", "*": "mul", "@": "matmul", "/": "truediv", "//": "floordiv", "%": "mod", "**": "pow",
               "** or pow()": "pow", "pow()": "pow", "<<": "lshift", ">>": "rshift", "&": "and", "^": "xor", "|": "or",
               "divmod()": "divmod"}
_UNARY_DUNDER = {"unary -": "__neg__", "unary +": "__pos__", "unary ~": "__invert__", "abs()": "__abs__"}
_CMP_DUNDER = {"<": ("__lt__", "__gt__"), ">": ("__gt__", "__lt__"), "<=": ("__le__", "__ge__"), ">=": ("__ge__", "__le__")}
_PROTO_RED = [(re.compile(rx), dunders, tuple(re.compile(p, re.I) for p in phrases)) for rx, dunders, phrases in (
    (r"does not support the asynchronous context manager protocol", ("__aenter__", "__aexit__"),
     (r"\basync(?:hronous)?\s+context\s+managers?\b", r"\basync\s+with\b")),
    (r"does not support the context manager protocol", ("__enter__", "__exit__"),
     (r"(?<!async )(?<!asynchronous )\bcontext\s+managers?\b", r"(?<!async )\bwith\s+statements?\b")),
    (r"'async for' requires an object with __aiter__ method|'async for' received an object from __aiter__ that does not implement "
     r"__anext__", ("__aiter__", "__anext__"), (r"\basync(?:hronous)?\s+(?:iterable|iterator|iteration)s?\b", r"\basync\s+for\b")),
    (r"object is not an async iterator", ("__anext__", "__aiter__"), (r"\basync(?:hronous)?\s+iterators?\b",)),
    (r"can't be used in 'await' expression|can't be awaited|object is not awaitable", ("__await__",), (r"\bawaitable\b",)),
    (r"object is not iterable|^argument of type '[\w.<>]+' is not (?:a container or )?iterable", ("__iter__", "__getitem__", "__contains__"),
     (r"\biterable\b",)),
    (r"object is not an iterator", ("__next__",), ()),
    (r"object is not reversible", ("__reversed__",), (r"\breversible\b",)),
    (r"has no len\(\)", ("__len__",), (r"\blen\(\)",)),
    (r"object is not subscriptable|^type '[\w.<>]+' is not subscriptable", ("__getitem__", "__class_getitem__"), (r"\bsubscriptable\b",)),
    (r"object does not support item assignment", ("__setitem__",), ()),
    (r"(?:doesn't|does not) support item deletion", ("__delitem__",), ()),
    (r"object is not callable", ("__call__",), ()),
    (r"unhashable type: '", ("__hash__",), (r"\bhashable\b",)),
    (r"cannot be interpreted as an integer", ("__index__",), ()),
    (r"doesn't define __round__ method", ("__round__",), ()),
    (r"argument must be a string(?:, a bytes-like object)? or a (?:real )?number, not", ("__int__", "__float__", "__index__", "__complex__"), ()),
    (r"expected str, bytes or os\.PathLike object, not", ("__fspath__",), (r"\bpath-?like\b",)),
    (r"unsupported format string passed to", ("__format__",), ()),
    (r"cannot convert '[\w.<>]+' object to bytes", ("__bytes__",), ()))]
_PROTO_TYPE_RES = [re.compile(p) for p in (
    r"^'(?P<t>[\w.<>]+)' object\b", r"^object of type '(?P<t>[\w.<>]+)'", r"unhashable type: '(?P<t>[\w.<>]+)'",
    r"^type '(?P<t>[\w.<>]+)' is not subscriptable", r"^argument of type '(?P<t>[\w.<>]+)'", r"__aiter__ method, got (?P<t>[\w.<>]+)",
    r"__anext__: (?P<t>[\w.<>]+)$", r"^object (?P<t>[\w.<>]+) can't be used in 'await' expression", r", not '(?P<t>[\w.<>]+)'$",
    r"object, not (?P<t>[\w.<>]+)$", r"^type (?P<t>[\w.<>]+) doesn't define", r"passed to (?P<t>[\w.<>]+)\.__format__",
    r"^cannot convert '(?P<t>[\w.<>]+)' object")]


def protocol_red(typ, msg, spec):
    """True when a baseline TypeError/AttributeError is the missing protocol TASK.md asks for (stage 26, see _PROTO_RED): the error
    names a protocol (with, async with, for, len, [], (), +, <, ...) on an object whose class the task's files define, or a
    subclass of one that the test defines (spec["_red_classes"], kept by _remember_task), and TASK.md (spec["_red_task"]) names that
    protocol: one of its dunders, or its plain-words name. A protocol error on None, an int or a class nobody here owns stays a
    wrong reason."""
    spec = spec or {}
    task = (spec.get("_red_task") or "").replace("\\_", "_")
    mine = set(spec.get("_red_classes") or ())
    msg = (msg or "").strip()
    if not task or not msg:
        return False

    def names(ds):
        return any(re.search(rf"(?<!\w){re.escape(d)}(?!\w)", task) for d in ds)

    def owner(t):
        return t.rsplit(".", 1)[-1] in mine

    if typ == "AttributeError":
        m = re.fullmatch(r"'?(__a?(?:enter|exit)__)'?", msg)  # python <= 3.10: `with x` / `async with x` without the dunder
        if m:
            return names((m.group(1),))
        m = re.match(r"(?:type object )?'([\w.<>]+)'(?: object)? has no attribute '(__\w+__)'", msg)
        return bool(m) and owner(m.group(1)) and names((m.group(2),))
    if typ != "TypeError" or not mine:
        return False
    m = re.search(r"unsupported operand type\(s\) for (.+?): '([\w.<>]+)' and '([\w.<>]+)'$", msg)
    if m:
        op = m.group(1)
        inplace = op.endswith("=") and op[:-1] in _BIN_DUNDER
        d = _BIN_DUNDER.get(op[:-1] if inplace else op)
        if not d:
            return False
        want = ([f"__i{d}__"] if inplace else []) + [f"__{d}__", f"__r{d}__"]
        return (owner(m.group(2)) or owner(m.group(3))) and names(want)
    m = re.search(r"'(<=|>=|<|>)' not supported between instances of '([\w.<>]+)' and '([\w.<>]+)'$", msg)
    if m:
        return (owner(m.group(2)) or owner(m.group(3))) and (names(_CMP_DUNDER[m.group(1)]) or bool(re.search(r"\btotal_ordering\b", task)))
    m = re.search(r"bad operand type for (unary [-+~]|abs\(\)): '([\w.<>]+)'$", msg)
    if m:
        return owner(m.group(2)) and names((_UNARY_DUNDER[m.group(1)],))
    for rx, dunders, phrases in _PROTO_RED:
        if rx.search(msg):
            t = next((m.group("t") for r in _PROTO_TYPE_RES for m in [r.search(msg)] if m), None)
            return bool(t) and owner(t) and (names(dunders) or any(p.search(task) for p in phrases))
    return False


def _remember_task(spec, cwd, files=()):
    """Keep what protocol_red reads in the spec's private keys: TASK.md's text, and the classes the task's files define plus the
    test file's subclasses of them, transitively (a2a's red named the test's own `class _TestTransport(ClientTransport)`; a class
    the test defines on another base cannot gain the protocol from the fix). A sorted list, so the spec stays JSON-safe."""
    try:
        spec["_red_task"] = Path(cwd, "TASK.md").read_text(errors="replace")
    except OSError:
        spec["_red_task"] = ""
    tf, found = spec.get("test_file"), set()
    for f in dict.fromkeys(as_list(files)):
        if str(f).endswith(".py") and f != tf:
            try:
                found.update(re.findall(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", Path(cwd, f).read_text(errors="replace"), re.M))
            except OSError:
                pass
    if tf and str(tf).endswith(".py") and found:
        try:
            tree = ast.parse(Path(cwd, tf).read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            tree = None
        subs = [(n.name, " ".join(ast.unparse(b) for b in n.bases)) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)] if tree else []
        grew = True
        while grew:
            grew = False
            for name, bases in subs:
                if name not in found and any(re.search(rf"(?<!\w){re.escape(c)}(?!\w)", bases) for c in found):
                    found.add(name)
                    grew = True
    spec["_red_classes"] = sorted(found)


def declared_tests(text, path):
    """(names, explicit): the TESTS = [...] list of a python test (explicit=True), else its top-level test_ functions."""
    if lang_of(path or "x.py") != "py":
        return None, False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None, False
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TESTS" for t in n.targets) and isinstance(n.value, (ast.List, ast.Tuple)):
            return [e.id if isinstance(e, ast.Name) else ast.unparse(e) for e in n.value.elts], True
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")], False


def declared_guards(text, path):
    """The test file's GUARDS: tests of behaviour the fix keeps, which must PASS on the current code and after the fix (python: a
    top-level `GUARDS = [test_fn, ...]`, names or strings; JS/TS: `const GUARDS = ["test title", ...]`). [] when absent.
    A guard is the model's claim about what the code does today, and the baseline run checks that claim: tomlarr2 2026-11-03 wrote
    a 'still works as before' test whose expected text dropped the trailing newline, kept it red at baseline (every test had to
    be), and phase B then stripped the newline from every output to turn it green."""
    if lang_of(path or "x.py") == "py":
        try:
            tree = ast.parse(text or "")
        except (SyntaxError, ValueError):
            return []
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "GUARDS" for t in n.targets) \
                    and isinstance(n.value, (ast.List, ast.Tuple)):
                return [e.id if isinstance(e, ast.Name) else e.value if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        else ast.unparse(e) for e in n.value.elts]
        return []
    m = re.search(r"\b(?:const|let|var)\s+GUARDS\s*=\s*\[([^\]]*)\]", text or "")
    return re.findall(r"""["'`]([^"'`\n]+)["'`]""", m.group(1)) if m else []


def guards_hint(tf):
    """How the test file tf declares its guards (the phase-A and auto prompts)."""
    if lang_of(tf) == "py":
        return ("`GUARDS = [test_fn, ...]` next to TESTS (a guard stays in TESTS too: GUARDS only marks which of the TESTS are guards; "
                "keep the main block as it is, one RESULT line per test in TESTS)")
    return "`const GUARDS = [\"test title\", ...]` (titles of top-level `test(...)` calls, not inside a describe)"


def _last_result_pos(out):
    """Where the last per-test line of a run's output ends (0 when none): the formats parse_test_results reads."""
    end = 0
    for rx in (_PT_TEMPLATE, _PT_OKFAIL, _PT_PYTEST, _PT_TAP):
        for m in rx.finditer(out):
            end = max(end, m.end())
    return end


def crash_hit(out, per):
    """Stage 25: the CRASH_RE match that counts against a red run. With no per-test line it is any match, as before. With per-test
    lines it is a run-level marker anywhere (a collection error, no tests ran, a timeout) or a crash after the last per-test line
    (the run died past its verdicts): a traceback the code under test logs on its way to the verdicts is not one. scrapy logs an
    optional download handler's ModuleNotFoundError on every run, which rejected right-reason reds (phase A r3, the fallback 11-06)."""
    out = out or ""
    if not per:
        return CRASH_RE.search(out)
    return RUNLEVEL_CRASH_RE.search(out) or CRASH_RE.search(out, _last_result_pos(out))


def fb_red_problem(typ, msg, spec):
    """The fallback's per-test wrong-reason check: red_reason_problem, except that a FAIL line whose text names no exception type
    (`FAIL test_x: Expected None, got <Spider>`: a plain assertion message, not an exception's repr) is a right-reason red when it
    says something. The test is this run's own and not frozen: the review reads it and the baseline output either way."""
    typ, msg = typ or "", msg or ""
    if typ != "AssertionError" and not _TYPE_RE.match(typ + ": ") and msg.strip() and not _EXC_WORD_RE.search(typ + " " + msg):
        return None
    return red_reason_problem(typ, msg, spec)


def per_test_red_problems(results, spec, text, path, targets=None):
    """G1: every frozen test red at baseline, each for an allowed reason, and as many results as declared tests; and (python)
    not every red test a presence check on the code's own text (py_source_only_tests; spec.source_checks "allow" skips it).
    A test the file lists in GUARDS (declared_guards; spec.guards false ignores the list) is the reverse: it must PASS at baseline,
    and the tests outside GUARDS must still number spec.min_tests red ones (checked as well when a test outside GUARDS is
    green, since its advice is to make it a guard)."""
    bad = []
    guards = set() if spec.get("guards") is False else set(declared_guards(text or "", path))
    green_ok = set(as_list(spec.get("baseline_green"))) | guards
    passing = [r[0] for r in results if r[1] == "PASS" and r[0] not in green_ok]
    for name, st, typ, msg in results:
        if st == "PASS":
            if name not in green_ok:
                bad.append(f"{name} PASSES at baseline (every frozen test must be red unless it is a guard: if it checks behaviour the "
                           "fix must keep, list it in the test file's GUARDS = [...] (or spec.baseline_green "
                           f"{json.dumps(as_list(spec.get('baseline_green')) + passing)}); never add a bug assertion to a guard to make it fail)")
            continue
        if name in guards:
            bad.append(f"{name} is in GUARDS (behaviour the fix keeps) but FAILS on the current code ({typ}: {(msg or '').strip()[:240]}): "
                       "its expectation of what the code does today is wrong. Run the code on that input to see the current value and fix "
                       "the expected value, or take the test out of GUARDS if it shows the bug")
            continue
        why = red_reason_problem(typ, msg, spec)
        if why:
            bad.append(f"{name} red for the wrong reason: {why}")
    if guards:
        unknown = sorted(guards - {r[0] for r in results})
        if unknown:
            bad.append(f"GUARDS names {unknown}, which are not tests that ran (python: a guard must also be in TESTS, which the main "
                       "block runs, one `RESULT <name> PASS|FAIL` line each: GUARDS only marks which of the TESTS are guards; "
                       "JS: titles of top-level test(...) calls, not of tests inside a describe)")
    if guards or (passing and spec.get("guards") is not False):
        # A green test outside GUARDS gets the advice above to list it as a guard, so say now when that would leave too few red
        # tests (moto r3 2026-11-06: A1 had one red test and one green one, A2 made the green one a guard and was rejected for
        # the count, which this check reported only once GUARDS was non-empty).
        n_red = sum(1 for r in results if r[1] != "PASS" and r[0] not in guards)
        need = int(spec.get("min_tests", 2))
        if n_red < need:
            bad.append(f"only {n_red} test(s) outside GUARDS fail at baseline; need >= {need} that show the bug (guards do not count)")
    names, explicit = declared_tests(text or "", path)
    if names:
        got = len(results)
        if (explicit and got != len(names)) or (not explicit and got < len(names)):
            bad.append(f"{got} per-test result(s) but the test declares {len(names)} ({'TESTS list' if explicit else 'test_ functions'}): the harness stopped early or skips tests")
    reds = [r for r in results if r[1] != "PASS"]
    if reds and lang_of(path or "") == "py" and spec.get("source_checks") != "allow" and spec.get("test_rules") != "off":
        only = py_source_only_tests(text or "", targets if targets is not None else as_list(spec.get("allowed")), reds)
        if len(only) == len(reds):
            bad.append(f"every test red at baseline ({', '.join(sorted(only)[:4])}) only checks the source text of the code "
                       "under test: " + SOURCE_CHECK_ADVICE)
    return bad


def _py_scope_bound(body):
    """Names a statement list binds in its own scope (not descending into nested def/class/lambda bodies), and its `global` names."""
    bound, glob = set(), set()
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            stack.extend(n.decorator_list)
            if not isinstance(n, ast.ClassDef):
                stack.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.Lambda):
            stack.extend(n.args.defaults)
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            glob.update(n.names)
        elif hasattr(ast, "MatchAs") and isinstance(n, (ast.MatchAs, ast.MatchStar)) and getattr(n, "name", None):
            bound.add(n.name)
        stack.extend(ast.iter_child_nodes(n))
    return bound, glob


def _py_scope_loads(body):
    """(name, line) loads in a statement list's own scope, plus the nested function/lambda/class nodes."""
    loads, nested, stack = [], [], list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            nested.append(n)
            stack.extend(getattr(n, "decorator_list", []))
            if not isinstance(n, ast.ClassDef):
                stack.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loads.append((n.id, n.lineno))
        stack.extend(ast.iter_child_nodes(n))
    return loads, nested


def py_undefined_names(tree):
    """[(name, function, line)] for names a function reads that no enclosing scope, the module or builtins binds (pyflakes F821-lite)."""
    import builtins
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
        return []
    known = set(dir(builtins)) | {"__file__", "__name__", "__builtins__", "__doc__", "__spec__", "__loader__", "__package__", "__path__", "__class__"}
    mod_bound, _ = _py_scope_bound(tree.body)
    for n in ast.walk(tree):
        if isinstance(n, ast.Global):
            mod_bound.update(n.names)
    out = []

    def visit(fn, enclosing):
        args = fn.args if not isinstance(fn, ast.ClassDef) else None
        params = set()
        if args:
            params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs} | {a.arg for a in (args.vararg, args.kwarg) if a}
        body = fn.body if isinstance(fn.body, list) else [fn.body]
        local, _g = _py_scope_bound(body)
        scope = enclosing | params | local
        loads, nested = _py_scope_loads(body)
        for name, line in loads:
            if name not in scope and name not in mod_bound and name not in known:
                out.append((name, getattr(fn, "name", "<lambda>"), line))
        for c in nested:
            visit(c, scope if not isinstance(fn, ast.ClassDef) else enclosing)

    loads, nested = _py_scope_loads(tree.body)
    for name, line in loads:
        if name not in mod_bound and name not in known:
            out.append((name, "<module>", line))
    for c in nested:
        visit(c, set())
    return out


_SRC_READ_FNS = ("inspect.getsource", "inspect.getsourcelines", "inspect.findsource", "linecache.getline",
                 "linecache.getlines", "tokenize.open")
_SRC_PATH_FNS = ("inspect.getfile", "inspect.getsourcefile")
_OPEN_FNS = ("open", "io.open", "codecs.open", "builtins.open")
_PATH_WRAP_FNS = ("str", "os.fspath", "os.path.abspath", "os.path.realpath", "os.path.normpath", "os.path.expanduser",
                  "os.path.join")
_TEXT_FNS = ("str", "len", "sorted", "list", "set", "tuple", "frozenset", "dict", "any", "all", "sum", "min", "max",
             "enumerate", "filter", "map", "reversed", "zip", "repr", "next", "iter", "bool", "getattr", "hasattr",
             "isinstance", "type")
_TEXT_MODS = ("re.", "ast.", "textwrap.", "hashlib.", "difflib.", "tokenize.", "fnmatch.")
_TEXT_METHODS = ("search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split", "join", "count", "visit")
_SIDE_METHODS = ("append", "extend", "add", "update", "insert", "visit", "generic_visit", "setdefault")
_EXEC_RE = re.compile(r"exec|eval|compile|run|call|popen|system|spawn|bash|shell|source|^sh$", re.I)
_CHECK_RE = re.compile(r"^(assert|check|expect|verify|ensure|require)", re.I)
_ABSENT_CALLS = ("assertnotin", "assertisnone", "assertnotregex", "assertnotregexpmatches")


def _py_alias(tree, stems):
    """Names a python test binds at module level to the code under test (imports of it, assignments naming its file,
    loader calls), plus loader assignments to `global` names inside functions."""
    stems_n = {s.replace("-", "_") for s in stems} | set(stems)
    alias = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            alias |= {a.asname or a.name.split(".")[0] for a in n.names if a.name.split(".")[0].replace("-", "_") in stems_n or a.name in stems_n}
        elif isinstance(n, ast.Assign) and any(isinstance(c, ast.Constant) and isinstance(c.value, str) and
                                               re.sub(r"\.py$", "", c.value.rsplit("/", 1)[-1]) in stems for c in ast.walk(n.value)):
            alias |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    loader = {"__import__", "import_module", "module_from_spec", "reload", "load_module", "load_source", "run_path"}
    is_load = lambda v: any(isinstance(c, ast.Call) and (_dotted(c.func) or "").rsplit(".", 1)[-1] in loader for c in ast.walk(v))
    for n in tree.body:
        if isinstance(n, ast.Assign) and is_load(n.value):
            alias |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _l, glob = _py_scope_bound(fn.body)
            for c in ast.walk(fn):
                if isinstance(c, ast.Assign) and is_load(c.value):
                    alias |= {t.id for t in c.targets if isinstance(t, ast.Name) and t.id in glob}
    return alias


SOURCE_CHECK_ADVICE = ("a check on the code's text or AST turns green when the fix merely contains that text, so it proves nothing "
                       "about behaviour. Run the code and assert on what it does (return values, raised errors, the files or "
                       "output it produces); loading it with exec or copying it into a sandbox is fine, and asserting that "
                       "removed text is gone is fine. (Operator: a deliberate wiring check can set spec.source_checks \"allow\" "
                       "with a spec.source_checks_reason.)")


def py_source_checks(text, tree, alias, code_files):
    """Where a python test checks the TEXT or AST of the code under test instead of running it (Fable review #4: such a
    frozen test turns green when the fix merely contains the text, so it proves nothing about behaviour).
    Sources: inspect.getsource / linecache / tokenize, or open() / read_text() / read_bytes() of a path naming a code
    target (code_files: basenames) or a module alias's __file__. The text carries through names (position-aware: a
    later rebinding to other data ends it), slicing, str/re/ast helpers and local helper functions that return it.
    Sinks: assert, assert*/check* calls, an if whose one branch raises/fails/exits, and a test function's return value.
    Returns (hits, per_fn). hits = [(line, snippet)] of PRESENCE checks ("x" in src, re.search(..), AST names, counts).
    Not hits: absence checks ("x" not in src, re.search(..) is None: what a removal TASK asks for) and found-it guards on
    text the test then executes (`assert m` before exec(m.group(0)) or bash -c). Loading the code with exec, or copying
    it into a sandbox, is no check at all. per_fn = {function name or "<module>": [presence hits, other assertions
    (behaviour, and absence checks: a removal test's "keep" lines are presence checks next to them), names of the
    functions it calls]}."""
    code_files = set(code_files or ())
    alias = set(alias or ())
    if not code_files and not alias:
        return [], {}
    is_def = lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))

    def split(stmts):                 # this scope's nodes, and the defs it owns (each def body is a scope of its own)
        nodes, defs, stack = [], [], list(stmts)
        while stack:
            n = stack.pop()
            if is_def(n):
                defs.append(n)
                continue
            nodes.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return nodes, defs

    def bind(t, out):
        if isinstance(t, ast.Name):
            out.append(t)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                bind(e, out)
        elif isinstance(t, ast.Starred):
            bind(t.value, out)

    def flows(n):                     # (target, value) pairs a node binds
        if isinstance(n, ast.Assign):
            return [(t, n.value) for t in n.targets]
        if isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value is not None:
            return [(n.target, n.value)]
        if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            return [(n.target, n.iter)]
        if isinstance(n, ast.withitem) and n.optional_vars is not None:
            return [(n.optional_vars, n.context_expr)]
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _SIDE_METHODS
                and isinstance(n.func.value, ast.Name)):   # acc.append(src_line): acc now carries the text too
            prior = ast.Name(id=n.func.value.id, ctx=ast.Load(), lineno=n.lineno, col_offset=n.col_offset)
            tgt = ast.Name(id=n.func.value.id, ctx=ast.Store(), lineno=n.lineno, col_offset=n.col_offset + 1)
            return [(tgt, ast.Tuple(elts=list(n.args) + [k.value for k in n.keywords] + [prior], ctx=ast.Load()))]
        return []

    def names_in(n):
        return {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}

    # names whose value the test executes (exec/compile/subprocess/bash...): a found-it guard on them is a precondition
    fed = set()
    for c in ast.walk(tree):
        if isinstance(c, ast.Call):
            d = _dotted(c.func) or ""
            if _EXEC_RE.search(d.rsplit(".", 1)[-1]) and not d.startswith(("re.", "ast.")):
                for a in list(c.args) + [k.value for k in c.keywords]:
                    fed |= names_in(a)
    all_flows = [fl for n in ast.walk(tree) for fl in flows(n)]
    for _ in range(8):
        k = len(fed)
        for t, v in all_flows:
            tn = []
            bind(t, tn)
            if any(x.id in fed for x in tn):
                fed |= names_in(v)
        if len(fed) == k:
            break

    def emptyish(r):
        if isinstance(r, ast.Constant):
            return r.value is None or (r.value in (0, "", b"") and r.value is not True)
        if isinstance(r, (ast.List, ast.Tuple, ast.Set)):
            return not r.elts
        if isinstance(r, ast.Dict):
            return not r.keys
        return isinstance(r, ast.Call) and _dotted(r.func) in ("set", "list", "dict", "tuple", "frozenset") and not r.args

    helpers, hits, per_fn, returns = set(), [], {}, set()

    def visit(stmts, fname, path_in, tracked_in):
        nodes, defs = split(stmts)
        path = set(path_in)
        binds = {}                    # name -> [[(line, col), value, carries text]]
        for n in nodes:
            for t, v in flows(n):
                tn = []
                bind(t, tn)
                for x in tn:
                    binds.setdefault(x.id, []).append([(x.lineno, x.col_offset), v, False])

        def names_target(n):          # does this path expression name a code target file?
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                return n.value.rstrip("/").rsplit("/", 1)[-1] in code_files
            if isinstance(n, ast.JoinedStr):
                return bool(n.values) and names_target(n.values[-1])
            if isinstance(n, ast.Name):
                return n.id in path
            if isinstance(n, ast.Attribute):
                return n.attr in ("__file__", "origin") and isinstance(n.value, ast.Name) and n.value.id in alias
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                return names_target(n.right)          # ROOT / "calc.py"; .parent / "fixtures" / "x.json" is not it
            if isinstance(n, ast.Call):
                d = _dotted(n.func) or ""
                if d in _SRC_PATH_FNS:
                    return True
                if d in _PATH_WRAP_FNS or d.rsplit(".", 1)[-1] in ("Path", "PurePath", "PosixPath"):
                    return bool(n.args) and names_target(n.args[-1])
                if isinstance(n.func, ast.Attribute) and n.func.attr in ("resolve", "absolute", "expanduser"):
                    return names_target(n.func.value)
                if isinstance(n.func, ast.Attribute) and n.func.attr == "joinpath":
                    return bool(n.args) and names_target(n.args[-1])
            return False

        def writes(mode):
            return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(c in mode.value for c in "wax")

        def is_read(c):
            d = _dotted(c.func) or ""
            kw = {k.arg: k.value for k in c.keywords if k.arg}
            if d in _SRC_READ_FNS:
                return True
            if d in _OPEN_FNS:
                p = c.args[0] if c.args else kw.get("file")
                return p is not None and not writes(c.args[1] if len(c.args) > 1 else kw.get("mode")) and names_target(p)
            if isinstance(c.func, ast.Attribute) and c.func.attr in ("read_text", "read_bytes"):
                return names_target(c.func.value)
            if isinstance(c.func, ast.Attribute) and c.func.attr == "open" and d not in _OPEN_FNS:
                return not writes(c.args[0] if c.args else kw.get("mode")) and names_target(c.func.value)
            return False

        def prop(n):                  # does this expression carry the target's source text (or its AST)?
            if isinstance(n, ast.Name):
                bl = binds.get(n.id)
                if not bl:
                    return n.id in tracked_in
                pos = (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))
                prev = [b for b in bl if b[0] < pos]
                return max(prev, key=lambda b: b[0])[2] if prev else any(b[2] for b in bl)
            if isinstance(n, ast.Call):
                if is_read(n):
                    return True
                f, d = n.func, _dotted(n.func) or ""
                args = list(n.args) + [k.value for k in n.keywords]
                if isinstance(f, ast.Attribute) and (prop(f.value) or (f.attr in _TEXT_METHODS and any(prop(a) for a in args))):
                    return True
                if d.rsplit(".", 1)[-1] in helpers and (isinstance(f, ast.Name) or d.startswith(("self.", "cls."))):
                    return True
                return (d in _TEXT_FNS or d.startswith(_TEXT_MODS)) and any(prop(a) for a in args)
            if isinstance(n, (ast.Attribute, ast.Subscript, ast.Starred, ast.NamedExpr)):
                return prop(n.value)
            if isinstance(n, ast.Compare):
                return prop(n.left) or any(prop(c) for c in n.comparators)
            if isinstance(n, ast.BinOp):
                return prop(n.left) or prop(n.right)
            if isinstance(n, ast.BoolOp):
                return any(prop(v) for v in n.values)
            if isinstance(n, ast.UnaryOp):
                return prop(n.operand)
            if isinstance(n, ast.IfExp):
                return prop(n.test) or prop(n.body) or prop(n.orelse)
            if isinstance(n, ast.JoinedStr):
                return any(prop(v.value) for v in n.values if isinstance(v, ast.FormattedValue))
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                return prop(n.elt) or any(prop(g.iter) for g in n.generators)
            if isinstance(n, ast.DictComp):
                return prop(n.key) or prop(n.value) or any(prop(g.iter) for g in n.generators)
            if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
                return any(prop(e) for e in n.elts)
            if isinstance(n, ast.Dict):
                return any(prop(e) for e in n.keys + n.values if e is not None)
            return False

        for _ in range(6):            # path names: CODE = HERE / "calc.py"
            k = len(path)
            for n in nodes:
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and names_target(n.value):
                    tn = []
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        bind(t, tn)
                    path |= {x.id for x in tn}
            if len(path) == k:
                break
        for _ in range(10):           # which bindings carry the text (monotone fixpoint)
            changed = False
            for bl in binds.values():
                for b in bl:
                    if not b[2] and prop(b[1]):
                        b[2] = changed = True
            if not changed:
                break

        def absent(e):                # does this condition assert that something is NOT in the text?
            if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
                return not absent(e.operand)
            if isinstance(e, ast.BoolOp):
                parts = [v for v in e.values if prop(v)]
                return bool(parts) and all(absent(v) for v in parts)
            if isinstance(e, ast.Compare):
                if all(isinstance(o, ast.NotIn) for o in e.ops):
                    return True
                if len(e.ops) == 1 and emptyish(e.comparators[0]):
                    return isinstance(e.ops[0], (ast.Is, ast.Eq, ast.LtE))
                if len(e.ops) == 1 and isinstance(e.ops[0], ast.Lt) and isinstance(e.comparators[0], ast.Constant) and e.comparators[0].value == 1:
                    return True
                return False
            if isinstance(e, ast.Call) and _dotted(e.func) == "all" and e.args and isinstance(e.args[0], (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                return absent(e.args[0].elt)
            return False

        def fedname(x):
            if isinstance(x, ast.Call) and _dotted(x.func) == "len" and x.args:
                x = x.args[0]
            return isinstance(x, ast.Name) and x.id in fed

        def guard(e, neg):            # "found it" on text the test then executes: a precondition, not a check
            while isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
                e, neg = e.operand, not neg
            if fedname(e):
                return not neg
            if isinstance(e, ast.Compare) and len(e.ops) == 1 and fedname(e.left):
                op, r = e.ops[0], e.comparators[0]
                if emptyish(r):
                    return (isinstance(op, (ast.IsNot, ast.NotEq, ast.Gt)) and not neg) or (isinstance(op, (ast.Is, ast.Eq)) and neg)
                if isinstance(op, ast.GtE) and isinstance(r, ast.Constant) and r.value == 1:
                    return not neg
            return False

        def fails(stmts):             # does this branch end the test red?
            for s in stmts:
                for x in ([s] if is_def(s) else ast.walk(s)):
                    if is_def(x):
                        continue
                    if isinstance(x, ast.Raise):
                        return True
                    if isinstance(x, ast.Call):
                        d = _dotted(x.func) or ""
                        last = d.rsplit(".", 1)[-1]
                        if last == "fail" or (d in ("sys.exit", "exit", "os._exit") and not (x.args and _num(x.args[0]) == 0)):
                            return True
                        if last == "print" and any(isinstance(c, ast.Constant) and isinstance(c.value, str) and "FAIL" in c.value
                                                   for a in x.args for c in ast.walk(a)):
                            return True
                    if (isinstance(x, ast.Return) and fname.startswith("test") and isinstance(x.value, ast.Constant)
                            and x.value.value in (False, "FAIL")):
                        return True
            return False

        st = per_fn.setdefault(fname, [0, 0, set()])
        for n in nodes:
            if isinstance(n, ast.Call):
                d = _dotted(n.func) or ""
                if isinstance(n.func, ast.Name) or d.startswith(("self.", "cls.")):
                    st[2].add(d.rsplit(".", 1)[-1])
            cond, neg = None, False
            if isinstance(n, ast.Assert):
                cond = n.test
            elif isinstance(n, ast.If):
                fb, fo = fails(n.body), fails(n.orelse)
                if fb != fo:
                    cond, neg = n.test, fb    # the failing branch runs when the asserted condition is false
            elif isinstance(n, ast.Return) and n.value is not None:
                if prop(n.value):
                    returns.add(fname)
                if fname.startswith("test"):
                    cond = n.value
            elif isinstance(n, ast.Call):
                last = (_dotted(n.func) or "").rsplit(".", 1)[-1]
                if _CHECK_RE.match(last) and not last.endswith(("Error", "Exception", "Warning")):  # not AssertionError(..)
                    args = list(n.args) + [k.value for k in n.keywords]
                    src = [a for a in args if prop(a)]
                    if not src:
                        st[1] += 1
                        continue
                    low = last.lower()
                    if low in ("asserttrue", "assertisnotnone") and guard(src[0], False):
                        continue
                    if (low in _ABSENT_CALLS or (low == "assertfalse" and not absent(src[0]))
                            or (low in ("assertequal", "assertequals") and any(emptyish(a) for a in args))
                            or (low != "assertfalse" and low.startswith(("asserttrue", "check", "expect", "verify", "ensure", "require"))
                                and absent(src[0]))):
                        st[1] += 1            # an absence check: what a removal TASK asks for
                        continue
                    st[0] += 1
                    hits.append((n.lineno, " ".join((ast.get_source_segment(text, n) or "").split())[:100]))
                continue
            if cond is None:
                continue
            if not prop(cond):
                st[1] += 1
                continue
            if guard(cond, neg):
                continue
            if absent(cond) != neg:
                st[1] += 1                    # an absence check: what a removal TASK asks for
                continue
            st[0] += 1
            hits.append((n.lineno, " ".join((ast.get_source_segment(text, cond) or "").split())[:100]))
        for f in defs:
            loc, glob = _py_scope_bound(f.body)
            a = f.args
            shadow = (loc - glob) | {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | {x.arg for x in (a.vararg, a.kwarg) if x}
            inherited = {nm for nm, bl in binds.items() if any(b[2] for b in bl)} | set(tracked_in)
            visit(f.body, f.name, path - shadow, inherited - shadow)

    for _ in range(4):                # helpers that return the text make their call sites carry it too
        hits, per_fn, returns = [], {}, set()
        visit(tree.body, "<module>", set(), set())
        if returns <= helpers:
            break
        helpers |= returns
    return sorted(set(hits)), per_fn


def py_source_only_tests(text, targets, results=None):
    """Names of the python test functions (or, given per-test results, of those results) whose assertions -- their own
    and those of the local helpers they call -- are only presence checks on the code's text (py_source_checks)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    code = [Path(t).name for t in targets or () if lang_of(t) or Path(t).suffix in ("", ".sh", ".bash", ".zsh")]
    hits, per_fn = py_source_checks(text, tree, _py_alias(tree, _stems(targets or [])), code)
    if not hits:
        return set()

    def agg(name, seen):
        if name in seen or name not in per_fn:
            return 0, 0
        seen.add(name)
        p, b, called = per_fn[name]
        for c in called:
            cp, cb = agg(c, seen)
            p, b = p + cp, b + cb
        return p, b

    out = set()
    names = [r[0] for r in results] if results is not None else [k for k in per_fn if k.startswith("test")]
    for nm in names:
        fn = next((w for w in reversed(re.findall(r"[A-Za-z_]\w*", nm)) if w in per_fn), None)
        if fn:
            p, b = agg(fn, set())
            if p and not b:
                out.add(nm)
    return out


def py_test_rules(text, stems, code_files=(), source_checks=True):
    """G5 frozen-test static rules for a python test (empty list = fine): explicit test order (no globals()/dir() discovery), the module
    alias rebound only through one `global` helper, no undefined names, no bare except, no sys.exit(0) inside a test, and not a
    test whose every assertion is a presence check on the code's own text (code_files: basenames of the code targets;
    py_source_checks; source_checks=False when the spec sets source_checks "allow")."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    bad = []
    for n in ast.walk(tree):
        iters = [n.iter] if isinstance(n, (ast.For, ast.AsyncFor)) else [g.iter for g in n.generators] if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)) else []
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("sorted", "list", "filter", "map", "reversed", "tuple") and n.args:
            iters.append(n.args[-1])
        for it in iters:
            if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in ("globals", "dir", "vars", "locals") for c in ast.walk(it)):
                bad.append(f"test discovery via globals()/dir() (line {n.lineno}): list the tests explicitly, TESTS = [test_a, test_b, ...]")
                break
        if bad and bad[-1].startswith("test discovery"):
            break
    alias = _py_alias(tree, stems)  # module alias: names bound to the code under test
    rebinders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local, glob = _py_scope_bound(fn.body)
        for a in sorted(alias & local):
            if a in glob:
                rebinders.append(fn.name)
            else:
                bad.append(f"`{fn.name}` assigns the module alias `{a}` without `global` (UnboundLocalError at its first use): "
                           f"rebind it only inside one reload helper that declares `global {a}`")
    if len(set(rebinders)) > 1:
        bad.append(f"the module alias is rebound in {len(set(rebinders))} functions {sorted(set(rebinders))}: only one reload helper may do it")
    und = py_undefined_names(tree)
    if und:
        bad.append("undefined name(s): " + ", ".join(f"`{nm}` in {fn} (line {ln})" for nm, fn, ln in und[:6]))
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler) and n.type is None:
            bad.append(f"bare `except:` in the test (line {n.lineno}): catch Exception and report it")
            break
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and _dotted(c.func) in ("sys.exit", "exit", "os._exit") and c.args and _num(c.args[0]) == 0:
                    bad.append(f"`{fn.name}` calls {_dotted(c.func)}(0): a test must never end the run green")
    if source_checks:
        hits, per_fn = py_source_checks(text, tree, alias, code_files)
        if hits and not any(v[1] for v in per_fn.values()):
            ln, seg = hits[0]
            bad.append(f"every assertion checks the source text of the code under test (line {ln}: `{seg}`"
                       + (f"; +{len(hits) - 1} more" if len(hits) > 1 else "") + "): " + SOURCE_CHECK_ADVICE)
    return bad


NO_HACKS = [
    (re.compile(r"\bsys\._getframe\b|\binspect\.(?:currentframe|stack|getouterframes|getframeinfo|trace)\b|\.f_back\b|\.f_globals\b|\.f_locals\b|\.f_code\b"), "frame introspection"),
    (re.compile(r"\bglobals\(\)\s*\[|\bglobals\(\)\.(?:update|setdefault|__setitem__)\b|\bvars\(\)\s*\[|\blocals\(\)\s*\["), "writes through globals()/vars()/locals()"),
    (re.compile(r"\bsys\.modules\[[^\]]*\]\.\w+\s*=(?!=)|\bsetattr\(\s*sys\.modules"), "writes into another module via sys.modules"),
    (re.compile(r"__builtins__|\bbuiltins\.\w+\s*=(?!=)"), "touches builtins"),
    (re.compile(r"__main__"), "references __main__"),
    (re.compile(r"PYTEST_CURRENT_TEST|for test compat|test compatibility", re.I), "test-detection / test-compat code"),
    (re.compile(r"^\s*(?:import|from)\s+(?:unittest|pytest)\b"), "imports a test framework in non-test code"),
    (re.compile(r"new Error\(\)\.stack|\barguments\.callee\b|\bglobalThis\.\w+\s*=(?!=)|\brequire\.cache\b"), "JS runtime introspection / global write"),
]


def added_lines(cwd, allowed, base_texts):
    """[(file, line_no, text)] for lines the fix added (difflib against the baseline; a new file is all added)."""
    out = []
    for f in allowed:
        p = Path(cwd, f)
        if not p.is_file():
            continue
        new = p.read_text(errors="replace").splitlines()
        base = (base_texts.get(f) or "").splitlines()
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, base, new, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                out += [(f, j + 1, new[j]) for j in range(j1, j2)]
    return out


def no_hacks_check(cwd, spec, allowed, base_texts):
    """G4 anti-gaming scan on the + lines of the allowed files. (ok, detail); soft hits (except/pass around a target call) only WARN."""
    if spec.get("no_hacks") in (False, "off"):
        return True, "off"
    adds = added_lines(cwd, allowed, base_texts)
    stems = {Path(t).stem for t in [spec.get("test_file"), *as_list(spec.get("update_tests"))] if t}
    stem_rx = re.compile("|".join(rf"\b{re.escape(s)}\b" for s in sorted(stems) if len(s) > 4)) if any(len(s) > 4 for s in stems) else None
    hits, soft = [], []
    for f, ln, txt in adds:
        for rx, why in NO_HACKS:
            if rx.search(txt):
                hits.append(f"{f}:{ln} {why}: `{txt.strip()[:100]}`")
                break
        else:
            if stem_rx and stem_rx.search(txt):
                hits.append(f"{f}:{ln} names the test file: `{txt.strip()[:100]}`")
    tg = as_list(spec.get("target_fn"))
    for i, (f, ln, txt) in enumerate(adds):
        if re.match(r"\s*except(?:\s+Exception)?\s*:\s*(?:pass)?\s*$", txt):
            nxt = adds[i + 1][2] if i + 1 < len(adds) and adds[i + 1][0] == f and adds[i + 1][1] == ln + 1 else ""
            prev = "\n".join(t for ff, l2, t in adds[max(0, i - 6):i] if ff == f)
            if (txt.rstrip().endswith("pass") or nxt.strip() == "pass") and any(re.search(rf"\b{re.escape(t)}\s*\(", prev) for t in tg):
                soft.append(f"{f}:{ln} swallows exceptions around a target_fn call")
    if hits:
        return False, f"{len(hits)} anti-gaming hit(s): " + "; ".join(hits[:8])
    return True, f"{len(adds)} added line(s) scanned, clean" + (f"; WARN {'; '.join(soft[:4])}" if soft else "")


LINT_PY_CODES = ("F401", "F811", "F841")   # unused import, redefinition of an unused name, unused local variable
LINT_TS_CODES = ("TS6133", "TS6138", "TS6192", "TS6196", "TS6198")   # declared/imported but never read or used (--noUnusedLocals)
_TS_DIAG_RE = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")


def _ruff_bin():
    """ruff for the lint gate and the style pass: the lane's pinned copy next to mini first (a remote job's setup installs
    LANE_TOOLS into the mini venv; the runner host has no other ruff), else PATH, else Homebrew's."""
    own = Path(MINI).parent / "ruff"
    if os.access(own, os.X_OK):
        return str(own)
    return shutil.which("ruff") or next((p for p in ("/opt/homebrew/bin/ruff", "/usr/local/bin/ruff") if os.access(p, os.X_OK)), None)


def ruff_findings(text, name, ruff=None):
    """[(code, message, row)] of LINT_PY_CODES in one python source text, or None when ruff cannot run. --isolated: the repo's own
    ruff config does not apply, so the baseline and the fix are judged by the same rules. Line numbers in a message become 'line N'
    (F811 names the earlier line), so a finding the fix only moved is the same finding."""
    ruff = ruff or _ruff_bin()
    if not ruff:
        return None
    try:
        r = subprocess.run([ruff, "check", "--isolated", "--no-cache", "--select", ",".join(LINT_PY_CODES), "--output-format", "json",
                            "--stdin-filename", name, "-"], input=text, capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout or "[]") if r.returncode in (0, 1) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return [(x["code"], re.sub(r"\bline \d+", "line N", str(x.get("message") or "")), (x.get("location") or {}).get("row"))
            for x in rows if isinstance(x, dict) and x.get("code") in LINT_PY_CODES]


def _tsc_lint_cmd(cmd):
    """typecheck_cmd again with --noUnusedLocals when its last command is a plain tsc call (not a -b build or watch); else None.
    None as well when it already has the flag: the typecheck gate then reports a new unused name itself."""
    if not cmd or "noUnusedLocals" in cmd:
        return None
    try:
        toks = shlex.split(re.split(r"&&|\|\||;|\|", cmd)[-1])
    except ValueError:
        return None
    if "tsc" not in {Path(t).name for t in toks} or {"-b", "--build", "-w", "--watch"} & set(toks):
        return None
    return f"{cmd} --noUnusedLocals --pretty false"


def _match_file(p, files):
    p = p.strip()[2:] if p.strip().startswith("./") else p.strip()
    return next((f for f in files if f == p or f.endswith("/" + p) or p.endswith("/" + f)), None)


def lint_check(cwd, spec, allowed, base_texts):
    """G4b: unused imports and locals the fix ADDED (2026-11-03 sampled review: 4 of 16 false PASSes carried dead code no gate saw;
    3 of them, an unused import or an unused function, are what this flags). A soft gate: the next attempt is told what to remove; if it is still there at the end the job is
    `PASS needs-review soft=lint`. Python: ruff LINT_PY_CODES on the baseline and the fixed text of each allowed .py file; a finding
    is new when the fix has more of it (code + message) than the baseline had. TypeScript: when typecheck_cmd is a tsc call, the
    same call again with --noUnusedLocals, keeping LINT_TS_CODES on lines the fix added. -> (ok, detail), or None when nothing could
    be linted (no .py file, no tsc typecheck). spec.lint = "off" skips it."""
    if spec.get("lint") in (False, "off"):
        return None
    new, notes, ran = [], [], False
    pys = [f for f in allowed if f.endswith(".py") and Path(cwd, f).is_file()]
    ruff = _ruff_bin() if pys else None
    if pys and not ruff:
        notes.append("python lint skipped: ruff not found")
    for f in pys if ruff else ():
        after = ruff_findings(Path(cwd, f).read_text(errors="replace"), f, ruff)
        before = ruff_findings(base_texts.get(f) or "", f, ruff)
        if after is None or before is None:
            notes.append(f"{f}: ruff did not run")
            continue
        ran, left = True, {}
        for c, m, _row in before:
            left[(c, m)] = left.get((c, m), 0) + 1
        for c, m, row in after:
            if c == "F401" and Path(f).name == "__init__.py":
                continue   # a package's re-exports
            if left.get((c, m)):
                left[(c, m)] -= 1
            else:
                new.append(f"{f}:{row} {c} {m}")
    ts_files = [f for f in allowed if re.search(r"\.[cm]?tsx?$", f) and Path(cwd, f).is_file()]
    tsc = _tsc_lint_cmd(spec.get("typecheck_cmd")) if ts_files else None
    if tsc:
        rc, out, _ = run_cmd(tsc, cwd, spec, timeout=int(spec.get("cmd_timeout", 300)) * 2)
        if rc == 124:
            notes.append("tsc lint timed out")
        else:
            ran = True
            added = {(f, ln) for f, ln, _t in added_lines(cwd, ts_files, base_texts)}
            for line in out.splitlines():
                m = _TS_DIAG_RE.match(line.strip())
                f = _match_file(m["file"], ts_files) if m and m["code"] in LINT_TS_CODES else None
                if f and (f, int(m["line"])) in added:
                    new.append(f"{f}:{m['line']} {m['code']} {m['msg'][:120]}")
    if not ran and not notes:
        return None
    if new:
        return False, f"{len(new)} new unused import(s)/variable(s), remove them: " + "; ".join(new[:8])
    return True, "; ".join((["no new unused imports/variables"] if ran else []) + notes)


STYLE_RULES = ("F841", "I001", "format")   # in this order: dropping an unused local can leave an import or a layout to redo
STYLE_NAMES = {"F841": "unused locals", "I001": "import order", "format": "ruff format"}
STYLE_SIBLINGS = 6   # a new file follows a rule when its tracked .py siblings (at most this many) all do
STRAY_GAP = 3        # stray_blanks: a blank-line change is stray when more unchanged lines than this part it from every other change


def stray_blanks(base, text):
    """(text, n): text with its n stray blank-line changes put back to the baseline. A stray change only adds, drops or
    re-spaces blank lines, more than STRAY_GAP lines from every other change in the file (conan2 r2, 2026-11-06: a pass
    added a second blank line in a parser method about 100 lines from the fix, left over from an abandoned edit; the
    project has no formatter that would drop it, so it reached the review as an unasked change). Python only: n is 0 and
    text comes back as is unless both texts parse to the same AST, so a blank line inside a string (data) is never touched."""
    a, b = base.splitlines(True), text.splitlines(True)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    blank = lambda xs: all(not x.strip() for x in xs)   # noqa: E731
    real = [(j1, j2) for t, i1, i2, j1, j2 in ops if t != "equal" and not (blank(a[i1:i2]) and blank(b[j1:j2]))]
    out, n = [], 0
    for t, i1, i2, j1, j2 in ops:
        if t != "equal" and blank(a[i1:i2]) and blank(b[j1:j2]) and all(max(r1 - j2, j1 - r2) > STRAY_GAP for r1, r2 in real):
            out += a[i1:i2]
            n += 1
        else:
            out += b[j1:j2]
    if not n:
        return text, 0
    new = "".join(out)
    try:
        same = ast.dump(ast.parse(new)) == ast.dump(ast.parse(text))
    except (SyntaxError, ValueError):
        same = False
    return (new, n) if same else (text, 0)


def _ruff_stdin(ruff, args, text, name, cwd):
    """ruff on one source text through stdin, run in the work dir so the PROJECT's own ruff config applies (no --isolated here;
    --force-exclude: a file the project excludes stays as it is). -> CompletedProcess, or None when ruff could not run."""
    try:
        return subprocess.run([ruff, *args, "--no-cache", "--force-exclude", "--stdin-filename", name, "-"], input=text,
                              capture_output=True, text=True, timeout=60, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None


def style_rule(ruff, rule, text, name, cwd):
    """One STYLE_RULES rule on one source text -> (dirty, fixed): dirty False (the text follows it), True (fixed = the text with
    ruff's fix applied) or None (ruff cannot judge the text: a syntax error, a crash, a timeout)."""
    if rule == "format":
        r = _ruff_stdin(ruff, ["format", "--check"], text, name, cwd)
        if r is None or r.returncode not in (0, 1):
            return None, None
        if r.returncode == 0:
            return False, None
        r = _ruff_stdin(ruff, ["format"], text, name, cwd)
        return (True, r.stdout) if r is not None and r.returncode == 0 and r.stdout.strip() else (None, None)
    sel = ["--select", rule, "--unsafe-fixes"]   # F841's fix is an unsafe one: `x = f()` keeps the call as `f()`
    r = _ruff_stdin(ruff, ["check", "--output-format", "json", *sel], text, name, cwd)
    try:
        rows = json.loads(r.stdout or "[]") if r is not None and r.returncode in (0, 1) else None
    except ValueError:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(x, dict) or x.get("code") != rule for x in rows):
        return None, None   # e.g. invalid-syntax, which ruff reports whatever --select says
    if not rows:
        return False, None
    r = _ruff_stdin(ruff, ["check", "--fix", "--exit-zero", *sel], text, name, cwd)
    return (True, r.stdout) if r is not None and r.returncode == 0 and r.stdout.strip() else (None, None)


def _style_followed(ruff, rule, f, base, cwd, base_texts):
    """Does the project follow `rule` where f lives? Its baseline text does; for a new file, every one of its tracked .py siblings
    does (test_* ones first for a test file; no sibling: unknown, so no)."""
    if base is not None:
        return style_rule(ruff, rule, base, f, cwd)[0] is False
    d = Path(f).parent
    try:
        sibs = sorted(x for x in git(cwd, "ls-files", "-z", "--", str(d)).split("\0") if x.endswith(".py") and x != f and Path(x).parent == d)
    except Exception:
        return False
    if Path(f).name.startswith("test_"):
        sibs.sort(key=lambda x: not Path(x).name.startswith("test_"))
    texts = []
    for s in sibs[:STYLE_SIBLINGS]:
        try:
            texts.append((s, base_texts[s] if base_texts.get(s) is not None else Path(cwd, s).read_bytes().decode()))
        except (OSError, UnicodeDecodeError):
            return False
    return bool(texts) and all(style_rule(ruff, rule, t, s, cwd)[0] is False for s, t in texts)


def style_pass(cwd, spec, files, base_texts, check, test_file=None):
    """The project's own import order and formatting on the .py files the model changed (2026-11-05 bench: Claude spent 14 review
    calls on one pass's ruff findings, all in lane-written lines: an unsorted import block, unused test locals, unformatted code;
    the lint gate never saw them, as the runner host had no ruff). Mechanical, never a gate: ruff with the PROJECT's config, and a
    rule only where the project already follows it (style_rule on the file's baseline text; a new file: on its siblings), so the
    lane never restyles code the project leaves alone. F841 only on test_file, where dropping an unused name keeps the call.
    First, in a file the baseline has (base_texts), stray_blanks puts back blank lines the model added or dropped far from its
    change; that needs no ruff and no project rule, since it restores the project's own text.
    check() -> (ok, why) re-runs the test on the restyled files; not ok puts every file back. -> a detail line, or None (nothing
    to restyle, spec.style or MINI_STYLE off)."""
    if spec.get("style") in (False, "off") or os.environ.get("MINI_STYLE") == "off":
        return None
    files = [f for f in files if f.endswith(".py") and Path(cwd, f).is_file() and not _crlf(Path(cwd, f))]
    ruff = _ruff_bin() if files else None
    new, did = {}, []
    for f in files:
        try:
            text = Path(cwd, f).read_bytes().decode()
        except (OSError, UnicodeDecodeError):
            continue
        base = base_texts.get(f)
        if base is not None and text == base:
            continue   # the model left it alone
        cur, rules = text, []
        if base is not None:
            cur, n = stray_blanks(base, cur)
            if n:
                rules.append(f"{n} stray blank-line change{'s' if n > 1 else ''} put back")
        for rule in STYLE_RULES if ruff else ():
            if rule == "F841" and f != test_file:
                continue   # an unused local in the fix is the lint gate's to report, not the style pass's to drop
            dirty, fixed = style_rule(ruff, rule, cur, f, cwd)
            if dirty and fixed and fixed != cur and _style_followed(ruff, rule, f, base, cwd, base_texts):
                cur, rules = fixed, [*rules, STYLE_NAMES[rule]]
        if cur != text:
            new[f] = (text, cur)
            did.append(f"{f} ({', '.join(rules)})")
    if not new:
        return None if ruff or not files else "skipped: ruff not found"
    for f, (_old, t) in new.items():
        Path(cwd, f).write_bytes(t.encode())
    ok, why = check()
    if not ok:
        for f, (old, _t) in new.items():
            Path(cwd, f).write_bytes(old.encode())
        return f"reverted, {why}: {'; '.join(did)}"
    return f"{'; '.join(did)}; {why}" + ("" if ruff else " (ruff not found: its rules skipped)")


def _style_same_red(cwd, spec, ev):
    """style_pass check for the phase A test: still red, with the same per-test results and count as the run test_gate froze."""
    rc, out, _ = run_cmd(spec["test_cmd"], cwd, spec)
    per = sorted(f"{n} {st} {t or ''}".strip() for n, st, t, _m in parse_test_results(out))
    if rc != 0 and per == sorted(ev.get("per_test") or []) and test_count(out) == ev.get("tests"):
        return True, "the test re-run fails the same tests the same way"
    return False, f"the test re-run changed (rc={rc}, {per[:4]})"


def _style_still_green(cwd, spec, base_tests):
    """style_pass check for the fix: the test re-run is green with at least base_tests tests."""
    rc, out, _ = run_cmd(spec["test_cmd"], cwd, spec)
    n = test_count(out)
    if rc == 0 and (n is None or base_tests is None or n >= base_tests):
        return True, f"the test re-run is green ({n} tests)"
    return False, f"the test re-run is red (rc={rc}, tests={n})"


# ---- doc pass (spec doc_pass, after a green fix) -------------------------------------------------------------------------------
DOC_PASS_TIMEOUT = 240    # seconds for one doc-pass model call
DOC_PASS_MAX = 2          # docstrings asked about per run (the target functions first)
DOC_PASS_MAX_LINES = 200  # a longer function is not sent
DOC_EDIT_CAP = 4          # lines a correction may change: this many, or half the docstring if that is more
DOC_DATA_RE = re.compile(r"\b__doc__\b|\bgetdoc\(|^[ \t]*(?:from|import)[ \t]+(?:click|typer|docopt|ply|fire|invoke|cmd2?)\b", re.M)  # docstrings as data
DOC_READ_RE = r"__doc__|getdoc\("  # git grep -E: another file that reads docstrings (doc_readers)
_DOC_LIT_RE = re.compile(r"([rRuU]?)(\"\"\"|''')(.*)\2", re.S)


def doc_pass_on(spec):
    return bool(spec.get("doc_pass")) and os.environ.get("MINI_DOC_PASS") != "off"


# ---- direct-fix fallback (stage 22, 2026-11-06) --------------------------------------------------------------------------------
# A job the lane's own gates or limits stop (phase A never froze a test; phase B's fix failed its gates) ends as a failure, and
# Claude then fixes the task blind at 20-40x a review's cost. Batch 1: scrapy r3 and moto r3 stopped in phase A with 1700-2000s of
# the stage left; param r3's fix failed test_green. The fallback spends what is left of the stage (plus job room up to FB_WANT_S,
# base_ready's rule) on one plain run: the model writes or repairs the test and fixes the code in the same run, from TASK.md plus
# what the stopped path left (its rejected or frozen test, its reverted fix's diff). The fix-side gates that still apply run
# (test green, red at baseline for the right reason, the repo suite, no_hacks, lint, scope, literals, mutation where the fix is
# inside the diagnosed function, style) and the result goes to the normal Claude review as `PASS needs-review soft=fallback,...`,
# never auto-accepted; REVIEW.md and REVIEW-AUTO.md say it is a fallback, what it skipped and what evidence it has. No evidence (no
# test, no fix, green at baseline, a crash instead of an assertion at baseline) ends the job as it would have today, with
# ` fallback=direct FAIL (<why>)` on the line. spec direct_fallback (auto(): on for every job unless the job spec says false) and
# env MINI_DIRECT_FALLBACK=off turn it off; the spec field is the switch a remote run sees (MINI_* does not reach lane_remote).
FB_MIN_S = 300           # the least stage time the fallback starts with, on top of the gates' share
FB_WANT_S = 1500         # what it would like: job room beyond the stage deadline is added up to this
FB_SKIPPED = ("phase A's test gate (red for the right reason per test, target reach and body coverage, negative control, grounding, "
              "user view, min_tests), fn_scope, the diff_reach floor; not run: breadth retry, doc pass")


def fallback_on(spec):
    return bool(spec.get("direct_fallback")) and os.environ.get("MINI_DIRECT_FALLBACK") != "off"


def fallback_prompt(spec, allowed, why, notes, cwd=None):
    """The direct-fix fallback's task text (its first line is the marker the stub keys on): why the structured path stopped, the
    order of work (reproduce with a red test, fix, re-run), the rules the gates check, what the stopped path left (notes: already
    formatted), the target function's slice."""
    tf, files = spec["test_file"], ", ".join(allowed)
    s = (f"DIRECT-FIX FALLBACK: REPRODUCE THE BUG WITH A TEST, THEN FIX IT, IN THIS ONE RUN.\n\n"
         f"A structured attempt at this task stopped: {why}. Nothing it produced is accepted; the code is back at its baseline. "
         f"If it wrote a test, `{tf}` is still in the work dir (shown below with what was wrong): keep what is right in it or "
         "replace it.\n\nWork in this order:\n\n"
         f"1. Reproduce: write (or repair) `{tf}` so that it asserts exactly what TASK.md's Problem asks for, through the public API "
         "TASK.md's example uses where it shows one, with a second test on a nearby input the Problem implies where that is "
         f"natural. Run it:\n\n    {spec['test_cmd']}\n\n"
         "   It must FAIL now, on an assertion about the behaviour TASK.md reports: not on an import, name or attribute error, and "
         "not because the test itself is broken. A test that passes before the fix is no evidence and ends this run as a failure. "
         "When that command runs the file as a plain script, end the file with an `if __name__ == \"__main__\":` block that runs "
         "every test, prints `RESULT <name> PASS` or `RESULT <name> FAIL <ExceptionType>: <message>` for each and exits 1 when any "
         "failed.\n"
         f"2. Fix the root cause with the smallest change to {files}, in place (staging a rewrite in /tmp does not count). Run the "
         "test again: every test in the file must pass.\n")
    for k, what in (("suite_cmd", "repo suite"), ("typecheck_cmd", "type check")):
        if spec.get(k):
            s += (f"3. The {what} must not gain new failures: `{_short_cmd(spec[k])}` (the runner runs it after you submit; run at "
                  "least an existing test file that imports the code you change).\n")
            break
    upd = as_list(spec.get("update_tests"))
    s += (f"\nRules: edit only {files} and `{tf}`"
          + (f" (and {', '.join(upd)} where a test there asserts exactly the old behaviour the task changes)" if upd else "")
          + ". Do not special-case the test's inputs, detect the test from the code, or weaken the test to fit the code; leave no "
          "unused import or variable; create no other file. The changed files must keep every literal on a `Must keep:` line of "
          "TASK.md and contain every literal under `## Must contain`. Submit when the test passes.\n\n")
    if notes:
        s += "--- WHAT THE STOPPED ATTEMPT LEFT ---\n\n" + notes + "\n\n--- end ---\n\n"
    if cwd is not None and spec.get("target_fn"):
        s += fn_slice(cwd, allowed, spec["target_fn"], spec.get("reach_anchor"))
    return s


def fallback_section(after, why, gates, fev, same_test, main_packet):
    """REVIEW.md's `## Direct-fix fallback (read first)` body: what stopped, what the one run produced, which gates it skipped, the
    evidence it has, where the stopped path's packet is."""
    red = [x for x in fev.get("per_test") or [] if " FAIL" in x]
    verdict, failing = gate_verdict(gates)
    ev = (f"at baseline (allowed files reverted) the test exits {fev.get('baseline_rc')}, "
          + (f"red: {', '.join(red)}" if red else "no per-test lines")
          + f"; with the fix: {gates.get('test_green', (None, '?'))[1]}"
          + (f"; repo suite: {gates['suite'][1]}" if "suite" in gates else "; no repo suite configured")
          + (f"; mutation: {gates['mutation'][1]}" if "mutation" in gates else "")
          + f"; where the fix landed: {gates.get('fix_locus', (None, '?'))[1]}")
    test = ("the stopped path's own, unchanged: it passed phase A's gate (red at baseline for the right reason, reaching the target)"
            if same_test else
            "NOT frozen before the fix and never passed phase A's gate: it may be written to fit the fix. Check it against the Problem first")
    return (f"**A fallback candidate, not a structured-lane pass.** The structured path stopped after {after}: {why}\n\n"
            f"One plain run then wrote the test and the fix together, from TASK.md and what the stopped path left. The test is {test}.\n\n"
            f"Skipped here: {FB_SKIPPED}.\n\nEvidence: {ev}.\n\nVerdict of the gates that ran: {verdict}"
            + (f" (failing: {', '.join(failing)})" if failing else "") + f".\n\nThe stopped path's packet: {main_packet}")


def _auto_fallback(run_dir):
    """REVIEW.md's `## Direct-fix fallback` section (stage 22), or ""."""
    p = Path(run_dir) / "REVIEW.md"
    return md_sec(md_h2(p.read_text(errors="replace")), "direct-fix fallback") if p.is_file() else ""


def _doc_stmt(n):
    """The docstring statement of a function or class node, on lines of its own (after the def line, before the next statement), or None."""
    b = getattr(n, "body", None) or []
    d = b[0] if b else None
    if not (isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) and isinstance(d.value.value, str)):
        return None
    if d.lineno <= n.lineno or (len(b) > 1 and b[1].lineno <= (d.end_lineno or d.lineno)):
        return None
    return d


def _py_defs(tree):
    """{qualname: (function node, its class node or None)} for a module (methods as C.m, nested as f.<locals>.g); a qualname defined
    twice (if/else variants) maps to None."""
    out = {}

    def walk(nodes, pfx, cls):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = pfx + n.name
                out[q] = None if q in out else (n, cls)
                walk(n.body, q + ".<locals>.", None)
            elif isinstance(n, ast.ClassDef):
                walk(n.body, pfx + n.name + ".", n)
            elif isinstance(n, ast.excepthandler):
                walk(n.body, pfx, cls)
            elif isinstance(n, ast.stmt):
                walk([c for c in ast.iter_child_nodes(n) if isinstance(c, (ast.stmt, ast.excepthandler))], pfx, cls)
    walk(tree.body, "", None)
    return out


def _changed_lines(base, cur):
    """1-based lines of cur that a line diff from base changed or added; a deletion marks the line before it."""
    out = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, base.split("\n"), cur.split("\n"), autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
        elif tag == "delete":
            out.add(max(j1, 1))
    return out


def _def_start(n):
    return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])


def _own_lines(n):
    """The lines of function n that are its own code: decorators to end, less its docstring and its nested functions and classes."""
    own = set(range(_def_start(n), (n.end_lineno or n.lineno) + 1))
    d = _doc_stmt(n)
    if d is not None:
        own -= set(range(d.lineno, (d.end_lineno or d.lineno) + 1))
    for c in ast.walk(n):
        if c is not n and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            own -= set(range(_def_start(c), (c.end_lineno or c.lineno) + 1))
    return own


def _doc_span(text, d):
    """(start, end) character offsets in text of docstring statement d's string literal (ast columns count UTF-8 bytes)."""
    lines = text.split("\n")
    off = lambda ln, col: sum(len(x) + 1 for x in lines[:ln - 1]) + len(lines[ln - 1].encode()[:col].decode())
    return off(d.value.lineno, d.value.col_offset), off(d.value.end_lineno, d.value.end_col_offset)


def _doc_layout(text, d):
    """(start, end, prefix, quotes, indent, continuation indent, starts with a newline, tail before the closing quotes) of a triple-quoted
    docstring indented with spaces, or None for any other literal."""
    s, e = _doc_span(text, d)
    seg = text[s:e]
    m = _DOC_LIT_RE.fullmatch(seg)
    first = text.split("\n")[d.lineno - 1]
    indent = first[:len(first) - len(first.lstrip())]
    if seg != ast.get_source_segment(text, d.value) or not m or m.group(2) in m.group(3) or "\t" in seg + indent:
        return None
    pre, q, body = m.groups()
    conts = [x for x in body.split("\n")[1:] if x.strip()]
    ci = min((len(x) - len(x.lstrip(" ")) for x in conts), default=len(indent))
    if ci < len(indent) or any(not x.startswith(indent) for x in conts):
        return None
    last = body[body.rfind("\n"):] if "\n" in body else ""
    return s, e, pre, q, indent, " " * ci, body.startswith("\n"), (last if last and not last.strip() else "")


def doc_readers(cwd, f, name):
    """Tracked python files other than f that read docstrings (DOC_READ_RE) and name `name` or f's module (a word match): a help
    text built there, or a suite test that pins the words, would change with the docstring. [] when git cannot say."""
    try:
        hits = [h for h in git(cwd, "grep", "-l", "-E", DOC_READ_RE, "--", "*.py").split("\n") if h and h != f]
    except Exception:  # GitStall or no repo: no reader known
        return []
    mod = Path(f).parent.name if Path(f).stem in ("__init__", "__main__") else Path(f).stem
    rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted({name, mod}) if n) + r")\b")
    out = []
    for h in hits:
        try:
            if rx.search(Path(cwd, h).read_text(errors="replace")):
                out.append(h)
        except OSError:
            pass
    return out


def _doc_problem(task_md):
    """The Problem of a TASK.md for the doc pass: requirement_text less its title, the Problem heading and the sections
    auto_fix_task writes after it (they instruct the fixer), at most 4000 characters."""
    t = re.split(r"(?m)^## (?:Entry point|Required change|Must keep|Test notes)\b", requirement_text(task_md))[0]
    t = re.sub(r"\A\s*## Problem\b.*\n", "", re.sub(r"\A\s*# TASK\b.*\n", "", t, count=1), count=1).strip()
    return t if len(t) <= 4000 else t[:4000] + "\n... (truncated)"


def _doc_owner(tree, c):
    hit = _py_defs(tree).get(c["qual"])
    return None if not hit else (hit[0] if c["kind"] == "function" else hit[1])


def doc_candidates(cwd, spec, files, base_texts):
    """The docstrings the doc pass asks about: each python function in files whose own code the fix changed while its docstring stayed
    as it was (an __init__/__new__ without one: its class's docstring), skipping example-bearing ones (doctests), YAML-bearing
    ones (an API spec), escapes in a non-raw literal (the model sees the value, not the source), modules that read docstrings
    as data (DOC_DATA_RE) and docstrings another file reads (doc_readers). The target functions first, at most DOC_PASS_MAX.
    -> [dict]"""
    tg = spec.get("target_fn")
    tg = {tg} if isinstance(tg, str) else set(tg or [])
    out, seen = [], set()
    for f in files:
        p, base = Path(cwd, f), base_texts.get(f)
        if not f.endswith(".py") or base is None or not p.is_file() or _crlf(p):
            continue
        try:
            cur = p.read_bytes().decode()
            tb, tc = ast.parse(base), ast.parse(cur)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        if cur == base or DOC_DATA_RE.search(cur):
            continue
        db, dc, changed = _py_defs(tb), _py_defs(tc), _changed_lines(base, cur)
        bl, cl = base.split("\n"), cur.split("\n")
        for q, hit in dc.items():
            if not hit or not db.get(q) or not (_own_lines(hit[0]) & changed):
                continue
            (n, cls), (bn, bcls) = hit, db[q]
            c = {"file": f, "qual": q, "name": n.name, "kind": "function", "owner": q}
            own, bown = n, bn
            if _doc_stmt(n) is None and _doc_stmt(bn) is None and n.name in ("__init__", "__new__") and cls and bcls:
                c.update(kind="class", owner=q.rsplit(".", 1)[0])
                own, bown = cls, bcls
            d, bd = _doc_stmt(own), _doc_stmt(bown)
            if d is None or bd is None or d.value.value != bd.value.value or (f, c["owner"]) in seen:
                continue
            doc = inspect.cleandoc(d.value.value)
            lay = _doc_layout(cur, d)
            before = "\n".join(bl[_def_start(bn) - 1:bn.end_lineno])
            after = "\n".join(cl[_def_start(n) - 1:n.end_lineno])
            if lay is None or not doc.strip() or ">>>" in doc or re.search(r"(?m)^\s*---\s*$", doc) or doc.count("\n") > 80 or \
                    ("\\" in cur[lay[0]:lay[1]] and "r" not in lay[2].lower()) or \
                    max(before.count("\n"), after.count("\n")) >= DOC_PASS_MAX_LINES:
                continue
            seen.add((f, c["owner"]))
            if doc_readers(cwd, f, c["owner"].split(".")[-1]):
                continue
            width = max([79] + [len(x) for x in cl[d.lineno - 1:d.end_lineno]])
            c.update(doc=doc, before=before, after=after, width=width, text_width=width - len(lay[5]) - len(lay[2]) - 6)
            out.append(c)
    out.sort(key=lambda c: c["name"] not in tg and c["qual"] not in tg)  # stable: the target functions first, then file order
    return out[:DOC_PASS_MAX]


def doc_prompt(c, problem):
    what = (f"the docstring of `{c['name']}`" if c["kind"] == "function"
            else f"the docstring of class `{c['owner']}` (it documents the constructor)")
    s = (f"A bug fix changed the code of `{c['qual']}` in `{c['file']}` but left {what} as it was. Your one job is to decide "
         "whether that docstring still tells the truth about the fixed code.\n\n"
         f"## The problem the fix solves\n\n{problem}\n\n"
         f"## `{c['name']}` before the fix\n\n```python\n{c['before']}\n```\n\n"
         f"## `{c['name']}` after the fix\n\n```python\n{c['after']}\n```\n\n")
    if c["kind"] == "class":
        s += f"## The docstring of class `{c['owner']}`\n\n```text\n{c['doc']}\n```\n\n"
    return s + (
        "Check each sentence and each field of the docstring (:param:, :type:, :returns:, :rtype:, :raises:, Args:, Returns:, "
        "Raises: and the like) against the fixed code, for every input it accepts, including the inputs it now handles "
        "differently. A sentence is false when it still describes what the code did before the fix, or names fewer accepted "
        "inputs, return values or raised errors than the fixed code has.\n\n"
        "- If every sentence and field still holds, reply with the single word KEEP.\n"
        "- Otherwise reply with the corrected docstring in ONE ```text block: the docstring text only, without the quotes and "
        "without the indentation. Change only the words the fix made false; keep every other line exactly as it is, keep each "
        f"line at most {c['text_width']} characters long, and add no examples.\n")


def doc_reply(content):
    """('keep', None), ('fix', cleaned docstring text) or ('bad', why) for a doc-pass reply."""
    t = re.sub(r"(?s)<think>.*?</think>", "", content or "").strip()
    blocks = re.findall(r"(?ms)^[ \t]*```[\w+-]*[ \t]*\n(.*?)^[ \t]*```[ \t]*$", t)
    if len(blocks) > 1:
        return "bad", "more than one code block"
    if blocks:
        m = re.fullmatch(r"(?s)\s*[rRuU]?(\"\"\"|''')(.*)\1\s*", blocks[0])
        b = m.group(2) if m else blocks[0]
        if re.search(r"(?m)^[ \t]*(?:async[ \t]+def|def|class)[ \t]+\w+[ \t]*[(:]", b):
            return "bad", "a code block, not a docstring"
        if "\t" in b:
            return "bad", "a tab in the docstring"  # inspect.cleandoc would expand it, unseen by doc_apply
        return "fix", inspect.cleandoc(b)
    if re.search(r"(?m)^\W*KEEP\W*$", t):
        return "keep", None
    return "bad", "neither KEEP nor one code block"


def _compile_warnings(text, name):
    """Sorted compile-time warning messages of a python text, or None when it does not compile."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            compile(text, name, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return None
    return sorted(str(x.message) for x in w)


def doc_apply(text, c, new):
    """(new file text, number of docstring lines changed) with c's docstring replaced by new (cleaned text, laid out as the old one
    was: quotes, prefix, indentation, closing-quote line), or (None, why not). Refused: anything that changes more than that one
    docstring (the module's AST is compared with both docstrings blanked), a new compile warning (an invalid escape), an added
    example or backslash, a quote run, a line past the docstring's width, a change over DOC_EDIT_CAP lines."""
    tree = ast.parse(text)
    own = _doc_owner(tree, c)
    d = _doc_stmt(own) if own is not None else None
    lay = _doc_layout(text, d) if d is not None else None
    if lay is None:
        return None, "the docstring is gone or no longer plain"
    s, e, pre, q, indent, cont, lead, tail = lay
    if "\t" in new or "\0" in new:  # before inspect.cleandoc, which expands tabs
        return None, "an example, a quote run or a tab"
    old, new = inspect.cleandoc(d.value.value), "\n".join(x.rstrip() for x in inspect.cleandoc(new).split("\n"))
    if not new.strip():
        return None, "an empty docstring"
    if new == old:
        return None, "no change"
    if ">>>" in new or q in new or new.endswith(q[0]):
        return None, "an example, a quote run or a tab"
    if "\\" in new and "r" not in pre.lower():
        return None, "a backslash in a non-raw docstring"
    ol, nl = old.split("\n"), new.split("\n")
    ch = sum(max(i2 - i1, j2 - j1) for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes() if t != "equal")
    cap = max(DOC_EDIT_CAP, (len(ol) + 1) // 2)
    if ch > cap:
        return None, f"it rewrites {ch} of {len(ol)} lines (cap {cap})"
    body = ("\n" + cont if lead else "") + nl[0] + "".join("\n" + (cont + x if x.strip() else "") for x in nl[1:])
    if not tail and len(nl) > 1 and len(ol) == 1:
        tail = "\n" + indent  # a one-line docstring that became several: the closing quotes on their own line
    seg = pre + q + body + tail + q
    out = text[:s] + seg + text[e:]
    span = out.split("\n")[d.lineno - 1:d.lineno + seg.count("\n")]
    if max(map(len, span)) > c["width"]:
        return None, f"a line over {c['width']} characters"
    try:
        t2 = ast.parse(out)
    except (SyntaxError, ValueError):
        return None, "the file no longer parses"
    own2 = _doc_owner(t2, c)
    d2 = _doc_stmt(own2) if own2 is not None else None
    if d2 is None:
        return None, "the docstring did not survive"
    d.value.value = d2.value.value = ""
    if ast.dump(tree) != ast.dump(t2):
        return None, "it changes more than the docstring"
    w0, w1 = _compile_warnings(text, c["file"]), _compile_warnings(out, c["file"])
    if w1 is None or (w0 is not None and [x for x in w1 if w1.count(x) > w0.count(x)]):
        return None, "a new compile warning"
    return out, ch


def doc_pass(cwd, spec, files, base_texts, task_md, model, local, usage, budget, check, call=None):
    """spec doc_pass (2026-11-06 bench: canvas r3's one repair was a docstring its fix left stale, although the fix prompt asks for
    that; libseed's module docstring went stale the same way, which review ask 4 still covers). After a green fix, ONE question
    per docstring that doc_candidates picks: does every sentence still hold for the fixed code? The model replies KEEP or the
    corrected docstring; doc_apply lays a correction out as the old one was and refuses anything but a small edit of that one
    docstring. Applied, check() re-runs the test and no_hacks_check rescans; anything but green puts the file back byte for
    byte. Never a gate: -> (True, detail) for the informational "docstring" row, or None when there is nothing to ask."""
    call = call or ss_call
    t_end = _now() + budget
    cands = doc_candidates(cwd, spec, files, base_texts)
    if not cands:
        return None
    problem = _doc_problem(task_md)
    notes = []
    for c in cands:
        left = t_end - _now()
        p = Path(cwd, c["file"])
        raw = p.read_bytes()
        if left < 90:
            note = "not asked (no time left)"
        else:
            try:
                note = _doc_one(cwd, spec, c, problem, model, local, usage, int(min(DOC_PASS_TIMEOUT, left - 60)), check, call, base_texts)
            except Exception as e:  # a review aid: it must never cost the green fix
                p.write_bytes(raw)
                note = f"error ({type(e).__name__}: {str(e)[:80]}), docstring left as it was"
        notes.append(f"`{c['owner'] if c['kind'] == 'class' else c['qual']}` ({c['file']}): {note}")
    return True, "; ".join(notes).replace("|", "/").replace("\n", " ")[:600]


def _doc_one(cwd, spec, c, problem, model, local, usage, timeout, check, call, base_texts):
    p = Path(cwd, c["file"])
    raw = p.read_bytes()
    t0 = time.time()
    r = call(model, local, [{"role": "user", "content": doc_prompt(c, problem)}], no_think_for(spec, "B"), timeout)
    u = r.get("usage") or {}
    if u:
        usage.rows.append((int(time.time()), int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))))
        usage.secs += max(time.time() - t0, 0)
    print(f"[mini-runner] doc pass `{c['qual']}`: {u.get('prompt_tokens', '?')} in / {u.get('completion_tokens', '?')} out, "
          f"{r.get('secs', '?')}s, finish={r.get('finish_reason')}", flush=True)
    if r.get("error"):
        return f"model call failed ({str(r['error'])[:80]}), docstring left as it was"
    if r.get("finish_reason") == "length":
        return "the reply was cut off, docstring left as it was"
    kind, new = doc_reply(r.get("content"))
    if kind == "keep":
        return "model: still true (KEEP)"
    if kind == "bad":
        return f"reply not used ({new}), docstring left as it was"
    text = raw.decode()
    out, n = doc_apply(text, c, new)
    if out is None:
        return ("model: still true (it repeated the docstring)" if n == "no change"
                else f"correction refused ({n}), docstring left as it was")
    h0 = no_hacks_check(cwd, spec, [c["file"]], base_texts)
    p.write_bytes(out.encode())
    ok, how = check()
    h1 = no_hacks_check(cwd, spec, [c["file"]], base_texts) if ok else (True, "")
    if not ok or (h0[0] and not h1[0]):
        p.write_bytes(raw)
        return f"correction put back ({how if not ok else 'no_hacks: ' + h1[1][:80]})"
    return f"model corrected it ({n} line(s), in the diff); {how}"


def _py_top_spans(text):
    """[(bound_names, first_line, last_line, is_import)] for the top-level statements of a python file, or None if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = []
    for n in tree.body:
        a = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        names, _ = _py_scope_bound([n])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = {n.name}
        out.append((names, a, n.end_lineno or n.lineno, isinstance(n, (ast.Import, ast.ImportFrom))))
    return out


JS_TOP_DECL = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=(?!=)", re.M)
_JS_RE_PREV = set("=(,:[!&|?{};")


def _js_stmt_end(text, i):
    """Index just past the end of the top-level JS statement whose initializer starts at i: a `;` at bracket depth 0, or a newline at
    depth 0 whose next line does not continue the expression. Strings, template literals, comments and regex literals are skipped.
    None when it cannot be told -- unbalanced brackets, an unterminated literal, a second declarator (`, b = 2`), or a line at
    column 0 that is not a closing bracket while a bracket is open -- so a caller never lets a span run on into other code."""
    n, d = len(text), 0
    while i < n and text[i] in " \t\n":
        i += 1
    prev = "="
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    return None
                j += 1
            if j >= n:
                return None
            i, prev = j + 1, c
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
            continue
        if c == "/" and prev in _JS_RE_PREV:  # a regex literal: its brackets are not code
            j, cls = i + 1, False
            while j < n and (text[j] != "/" or cls):
                if text[j] == "\n":
                    return None
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    cls = True
                elif text[j] == "]":
                    cls = False
                j += 1
            i = j + 1
            while i < n and text[i].isalpha():
                i += 1
            prev = "/"
            continue
        if c in "([{":
            d += 1
        elif c in ")]}":
            d -= 1
            if d < 0:
                return None
        elif c == "," and d == 0:
            return None
        elif c == ";" and d == 0:
            return i + 1
        elif c == "\n":
            nxt = text[i + 1:].split("\n", 1)[0]
            if d > 0 and nxt[:1] not in ("", " ", "\t", "}", "]", ")"):
                return None
            if d == 0 and not re.match(r"\s*[.?:+\-*/%&|^=<>]", nxt):
                return i
        if not c.isspace():
            prev = c
        i += 1
    return n if d == 0 else None


def _js_top_spans(text):
    """[(bound_names, first_line, last_line, False)] of the top-level `[export] const|let|var NAME = ...` statements of a JS/TS file
    (one name, at column 0): the JS side of _py_top_spans, for spec.new_names (a module-level table the fix edits: bench-cfgkeys
    2cdee16a8b71). A statement whose end cannot be told (_js_stmt_end None) is left out: its lines stay outside the scope."""
    out = []
    for m in JS_TOP_DECL.finditer(text):
        e = _js_stmt_end(text, m.end())
        if e is None:
            continue
        out.append(({m.group(1)}, text.count("\n", 0, m.start()) + 1, text.count("\n", 0, max(m.end(), e - 1)) + 1, False))
    return out


def _py_top_classes(text):
    """[(name, first_line, last_line)] of the top-level classes of a python file (span from the first decorator); [] if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    return [(n.name, min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno or n.lineno)
            for n in tree.body if isinstance(n, ast.ClassDef)]


def _py_doc_lines(text):
    """1-based lines of the module, class and function docstrings of a python text (empty if it does not parse). They cannot
    change what the code does, so keeping one true is never out of scope; a docstring sharing a line with code is not included."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            d = n.body[0]
            if not (isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) and isinstance(d.value.value, str)):
                continue
            z = d.end_lineno or d.lineno
            if (not isinstance(n, ast.Module) and d.lineno <= n.lineno) or (len(n.body) > 1 and n.body[1].lineno <= z):
                continue
            out.update(range(d.lineno, z + 1))
    return frozenset(out)


def _new_name_line(spans, ln, fn, new_names):
    """True when line ln belongs to a top-level statement (or a function) that only binds names listed in spec.new_names."""
    if not new_names:
        return False
    if fn and fn in new_names:
        return True
    for names, a, z, _imp in spans or ():
        if a <= ln <= z:
            return bool(names) and names <= new_names
    return False


FAIL_CLASSES = (
    ("TEST-SUSPECT", re.compile(r"TEST-SUSPECT")),
    ("INFRA", re.compile(r"Provider capacity|provider stayed unavailable|\bPAUSED\b|\bpreempted\b|BadRequestError|APIConnectionError|"
                         r"Connection(?:Error| refused| reset)|ServiceUnavailable|bad-cwd|git-stall|disk-stall|model-not-served|"
                         r"\bremote-(?:infra|scope)\b")),
    ("SPEC", re.compile(r"\bbad-spec\b|\bpreflight\b|fixture never (?:reaches|runs|executes)|never executes line|missing=True|"
                        r"literals=FAIL|must_keep=FAIL|agent reported MISSING|test-changed-while-parked|"
                        # an accepted diagnosis whose target the lane cannot take (an HTML inline script, not-local, too many
                        # functions): another model attempt fails the same way; the next step is a direct fix, not a retry
                        r"phase=derive (?:mini-spec:|intake=|the diagnosis names \d+ functions)")),
    ("TEST", re.compile(r"test-gate|phase=test|test_unchanged=FAIL|red_without_fix=FAIL|no_hacks=FAIL")),
    ("SIZE", re.compile(r"LimitsExceeded|TimeExceeded|no-time-left|timeout-total|step limit|wall-time limit")),
)


def fail_class(line):
    """G7: INFRA|SPEC|TEST|SIZE|MODEL (or TEST-SUSPECT) for a RESULT FAIL line, by the triage signatures."""
    for cls, rx in FAIL_CLASSES:
        if rx.search(line or ""):
            return cls
    return "MODEL"


def tag_result(line):
    """'RESULT FAIL ...' / 'FAIL ...' -> the same line plus ' class=<C>' (once). PASS lines are returned unchanged."""
    s = line.rstrip("\n")
    if re.match(r"(?:RESULT )?FAIL\b", s) and " class=" not in s:
        return f"{s} class={fail_class(s)}"
    return s


SOFT_GATES = ("fn_scope", "literals", "lint", "must_not_contain", "fix_hunks", "ws_only")


def gate_verdict(gates):
    failing = sorted(n for n, (ok, _) in gates.items() if not ok)
    if not failing:
        return ("PASS", [])
    soft = set(SOFT_GATES)
    if all(f in soft for f in failing):
        if gates.get("test_green", (False, ""))[0] and gates.get("test_unchanged", (False, ""))[0]:
            return ("NEEDS-REVIEW", failing)
    return ("FAIL", failing)


def final_result(gates, summary, tail, chain=False):
    """PURE. (rc, RESULT line without the `RESULT ` prefix) for a finished phase-B gate set. A run whose only failing gates are
    SOFT_GATES (test_green/test_unchanged held) is `PASS needs-review soft=<names>`, rc 0; a chain step keeps the strict verdict."""
    verdict, failing = gate_verdict(gates)
    if verdict == "NEEDS-REVIEW" and not chain:
        return RC_OK, f"PASS needs-review soft={','.join(failing)} mode=two-phase {summary}{tail}"
    ok = verdict == "PASS"
    return (RC_OK if ok else RC_FAIL), f"{'PASS' if ok else 'FAIL'} mode=two-phase {summary}{tail}"


SITECUSTOMIZE = '''import atexit, importlib, json, os, sys, threading
_d = os.path.dirname(os.path.abspath(__file__))  # chain to the interpreter's own sitecustomize (Homebrew adds site-packages there)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _d]; _me = sys.modules.pop("sitecustomize", None)
try: importlib.import_module("sitecustomize")
except ImportError: pass
if _me is not None: sys.modules.setdefault("sitecustomize", _me)  # nothing chained: back in, or site prints a KeyError
_T = tuple(os.environ["MINI_REACH_TARGETS"].split(os.pathsep)); _H = set(); _L = {}
def _loc(f, ev, a):
    if ev == "line": _L.setdefault(f.f_code.co_filename, set()).add(f.f_lineno)
    return _loc
def _g(f, ev, a):  # line tracing only inside the target files; everything else runs untraced
    fn = f.f_code.co_filename
    if not fn.endswith(_T): return None
    _H.add(f.f_code.co_name); _L.setdefault(fn, set()).add(f.f_lineno)
    return _loc
sys.settrace(_g); threading.settrace(_g)
atexit.register(lambda: open(os.environ["MINI_REACH_OUT"] + "/py-%d.json" % os.getpid(), "w").write(
    json.dumps({"fns": sorted(_H), "lines": {k: sorted(v) for k, v in _L.items()}})))
'''

JS_SKIP_LINE = re.compile(r"^\s*(?:$|//|/\*|\*|[{}()\[\];,]+\s*$|(?:else|try|finally)\s*\{?\s*$|\}\s*(?:else|finally)\s*\{?\s*$)")


def _py_stmt_spans(src):
    """[(lineno, end_lineno, node)] of every statement, for mapping lines to the statement that runs them."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None, None
    return tree, [(n.lineno, n.end_lineno or n.lineno, n) for n in ast.walk(tree) if isinstance(n, ast.stmt)]


def _py_body_lines(fn):
    body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) \
        and isinstance(fn.body[0].value.value, str) else fn.body
    return {n.lineno for b in body for n in ast.walk(b) if isinstance(n, ast.stmt)}


def const_guard_stop(src, fn, ran, consts):
    """Line of the guard `fn` stopped at when that stop IS the bug path, else None: the last body line it ran is a raise or
    return directly under an `if` whose test reads one of `consts` (the diagnosis's Constant: values, which the fix edits), so
    no test can get past it at baseline. bench-cfgkeys3 2d079d373db0: set_setting(db, "machines", ...) stops at
    `if key not in DEFAULTS: raise ValueError` (2 of its 7 body lines, under the 30% floor) because the missing DEFAULTS keys
    are the bug. Python only; a JS target keeps the floor."""
    names = set(consts or ())
    if not names:
        return None
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    for f in ast.walk(tree):
        if not (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == fn):
            continue
        hit = _py_body_lines(f) & set(ran)
        if not hit:
            return None
        last = max(hit)
        for node in ast.walk(f):
            if isinstance(node, ast.If) and any(isinstance(s, (ast.Raise, ast.Return)) and s.lineno == last for s in node.body + node.orelse):
                reads = {x.id for x in ast.walk(node.test) if isinstance(x, ast.Name)}
                reads |= {x.attr for x in ast.walk(node.test) if isinstance(x, ast.Attribute)}
                if reads & names:
                    return node.lineno
        return None
    return None


def _line_list(v):
    """True for spec.diag_lines' shape: a list of at most 40 positive line numbers."""
    return isinstance(v, list) and len(v) <= 40 and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in v)


def _py_fn_body_span(fn):
    """(first, last) source line of a function's body after its docstring; None for a docstring-only body. The def line is
    not in it: the module import runs that line, so it proves nothing about a call."""
    body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) \
        and isinstance(fn.body[0].value.value, str) else fn.body
    return (body[0].lineno, fn.end_lineno or body[-1].lineno) if body else None


def diag_line_hit(src, fn, ran, cited):
    """The first of `cited` (lines the accepted diagnosis names as the fault: spec.diag_lines, from diag_check.cited_lines) inside
    function `fn`'s body that the test ran, else None. A cited line counts when it ran, when it continues a simple statement
    whose first line ran, or, for an `except` line, when its handler's body ran. A line inside a compound statement that ran
    only its header (a comment in an `if` block) does not count. Python only."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    ran = set(ran)
    for f in ast.walk(tree):
        if not (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == fn):
            continue
        span = _py_fn_body_span(f)
        if not span:
            continue
        stmts = [s for s in ast.walk(f) if isinstance(s, ast.stmt) and s is not f]
        for n in sorted(set(cited)):
            if not span[0] <= n <= span[1]:
                continue
            if n in ran:
                return n
            inner = min((s for s in stmts if s.lineno <= n <= (s.end_lineno or s.lineno)),
                        key=lambda s: (s.end_lineno or s.lineno) - s.lineno, default=None)
            if inner is not None and inner.lineno in ran and not any(isinstance(x, ast.stmt) for x in ast.walk(inner) if x is not inner):
                return n
            for h in ast.walk(f):
                if isinstance(h, ast.ExceptHandler) and h.lineno == n and {x.lineno for b in h.body for x in ast.walk(b) if isinstance(x, ast.stmt)} & ran:
                    return n
    return None


def _py_exec_line(spans, src_lines, ln):
    """The line whose execution proves changed line `ln` ran: its innermost enclosing statement's first line."""
    if not (0 < ln <= len(src_lines)) or not src_lines[ln - 1].strip() or src_lines[ln - 1].strip().startswith("#"):
        return None
    best = None
    for s, e, n in spans:
        if s <= ln <= e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e)
    return best[0] if best else None


class _JsMap:
    """V8 offsets (UTF-16 units) -> source lines. V8 offsets match the file for plain JS and node's TS type stripping."""

    def __init__(self, src):
        self.src = src
        self.u16 = None if src.isascii() else [0]
        if self.u16 is not None:
            for ch in src:
                self.u16.append(self.u16[-1] + (2 if ord(ch) > 0xFFFF else 1))
        self.n16 = self.u16[-1] if self.u16 else len(src)
        self.lines, pos = [], 0
        for ln in src.split("\n"):
            lead = len(ln) - len(ln.lstrip())
            self.lines.append((self.to16(pos + lead), ln))
            pos += len(ln) + 1

    def to16(self, i):
        return i if self.u16 is None else self.u16[min(i, len(self.u16) - 1)]

    def line_of16(self, off):
        lo, hi = 0, len(self.lines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.lines[mid][0] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1


def _js_cov(src, entries):
    """entries: [functions list per process]. Returns (covered line set, exec line set, {fn: [cov fractions]}, mapped)."""
    jm = _JsMap(src)
    covered = 0
    fns, named_ok, named = {}, 0, 0
    spans = []
    for functions in entries:
        paint = bytearray(jm.n16 + 1)
        for r in sorted((r for f in functions for r in f.get("ranges", [])), key=lambda r: (r["startOffset"], -r["endOffset"])):
            s, e = max(0, r["startOffset"]), min(jm.n16, r["endOffset"])
            if e > s:
                paint[s:e] = (b"\x01" if r.get("count", 0) > 0 else b"\x00") * (e - s)
        covered |= int.from_bytes(paint, "big")
        for f in functions[1:]:
            r0 = (f.get("ranges") or [{}])[0]
            spans.append((f.get("functionName") or "<anon>", r0.get("startOffset", 0), r0.get("endOffset", 0)))
    cov = covered.to_bytes(jm.n16 + 1, "big") if entries else bytes(jm.n16 + 1)
    exec_lines = {i + 1 for i, (off, txt) in enumerate(jm.lines) if not JS_SKIP_LINE.match(txt)}
    cov_lines = {i + 1 for i, (off, txt) in enumerate(jm.lines) if (i + 1) in exec_lines and off <= jm.n16 and cov[off]}
    for name, s, e in spans:
        if name != "<anon>":
            named += 1
            pi = s if jm.u16 is None else bisect.bisect_left(jm.u16, s)
            named_ok += name in src[max(0, pi - 80): pi + 200]
        first = jm.line_of16(s)
        body = [ln for ln in exec_lines if first < ln and jm.lines[ln - 1][0] < e]
        frac_lines = (sum(ln in cov_lines for ln in body) / len(body)) if body else (1.0 if cov[s] else 0.0)
        frac_chars = sum(cov[s:e]) / max(1, e - s)
        fns.setdefault(name, []).append((frac_lines, frac_chars))
    mapped = named == 0 or named_ok / named >= 0.8
    return cov_lines, exec_lines, {n: max(v[0] if mapped else v[1] for v in vs) for n, vs in fns.items()}, mapped


def collect_reach(cov, targets, cwd):
    """What the run executed in the target files: {"fns": names that ran, "fn_cov": {name: fraction of its body lines},
    "lines": {target: covered lines}, "exec": {target: executable lines}, "mapped": {target: bool}}; None = no probe output."""
    py, js, seen = {}, {}, False
    for f in glob.glob(os.path.join(cov, "*.json")):
        try:
            data = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        seen = True
        if isinstance(data, list):  # older probe format: function names only
            py.setdefault("<fns>", set()).update(data)
            continue
        if "fns" in data:  # python trace hook
            py.setdefault("<fns>", set()).update(data["fns"])
            for fn, lines in data.get("lines", {}).items():
                for t in targets:
                    if fn.endswith("/" + t):
                        py.setdefault(t, set()).update(lines)
            continue
        for r in data.get("result", []):
            url = unquote(r.get("url", "")).replace("file://", "")
            for t in targets:
                if url.endswith("/" + t):
                    js.setdefault(t, []).append(r.get("functions", []))
    if not seen:
        return None
    out = {"fns": set(), "fn_cov": {}, "lines": {}, "exec": {}, "mapped": {}}
    out["fns"] |= {n for n in py.get("<fns>", set()) if n != "<module>"}
    for t in targets:
        p = Path(cwd, t)
        src = p.read_text(errors="replace") if p.is_file() else ""
        if t in js:
            cl, el, fc, mapped = _js_cov(src, js[t])
            out["lines"][t], out["exec"][t], out["mapped"][t] = cl, el, mapped
            for entry in js[t]:
                for fn in entry[1:]:
                    if fn.get("ranges") and fn["ranges"][0].get("count", 0) > 0:
                        out["fns"].add(fn.get("functionName") or "<anon>")
            for n, v in fc.items():
                if n in out["fns"]:
                    out["fn_cov"][n] = max(v, out["fn_cov"].get(n, 0))
        elif t in py or lang_of(t) == "py" and py:
            ran = py.get(t, set())
            tree, spans = _py_stmt_spans(src)
            out["lines"][t], out["mapped"][t] = ran, tree is not None
            if tree is None:
                continue
            out["exec"][t] = {s for s, _, _ in spans}
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in out["fns"]:
                    body = _py_body_lines(n)
                    frac = (len(body & ran) / len(body)) if body else 1.0
                    out["fn_cov"][n.name] = max(frac, out["fn_cov"].get(n.name, 0))
    return out


def reach_hits(cov, targets, cwd="."):
    r = collect_reach(cov, targets, cwd)
    return None if r is None else r["fns"]


def changed_lines(cwd, f, base_text):
    """New-side line numbers the fix added or changed in f (all lines for a new file)."""
    p = Path(cwd, f)
    if not p.is_file():
        return set()
    if base_text is None:
        return set(range(1, len(p.read_text(errors="replace").splitlines()) + 1))
    out = set()
    # Judge against the text this step started from, not git HEAD: in a chained job the earlier
    # steps' edits are uncommitted, so a git diff blamed step N for step 1..N-1's lines.
    old, new = base_text.splitlines(), p.read_text(errors="replace").splitlines()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def diff_reach(cwd, reach, files, base_texts):
    """(covered, executable, uncovered [(file, line)], unmapped files) over the lines the fix changed."""
    cov = ex = 0
    miss, unmapped = [], []
    for f in files:
        lines = changed_lines(cwd, f, base_texts.get(f))
        if not lines:
            continue
        if not reach["mapped"].get(f, False):
            unmapped.append(f)
            continue
        src = Path(cwd, f).read_text(errors="replace")
        ran = reach["lines"].get(f, set())
        if lang_of(f) == "py":
            _, spans = _py_stmt_spans(src)
            src_lines = src.splitlines()
            doc = _py_doc_lines(src)
            for ln in sorted(lines - doc):
                x = _py_exec_line(spans, src_lines, ln)
                if x is None:
                    continue
                ex += 1
                if x in ran:
                    cov += 1
                else:
                    miss.append((f, ln))
        else:
            for ln in sorted(lines & reach["exec"].get(f, set())):
                ex += 1
                if ln in ran:
                    cov += 1
                else:
                    miss.append((f, ln))
    return cov, ex, miss, unmapped


_MG = None


def _mg():
    """The mini_graph module (loaded by path, read-only graphify queries) or None when it cannot be loaded."""
    global _MG
    if _MG is None:
        try:
            import importlib.util
            sp = importlib.util.spec_from_file_location("mini_graph", os.environ.get("MINI_GRAPH") or Path(os.path.realpath(__file__)).parent / "mini_graph.py")
            m = importlib.util.module_from_spec(sp)
            sp.loader.exec_module(m)
            _MG = m
        except Exception:
            _MG = False
    return _MG or None


def graph_lookup_note(cwd, spec):
    """2-3 prompt lines telling the model it may run the read-only `mini-graph` lookup. '' when MINI_GRAPH_LOOKUP=off or no graph exists
    for the repo. Absolute script path and an explicit --graph so it works whatever the shell's PATH and cwd (a clone has no graphify-out)."""
    if os.environ.get("MINI_GRAPH_LOOKUP", "").strip().lower() in ("off", "0", "no", "false"):
        return ""
    try:
        mg = _mg()
        gp = mg.locate_graph(cwd, spec.get("graph")) if mg else None
    except Exception:
        gp = None
    if not gp:
        return ""
    exe = Path(os.path.realpath(__file__)).parent / "mini-graph"
    base = f"{shlex.quote(str(exe))} --graph {shlex.quote(str(gp))}"
    return (f"Code graph (optional, read-only, via the bash tool): `{base} callers FILE FUNC`, `... callees FILE FUNC`, `... where NAME` "
            "(definitions with file:line), `... neighbors FILE` (importers and peers). Output is short. "
            "If it prints STALE or says no graph, use grep instead; the graph is a lead, read the code before relying on it.\n\n")


_IMPORT_KW = {"import", "from", "const", "let", "var", "require", "as", "default", "export", "await", "type"}


def _fn_spans(f, text):
    """[(name, first, last)] of the functions of f whose end is known. Python: every definition, methods of the same name in
    different classes too (stage 26, scrapy2: functions() kept only the first `pop`, so edits in another class's `pop` counted as
    module level); other languages: functions()."""
    if lang_of(f) == "py":
        return [(n, a, z) for n, _q, a, z, _s in py_all_defs(text) if a and z]
    try:
        return [(n, sp[0], sp[1]) for n, sp in functions(f, text).items() if sp and sp[0] and sp[1]]
    except Exception:
        return []


def _innermost(spans, line):
    c = [(last - first, name) for name, first, last in spans if first <= line <= last]
    return min(c)[1] if c else None


def _top_imports(spans, lines):
    """{line_no} of the module-level import/require lines: the runner's _import_lines plus multi-line destructured requires
    (`const {\\n a,\\n b } = require('x')`), which it does not recognise. An indented require inside a function is not one."""
    out = {n for n, _t in _import_lines(lines, len(lines)) if _innermost(spans, n) is None}
    text = "\n".join(lines)
    for m in re.finditer(r"^(?:const|let|var)\s*[{\[][^;]*?[}\]]\s*=\s*(?:await\s+)?require\([^)]*\)", text, re.M | re.S):
        first = text.count("\n", 0, m.start()) + 1
        last = text.count("\n", 0, m.end()) + 1
        if _innermost(spans, first) is None:
            out.update(range(first, last + 1))
    return out


def _import_facts(lines, which, py=False):
    """(names, specs) of the import lines `which` (1-based). names: what the lines bind -- the alias of `x as y`, the first part of a
    python `import a.b`. specs: module paths -- quoted JS specifiers, python dotted modules. Neither the name before `as` nor the
    module of a python `from M import n` is bound, and a comment is not code: counting them made `import bloom_settings as BS  #
    noqa: E402` an unused added name `bloom_settings` (3 jobs 2026-10-30..11-03; a renamed alias still shows as dropped)."""
    names, specs = set(), set()
    for n in sorted(which):
        ln = lines[n - 1]
        specs.update(re.findall(r"""['"]([^'"]+)['"]""", ln))
        code = re.sub(r"#.*" if py else r"//.*|/\*.*?(?:\*/|$)", " ", re.sub(r"""['"][^'"]*['"]""", " ", ln))
        m = re.match(r"\s*from\s+([\w.]+)\s+import\b(.*)", code) if py else None
        if m:
            specs.add(m.group(1))
            code = "" if m.group(1) == "__future__" else m.group(2)  # a compiler directive binds nothing
        elif py and re.match(r"\s*import\s", code):
            for part in re.sub(r"^\s*import\s", "", code).split(","):
                mm = re.match(r"\s*([\w.]+)(?:\s+as\s+(\w+))?", part)
                if mm:
                    specs.add(mm.group(1))
                    names.add(mm.group(2) or mm.group(1).split(".")[0])
            continue
        code = re.sub(r"[A-Za-z_$][\w$]*\s+as\s+(?=[A-Za-z_$])", " ", code)  # `x as y` binds y
        if not py and re.search(r"\b(?:const|let|var)\b", code):
            code = re.sub(r"[A-Za-z_$][\w$]*\s*:\s*(?=[A-Za-z_$])", " ", code)  # `const { x: y } = require(..)` binds y
        names.update(x for x in re.findall(r"[A-Za-z_$][\w$]*", code) if x not in _IMPORT_KW)
    return names, specs


def _ranges(nums):
    nums, out = sorted(set(nums)), []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def _calls(name, text, qualified=False):
    """Does text call name(...)? qualified=True also counts `obj.name(` / `mod.name(` (a method or cross-module call)."""
    return bool(re.search((r"(?<![\w$])" if qualified else r"(?<![\w$.])") + re.escape(name) + r"\s*\(", text))


def _cjs_export_lines(lines):
    """Return set of 1-based line numbers of module-level CommonJS export statements:
    `module.exports = ...` (through closing brace or single line) and
    `exports.name = ...` (single line)."""
    out = set()
    text = "\n".join(lines)
    # Match: module.exports = { ... } (possibly multi-line) or exports.name = value;
    for m in re.finditer(r"(?:^|(?<=\n))(module\.exports|exports)(\.[A-Za-z_$][\w$]*)?\s*=\s*(?:{[^}]*}|[^;]+);?", text, re.M):
        first = text.count("\n", 0, m.start()) + 1
        last = text.count("\n", 0, m.end()) + 1
        if _innermost([], first) is None:  # module-level only
            out.update(range(first, last + 1))
    return out


def fn_scope_check(cwd, spec, allowed, base_texts):
    """Gate fn_scope: did the fix stay inside target_fn? Every changed line (new side by difflib against the baseline text, plus
    pure deletions mapped through the baseline spans) is one of: target (inside a target_fn span, nested included); helper (inside a
    function absent from the baseline that the target calls by name, transitively; python: also a new top-level class the target or
    a helper names, methods included); import (a module-level import change that only
    ADDS names the target/helper lines use; dropping or renaming names, or changing a module path, is outside); neighbor (inside a
    function the graph says is a 1-hop calls neighbour of the target, or a text-regex neighbour when the graph is stale or absent);
    outside (anything else). Fails on any outside line, or neighbor lines beyond spec.scope_slack (default 4).
    spec.fn_scope: "fail" (default since 2026-10-24) / "enforce" (False), "warn" (True with a WARN detail; precheck demands
    spec.fn_scope_reason), "off". spec.new_names: top-level names the TASK adds or edits (constants, module state, new helpers):
    python top-level statements binding only those names (JS/TS: one-name `[export] const|let|var NAME = ...` statements at column
    0), and lines inside functions so named, count as in scope. The detail names the functions to revert."""
    mode = spec.get("fn_scope", "fail")
    new_names = set(as_list(spec.get("new_names")))
    n_new = 0
    targets = as_list(spec.get("target_fn"))
    if mode == "off" or not targets:
        return True, "off" if mode == "off" else "no target_fn: not checked"
    slack = int(spec.get("scope_slack", 4))
    mg, G, gstat = _mg(), None, "graph: absent"
    if mg:
        try:
            gp = mg.locate_graph(cwd, spec.get("graph"))
            G = mg.load(gp) if gp else None
        except Exception:
            G = None
    outside, neighbors, helpers_seen, nt = {}, {}, [], 0
    ni = nh = 0
    notes = []
    churn = []
    for f in allowed:
        base, p = base_texts.get(f), Path(cwd, f)
        if base is None or not p.is_file():
            continue  # a new file has no baseline to stay inside of
        new = p.read_text(errors="replace")
        if new == base:
            continue
        nl, bl = new.splitlines(), base.splitlines()
        nsp, bsp = _fn_spans(f, new), _fn_spans(f, base)
        ntop, btop = (_py_top_spans(new), _py_top_spans(base)) if f.endswith(".py") else (None, None)
        nnsp, bnsp = (ntop, btop) if f.endswith(".py") else ((_js_top_spans(new), _js_top_spans(base)) if new_names else (None, None))
        # stage 26: a target_fn that names a class (a2a: ClientTransport gains __aenter__/__aexit__) covers the class body and the
        # methods the fix adds there; before, every one of those lines was "outside"
        tspans = [s for s in nsp if s[0] in targets] + [s for s in _py_class_spans(new, f) if s[0] in targets]
        btspans = [s for s in bsp if s[0] in targets] + [s for s in _py_class_spans(base, f) if s[0] in targets]
        ttext = "\n".join("\n".join(nl[a - 1:z]) for _n, a, z in tspans)
        new_ch, old_del = set(), set()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, bl, nl, autojunk=False).get_opcodes():
            if tag != "equal":
                new_ch.update(range(j1 + 1, j2 + 1))
            if tag == "delete":
                old_del.update(range(i1 + 1, i2 + 1))
        _code = lambda t: t.strip() and not re.match(r"\s*(?:#|//|/\*|\*)", t)  # comment-only lines cannot change behaviour
        new_ch = {n for n in new_ch if _code(nl[n - 1])}
        old_del = {n for n in old_del if _code(bl[n - 1])}
        if f.endswith(".py"):  # docstring lines (the module's too) cannot change behaviour either: keeping one true is in scope
            new_ch -= _py_doc_lines(new)
            old_del -= _py_doc_lines(base)
        # CJS export swap: when target is declared with `export` in either new or baseline,
        # subtract module-level CommonJS export lines from new_ch and old_del.
        ttext_all = "\n".join(nl)
        bl_text_all = "\n".join(bl)
        has_export_target = any(
            re.search(rf"\bexport\s+(default\s+)?(async\s+)?function\s+{re.escape(t)}\s*\(", ttext_all) or
            re.search(rf"\bexport\s+(default\s+)?(async\s+)?function\s+{re.escape(t)}\s*\(", bl_text_all)
            for t in targets
        )
        if has_export_target:
            cjs_new = _cjs_export_lines(nl)
            cjs_old = _cjs_export_lines(bl)
            new_ch -= cjs_new
            old_del -= cjs_old
        base_names = {n for n, _a, _z in bsp}
        # python: a top-level class that no baseline top-level name binds is a helper too once the target (or a helper) names it --
        # instantiates, subclasses, raises or isinstance-checks it. Its class line and methods count as helper lines, its text as
        # helper text for the import check (2026-11-03: docsum-cli `class _HelpParser(argparse.ArgumentParser)` used by parse_args
        # failed fn_scope twice with test_green=ok). A class that rebinds a baseline name, or that nothing in scope names, stays outside.
        btop_names = {x for names, _a, _z, _i in btop or () for x in names}
        ncls = [c for c in _py_top_classes(new) if c[0] not in btop_names | base_names | set(targets)] if btop is not None else []
        helpers, hcls, grew = set(), set(), True
        while grew:
            grew = False
            hay = "\n".join([ttext] + ["\n".join(nl[a - 1:z]) for n, a, z in nsp if n in helpers]
                            + ["\n".join(nl[a - 1:z]) for n, a, z in ncls if n in hcls])
            for n, a, z in ncls:
                if n not in hcls and re.search(rf"(?<![\w$.]){re.escape(n)}(?![\w$])", hay):
                    hcls.add(n)
                    grew = True
            for n, a, z in nsp:
                in_cls = any(ca <= a and z <= cz for cn, ca, cz in ncls if cn in hcls)  # a method of a helper class is covered by it
                if n not in base_names and n not in helpers and n not in targets and not in_cls and _calls(n, hay):
                    helpers.add(n)
                    grew = True
        helpers_seen += sorted(helpers | hcls)
        hspans = [(a, z) for n, a, z in nsp if n in helpers] + [(a, z) for n, a, z in ncls if n in hcls]
        htext = "\n".join("\n".join(nl[a - 1:z]) for a, z in hspans)
        # neighbours: graph when the baseline blob equals the graph's, else text
        neigh = set()
        if G and mg.fresh_text(G, cwd, f, base):
            ids = [i for i in (mg.node_for(G, f, t) for t in targets) if i]
            neigh = {re.sub(r"[(][)]$", "", G["by_id"][i].get("label", "")) for i in mg.callers(G, ids) + mg.callees(G, ids)
                     if G["by_id"][i].get("source_file") == f} - set(targets)
            gstat = "graph: fresh@" + G["built_at_commit"][:7]
        else:
            gstat = "graph: stale" if G else "graph: absent"
            # qualified calls count here (2026-11-03: settings-admin-key-removal's one-line `BS.admin_post(...)` call-site update in
            # server.py's do_POST, required by the task's signature change, was "outside"); neighbours stay capped by scope_slack
            neigh = {n for n, a, z in nsp if n not in targets and n in base_names
                     and (_calls(n, ttext, True) or any(_calls(t, "\n".join(nl[a - 1:z]), True) for t in targets))}
        # imports
        new_imp, old_imp = _top_imports(nsp, nl), _top_imports(bsp, bl)
        imp_changed = (new_ch & new_imp) | (old_del & old_imp)
        if imp_changed:
            on, os_ = _import_facts(bl, old_imp, f.endswith(".py"))
            nn, ns = _import_facts(nl, new_imp, f.endswith(".py"))
            dropped = sorted((on - nn) | (os_ - ns))
            # an import the spec's new_names use counts too (2026-11-03 ledger-receipt-files: `import re` for a new `PATH_RE =
            # re.compile(..)` that the target matches with was "added but unused")
            nntext = "\n".join(nl[ln - 1] for ln in sorted(new_ch - new_imp) if _new_name_line(nnsp, ln, _innermost(nsp, ln), new_names))
            used = ttext + "\n" + htext + "\n" + nntext
            unused = sorted(a for a in nn - on if not re.search(rf"(?<![\w$]){re.escape(a)}(?![\w$])", used))
            if dropped or unused:
                outside.setdefault("module imports", []).extend(sorted(imp_changed))
                notes.append((f"dropped/renamed {dropped}" if dropped else "") + ("; " if dropped and unused else "")
                             + (f"added but unused by target/helper {unused}" if unused else ""))
            else:
                ni += len(imp_changed)
        # classify
        for ln in sorted(new_ch - imp_changed):
            if any(a <= ln <= z for _n, a, z in tspans):
                nt += 1
                continue
            fn = _innermost(nsp, ln)
            if _new_name_line(nnsp, ln, fn, new_names):
                n_new += 1
            elif any(a <= ln <= z for a, z in hspans):
                nh += 1
            elif fn in neigh:
                neighbors.setdefault(fn, []).append(ln)
            else:
                outside.setdefault(fn or "module level", []).append(ln)
        for ln in sorted(old_del - imp_changed):  # pure deletions: where were they in the baseline?
            if any(a <= ln <= z for _n, a, z in btspans):
                nt += 1
                continue
            fn = _innermost(bsp, ln)
            if _new_name_line(bnsp, ln, fn, new_names):
                n_new += 1
            elif fn in neigh:
                neighbors.setdefault(fn, []).append(ln)
            else:
                outside.setdefault((fn or "module level") + " (deleted lines, baseline numbering)", []).append(ln)
        span = sum(z - a + 1 for _n, a, z in tspans)
        if span and nt / span > 0.5:
            churn.append(f"{f}: {nt}/{span} lines of {targets} changed")
    nn_total = sum(len(v) for v in neighbors.values())
    n_out = sum(len(v) for v in outside.values())
    summary = f"target {nt}, helper {nh}" + (f" ({', '.join(dict.fromkeys(helpers_seen))})" if helpers_seen else "") + \
        f", import {ni}, new_names {n_new}, neighbor {nn_total} (slack {slack}), outside {n_out}; {gstat}"
    viol = []
    for k, v in sorted(outside.items()):
        viol.append(f"`{k}` lines {_ranges(v)}" + (f" ({'; '.join(notes)})" if k == "module imports" and notes else ""))
    if nn_total > slack:
        viol.append("neighbor " + ", ".join(f"`{k}` lines {_ranges(v)}" for k, v in sorted(neighbors.items())) + f" ({nn_total} > scope_slack {slack})")
    note = ("; NOTE churn (renames inside the target cannot be detected): " + "; ".join(churn)) if churn else ""
    if viol:
        why = f"edits outside target_fn {targets}: " + "; ".join(viol) + f". Revert these; change only {targets} (plus new helper functions/classes it uses, plus added import names they use). [{summary}]" + note
        return (True, "WARN " + why) if mode == "warn" else (False, why + (f" If the TASK adds module-level names, list them in spec.new_names." if not new_names else ""))
    return True, f"inside target_fn {targets} [{summary}]" + note


def _bash(cmd, cwd, env, timeout, wrap=(), on_start=None):
    """bash -c in its own session (tracked for SIGTERM); the whole tree is killed at the timeout. wrap = argv prefix (the sandbox).
    on_start(pid) runs once the child exists (the background baselines record it so stop() can kill the run)."""
    if timeout is not None and timeout <= 0:
        return 124, "TIMEOUT total deadline reached"
    p = _popen([*wrap, "bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    try:
        if on_start:
            on_start(p.pid)
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return 124, f"TIMEOUT {int(timeout)}s"
    finally:
        _CHILDREN.discard(p.pid)


_MM = []
_CONTAIN_SAID = []


def contain_runs(spec):
    """True when this job's commands run a test the local model drafted: a phase_a_only (test_draft) spec, or MINI_CONTAIN=1 from the
    caller (mini-testdraft check -> mini-validate -> runner --check-only). Fable review 2026-11-02 #5: those runs had the real HOME and
    no sandbox, so a draft that mis-set its isolation could write live state before the contained mutation scorer ran."""
    return bool(spec.get("phase_a_only")) or os.environ.get("MINI_CONTAIN") == "1"


def contain(cmd, cwd, env, ct):
    """-> (env, wrap, cmd) for a run contained like the mutation scorer (mini_mutate._contain: throwaway HOME under ct, sandbox-exec
    with no writes outside the temp dirs and cwd, no connects to live or remote ports, no signals out). cwd stays writable: it is the
    job's throwaway work dir. Without sandbox-exec, or if mini_mutate cannot load, the HOME/env part alone (never a plain run)."""
    try:
        if not _MM:
            import importlib.util
            spc = importlib.util.spec_from_file_location("mini_mutate", Path(os.path.realpath(__file__)).parent / "mini_mutate.py")
            m = importlib.util.module_from_spec(spc)
            spc.loader.exec_module(m)
            _MM.append(m)
        got = _MM[0]._contain(ct, env, cmd, writable=[cwd], strict=False)
    except Exception as e:  # noqa: BLE001 -- a broken helper must not turn containment off
        got = f"mini_mutate unavailable ({type(e).__name__}: {e})"
    if isinstance(got, str):
        Path(ct, "home").mkdir(exist_ok=True)
        print(f"[mini-runner] containment: HOME only ({got})", flush=True)
        got = ({**env, "HOME": str(Path(ct, "home")), "DISPATCH_VERIFY_SANDBOX": "1"}, [], cmd)
    if not _CONTAIN_SAID:
        _CONTAIN_SAID.append(1)
        print(f"[mini-runner] drafted-test runs are contained: throwaway HOME{', sandbox-exec' if got[1] else ''}", flush=True)
    return got


def run_cmd(cmd, cwd, spec, timeout=None, probe_targets=None, full_reach=False, on_start=None):
    """bash -c cmd in cwd with spec env. Returns (rc, output, reach) -- reach is the function-name set (or the full
    collect_reach dict with full_reach=True), None without a probe. Capped by the job's total deadline. Contained when
    contain_runs(spec): the command runs a test the local model drafted. Every run gets DISPATCH_VERIFY_SANDBOX=1 (after the spec env,
    so a spec cannot turn it off): the code under test can read the real queue but never enqueue or change a job."""
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}, "PYTHONDONTWRITEBYTECODE": "1", "DISPATCH_VERIFY_SANDBOX": "1"}
    env.pop("OPENAI_API_KEY", None)
    cov = None
    if probe_targets:
        cov = tempfile.mkdtemp(prefix="mini-reach-")
        Path(cov, "site").mkdir()
        Path(cov, "site/sitecustomize.py").write_text(SITECUSTOMIZE)
        env.update(NODE_V8_COVERAGE=cov, MINI_REACH_OUT=cov, MINI_REACH_TARGETS=os.pathsep.join("/" + t for t in probe_targets),
                   PYTHONPATH=os.pathsep.join(filter(None, [str(Path(cov, "site")), env.get("PYTHONPATH")])))
    ct, wrap = None, ()
    if contain_runs(spec):
        ct = tempfile.mkdtemp(prefix="mini-ct-")  # under the per-user temp dir: writable inside the sandbox, like cov
        env, wrap, cmd = contain(cmd, cwd, env, ct)
    try:
        rc, out = _bash(cmd, cwd, env, remaining(timeout or int(spec.get("cmd_timeout", 300))), wrap, on_start=on_start)
    finally:
        if ct:
            shutil.rmtree(ct, ignore_errors=True)
    reach = collect_reach(cov, probe_targets, cwd) if cov else None
    if cov:
        shutil.rmtree(cov, ignore_errors=True)
    return rc, out, (reach if full_reach or reach is None else reach["fns"])


def fail_lines(rc, out):
    if rc == 0:
        return set()
    # stage 25: pytest's captured-log lines (`ERROR    logger:file.py:NN msg`) are not failure lines: their text varies between runs
    # (ports, addresses), so one read as a failure never matches the baseline set (scrapy 11-06: a retry log line failed the suite gate)
    norm = {re.sub(r"\(\d+(\.\d+)?m?s\)|\d+(\.\d+)?ms|^\s*not ok \d+ - ", "", ln).strip() for ln in out.splitlines()
            if FAILLINE_RE.search(ln) and not _CAPLOG_RE.match(ln)}
    # the failure REASON counts too: a file that was red at baseline and is red for a new reason is a new failure
    norm |= {re.sub(r"/\S+", "<path>", ln).strip()[:200] for ln in out.splitlines() if REASONLINE_RE.search(ln)}
    return norm or {f"rc={rc}"}


def swap_texts(cwd, texts):
    """Write {rel: text|None} into cwd (None = remove); returns the previous {rel: text|None}."""
    prev = {}
    for rel, txt in texts.items():
        p = Path(cwd) / rel
        prev[rel] = p.read_text() if p.exists() else None
        if txt is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt)
    return prev


def as_list(v):
    """A spec value that may be a string or a list -> list of strings."""
    return [] if not v else [v] if isinstance(v, str) else list(v)


def anchor_line(text, lit):
    """1-based line where the literal starts, or None."""
    i = text.find(lit)
    return None if i < 0 else text.count("\n", 0, i) + 1


def last_ran_before(src, covered, n):
    """'M `<line>`' for the largest covered line below n (non-blank), or 'none'."""
    rows = src.splitlines()
    prev = [m for m in covered if m < n and m <= len(rows) and rows[m - 1].strip()]
    if not prev:
        return "none"
    m = max(prev)
    return f"{m} `{rows[m - 1].strip()}`"


def _zero_tests_ran(tf, text, out, per, test_cmd):
    """True when a python test run as a plain script (`python3 test_x.py`) exited 0 without running anything: it defines test
    functions and nothing at module level calls them. 'passes at baseline' would mislead (2026-11-02: a pytest-style draft got that
    message twice and never added a runner)."""
    if lang_of(tf) != "py" or per or test_count(out) or re.search(r"\b(pytest|unittest|nose2?|tox)\b", test_cmd):
        return False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") for n in ast.walk(tree)):
        return False
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        if isinstance(top, ast.If) and "__main__" in ast.unparse(top.test):
            return False
        if isinstance(top, (ast.For, ast.AsyncFor, ast.While)) and any(isinstance(c, ast.Call) for c in ast.walk(top)):
            return False
        for c in ast.walk(top):
            leaf = (_dotted(c.func) or "").rsplit(".", 1)[-1] if isinstance(c, ast.Call) else ""
            if leaf.startswith("test") or leaf in ("main", "run", "run_tests", "runTests"):
                return False
    return True


# ---- expectation grounding (phase A): a value the test expects must come from the requirement, the code or the test's inputs ----
GROUND_MIN_LETTERS = 3  # tokens with fewer letters ("id", "ok", "T00", "v2") say nothing about where a value came from
_GTOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PY_EQ_ASSERTS = frozenset({"assertEqual", "assertEquals", "assertIs", "assertIn", "assertListEqual", "assertDictEqual",
                            "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertItemsEqual", "assertSequenceEqual",
                            "assertMultiLineEqual", "assert_equal"})
_PY_LIT_WRAPS = frozenset({"sorted", "set", "list", "tuple", "frozenset", "dict"})
_PY_PROSE_CALLS = frozenset({"print", "fail", "AssertionError", "subTest", "skipTest"})
_JS_EQ_RE = re.compile(r"(?<![\w$])(?:strictEqual|deepStrictEqual|equal|deepEqual|partialDeepStrictEqual)\s*\(")
_JS_EXPECT_RE = re.compile(r"\.(?:toBe|toEqual|toStrictEqual|toContain|toContainEqual|toMatchObject|toHaveProperty)\s*\(")
_JS_OK_RE = re.compile(r"(?<![\w$.])assert(?:\.ok)?\s*\(")
_JS_PROSE_RE = re.compile(r"(?<![\w$.])(?:it|test|describe|suite)(?:\.\w+)?\s*\(|(?<![\w$])(?:console\.\w+|assert\.fail|fail|Error)\s*\(")
_REPORT_MD_RE = re.compile(r"(?:^|/)(?:TASK[^/]*|DIAGNOSIS|NEEDS|REVIEW[^/]*)\.md$")


def requirement_text(task_text):
    """The part of a TASK.md that states the requirement: all of it but a `## Diagnosis` section (a model's reasoning, which may
    suggest more than the Problem asks for)."""
    return re.sub(r"(?ms)^##[ \t]+Diagnosis\b.*?(?=^##[ \t]|\Z)", "", task_text or "")


def _py_claims(text):
    """(claims, inputs) of a python test; None if it does not parse. claims: [(string, line)] that a positive assertion says the
    code produces: the string constants in the operands of ==, in, is and of assertEqual-style calls, looking into list/tuple/
    set/dict displays, sorted()/set()/... wrappers, f-string text, `+` joins, subscript keys, and names bound only to such a
    value; never into a call's arguments (those are inputs). inputs: every other string constant except prose (docstrings,
    assert messages, print() and fail() text)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    prose, operands, binds = set(), [], {}

    def mark(node):
        prose.update(id(m) for m in ast.walk(node))

    def compares(t):
        if isinstance(t, ast.Compare):
            ops = [t.left, *t.comparators]
            for i, op in enumerate(t.ops):
                if isinstance(op, (ast.Eq, ast.In, ast.Is)):
                    operands.extend((ops[i], ops[i + 1]))
        elif isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And):
            for v in t.values:
                compares(v)

    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            mark(n.value)
        elif isinstance(n, ast.Assert):
            compares(n.test)
            if n.msg is not None:
                mark(n.msg)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            if len(tg) == 1 and isinstance(tg[0], ast.Name):
                binds.setdefault(tg[0].id, []).append(n.value)
        elif isinstance(n, ast.Call):
            leaf = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            msgs = [k.value for k in n.keywords if k.arg == "msg"]
            if leaf in _PY_EQ_ASSERTS:
                operands.extend(n.args[:2])
                msgs += n.args[2:]
            elif leaf in ("assertTrue", "assert_") and n.args:
                compares(n.args[0])
                msgs += n.args[1:]
            elif leaf in _PY_PROSE_CALLS:
                msgs.append(n)
            for x in msgs:
                mark(x)
    in_ops = {id(m) for o in operands for m in ast.walk(o)}
    elsewhere = {m.id for m in ast.walk(tree) if isinstance(m, ast.Name) and isinstance(m.ctx, ast.Load)
                 and id(m) not in in_ops and id(m) not in prose}
    claimed, followed = {}, set()

    def walk(o):
        if isinstance(o, ast.Constant):
            if isinstance(o.value, str) and id(o) not in prose:
                claimed[id(o)] = (o.value, o.lineno)
        elif isinstance(o, (ast.List, ast.Tuple, ast.Set)):
            for e in o.elts:
                walk(e)
        elif isinstance(o, ast.Dict):
            for e in [k for k in o.keys if k is not None] + list(o.values):
                walk(e)
        elif isinstance(o, (ast.Starred, ast.Attribute)):
            walk(o.value)
        elif isinstance(o, ast.BinOp):
            walk(o.left)
            walk(o.right)
        elif isinstance(o, ast.JoinedStr):
            for v in o.values:
                if isinstance(v, ast.Constant):
                    walk(v)
        elif isinstance(o, ast.Subscript):
            walk(o.value)
            if isinstance(o.slice, ast.Constant):
                walk(o.slice)
        elif isinstance(o, ast.Call):
            if isinstance(o.func, ast.Name) and o.func.id in _PY_LIT_WRAPS:
                for a in o.args:
                    walk(a)
            else:
                walk(o.func)  # build(...)["k"], r.get(...): the callee chain, never its arguments (inputs)
        elif isinstance(o, ast.Name) and o.id in binds and o.id not in elsewhere and o.id not in followed:
            followed.add(o.id)  # expected = [...] used only in assertions: its strings are claims
            for v in binds[o.id]:
                walk(v)

    for o in operands:
        walk(o)
    inputs = [m.value for m in ast.walk(tree) if isinstance(m, ast.Constant) and isinstance(m.value, str)
              and id(m) not in prose and id(m) not in claimed]
    return sorted(set(claimed.values()), key=lambda c: (c[1], c[0])), inputs


def _js_lex(s):
    """[(kind, start, end)] spans of the string literals ('str', quotes included) and comments ('com') of JS/TS source. Regex
    literals are not recognised (a quote inside one can shift the spans after it: good enough to pick out literals)."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"`":
            j = i + 1
            while j < n and s[j] != c and not (c != "`" and s[j] == "\n"):
                j += 2 if s[j] == "\\" else 1
            out.append(("str", i, min(j + 1, n)))
            i = j + 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(("com", i, j))
            i = j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(("com", i, j))
            i = j
        else:
            i += 1
    return out


def _js_claims(text):
    """(claims, inputs) of a JS/TS test, read with its strings and comments masked: claims are the string literals and object
    keys of the expected argument of assert.equal/strictEqual/deepStrictEqual/deepEqual (the 2nd) and of expect(...).toBe/
    toEqual/... (not after .not), and string operands of === / == inside assert(...)/assert.ok(...). inputs: every other
    string literal except prose (test names, assertion messages, console output, Error text)."""
    spans = _js_lex(text)
    m = list(text)
    for kind, a, b in spans:
        for k in range(a + 1 if kind == "str" else a, b - 1 if kind == "str" else b):
            if m[k] != "\n":
                m[k] = " "
    masked = "".join(m)
    strs = [(a, b) for kind, a, b in spans if kind == "str" and b - a >= 2]
    claim_rng, prose_rng = [], []

    def args_at(i):
        """[(start, end)] of the top-level arguments of the call whose '(' is masked[i]."""
        out, depth, start = [], 0, i + 1
        for j in range(i, len(masked)):
            c = masked[j]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    out.append((start, j))
                    break
            elif c == "," and depth == 1:
                out.append((start, j))
                start = j + 1
        return [(a, b) for a, b in out if masked[a:b].strip()]

    for mt in _JS_EQ_RE.finditer(masked):
        args = args_at(mt.end() - 1)
        if len(args) >= 2:
            claim_rng.append(args[1])
            prose_rng.extend(args[2:])
    for mt in _JS_EXPECT_RE.finditer(masked):
        if not re.search(r"\.not\s*$", masked[:mt.start()]):
            claim_rng.extend(args_at(mt.end() - 1)[:1])
    for mt in _JS_OK_RE.finditer(masked):
        args = args_at(mt.end() - 1)
        if args:
            a0, b0 = args[0]
            claim_rng += [(a, b) for a, b in strs if a0 <= a and b <= b0 and (
                re.search(r"(?<![!=])===?\s*$", masked[a0:a]) or re.match(r"\s*===?(?!=)", masked[b:b0]))]
            prose_rng.extend(args[1:])
    for mt in _JS_PROSE_RE.finditer(masked):
        args = args_at(mt.end() - 1)
        if args:
            prose_rng.append(args[0] if re.match(r"(?:it|test|describe|suite)\b", mt.group(0)) else (args[0][0], args[-1][1]))

    def inside(rng, a, b):
        return any(x <= a and b <= y for x, y in rng)

    claims, inputs = set(), []
    for a, b in strs:
        s = text[a + 1:b - 1]
        if text[a] == "`":
            s = re.sub(r"\$\{[^}]*\}", " ", s)
        if inside(claim_rng, a, b):
            claims.add((s, text.count("\n", 0, a) + 1))
        elif not inside(prose_rng, a, b):
            inputs.append(s)
    for x, y in claim_rng:
        for km in re.finditer(r"[{,]\s*([A-Za-z_$][\w$]*)\s*:(?!:)", masked[x:y]):
            claims.add((km.group(1), text.count("\n", 0, x + km.start(1)) + 1))
    return sorted(claims, key=lambda c: (c[1], c[0])), inputs


def _ground_vocab(cwd, tf, req_text, inputs, override=None):
    """Lower-cased identifier tokens an expected value may come from: the requirement, every tracked text file except the test
    itself and the run's own reports (override: {path: text} read instead of the file on disk, e.g. a fixed file's baseline),
    and the test's input strings."""
    words = set(_GTOK.findall((req_text or "").lower()))
    for s in inputs:
        words.update(_GTOK.findall(s.lower()))
    override = dict(override or {})
    total = 0
    for f in dict.fromkeys([f for f in git(cwd, "ls-files", "-z").split("\0") if f] + list(override)):
        if f == tf or _REPORT_MD_RE.search(f):
            continue
        if f in override:
            txt = override[f] or ""
        else:
            p = Path(cwd, f)
            try:
                if not p.is_file() or p.stat().st_size > 1_000_000:
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue
            total += len(raw)
            if total > 64_000_000:
                break
            txt = raw.decode("utf-8", "replace")
        words.update(_GTOK.findall(txt.lower()))
    return words


def _checked_tokens(s):
    """The tokens of an expected string that need a source: every token of a key-like value (no whitespace); in a message (with
    whitespace) only identifier-shaped ones (snake_case, camelCase, letters with digits), since its plain words are prose."""
    prose = bool(re.search(r"\s", s.strip()))
    for t in _GTOK.findall(s):
        if sum(ch.isalpha() for ch in t) < GROUND_MIN_LETTERS:
            continue
        if len(t) >= 8 and sum(ch.isdigit() for ch in t) * 10 >= len(t) * 3:
            continue  # a hash or an id
        if prose and not ("_" in t.strip("_") or re.search(r"[a-z][A-Z]|[A-Za-z]\d", t)):
            continue
        yield t


def ungrounded_expectations(text, tf, task_text, cwd, override=None):
    """[(token, value, line)]: tokens of the values the test's assertions expect that appear in neither the requirement
    (task_text without its `## Diagnosis` section), nor a tracked file (the test and the run's reports aside), nor the test's
    own inputs. Added 2026-11-03 for bench-notset ca52c9841b08: the diagnosis's `"metered_days" (or similar)` became an
    expected entry of not_set that the Problem never asked for; the test and the fix agreed, every gate passed, and only the
    review caught it."""
    lang = lang_of(tf)
    got = _py_claims(text) if lang == "py" else _js_claims(text) if lang == "node" else None
    if not got or not got[0]:
        return []
    claims, inputs = got
    vocab = _ground_vocab(cwd, tf, requirement_text(task_text), inputs, override)
    out, seen = [], set()
    for s, line in claims:
        for t in _checked_tokens(s):
            if t.lower() not in vocab and t.lower() not in seen:
                seen.add(t.lower())
                out.append((t, s, line))
    return out


def ground_problem(found):
    """The phase A reject note for ungrounded_expectations."""
    shown = "; ".join(f"`{t}` (line {n}" + ("" if s == t else f", in {s[:60]!r}") + ")" for t, s, n in found[:4])
    return ("the test expects values found in neither TASK.md's Problem nor the code nor the test's own inputs: " + shown
            + (f" (+{len(found) - 4} more)" if len(found) > 4 else "")
            + ". Assert only what the Problem asks for, with the values it states or the code returns today; a diagnosis "
              "suggestion the Problem does not ask for (an extra key, value or message, \"or similar\") is not a requirement")


# User view (stage 21, 2026-11-06): when TASK.md shows the bug with python code that goes through the package's public API, a
# python test must go through that API too. loguru r4 d2619591a0d1: every test called ErrorInterceptor.print directly; with the
# Problem's catch=False, Handler.emit re-raises before print ever runs, so the fix passed its test while the Problem's own
# example still showed the bug.
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
_SPAN_RE = re.compile(r"(?<!`)`([^`]+)`(?!`)")
_NOT_PKG = {"src", "lib", "tests", "test", "testing", "docs"}


def _example_trees(task_text):
    """The python ASTs of the requirement's code (requirement_text: a Diagnosis section's code is a model's, not the user's):
    each fenced block, then each backtick span outside them, `>>>` / `...` prompts stripped; what does not parse (shell,
    output, other languages) is skipped."""
    req = requirement_text(task_text)
    out = []
    for code in [m.group(1) for m in _FENCE_RE.finditer(req)] + [m.group(1) for m in _SPAN_RE.finditer(_FENCE_RE.sub("", req))]:
        lines = code.splitlines()
        if any(ln.lstrip().startswith(">>>") for ln in lines):
            lines = [ln.lstrip()[4:] for ln in lines if ln.lstrip().startswith((">>>", "..."))]
        try:
            out.append(ast.parse(textwrap.dedent("\n".join(lines))))
        except (SyntaxError, ValueError, RecursionError):
            continue
    return out


def _bound_imports(tree):
    """{bound name: qualified name} for the absolute imports anywhere in tree (`from a.b import c as d`: d -> a.b.c; `import
    a.b`: a -> a; `import a.b as e`: e -> a.b)."""
    b = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            b.update({a.asname or a.name: f"{n.module}.{a.name}" for a in n.names if a.name != "*"})
        elif isinstance(n, ast.Import):
            b.update({a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0] for a in n.names})
    return b


def _target_pkgs(targets):
    """The top-level python packages (or single modules) the targets belong to: what a user's example imports them from."""
    out = set()
    for t in targets:
        if lang_of(t) != "py":
            continue
        parts = [p for p in Path(t).parts[:-1] if p not in _NOT_PKG] or [Path(t).stem]
        if parts[0].isidentifier():
            out.add(parts[0])
    return out


def _module_text(cwd, mod, override=None):
    """The source of python module `mod` in the checkout (a package's __init__.py; at the top, or under src/ or lib/), override
    {path: text} first; None when there is no such file."""
    rel = mod.replace(".", "/")
    for cand in (f"{b}{rel}{s}" for b in ("", "src/", "lib/") for s in ("/__init__.py", ".py")):
        if override and cand in override:
            return override[cand] or ""
        try:
            if Path(cwd, cand).is_file():
                return Path(cwd, cand).read_text(errors="replace")
        except OSError:
            return None
    return None


def _defined_today(cwd, qual, override=None):
    """True when the checkout defines qual: its longest prefix that is a module exists, and the next name appears in that
    module's text (`loguru.logger`: loguru/__init__.py mentions `logger`). A name the fix is to add is not defined today."""
    parts = qual.split(".")
    for i in range(len(parts), 0, -1):
        t = _module_text(cwd, ".".join(parts[:i]), override)
        if t is not None:
            return i == len(parts) or re.search(rf"(?<![\w.]){re.escape(parts[i])}\b", t) is not None
    return False


def _import_stmt(bound, qual):
    mod, _, name = qual.rpartition(".")
    if not mod:
        return f"import {qual}" + ("" if bound == qual else f" as {bound}")
    return f"from {mod} import {name}" + ("" if bound == name else f" as {bound}")


def example_entry_points(task_text, cwd, targets, override=None):
    """[(import statement, [calls], wanted)]: what TASK.md's python example (the user's own code) imports from the package
    under test and uses, when the checkout defines it today. wanted: the qualified names by which a test reaches the same API:
    the imported name itself, or for an imported module, the names the example calls on it (`import isort` and
    `isort.code(...)`: isort.code)."""
    pkgs = _target_pkgs(targets)
    found = {}
    for tree in _example_trees(task_text) if pkgs else ():
        bound = {k: q for k, q in _bound_imports(tree).items() if q.split(".")[0] in pkgs}
        uses = {}
        for n in ast.walk(tree) if bound else ():
            if isinstance(n, ast.Call):
                d = _dotted(n.func)
                if d and d.split(".")[0] in bound:
                    uses.setdefault(d.split(".")[0], []).append(d)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in bound:
                uses.setdefault(n.id, [])
        for k, calls in uses.items():
            q = bound[k]
            if _module_text(cwd, q, override) is not None:  # a module: the names the example calls on it
                want = {f"{q}.{d.split('.')[1]}" for d in calls if "." in d}
            else:
                want = {q}
            want = {w for w in want if _defined_today(cwd, w, override)}
            if want:
                imp, old, ow = found.get(k, (_import_stmt(k, q), [], set()))
                found[k] = (imp, list(dict.fromkeys(old + calls)), ow | want)
    return list(found.values())


def _loader_returns(tree):
    """{function name: the qualified name it returns} for a test's lazy loaders (the skeleton's `def _load_m(): import m;
    return m`)."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[-1], ast.Return):
            d = _dotted(n.body[-1].value)
            b = _bound_imports(ast.Module(body=n.body[:-1], type_ignores=[]))
            if d and d.split(".")[0] in b:
                head, _, rest = d.partition(".")
                out[n.name] = b[head] + (f".{rest}" if rest else "")
    return out


def _ref_name(n, qual, loaders):
    """The qualified name a Name/Attribute chain refers to through the test's imports, or through a lazy loader's call."""
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        base = qual.get(n.id)
    elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.args and not n.keywords:
        base = loaders.get(n.func.id)
    else:
        return None
    return ".".join([base, *reversed(parts)]) if base else None


def user_view_gap(text, tf, task_text, cwd, targets, override=None):
    """[(import statement, [calls])] for the public API TASK.md's python example uses (example_entry_points) when no code of the
    python test outside its GUARDS refers to any of it; [] when some does, or with no such example. A name bound by any import
    in the file counts wherever it is used (function-local imports included), as does a lazy loader's return value."""
    if lang_of(tf) != "py":
        return []
    ent = example_entry_points(task_text, cwd, targets, override)
    if not ent:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    guards, qual, loaders = set(declared_guards(text, tf)), _bound_imports(tree), _loader_returns(tree)
    wanted = set().union(*(w for _i, _c, w in ent))
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Import, ast.ImportFrom)) or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in guards):
            continue
        if isinstance(n, (ast.Name, ast.Attribute)):
            r = _ref_name(n, qual, loaders)
            if r and any(r == w or r.startswith(w + ".") for w in wanted):
                return []
        stack.extend(ast.iter_child_nodes(n))
    return [(imp, calls) for imp, calls, _w in ent]


def _uv_line(imp, calls):
    return f"`{imp}`" + (": " + ", ".join(f"`{c}`" for c in calls[:4]) + (" ..." if len(calls) > 4 else "") if calls else "")


def user_view_problem(gap):
    """The phase A reject note for user_view_gap."""
    return ("no test outside GUARDS goes through the public API that TASK.md's example uses ("
            + "; ".join(_uv_line(i, c) for i, c in gap[:3]) + "): every test calls internal code, which can pass while the user "
            "still sees the bug. Add a test that runs the example (or a smaller one with the same bug) through that API, with "
            "that plain import (inside the test is fine; keep the imports of the code under test), and asserts what the user "
            "sees: the output, the error raised or the value returned. The tests of internal code may stay")


TEST_RUN_S = 150  # test_gate's cap on one run of test_cmd (scrapy r2 5f2fbdc72e8e: a drafted test hung in reactor.run() and every run waited cmd_timeout, 300 s)
HANG_RE = re.compile(r"TIMEOUT (\d+)s")  # _bash's whole output when it killed a run at its timeout


def test_run_cap(spec):
    """Seconds test_gate gives one run of test_cmd: spec test_cmd_timeout (default TEST_RUN_S), at most cmd_timeout. A test the
    gate judges runs one test file, which finishes in seconds; one still running at the cap hangs."""
    return min(int(spec.get("test_cmd_timeout", TEST_RUN_S)), int(spec.get("cmd_timeout", 300)))


def hang_problem(cap):
    return (f"the test did not finish within {cap}s: it hangs (something it waits for never happens, e.g. an event loop or reactor "
            "that is never stopped, a callback that never fires, a network call, a lock or input()); every test must finish in "
            "seconds and fail on an assertion")


# ---- stage 26 (2026-11-07): the plain-script runner ----
# Three of batch 2's phase-A failures were the model-owned runner, not the test: unyt's and holoviews's own __main__ blocks
# printed "FAIL: name: err" and always exited 0 (the gate said "passes at baseline", the model believed it and the fallback reused
# the file); kopf wrote pytest-style tests and no block at all (zero tests ran; A2 spent its 30 steps). Phase A's gate now repairs
# the runner itself when the tests can run as plain calls (top-level, no required arguments) and the old block did nothing but run
# them, then judges the test with it.
_FAILISH = re.compile(r"^[ \t]*(?:\[?FAIL(?:ED|URE)?\]?[ \t]*:?[ \t]+\w|RESULT[ \t]+\w+[ \t]+FAIL\b"
                      r"|\w+[ \t]*(?:\.\.\.|:|-)?[ \t]*(?:FAIL(?:ED)?|[Ff]ailed)\b|[\u2717\u2718\u274c][ \t]*\w)")
_RUNNER_MODS = frozenset({"asyncio", "inspect", "os", "sys", "traceback", "time"})
_RUNNER_BUILTINS = frozenset({"print", "len", "str", "repr", "type", "isinstance", "getattr", "hasattr", "sorted", "enumerate", "list",
                              "range", "format", "callable", "exit", "SystemExit", "Exception", "int", "sum", "any", "all", "zip"})
_RUNNER_DOTTED = frozenset({"sys.exit", "sys.stdout.write", "sys.stderr.write", "sys.stdout.flush", "sys.stderr.flush",
                            "traceback.print_exc", "traceback.format_exc", "asyncio.run", "asyncio.get_event_loop",
                            "asyncio.new_event_loop", "asyncio.set_event_loop", "inspect.iscoroutinefunction", "inspect.iscoroutine",
                            "inspect.isawaitable", "time.time", "time.perf_counter", "time.monotonic", "os.environ.get", "os.getenv"})
_RUNNER_METHODS = frozenset({"append", "extend", "join", "format", "items", "keys", "values", "get", "add", "strip", "split", "upper",
                             "lower", "run_until_complete", "close"})


def _py_failed_silently(tf, text, out, spec):
    """(names, why) for a plain-script python run that exited 0 but did not pass: declared tests on a line that reports a failure
    in any common format (its __main__ block never exits 1), else async tests it never awaited (`coroutine 'test_x' was never
    awaited`: they 'passed' without running). ([], "") otherwise."""
    if lang_of(tf) != "py" or not py_direct_run(spec):
        return [], ""
    names, _explicit = declared_tests(text, tf)
    if not names:
        return [], ""
    out = out or ""
    passed = {r[0] for r in parse_test_results(out) if r[1] == "PASS"}  # a test's own print that says "failed" is not its result
    bad = [ln for ln in out.splitlines() if _FAILISH.match(ln)]
    failed = [n for n in names if n not in passed and any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", ln) for ln in bad)]
    if failed:
        return failed, f"the run exited 0 although it printed FAIL for {', '.join(failed[:4])}"
    lost = [n for n in names if re.search(rf"coroutine '(?:[\w.<>]+\.)?{re.escape(n)}' was never awaited", out)]
    if lost:
        return lost, f"it called the async test(s) {', '.join(lost[:4])} without awaiting them"
    return [], ""


def _required_args(fn):
    """True when a def needs an argument (a pytest fixture such as tmp_path): the plain runner calls each test with none."""
    a = fn.args
    return len(a.posonlyargs) + len(a.args) - len(a.defaults) > 0 or any(d is None for d in a.kw_defaults)


def _runs_only_tests(node, names, tree):
    """True when a python `if __name__ == "__main__":` block does nothing but run the tests and report: calls of the test functions,
    of names it binds itself, of print/len/... and of sys.exit, traceback, asyncio.run, inspect checks and str/list methods, and
    no name it binds (other than TESTS and GUARDS, which the rewrite keeps) is one a test reads as a global. Anything else (setup
    the tests need, a constant they read, a server, an import of the code under test) keeps the block the model's."""
    alias = {}
    for n in list(tree.body) + list(ast.walk(node)):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _RUNNER_MODS:
                    alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in _RUNNER_MODS and not n.level:
            for a in n.names:
                alias[a.asname or a.name] = f"{n.module}.{a.name}"
    for n in ast.walk(node):
        if isinstance(n, (ast.Delete, ast.Global, ast.Nonlocal, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                          ast.With, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom)):
            return False
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] not in _RUNNER_MODS for a in n.names):
            return False
        if isinstance(n, ast.ImportFrom) and (n.level or not n.module or n.module.split(".")[0] not in _RUNNER_MODS):
            return False
    local = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    local |= {n.name for n in ast.walk(node) if isinstance(n, ast.ExceptHandler) and n.name}

    def store_ok(t):
        if isinstance(t, ast.Name):
            return True
        if isinstance(t, (ast.Tuple, ast.List)):
            return all(store_ok(e) for e in t.elts)
        if isinstance(t, ast.Starred):
            return store_ok(t.value)
        return isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in local

    for n in ast.walk(node):
        tg = (n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For,
              ast.AsyncFor, ast.comprehension, ast.NamedExpr)) else [])
        if not all(store_ok(t) for t in tg):
            return False
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            if f.id in names or f.id in local or f.id in _RUNNER_BUILTINS or alias.get(f.id) in _RUNNER_DOTTED:
                continue
            return False
        d = _dotted(f)
        if d:
            head, _, rest = d.partition(".")
            if head in alias and alias[head] + ("." + rest if rest else "") in _RUNNER_DOTTED:
                continue
        if isinstance(f, ast.Attribute) and f.attr in _RUNNER_METHODS and (
                isinstance(f.value, (ast.Constant, ast.JoinedStr, ast.Call)) or (isinstance(f.value, ast.Name) and f.value.id in local)):
            continue
        return False
    free = set()
    for d in tree.body:  # a name the block binds that a test reads as a global (a constant set up there) must stay: refuse
        if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound = {x.id for x in ast.walk(d) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            bound |= {x.arg for x in ast.walk(d) if isinstance(x, ast.arg)}
            free |= {x.id for x in ast.walk(d) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)} - bound
    return not (free & (local - {"TESTS", "GUARDS"}))


def _block_lists(block):
    """{"TESTS": [...], "GUARDS": [...]}: the lists a python `if __name__ == "__main__":` block assigns at its own top level
    (holoviews defined both there, where declared_tests and declared_guards do not look), each element as source text."""
    out = {}
    for s in block.body:
        if (isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                and s.targets[0].id in ("TESTS", "GUARDS") and isinstance(s.value, (ast.List, ast.Tuple))):
            out[s.targets[0].id] = [ast.unparse(e) for e in s.value.elts]
    return out


def py_runner_rewrite(text, tf, replace=False):
    """text with the canonical runner block (py_main_block) in place of the model's `if __name__ == "__main__":` block (replace)
    or added at the end (no block yet: zero tests ran), plus a module-level TESTS (and GUARDS) when the file has none: the lists
    the old block defined itself (holoviews), else its top-level test functions. None when that is not safe: the file does not
    parse, a test is not a top-level function callable without arguments (pytest fixtures), or the old block does more than run
    the tests (_runs_only_tests)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names, explicit = declared_tests(text, tf)
    mains = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    if (replace and len(mains) != 1) or (not replace and mains):
        return None
    inner = _block_lists(mains[0]) if replace else {}
    if not explicit and inner.get("TESTS"):
        names = inner["TESTS"]
    top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not names or any(n not in top or _required_args(top[n]) for n in names):
        return None
    lines = text.splitlines()
    if replace:
        m = mains[0]
        if m.orelse or not _runs_only_tests(m, set(names), tree):
            return None
        later = [n for n in tree.body if n.lineno > m.end_lineno]
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names for n in later) or any(
                isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and any(isinstance(t, ast.Name) and t.id in ("TESTS", "GUARDS") for t in ast.walk(n)) for n in later):
            return None
        head, tail = lines[:m.lineno - 1], lines[m.end_lineno:]
    else:
        head, tail = lines, []
    while head and not head[-1].strip():
        head.pop()
    while tail and not tail[0].strip():
        tail.pop(0)
    bound = {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
             for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    add = [] if explicit else [f"TESTS = [{', '.join(names)}]  # explicit order: list every test function (never globals()/dir())"]
    if inner.get("GUARDS") is not None and "GUARDS" not in bound:
        add.append(f"GUARDS = [{', '.join(inner['GUARDS'])}]  # the tests in TESTS that PASS now and after the fix")
    return "\n".join(head + (["", ""] + add if add else []) + ["", ""] + py_main_block() + (["", ""] + tail if tail else [])) + "\n"


def _py_runner_fix(cwd, spec, targets, ground, tf, text, zero, silent):
    """Phase A's runner repair (stage 26, see the block comment above): write the canonical runner into the test file once and
    judge the test with it. None when the rewrite is not safe or changes nothing (the caller then reports the runner problem)."""
    new = py_runner_rewrite(text, tf, replace=not zero)
    if new is None or new == text:
        return None
    how = "added a `__main__` block" if zero else "replaced the `__main__` block"
    why = "nothing in the file ran the tests" if zero else silent[1]
    Path(cwd, tf).write_text(new)
    print(f"[mini-runner] test runner: the gate {how} in {tf} ({why}); re-running the test gate", flush=True)
    bad, ev = test_gate(cwd, spec, targets, ground)
    ev["runner_fixed"] = how
    if bad:
        bad = [f"the gate {how} in {tf} ({why}): keep that block as it is; with it the run shows: {bad[0]}"] + bad[1:]
    return bad, ev


def _fb_runner_fix(cwd, spec, tf):
    """The direct fallback's runner repair (stage 27): the fallback's test is its own and phase A's gate never sees it; holoviews's
    and unyt's fallbacks reused a block that printed FAIL and exited 0 (the gate then saw a test that passes at baseline), pydap's
    printed failures with no exception type. A plain-script python test whose `if __name__ == "__main__":` block prints no RESULT
    line, or that has no block and runs nothing, gets the canonical runner in place (py_runner_rewrite: only when the old block
    does nothing but run the tests). Returns how, or None (no change)."""
    if lang_of(tf) != "py" or not py_direct_run(spec):
        return None
    p = Path(cwd, tf)
    try:
        text = p.read_text()
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return None
    mains = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    if len(mains) == 1:
        if "RESULT" in (ast.get_source_segment(text, mains[0]) or ""):
            return None
        new, how = py_runner_rewrite(text, tf, replace=True), "replaced its `__main__` block"
    elif not mains and _zero_tests_ran(tf, text, "", [], spec.get("test_cmd") or ""):
        new, how = py_runner_rewrite(text, tf, replace=False), "added a `__main__` block"
    else:
        return None
    if new is None or new == text:
        return None
    p.write_text(new)
    return how


def _zero_tests_problem(tf, text, spec):
    """Phase A's problem for a plain-script python test whose run executed none of its tests (kopf: pytest fixtures and no
    runner): how to make every test callable without arguments, and the runner block to end the file with."""
    names, explicit = declared_tests(text, tf)
    tests = f"`TESTS = [{', '.join(names)}]` and " if names and not explicit else ""
    return (f"zero tests ran: `{spec['test_cmd']}` runs {tf} as a plain script and nothing in it calls the test functions. pytest "
            "fixtures (tmp_path, monkeypatch, capsys) do not exist there: use `tempfile.TemporaryDirectory()` and "
            "`unittest.mock.patch(...)` inside each test so that every test takes no arguments, then end the file with "
            f"{tests}the skeleton's runner: an `if __name__ == \"__main__\":` block that runs each test in TESTS (asyncio.run() "
            "for an async one), prints `RESULT <name> PASS` or `RESULT <name> FAIL <ExceptionType>: <message>` for each and exits 1 "
            "when any failed")


def _silent_fail_problem(tf, why):
    """Phase A's problem for a run that exited 0 while tests failed or never ran (and the gate could not safely rewrite the
    runner itself): what the block must print and how it must exit."""
    return (f"{why}: {tf}'s `if __name__ == \"__main__\":` block must print `RESULT <name> PASS` or `RESULT <name> FAIL "
            "<ExceptionType>: <message>` for each test and exit 1 when any failed. Keep any setup it does and run the tests the way "
            "the skeleton's runner does: each test in TESTS, asyncio.run() for an async one, count the failures, "
            "`sys.exit(1 if failed else 0)`")


def test_gate(cwd, spec, targets, ground="off", fix_runner=False):
    """Phase A gates on the test file at the baseline. Returns (problems, evidence). ground: the static checks against TASK.md
    in cwd, expectation grounding (ungrounded_expectations) and the user view (user_view_gap: a python test must go through the
    public API TASK.md's example uses): "hard" rejects, with both problems at once, before the test runs; "soft" only records
    ev["ungrounded"] and ev["user_view"] (phase A's last attempt, so a false alarm costs a review note, not the job); "off"
    (supplied tests, dry runs) skips them. spec ground / user_view false (env MINI_GROUND / MINI_USER_VIEW=off) turns one off.
    Each run of test_cmd is capped at test_run_cap(spec); a run killed there is rejected as a hang. fix_runner (phase A's own
    call, stage 26): a plain-script python test whose runner ran nothing or hid its failures (or, stage 27, failed with no RESULT
    line and no assertion text the gate can read: ultraplot r1 29b8852173c9) gets the canonical runner block from the gate itself,
    once, and is judged with it (_py_runner_fix)."""
    tf, ev = spec["test_file"], {}
    p = Path(cwd) / tf
    if not p.exists():
        return [f"{tf} was not created"], ev
    text = p.read_text()
    if SKELETON_MARK in text:
        return [f"{tf} still contains the skeleton placeholder {SKELETON_MARK}: replace every placeholder with a real test"], ev
    spec.pop("_base_red", None)
    bad = test_static(text, targets, int(spec.get("min_asserts", 2)), tf, cwd, test_rules=spec.get("test_rules") != "off",
                      source_checks=spec.get("source_checks") != "allow", fns=as_list(spec.get("target_fn")))
    if bad:
        return bad, ev
    tm = Path(cwd, "TASK.md")
    _remember_task(spec, cwd, targets)
    early = []
    if ground != "off" and spec.get("ground") != "off" and os.environ.get("MINI_GROUND") != "off" and tm.is_file():
        found = ungrounded_expectations(text, tf, tm.read_text(errors="replace"), cwd)
        if found:
            ev["ungrounded"] = [f"`{t}` (test line {n}): {s[:100]!r}" for t, s, n in found]
            if ground == "hard":
                early.append(ground_problem(found))
    if ground != "off" and spec.get("user_view") is not False and os.environ.get("MINI_USER_VIEW") != "off" and tm.is_file():
        try:
            gap = user_view_gap(text, tf, tm.read_text(errors="replace"), cwd, targets)
        except Exception as e:  # a missed nudge costs less than a crashed phase A
            gap, ev["user_view_error"] = [], f"{type(e).__name__}: {e}"[:200]
        if gap:
            ev["user_view"] = [_uv_line(imp, calls) for imp, calls in gap]
            if ground == "hard":
                early.append(user_view_problem(gap))
    if early:
        return early, ev
    probe = [t for t in targets if lang_of(t) and lang_of(t) == lang_of(tf)]
    cap = test_run_cap(spec)
    rc, out, reach = run_cmd(spec["test_cmd"], cwd, spec, timeout=cap, probe_targets=probe, full_reach=True)
    per = parse_test_results(out)  # G1: per-test red classification (falls back to the whole-output checks when nothing parses)
    hits = reach["fns"] if reach is not None else None
    ev.update(baseline_rc=rc, baseline_out=out[-3000:], reach=sorted(hits) if hits is not None else None, tests=test_count(out),
              fn_cov={k: round(v, 2) for k, v in sorted((reach or {}).get("fn_cov", {}).items())})
    hang = HANG_RE.fullmatch(out.strip()) if rc == 124 else None
    if hang and int(hang.group(1)) >= cap:  # a smaller number: the job's own deadline cut the run short, not a hang
        return [hang_problem(cap)], ev
    if rc == 0:
        zero = _zero_tests_ran(tf, text, out, per, spec["test_cmd"])
        silent = ([], "") if zero else _py_failed_silently(tf, text, out, spec)
        if (zero or silent[0]) and fix_runner:
            fixed = _py_runner_fix(cwd, spec, targets, ground, tf, text, zero, silent)
            if fixed is not None:
                return fixed
        if zero:
            return [_zero_tests_problem(tf, text, spec)], ev
        if silent[0]:
            return [_silent_fail_problem(tf, silent[1])], ev
        return ["test passes at baseline (no evidence of the bug)"], ev
    ev["per_test"] = [f"{n} {st} {t or ''}".strip() for n, st, t, _m in per]
    if per:
        pbad = per_test_red_problems(per, spec, text, tf, targets)
        if pbad:
            return pbad, ev
    crash = crash_hit(out, per)  # stage 25: a traceback the code logs on its way to the verdicts is not a crash
    if crash:
        return ["test crashes/errors instead of failing on an assertion: " + crash.group(0)], ev
    stub_err = as_list(spec.get("stub_error"))  # a seeded stub's own throw (`not implemented: fn`) is the red the test is supposed to show
    if not per and not spec.get("allow_error_fail") and not FAILASSERT_RE.search(out) and not any(x in out for x in stub_err):
        if fix_runner and lang_of(tf) == "py" and py_direct_run(spec):  # stage 27, ultraplot r1 29b8852173c9: the model's block
            # printed "FAIL: name" / "Error: msg" (no exception type) and exited 1, so no red could be classified
            fixed = _py_runner_fix(cwd, spec, targets, ground, tf, text, False,
                                   ([], "it printed no `RESULT <name> PASS|FAIL <ExceptionType>: <message>` line, so the gate could "
                                        "not see why each test failed"))
            if fixed is not None:
                return fixed
        return ["baseline failure is not an assertion failure (wrong-reason failure)"], ev
    if spec.get("symptom") and not re.search(spec["symptom"], out, re.S | re.M):
        return [f"baseline failure does not match symptom /{spec['symptom']}/"], ev
    if ev["tests"] is not None and ev["tests"] < int(spec.get("min_tests", 2)):
        return [f"only {ev['tests']} test(s) ran; need >= {spec.get('min_tests', 2)}"], ev
    if hits is not None:
        want = spec.get("target_fn") or []
        miss = [w for w in ([want] if isinstance(want, str) else want) if w not in hits]
        if miss:
            return [f"fixture never reaches `{'`, `'.join(miss)}` in {probe} (functions that ran: {sorted(hits) or 'none'})"], ev
        if not hits:
            return [f"fixture never runs any function in {probe}"], ev
        thr = float(spec.get("min_fn_cov", 0.3))
        names = [want] if isinstance(want, str) else list(want)
        anchors = as_list(spec.get("reach_anchor"))
        skip_cov = False
        for t in probe:
            if not (anchors and reach["mapped"].get(t)):
                continue
            skip_cov = True
            src = Path(cwd, t).read_text(errors="replace")
            for a in anchors:
                n = anchor_line(src, a)
                if n is not None and n not in reach["lines"].get(t, ()):
                    return [f"fixture never executes line {n} `{a}` (TASK entry point); last line it ran before it: "
                            f"{last_ran_before(src, reach['lines'].get(t, ()), n)}"], ev
        low = [(w, reach["fn_cov"][w]) for w in names if not skip_cov and reach["fn_cov"].get(w) is not None and reach["fn_cov"][w] < thr]
        waived = {}
        dls = spec.get("diag_lines") if _line_list(spec.get("diag_lines")) else []
        t0 = targets[0] if targets else None
        for w, _c in low:  # the stop is the guard that reads the Constant the fix edits: that guard is the bug (const_guard_stop)
            for t in probe:
                g = const_guard_stop(Path(cwd, t).read_text(errors="replace"), w, reach["lines"].get(t, ()), as_list(spec.get("new_names"))) \
                    if lang_of(t) == "py" and reach["mapped"].get(t) else None
                if g:
                    waived[w] = f"{t}:{g} (it stops at the guard that reads the Constant the fix edits)"
                    break
            # the test runs a line the accepted diagnosis names as the fault: a long function whose bug path is one of several
            # alternative branches (loguru r3 bf6183725d19: 26% of an 84-line emit) is driven far enough
            if w not in waived and dls and t0 in probe and lang_of(t0) == "py" and reach["mapped"].get(t0):
                n = diag_line_hit(Path(cwd, t0).read_text(errors="replace"), w, reach["lines"].get(t0, ()), dls)
                if n:
                    waived[w] = f"{t0}:{n} (it runs a line the diagnosis names as the fault)"
        if waived:
            ev["cov_waived"] = waived
            low = [(w, c) for w, c in low if w not in waived]
        if low:
            src = "\n".join(Path(cwd, t).read_text(errors="replace") for t in probe[:1])
            lastran = last_ran_before(src, reach["lines"].get(probe[0], ()), len(src.splitlines()) + 1) if probe and reach["mapped"].get(probe[0]) else None
            alt = f", or a run through one of the lines the diagnosis names as the fault in it: {', '.join(map(str, dls))} of {t0}" if dls else ""
            return [f"test runs only {int(c * 100)}% of `{w}`'s body lines (need >= {int(thr * 100)}%{alt}): it stops at an early "
                    f"return/guard; drive `{w}` through the code path TASK.md is about"
                    + (f"; last line it ran: {lastran}" if lastran and lastran != "none" else "") for w, c in low], ev
    ctl = {t: (Path(cwd, t).read_text() + ("\n# mini-runner negative control\n" if t.endswith(".py") else "\n// mini-runner negative control\n"))
           for t in probe if Path(cwd, t).exists()}
    if ctl:
        prev = swap_texts(cwd, ctl)
        try:
            rc2, out2, _ = run_cmd(spec["test_cmd"], cwd, spec, timeout=cap)
        finally:
            swap_texts(cwd, prev)
        ev["negative_control_rc"] = rc2
        if rc2 == 0:
            return ["negative control (no-op edit of the target) made the test pass: flaky or vacuous"], ev
    cp = collect_check(cwd, spec, tf, text)
    if cp:
        return [cp], ev
    if per:
        spec["_base_red"] = sorted(n for n, st, _t, _m in per if st == "FAIL")
    return [], ev


PYTEST_SUITE_RE = re.compile(r"^\s*(?:cd\s+[^\s;&|<>`$()]+\s*&&\s*)?(?:\S*/)?(?:python3?(?:\.\d+)?\s+-m\s+pytest|pytest|py\.test)"
                             r"(?:\s+[^|;&<>`$()\n]*)?$")
IMPORT_STATE_RE = re.compile(r"\b(?:sys\.path|sys\.modules|sys\.meta_path|os\.chdir|site\.addsitedir|importlib\.reload)\b")
COLLECT_ERR_RE = re.compile(r"^ERROR (\S+)(?: - (.*))?$", re.M)
COLLECT_SEC_RE = re.compile(r"^_+ ERROR collecting (\S+) _+$", re.M)


def collect_cmd(spec):
    """suite_cmd as a collect-only run with any -x/--maxfail dropped (one early error must not hide the rest), or None when
    suite_cmd is not a plain pytest command. A suite narrowed at baseline (spec _suite_full) still collects the whole suite: the
    collection is fast, and a test that breaks imports breaks files far from the change."""
    cmd = (spec.get("_suite_full") or spec.get("suite_cmd") or "").strip()
    if not PYTEST_SUITE_RE.match(cmd):
        return None
    return re.sub(r"\s+(?:-x|--exitfirst|--maxfail(?:=|\s+)\d+)(?=\s|$)", "", cmd) + " --collect-only"


def collect_reasons(out, cwd=None):
    """{path: reason} from pytest's `ERROR <path> - reason` summary lines, the reason taken from the file's `ERROR collecting`
    section (its last `E   ` line) when the summary line has none: pytest prints an ImportError at collection as a bare
    `ERROR <path>`, and the section's `ImportError: cannot import name 'x' from 'colorsys' (pkg/sub/colorsys.py)` is what shows
    the model the shadowing. Paths under cwd are shown relative to it."""
    secs, heads = {}, list(COLLECT_SEC_RE.finditer(out))
    for i, m in enumerate(heads):
        body = out[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(out)]
        es = re.findall(r"^E\s+(\S.*)$", re.split(r"^={3,}", body, maxsplit=1, flags=re.M)[0], re.M)
        if es:
            secs[m.group(1)] = es[-1].strip()
    res = {}
    for m in COLLECT_ERR_RE.finditer(out):
        r = (m.group(2) or "").strip() or secs.get(m.group(1), "")
        if cwd is not None:
            for root in {str(Path(cwd)), str(Path(cwd).resolve())}:
                r = r.replace(root + os.sep, "")
        res[m.group(1)] = r[:200]
    return res


def suite_collect_errors(cwd, spec):
    """{path: reason} for every file pytest reports as `ERROR <path>` while collecting the suite (collect_cmd), or None when
    suite_cmd is not a plain pytest command or the collect did not finish in time."""
    cmd = collect_cmd(spec)
    if cmd is None:
        return None
    rc, out, _ = run_cmd(cmd, cwd, spec, timeout=min(int(spec.get("cmd_timeout", 300)), 300))
    if rc == 124:
        return None
    return collect_reasons(out, cwd)


def collect_check(cwd, spec, tf, text):
    """Phase A: a python test that touches import state (sys.path, sys.modules, the working directory) must leave the rest of the
    suite importable. pytest imports every test file in one process, so a module-level sys.path insert of a package directory
    shadows stdlib and top-level modules for every file collected after it. 2026-11-05 (real-issue bench conan1 r2): the frozen
    test put ../conan/internal/model on sys.path, `profile` became conan's profile.py and test/performance/test_large_graph.py
    stopped importing; phase A accepted the test, phase B's suite gate failed a correct fix, and the frozen test could not change.
    Compares the suite's collect-only errors with the test file present against the same collect without it (cached per run in
    spec["_collect_base"]); new errors, the test file's own included, are the problem. The skeleton's repo-root sys.path line
    alone does not trigger the check: pytest and `python -m` already import from the repo root. Returns a problem or None."""
    if lang_of(tf) != "py" or not spec.get("suite_cmd"):
        return None
    tdir = str(Path(tf).parent) or "."
    root_line = f"sys.path.insert(0, str(Path(__file__).resolve().parent / {os.path.relpath('.', tdir)!r}))"
    if not IMPORT_STATE_RE.search(text.replace(root_line, "")):
        return None
    t0 = time.time()
    if "_collect_base" not in spec:
        tp = Path(cwd, tf)
        saved = tp.read_bytes() if tp.is_file() else None
        try:
            tp.unlink(missing_ok=True)
            spec["_collect_base"] = suite_collect_errors(cwd, spec)
        finally:
            if saved is not None:
                tp.write_bytes(saved)  # byte for byte: the frozen test's sha is taken after this gate
    base = spec["_collect_base"]
    now = suite_collect_errors(cwd, spec) if base is not None else None
    print(f"[mini-runner] collect check ({time.time() - t0:.0f}s): baseline errors="
          f"{'n/a' if base is None else len(base)} now={'n/a' if now is None else len(now)}", flush=True)
    if base is None or now is None:
        return None
    new = {p: r for p, r in now.items() if p not in base}
    if not new:
        return None
    own = [p for p in new if p == tf or tf.endswith("/" + p) or p.endswith("/" + tf)]
    cmd = collect_cmd(spec)
    if own:
        return (f"pytest cannot import `{tf}` while it collects the repo suite (`{cmd}`): {new[own[0]] or 'ERROR'}. "
                "The suite gate runs the whole suite with your test file in it, so the file must also import under pytest")
    shown = "; ".join(f"`{p}`: {r}" if r else f"`{p}`" for p, r in list(new.items())[:3])
    return (f"suite collection: with `{tf}` present, pytest can no longer import {len(new)} other test file(s) of the repo suite "
            f"(`{cmd}`): {shown}. pytest imports your file while it collects the whole suite, so a module-level change "
            "to sys.path, sys.modules or the working directory leaks into every file collected after it (a package directory on "
            "sys.path shadows stdlib modules such as `profile`). Keep the skeleton's imports: import the code under test by its "
            "package path, with at most the repo root on sys.path")


def _lit_bullets(body):
    """[(file_or_None, literal)] from the `- ` bullets of a literal section: every `backticked` span, `in <file>:` names the file."""
    out = []
    for line in body.splitlines():
        if not re.match(r"\s*-\s", line):
            continue
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        out += [(fm.group(1) if fm else None, lit) for lit in re.findall(r"`([^`\n]{1,200})`", line) if "TODO" not in lit]
    return out


def parse_must_contain(task_text):
    """[(file_or_None, literal)] from `## Must contain` (same grammar as check_literals.py)."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text, re.S | re.I)
    return _lit_bullets(m.group(1)) if m else []


def parse_must_not_contain(task_text):
    """[(file_or_None, literal)] from `## Must not contain`: stale text the task replaces (an old doc comment's claim, a wrong
    message). Same bullet grammar as `## Must contain`. Added 2026-11-03: the bfmr-split-tiebreak fix passed every gate with the
    header comment still describing the old first-row-wins rule that TASK.md said to update."""
    m = re.search(r"##+\s*Must not contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text, re.S | re.I)
    return _lit_bullets(m.group(1)) if m else []


def _prose(s):
    """Text compared as prose: leading comment markers (`/**`, `*`, `*/`, `//`, `#`) dropped from every line and all whitespace runs
    (line wraps included) collapsed to one space, so a phrase still matches after a doc comment re-wraps it."""
    return re.sub(r"\s+", " ", "\n".join(re.sub(r"^\s*(?:/\*+|\*+/?|//+|#+)", "", ln) for ln in (s or "").splitlines())).strip()


def stale_check(cwd, lits, allowed, base_texts):
    """(still_present, vacuous) for `## Must not contain` literals, matched as _prose. still_present: in a final allowed file
    (a bare literal: any allowed file). vacuous: not in the baseline of the file(s) it names, or names a file the fix may not edit,
    so its absence proves nothing (a spec typo): the gate fails on it too, and mini-validate's V4 runs this gate on the reference fix,
    so the spec author sees it before submitting."""
    present, vacuous = [], []
    for f, lit in lits:
        files = [f] if f else list(allowed)
        p = _prose(lit)
        if not p or any(x not in base_texts for x in files):
            vacuous.append(lit)
            continue
        if not any(p in _prose(base_texts.get(x) or "") for x in files):
            vacuous.append(lit)
        elif any(p in _prose(Path(cwd, x).read_text(errors="replace") if Path(cwd, x).is_file() else "") for x in files):
            present.append(lit)
    return present, vacuous


def stale_gate(cwd, allowed, base_texts, task_text):
    """The must_not_contain gate tuple, or None when TASK.md has no `## Must not contain` list."""
    lits = parse_must_not_contain(task_text)
    if not lits:
        return None
    present, vacuous = stale_check(cwd, lits, allowed, base_texts)
    bad = ([f"still present (TASK.md says this text must go: reword or remove it): {present}"] if present else []) + \
          ([f"not in the baseline of an allowed file, so the check proves nothing (fix the spec): {vacuous}"] if vacuous else [])
    return (not bad, "; ".join(bad) if bad else f"{len(lits)} stale phrase(s) gone")


def parse_must_keep(task_text):
    """[(file_or_None, literal)] from lines starting `Must keep:` (optionally bulleted) and an optional `## Must keep` section."""
    out, lines = [], []
    m = re.search(r"##+\s*Must keep[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text, re.S | re.I)
    if m:
        lines += [ln for ln in m.group(1).splitlines() if re.match(r"\s*-\s", ln)]
    lines += [ln for ln in task_text.splitlines() if re.match(r"\s*(?:[-*]\s+)?Must keep\s*:", ln, re.I)]
    for line in lines:
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        out += [(fm.group(1) if fm else None, lit) for lit in re.findall(r"`([^`\n]{1,200})`", line) if "TODO" not in lit]
    return out


def keep_check(cwd, keeps, allowed, base_texts):
    """Literals that are gone from the final allowed file(s), or that survive only if the diff's removed lines are ignored."""
    now = {f: Path(cwd, f).read_text() if Path(cwd, f).is_file() else "" for f in set(allowed) | {f for f, _ in keeps if f}}
    bad = []
    for f, lit in keeps:
        files = [f] if f else allowed
        if not any(lit in now[x] for x in files):
            bad.append(lit)
            continue
        rem = add = ""
        for ln in git(cwd, "diff", "-U0", "--", *files).splitlines():
            if ln.startswith("-") and not ln.startswith("---"):
                rem += ln[1:] + "\n"
            elif ln.startswith("+") and not ln.startswith("+++"):
                add += ln[1:] + "\n"
        nb = sum((base_texts.get(x) or "").count(lit) for x in files)
        nn = sum(now[x].count(lit) for x in files)
        if lit in rem and lit not in add and nn < nb:  # only occurrences the diff removed
            bad.append(lit)
    return bad


def _normalize_for_match(s):
    """Strip a backslash preceding a regex-special char [./ +*?()[]{}|^$]."""
    out = []
    i = 0
    specials = set('.-/+*?()[]{}|^$')
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s) and s[i + 1] in specials:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)


def literal_check(cwd, lits, allowed, base_texts):
    """(missing, already_at_baseline) literals; a bare literal may sit in any allowed file."""
    now = {f: Path(cwd, f).read_text() if Path(cwd, f).is_file() else "" for f in set(allowed) | {f for f, _ in lits if f}}
    missing, weak = [], []
    for f, lit in lits:
        files = [f] if f else allowed
        norm_lit = _normalize_for_match(lit)
        if not any(norm_lit in _normalize_for_match(now[x]) for x in files):
            missing.append(lit)
        elif any(norm_lit in _normalize_for_match(base_texts.get(x) or "") for x in files):
            weak.append(lit)
    return missing, weak


# ---- whitespace-only edits (gate ws_only, never fails) -------------------------------------------------------------------
# A fix that re-spaces lines it had no reason to touch (bfmr-split-tiebreak 2026-11-03: `column. A bad` -> `column.  A bad` in a
# comment) passes every gate and is a review reject. fix_gate restores the baseline whitespace on such lines while the acceptance
# test stays green. Only same-size replace hunks whose every line pair differs in whitespace alone (outside string literals) are
# touched, so line numbers (and the coverage the test just measured) do not move. Files whose whitespace is syntax or layout
# (markdown, yaml, Makefiles, shell, go) are never touched; indentation is left alone in python and next to a real code hunk.
_WS_CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".py", ".css", ".scss", ".java", ".c", ".h", ".cc",
                ".cpp", ".hpp", ".cs", ".rs", ".swift", ".kt", ".php"}
_WS_INDENT_EXT = {".py"}  # indentation is syntax
_STR_LIT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\\n]|\\.)*`')
# TASK.md asks for a code-formatting change, so ws_cleanup must not undo it. Formatting wording only: whitespace as DATA ("every
# whitespace character removed", "spaces collapsed", a tab key) is not a formatting task (bfmr-split-tiebreak's TASK.md said
# "whitespace" three times about tracking numbers, and the bare-word rule skipped the very cleanup the job needed).
_WS_TASK_RE = re.compile(
    r"\bre-?indent|\bindentation\b|\bindent(?:s|ed|ing)?\s+(?:the|this|that|these|those|each|every|all|its|their)\s"
    r"|\bre-?format|\bprettier\b|\b(?:code|source)\s+(?:formatting|style)\b|\bformatting[\s-]+only\b"
    r"|\btrailing\s+(?:white\s*-?\s*space|spaces?|tabs?|blanks?)\b"
    r"|\btabs?\s+(?:to|into|vs\.?|versus|or|and|for|instead\s+of)\s+spaces?\b|\bspaces?\s+(?:to|into|vs\.?|versus|or|and|for|instead\s+of)\s+tabs?\b"
    r"|\bwhite\s*-?\s*space(?:[\s-]+only)?[\s-]+(?:changes?|edits?|diffs?|fix(?:es)?|clean\s*-?up)\b"
    r"|\b(?:fix|clean\s*up|tidy(?:\s+up)?|adjust|change|normali[sz]e|standardi[sz]e)\s+(?:the\s+|its\s+|their\s+)?(?:white\s*-?\s*space|spacing|indent)"
    r"|\b(?:line\s+up|align)\s+(?:the\s+)?(?:columns?|comments?|assignments?|values?|code|lines?|equals|colons?|arguments?|param(?:eter)?s?)\b",
    re.I)
WS_TRY_CAP = 8  # single-line restores tried when restoring them all at once turns the test red


def _ws_squash(s):
    """s without whitespace, except one marker where whitespace separates two word characters (`let x` is not `letx`)."""
    return re.sub(r"\s+", "", re.sub(r"(?<=\w)\s+(?=\w)", "\x00", s))


def _ws_kind(x, y):
    """'lead' (indentation), 'inner' (spacing inside or after the text, or a blank line's whitespace) when line x became line y by
    whitespace alone; None when they are equal, differ in more than whitespace, or differ inside a string literal (that is data)."""
    if x == y or _ws_squash(x) != _ws_squash(y) or _STR_LIT_RE.findall(x) != _STR_LIT_RE.findall(y):
        return None
    if not x.strip():
        return "inner"
    return "lead" if x[:len(x) - len(x.lstrip())] != y[:len(y) - len(y.lstrip())] else "inner"


def ws_units(f, a, b):
    """[(j, baseline line, kind)]: lines b[j] (0-based) of file f that differ from the baseline a only in whitespace, each with the
    baseline line that restores it. The diff is taken on whitespace-squashed lines, so a re-spaced line next to a real edit is still
    found and nothing moves. Indentation ('lead') is skipped in _WS_INDENT_EXT files and on lines a code change reaches through
    blank or re-spaced lines only (a re-indent around new code is layout the fix needs, e.g. lines moved into a new block)."""
    ext = Path(f).suffix.lower()
    if ext not in _WS_CODE_EXT:
        return []
    toks = []  # (class, j, baseline line) in b order; one "code" token per inserted/deleted/replaced run
    sm = difflib.SequenceMatcher(None, [_ws_squash(x) for x in a], [_ws_squash(y) for y in b], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            toks.append(("code", None, None))
            continue
        for d in range(i2 - i1):
            x, y = a[i1 + d], b[j1 + d]
            toks.append((("same" if y.strip() else "blank") if x == y else (_ws_kind(x, y) or "code"), j1 + d, x))
    near = [False] * len(toks)
    for order in (range(len(toks)), range(len(toks) - 1, -1, -1)):
        flag = False
        for t in order:
            c = toks[t][0]
            if c == "code":
                flag = True
            elif c == "same":
                flag = False
            elif flag:
                near[t] = True
    return [(j, x, c) for t, (c, j, x) in enumerate(toks)
            if c == "inner" or (c == "lead" and ext not in _WS_INDENT_EXT and not near[t])]


def ws_cleanup(cwd, spec, allowed, base_texts, base_tests, task_text):
    """Restore the baseline whitespace on the ws_units of every allowed file, keeping a restore only while the acceptance test stays
    green with at least base_tests tests. All at once first; if that turns the test red, one line at a time (WS_TRY_CAP tries).
    Returns None (nothing to restore, spec ws_clean off, or TASK.md is about whitespace), else ((True, detail), test output of the
    final files or None when every restore was undone). Never fails: whitespace the test needs is kept."""
    if spec.get("ws_clean") in (False, "off") or _WS_TASK_RE.search(task_text or ""):
        return None
    files = {}  # f -> (current lines, units)
    for f in allowed:
        p = Path(cwd, f)
        if base_texts.get(f) is None or not p.is_file() or _crlf(p):
            continue
        b = p.read_text(errors="replace").splitlines(True)
        us = ws_units(f, base_texts[f].splitlines(True), b)
        if us:
            files[f] = (b, us)
    if not files:
        return None

    def write(keep):  # keep: {(f, j)} restored
        for f, (b, us) in files.items():
            nb = list(b)
            for j, base_ln, _k in us:
                if (f, j) in keep:
                    nb[j] = base_ln
            Path(cwd, f).write_text("".join(nb))

    def green():
        rc, out, _ = run_cmd(spec["test_cmd"], cwd, spec)
        n = test_count(out)
        return rc == 0 and (n is None or base_tests is None or n >= base_tests), out

    every = [(f, j) for f, (_b, us) in files.items() for j, _bl, _k in us]
    write(set(every))
    ok, out = green()
    kept = set(every) if ok else set()
    if not ok:
        out = None
        for u in every[:WS_TRY_CAP]:
            write(kept | {u})
            ok1, out1 = green()
            if ok1:
                kept.add(u)
                out = out1  # kept only grows on a green run, so this is the output of the final files
        write(kept)
    where = ", ".join(f"{f}:{j + 1}" for f, j in every if (f, j) in kept)
    left = len(every) - len(kept)
    if not kept:
        return (True, f"kept {left} whitespace-only line change(s): restoring the baseline spacing turned the test red"), None
    return (True, f"restored the original whitespace on {len(kept)} line(s) the fix changed only in spacing: {where[:300]}"
            + (f"; kept {left} (the test needs them, or past the {WS_TRY_CAP}-try cap)" if left else "")), out


# ---- untested fix hunks (gate fix_hunks, soft, after the loop) -----------------------------------------------------------
# mini-validate's V5 on the MODEL's fix: revert one code hunk at a time and rerun the acceptance test. A hunk the test (and the type
# check, when the spec has one) stays green without is not proven by anything: an untested branch, dead code, an unrelated edit.
# A cast inside a needed hunk is invisible to that (reverting the hunk drops the code around it too), so cast_check also drops each
# TypeScript `x as T` the fix added, alone, and reruns the type check: a cast the type check does not need is noise (bfmr-split-
# tiebreak 2026-11-03: a redundant `(l as BackfillLocalRow)` cast; the 11-03 replay of that job showed the hunk revert alone missed
# it). Both report under fix_hunks and make the run `PASS needs-review`: never fed back to the model (TASK.md may require code no test
# reaches), never run in a steps chain (a chain keeps the strict verdict).
FIX_HUNK_CAP = 12      # hunks reverted per run
FIX_HUNK_TC_CAP = 4    # type-check runs for hunks the test does not catch
FIX_CAST_CAP = 4       # new casts dropped per run (one type-check run each, plus one on the final files)


_COMMENT_LINE = re.compile(r"\s*(?:#|//|/\*|\*)")  # mini-validate's COMMENT_LINE (fn_scope's rule): blank/comment lines change nothing


def fix_hunks_on(spec):
    v = os.environ.get("MINI_FIX_HUNKS", "on").strip().lower()
    return spec.get("fix_hunks") not in (False, "off") and v not in ("off", "0", "no", "false")


def _code_hunks(a, b, da=frozenset(), db=frozenset()):
    """Non-equal opcodes of a -> b that touch a non-blank, non-comment line (mini-validate's comment_only rule, inverted). da/db:
    1-based docstring lines of a and b (python, _py_doc_lines), which count as comment lines."""
    def code(lines, i0, doc):
        return any(ln.strip() and not _COMMENT_LINE.match(ln) and i0 + k + 1 not in doc for k, ln in enumerate(lines))
    return [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if op[0] != "equal" and (code(a[op[1]:op[2]], op[1], da) or code(b[op[3]:op[4]], op[3], db))]


def _crlf(p):
    """A CRLF file: read_text/write_text would rewrite every line ending, so the whitespace and hunk passes leave it alone."""
    try:
        return b"\r" in Path(p).read_bytes()
    except OSError:
        return True


def _revert_op(a, b, op):
    """b with the one hunk `op` (an opcode of a -> b) put back to a."""
    _t, i1, i2, j1, j2 = op
    return b[:j1] + a[i1:i2] + b[j2:]


def fix_hunk_check(cwd, spec, allowed, base_texts, base_tc, budget):
    """(ok, detail) after reverting each code hunk of the final fix alone (FIX_HUNK_CAP, within budget seconds); None when the fix
    has fewer than two code hunks (red_without_fix already proves a single one). The files are restored afterwards."""
    t_end = _now() + max(0, budget)
    todo = []
    for f in allowed:
        p = Path(cwd, f)
        if base_texts.get(f) is None or not p.is_file() or _crlf(p):
            continue  # a new file is one hunk; deleting it is not a revert the code could survive
        a, b = base_texts[f].splitlines(True), p.read_text(errors="replace").splitlines(True)
        da, db = (_py_doc_lines("".join(a)), _py_doc_lines("".join(b))) if f.endswith(".py") else (frozenset(), frozenset())
        todo += [(f, a, b, op) for op in _code_hunks(a, b, da, db)]
    if len(todo) < 2:
        return None
    uncaught, checked, tc_runs, tc_saved = [], 0, 0, 0
    try:
        for f, a, b, op in todo[:FIX_HUNK_CAP]:
            if _now() >= t_end:
                break
            Path(cwd, f).write_text("".join(_revert_op(a, b, op)))
            rc, out, _ = run_cmd(spec["test_cmd"], cwd, spec)
            checked += 1
            per = parse_test_results(out)
            if rc == 0 or (per and not any(x[1] == "FAIL" for x in per)):
                if spec.get("typecheck_cmd") and tc_runs < FIX_HUNK_TC_CAP and _now() < t_end:
                    tc_runs += 1
                    rc3, out3, _ = run_cmd(spec["typecheck_cmd"], cwd, spec, timeout=int(spec.get("cmd_timeout", 300)) * 2)
                    if fail_lines(rc3, out3) - (base_tc or set()):
                        tc_saved += 1
                        Path(cwd, f).write_text("".join(b))
                        continue
                _t, i1, i2, j1, j2 = op
                snip = next((ln.strip() for ln in (b[j1:j2] or a[i1:i2]) if ln.strip()), "")[:60]
                uncaught.append(f"{f}:{j1 + 1}-{max(j1 + 1, j2)} `{snip}`")
            Path(cwd, f).write_text("".join(b))
    finally:
        for f in {x[0] for x in todo}:
            Path(cwd, f).write_text("".join(next(x[2] for x in todo if x[0] == f)))
    if not checked:
        return (True, f"skipped: no time left to revert {len(todo)} code hunk(s)")
    note = (f" ({tc_saved} more caught by the type check)" if tc_saved else "") + \
           (f"; {len(todo) - checked} not checked (cap {FIX_HUNK_CAP} or time)" if checked < len(todo) else "")
    if uncaught:
        return (False, f"{len(uncaught)} of {checked} code hunk(s) the test stays green without (revert one alone): "
                       f"{'; '.join(uncaught)[:500]} -- check each is needed and right{note}")
    return (True, f"each of {checked} code hunk(s) is needed by the test{note}")


_TS_EXT = {".ts", ".tsx", ".mts", ".cts"}
_CAST_RE = re.compile(r"\s+as\s+(?!(?:const|unknown|any)\b)[A-Za-z_$][\w$.]*(?:<[^<>()\n]*>)?(?:\[\])*")


def _new_casts(f, a, b):
    """[(j, start, end, cast)]: TypeScript `x as T` casts on lines b[j] (0-based) inside a code hunk of a -> b, outside string
    literals and comments. Skipped: `as const`/`as unknown`/`as any` and the `as T` after one (dropping half a double cast changes
    its meaning), import/export aliases, and a cast of an empty `[]`/`{}` literal (it names a type the literal cannot)."""
    if Path(f).suffix.lower() not in _TS_EXT:
        return []
    out, blank = [], lambda m: " " * len(m.group(0))  # blanked to the same length, so the spans index the real line
    for op in _code_hunks(a, b):
        for j in range(op[3], op[4]):
            ln = b[j]
            if _COMMENT_LINE.match(ln) or re.match(r"\s*(?:import|export)\s+(?:type\s+)?[{*]", ln):
                continue
            code = re.sub(r"//.*", blank, re.sub(r"/\*.*?\*/", blank, _STR_LIT_RE.sub(blank, ln)))
            for m in _CAST_RE.finditer(code):
                if not re.search(r"(?:\[\s*\]|\{\s*\})\s*$|\bas\s+(?:unknown|any)\s*$", code[:m.start()]):
                    out.append((j, m.start(), m.end(), m.group(0).strip()))
    return out


def cast_check(cwd, spec, allowed, base_texts, budget):
    """(ok, detail) after dropping each TypeScript cast the fix added, alone, and rerunning the type check (FIX_CAST_CAP casts within
    budget seconds, compared with the type check of the final files); None without a typecheck_cmd or a new cast. A cast never
    changes what runs (types are erased), so the type check is the only thing that can need one. The files are restored afterwards."""
    tc = spec.get("typecheck_cmd")
    if not tc:
        return None
    todo = []
    for f in allowed:
        p = Path(cwd, f)
        if base_texts.get(f) is None or not p.is_file() or _crlf(p):
            continue
        b = p.read_text(errors="replace").splitlines(True)
        todo += [(f, b, c) for c in _new_casts(f, base_texts[f].splitlines(True), b)]
    if not todo:
        return None
    t_end, tmo = _now() + max(0, budget), int(spec.get("cmd_timeout", 300)) * 2
    if _now() >= t_end:
        return (True, f"skipped: no time left to check {len(todo)} new cast(s)")
    rc0, out0, _ = run_cmd(tc, cwd, spec, timeout=tmo)
    ref = fail_lines(rc0, out0)
    if ref == {f"rc={rc0}"}:  # red with no error line: a dropped cast could not be told apart from the final files
        return (True, f"skipped: the type check of the final files failed with no error line to compare ({len(todo)} new cast(s))")
    redundant, checked = [], 0
    try:
        for f, b, (j, s, e, cast) in todo[:FIX_CAST_CAP]:
            if _now() >= t_end:
                break
            Path(cwd, f).write_text("".join(b[:j] + [b[j][:s] + b[j][e:]] + b[j + 1:]))
            rc, out, _ = run_cmd(tc, cwd, spec, timeout=tmo)
            checked += 1
            if not fail_lines(rc, out) - ref:
                redundant.append(f"{f}:{j + 1} `{cast}`")
            Path(cwd, f).write_text("".join(b))
    finally:
        for f in {x[0] for x in todo}:
            Path(cwd, f).write_text("".join(next(x[1] for x in todo if x[0] == f)))
    if not checked:
        return (True, f"skipped: no time left to check {len(todo)} new cast(s)")
    note = f"; {len(todo) - checked} not checked (cap {FIX_CAST_CAP} or time)" if checked < len(todo) else ""
    if redundant:
        return (False, f"{len(redundant)} of {checked} new cast(s) the type check passes without (drop one alone): "
                       f"{'; '.join(redundant)[:400]} -- drop each unless it is needed{note}")
    return (True, f"each of {checked} new cast(s) is needed by the type check{note}")


def merge_soft(*gs):
    """One soft gate tuple from several checks (None = did not run): ok only when every one that ran is ok, failing details first."""
    gs = [g for g in gs if g is not None]
    if not gs:
        return None
    return (all(g[0] for g in gs), "; ".join(g[1] for g in sorted(gs, key=lambda g: g[0])))


def suite_gates(g, cwd, spec, base_suite, base_tc):
    """The suite and typecheck gates: no new failure lines against the baseline sets. A slow suite (SUITE_SLOW_S or more at
    baseline) is skipped when the attempt already failed on its own test (red, or the frozen test edited): the attempt is rejected
    either way, and the minutes go to the next attempt instead; the row says so. A narrowed suite (spec _suite_full) says it ran
    the tests nearest the change."""
    for key, base in (("suite_cmd", base_suite), ("typecheck_cmd", base_tc)):
        if not spec.get(key):
            continue
        name = key.replace("_cmd", "")
        secs = spec.get("_suite_secs")
        if key == "suite_cmd" and secs is not None and secs >= SUITE_SLOW_S and not (
                g.get("test_green", (True,))[0] and g.get("test_unchanged", (True,))[0]):
            g[name] = (True, f"not run: the test is not green, and the suite took {secs:.0f}s at baseline (it runs on a green attempt)")
            continue
        rc3, out3, _ = run_cmd(spec[key], cwd, spec, timeout=int(spec.get("cmd_timeout", 300)) * 2)
        new = sorted(fail_lines(rc3, out3) - base)
        g[name] = (not new, f"rc={rc3} new failures: {new[:8]}" if new else f"rc={rc3}, no new failures"
                   + (f" (WEAK: baseline already had {len(base)} red line(s))" if base else ""))
        if key == "suite_cmd" and spec.get("_suite_full"):
            g[name] = (g[name][0], g[name][1] + " (narrowed: the whole suite timed out at baseline; this runs the tests nearest the change)")


def fix_gate(cwd, spec, allowed, base_texts, test_sha, base_tests, base_suite, base_tc, log, pre=(), ws_clean=True, traj=None):
    """Phase B gates. Returns (gate dict name->(ok, detail), test output). ws_clean: restore whitespace-only line changes first
    (ws_cleanup; it edits the work files), off for model-free reference runs."""
    tf, g = spec["test_file"], {}
    task_text = Path(cwd, "TASK.md").read_text(errors="replace") if Path(cwd, "TASK.md").is_file() else ""
    p = Path(cwd) / tf
    g["test_unchanged"] = (p.exists() and sha(p) == test_sha, "" if p.exists() else "test file deleted")
    probe = [t for t in allowed if lang_of(t) and lang_of(t) == lang_of(tf)]
    rc, out, reach = run_cmd(spec["test_cmd"], cwd, spec, probe_targets=probe, full_reach=True)
    n = test_count(out)
    g["test_green"] = (rc == 0 and (n is None or base_tests is None or n >= base_tests), f"rc={rc} tests={n} (baseline {base_tests})")
    if ws_clean and g["test_green"][0] and g["test_unchanged"][0]:  # line numbers do not move, so `reach` stays valid
        ws = ws_cleanup(cwd, spec, allowed, base_texts, base_tests, task_text)
        if ws is not None:
            g["ws_only"] = ws[0]
            out = ws[1] if ws[1] is not None else out
    if reach is not None:  # the test must execute the lines the fix changed (else the green is not about the diff)
        thr = float(spec.get("min_diff_cov", 0.5))
        c, x, miss, unmapped = diff_reach(cwd, reach, probe, base_texts)
        where = ", ".join(f"{f}:{ln}" for f, ln in miss[:10])
        if x == 0:
            g["diff_reach"] = (True, "no executable changed lines" + (f" (unmapped: {unmapped})" if unmapped else ""))
        else:
            g["diff_reach"] = (c / x >= thr, f"test executes {c}/{x} changed lines (need >= {int(thr * 100)}%)"
                               + (f"; not run: {where}" if miss else "") + (f"; unmapped: {unmapped}" if unmapped else ""))
    if spec.get("target_fn") and spec.get("fn_scope", "fail") != "off":  # did the fix stay inside target_fn (+ spec.new_names)? fails by default
        g["fn_scope"] = fn_scope_check(cwd, spec, allowed, base_texts)
    g["no_hacks"] = no_hacks_check(cwd, spec, allowed, base_texts)
    lint = lint_check(cwd, spec, allowed, base_texts)
    if lint is not None:
        g["lint"] = lint
    prev = swap_texts(cwd, base_texts)
    try:
        rc2, out2, _ = run_cmd(spec["test_cmd"], cwd, spec)
    finally:
        swap_texts(cwd, prev)
    g["red_without_fix"] = (rc2 != 0, f"rc={rc2} with the allowed files reverted to baseline")
    want = spec.get("_base_red")
    if rc2 != 0 and want is not None:  # G2: the revert must fail the SAME tests phase A froze, each for an allowed reason
        _remember_task(spec, cwd, allowed)
        per2 = parse_test_results(out2)
        red2 = sorted(n for n, st, _t, _m in per2 if st == "FAIL")
        wrong = [f"{n}: {red_reason_problem(t, m, spec)}" for n, st, t, m in per2 if st == "FAIL" and red_reason_problem(t, m, spec)]
        if per2 and (red2 != want or wrong):
            g["red_without_fix"] = (False, f"rc={rc2} reverted run fails {red2} (phase A froze {want})" + (f"; wrong-reason: {wrong[:4]}" if wrong else ""))
    suite_gates(g, cwd, spec, base_suite, base_tc)
    prior = spec.get("_prior_tests") or []  # chain: earlier steps' tests stay in the tree and must stay green
    if prior:
        bad_prior = []
        for pc in prior:
            rcp, outp, _ = run_cmd(pc, cwd, spec)
            if rcp != 0:
                bad_prior.append(f"`{pc}` rc={rcp}")
        g["prior_tests"] = (not bad_prior, f"red: {bad_prior}" if bad_prior else f"{len(prior)} earlier test command(s) green")
    lits = parse_must_contain(task_text)
    miss, weak = literal_check(cwd, lits, allowed, base_texts)
    g["literals"] = (not miss, f"missing {miss}" if miss else f"{len(lits)} present" + (f"; already at baseline (no evidence): {weak}" if weak else ""))
    keeps = parse_must_keep(task_text)
    if keeps:
        lost = keep_check(cwd, keeps, allowed, base_texts)
        g["must_keep"] = (not lost, f"removed or missing {lost}" if lost else f"{len(keeps)} kept")
    stale = stale_gate(cwd, allowed, base_texts, task_text)
    if stale is not None:
        g["must_not_contain"] = stale
    dropped = set(spec.get("drop") or [])  # removed by the runner itself (e.g. a tracked hand oracle)
    tp = Path(os.path.expanduser(spec.get("task", "TASK.md")))
    if tp.is_absolute() and tp.is_file() and Path(cwd, "TASK.md").is_file() and Path(cwd, "TASK.md").read_bytes() == tp.read_bytes():
        dropped.add("TASK.md")  # prepare() copied the job's own TASK.md over the baseline's; unchanged since, so not the agent's
    changed = [ln for ln in step_changed(cwd, spec)
               if not (ln in dropped and (ln == "TASK.md" or not Path(cwd, ln).exists()))]
    edit_ok = edit_scope(spec, allowed)
    out_scope = scope_violations([c for c in changed if c != tf], edit_ok)
    extra = untracked_violations(git(cwd, "status", "--short", "--untracked-files=all").splitlines(), extra_ok=(tf, *edit_ok, *pre))
    tamper = guard_violations(cwd, spec, tf, edit_ok)
    g["scope"] = (not out_scope and not extra and not tamper, f"changed={changed} out_of_scope={out_scope} new_files={extra}"
                  + (f" earlier_steps_files_edited={tamper}" if tamper else ""))
    if spec.get("update_tests"):
        g["update_tests"] = update_tests_check(cwd, spec)
        flip = update_flip_check(cwd, spec) if spec.get("update_tests_strict") else None
        if flip is not None:
            g["update_flip"] = flip
    miss = said_missing(log, traj)
    g["no_missing"] = (not miss, "agent reported MISSING:" if miss else "")
    if spec.get("verify"):  # optional stricter mode: a hand oracle on top of qwen's own test
        vrc, _ = _bash(f"bash {shlex.quote(spec['verify'])}", cwd, None, remaining(int(spec.get("cmd_timeout", 300)) * 2))
        g["verify"] = (vrc == 0, f"rc={vrc}")
    return g, out


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def edit_scope(spec, allowed):
    """Files the model may change in Phase B: allowed plus spec `update_tests` (existing tests that assert the old behaviour;
    editable but never `allowed`, so they stay out of the coverage/single-shot/literal logic)."""
    return [*allowed, *as_list(spec.get("update_tests"))]


CASE_RE = re.compile(r"^[ \t]*(?:(?:test|it)[ \t]*\(|(?:async[ \t]+)?def[ \t]+test_\w*)", re.M)


def case_count(text):
    """Test cases in a test file: test( / it( calls and def test_ functions (test.skip( / it.only( are not counted)."""
    return len(CASE_RE.findall(text or ""))


def update_tests_check(cwd, spec):
    """Anti-weakening gate for spec `update_tests` (existing tests the fix may edit): every file keeps at least its baseline number of
    test cases (spec["_upd_base"], snapshotted before the model ran). Changing expectations is the point; deleting or skipping cases is not."""
    base, bad, n = spec.get("_upd_base") or {}, [], 0
    for f in as_list(spec.get("update_tests")):
        p = Path(cwd, f)
        if not p.is_file():
            bad.append(f"{f}: deleted")
            continue
        if base.get(f) is None:
            continue
        n += 1
        was, now = case_count(base[f]), case_count(p.read_text(errors="replace"))
        if now < was:
            bad.append(f"{f}: {now} test case(s), baseline {was}")
    return (not bad, f"fewer test cases than baseline: {bad}" if bad else f"{n} updated test file(s) keep every test case")


UPD_EDIT_MAX = 3  # existing test functions one update_tests_strict fix may edit (gate update_flip)
_PYTEST_ROW_RE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+([^\s\[]+)", re.M)  # pytest -rA summary rows


def _assert_count(node):
    """Assertions in a test function: assert statements, assert*/assert_* calls (unittest, mock, numpy), pytest.raises/warns."""
    n = 0
    for x in ast.walk(node):
        if isinstance(x, ast.Assert):
            n += 1
        elif isinstance(x, ast.Call):
            nm = x.func.attr if isinstance(x.func, ast.Attribute) else getattr(x.func, "id", "")
            n += nm.startswith("assert") or nm in ("raises", "warns", "deprecated_call")
    return n


def _py_tests(text):
    """A python test file -> ({pytest id: (ast dump, assertion count, source)}, [ast dumps of everything else]), or None when it
    does not parse. Ids: `test_x` (module level) and `TestK::test_x` (methods); a class's other statements are in its row."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines, tests, rest = text.splitlines(True), {}, []
    is_t = lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")

    def put(tid, n):
        start = min([n.lineno, *(d.lineno for d in n.decorator_list)])
        tests[tid] = (ast.dump(n), _assert_count(n), "".join(lines[start - 1:n.end_lineno]))
    for n in tree.body:
        if is_t(n):
            put(n.name, n)
        elif isinstance(n, ast.ClassDef):
            body = []
            for m in n.body:
                if is_t(m):
                    put(f"{n.name}::{m.name}", m)
                else:
                    body.append(ast.dump(m))
            rest.append(repr(("class", n.name, [ast.dump(x) for x in [*n.bases, *n.keywords, *n.decorator_list]], body)))
        else:
            rest.append(ast.dump(n))
    return tests, rest


def _pytest_rows(out, f, tid):
    """The statuses pytest -rA printed for test id tid of file f (every parametrized case), as a set."""
    got = set()
    for st, nid in _PYTEST_ROW_RE.findall(out or ""):
        path, _, rest = nid.partition("::")
        if rest == tid and (path == f or f.endswith("/" + path) or path.endswith("/" + f)):
            got.add(st)
    return got


def update_flip_check(cwd, spec):
    """Gate update_flip (spec update_tests_strict: auto's update_tests): an existing test the fix updates must be one the fix's
    behaviour change makes wrong. In each changed update_tests file only test functions change (imports may be added; a new test
    is fine, a removed or renamed one is not), at most UPD_EDIT_MAX of them, and each edited one names a target function, keeps
    at least its baseline assertion count, FAILS on the fix with its baseline text (the edit was needed) and PASSES with the edit
    (pytest by test id, the suite's python). It never asks the baseline test to be red: it passed on the old code. None when no
    update_tests file changed."""
    base, changed = spec.get("_upd_base") or {}, {}
    for f in as_list(spec.get("update_tests")):
        p = Path(cwd, f)
        if base.get(f) is not None and p.is_file():
            now = p.read_text(errors="replace")
            if now != base[f]:
                changed[f] = now
    if not changed:
        return None
    names = [x for x in [*as_list(spec.get("target_fn")), *as_list(spec.get("new_names"))] if x]
    bad, edited = [], []
    imp = ("Import(", "ImportFrom(")
    for f, now in changed.items():
        b0, n0 = _py_tests(base[f]), _py_tests(now)
        if b0 is None or n0 is None:
            bad.append(f"{f} does not parse")
            continue
        (bt, br), (nt, nr) = b0, n0
        if [d for d in nr if not (d.startswith(imp) and d not in br)] != br:
            bad.append(f"{f}: code other than test functions changed (only test functions may change, and imports be added)")
        gone = [i for i in bt if i not in nt]
        if gone:
            bad.append(f"{f}: test(s) removed or renamed: {', '.join(gone[:5])}")
        for i in bt:
            if i not in nt or nt[i][0] == bt[i][0]:
                continue
            edited.append((f, i))
            if nt[i][1] < bt[i][1]:
                bad.append(f"{f}::{i} has {nt[i][1]} assertion(s), {bt[i][1]} at baseline")
            if names and not any(re.search(rf"\b{re.escape(x)}\b", nt[i][2]) for x in names):
                bad.append(f"{f}::{i} names none of {', '.join(names)}: only a test of the changed code may be updated")
    if len(edited) > UPD_EDIT_MAX:
        bad.append(f"{len(edited)} existing test functions edited (at most {UPD_EDIT_MAX})")
    if bad or not edited:
        return (not bad, "; ".join(bad)[:700] if bad else f"{', '.join(changed)}: tests added, no existing test edited")
    m = re.match(r"\s*(\S+) -m pytest\b", str(spec.get("_suite_full") or spec.get("suite_cmd") or ""))
    if not m:
        return (False, "an existing test was edited, but suite_cmd is not `<python> -m pytest ...`, so the edit cannot be checked")
    cmd = f"{m.group(1)} -m pytest -q -p no:cacheprovider -rA " + " ".join(shlex.quote(f"{f}::{i}") for f, i in edited)
    tmo = int(spec.get("cmd_timeout", 300)) * 2
    prev = swap_texts(cwd, {f: base[f] for f in changed})
    try:
        _rc0, out0, _ = run_cmd(cmd, cwd, spec, timeout=tmo)
    finally:
        swap_texts(cwd, prev)
    _rc1, out1, _ = run_cmd(cmd, cwd, spec, timeout=tmo)
    show = lambda s: "/".join(sorted(s)) or "not run"
    for f, i in edited:
        s0, s1 = _pytest_rows(out0, f, i), _pytest_rows(out1, f, i)
        if "FAILED" not in s0:
            bad.append(f"{f}::{i} without the edit: {show(s0)} on the fix (only a test the fix makes fail may be updated)")
        if s1 != {"PASSED"}:
            bad.append(f"{f}::{i} with the edit: {show(s1)}")
    ids = ", ".join(f"{f}::{i}" for f, i in edited)
    return (not bad, "; ".join(bad)[:700] if bad else f"{len(edited)} edited test(s) fail on the fix without the edit and pass with it: {ids}"[:700])


def guard_hashes(cwd, tf, allowed):
    """{path: sha256} of this step's untracked test file and untracked (new) allowed files: what later chain steps must leave alone."""
    untracked = {ln[3:].strip() for ln in git(cwd, "status", "--short", "--untracked-files=all").splitlines() if ln.startswith("??")}
    return {f: sha(Path(cwd, f)) for f in (tf, *allowed) if f in untracked and Path(cwd, f).is_file()}


def guard_violations(cwd, spec, tf, allowed):
    """Earlier steps' guarded files whose content changed (or vanished) in this step; files this step may edit (its allowed list / test file) are exempt."""
    own = {tf, *allowed}
    return [f for f, h in (spec.get("_guard") or {}).items()
            if f not in own and (not Path(cwd, f).is_file() or sha(Path(cwd, f)) != h)]


SKELETON_MARK = "TODO(mini-skeleton)"
FROZEN_MARK = "TODO(frozen-test)"  # mini-spec's frozen-test template (Opus/a human fills it): test_static rejects it while any is left


def py_main_block():
    """The canonical runner block of a python test run as a plain script (the design doc's frozen-test template): TEST_ORDER=reverse
    (V7), one `RESULT <name> PASS|FAIL <Type>: <msg>` line per test (G1 reads each red's type there, never from pytest's width-cut
    summary), asyncio.run() for an async test (stage 26: a coroutine that is never awaited 'passed' without running) and exit 1 on
    any failure. It imports what it uses under private names, so the file needs no imports for it; the test gate writes it into a
    test whose own runner ran nothing or hid its failures (_py_runner_fix)."""
    return ['if __name__ == "__main__":',
            "    import asyncio as _asyncio",
            "    import inspect as _inspect",
            "    import os as _os",
            "    import sys as _sys",
            '    _order = TESTS[::-1] if _os.environ.get("TEST_ORDER") == "reverse" else TESTS',
            "    _failed = 0",
            "    for _t in _order:",
            "        try:",
            "            _r = _t()",
            "            if _inspect.iscoroutine(_r):  # an async test",
            "                _asyncio.run(_r)",
            '            print(f"RESULT {_t.__name__} PASS", flush=True)',
            "        except Exception as _e:",
            "            _failed += 1",
            '            print(f"RESULT {_t.__name__} FAIL {type(_e).__name__}: {_e}", flush=True)',
            '    print(f"{len(_order)} tests, {_failed} failed")',
            "    _sys.exit(1 if _failed else 0)"]


def py_harness(names):
    """Lines that end a python test run as a plain script: the explicit TESTS list (V2 refuses globals()/dir() discovery), the GUARDS
    list and the canonical runner block (py_main_block)."""
    return ["", "", f"TESTS = [{', '.join(names)}]  # explicit order: list every test function (never globals()/dir())",
            "GUARDS = []  # the tests in TESTS that check behaviour the fix must keep: they PASS now and after the fix; every other test FAILS now",
            "", "", *py_main_block()]


def _py_import_root(cwd, f):
    """(root, dotted) for importing repo file f the way the repo's own code does: climb from its directory while that directory is
    a regular package (has an __init__.py); root is the first directory that is not one ("." for the repo root) and the module
    is the dotted path below it: conan/internal/model/cpp_info.py -> (".", "conan.internal.model.cpp_info"), src-layout
    src/pkg/m.py -> ("src", "pkg.m"), a plain scripts/tool.py -> ("scripts", "tool")."""
    p = Path(f)
    parts = [] if p.stem == "__init__" else [p.stem]
    d = p.parent
    while str(d) not in ("", ".") and d.name.isidentifier() and Path(cwd, d, "__init__.py").is_file():
        parts.insert(0, d.name)
        d = d.parent
    return (str(d) if str(d) not in ("", ".") else "."), ".".join(parts)


def _py_loads(tf, files, cwd=None):
    """(sys.path lines, lazy loader lines, module names) for loading python `files` from test file tf: a file under the test's
    directory imports by dotted path; one outside it imports by its package path (_py_import_root) with that import root on
    sys.path, never its own directory. Loaders are lazy: importing at module top level would crash collection when the env lacks
    the module's deps. sys.path lines need sys + Path.
    2026-11-05 (real-issue bench conan1 r2): an outside file used to put its own directory on sys.path and import by stem. For
    conan/internal/model/cpp_info.py that inserted ../conan/internal/model, whose profile.py then shadowed the stdlib `profile`
    for every test file pytest collected after the frozen test: test/performance/test_large_graph.py stopped importing and phase
    B's suite gate failed a correct fix on a test it could no longer change."""
    tdir = str(Path(tf).parent) or "."
    paths, mods = [], []
    for f in files:
        rel = os.path.relpath(f, tdir).replace(os.sep, "/")
        root, dotted = _py_import_root(cwd, f) if cwd is not None else (None, "")
        if rel.startswith("..") and dotted:
            paths.append(os.path.relpath(root, tdir).replace(os.sep, "/"))
            mods.append(dotted)
        elif rel.startswith(".."):
            paths.append(os.path.dirname(rel))
            mods.append(Path(f).stem)
        else:
            mods.append(re.sub(r"\.__init__$", "", rel[:-3].replace("/", ".")))
    syspath = [f"sys.path.insert(0, str(Path(__file__).resolve().parent / {p!r}))" for p in dict.fromkeys(paths)]
    loaders = []
    for m in mods:
        if all(p.isidentifier() for p in m.split(".")):
            loaders += ["", f"def {_py_loader_name(m)}():", f"    import {m}", f"    return {m}"]
        else:  # bench-dupes5: arr-webhook.py gave `import arr-webhook`, a SyntaxError the model "fixed" to `import arr_webhook`
            # (no such module), and phase A failed twice; the import system finds the file by its own name, as the repo's
            # tests do (aw = __import__('arr-webhook'))
            loaders += ["", f"def {_py_loader_name(m)}():", "    import importlib", f"    return importlib.import_module({m!r})"]
    return syspath, loaders, mods


def _py_loader_name(m):
    """The skeleton's lazy loader for module m, a valid identifier: _load_ + m with . and - (any non-word character) as _."""
    return "_load_" + re.sub(r"\W", "_", m)


def py_direct_run(spec):
    """True when the python test_cmd runs the test file as a plain script (no pytest/unittest): the file needs its own runner block."""
    return not re.search(r"\b(?:pytest|unittest|nose2?|tox)\b", spec.get("test_cmd") or "")


_FROZEN_RULES = [
    "Frozen test (mini-spec template). Replace every placeholder line that carries the frozen-test TODO marker, then run",
    "`mini-spec --freeze <run dir>`: it refuses while a marker is left and commits the file to baseline_from.",
    "Rules (mini-validate V2-V7):",
    "- Each test calls the real code with concrete inputs from TASK.md and asserts the exact expected value.",
    "- Every test is red on the current code with an AssertionError, or with a red that spec red_ok names (a missing key:",
    '  {"type": "KeyError", "match": "sandbox"}), and green with the reference fix (ref/ in the run dir).',
    "- A test that must already pass at baseline (it guards behaviour the fix keeps) is listed in spec baseline_green.",
    "- Fake only what the code calls out to (network, clock, env, files under a temp dir), never the code under test.",
    "- 2 to 5 tests, a few seconds each: no network, no sleeps.",
]


def frozen_template(cwd, spec, allowed):
    """mini-spec's starting point for a frozen test that Opus or a human writes (design doc template): the skeleton's module
    loads, min_tests placeholder tests and (python) the RESULT harness, so the file already has the shape mini-validate checks.
    Each placeholder is a real assertion on a FROZEN_MARK string, and test_static rejects the file while any marker is left.
    None for an unknown language."""
    tf = spec["test_file"]
    lang, tdir = lang_of(tf), (str(Path(tf).parent) or ".")
    fns = as_list(spec.get("target_fn"))
    n = max(2, int(spec.get("min_tests", 2)))
    mine = [f for f in allowed if lang_of(f) == lang]
    if lang == "py":
        syspath, loaders, mods = _py_loads(tf, mine, cwd)
        call = f"{_py_loader_name(mods[0])}()." if mods else ""
        call += f"{fns[0] if fns else '<function>'}(...)"
        how = (["- Plain functions, run by the RESULT block at the bottom (test_cmd runs this file as a script): no pytest fixtures",
                "  (tmp_path, monkeypatch, capsys). Files the code writes go under TMP. Keep TESTS in step with the test functions."]
               if py_direct_run(spec) else
               ["- pytest runs this file (the RESULT block also lets it run as a plain script). Files the code writes go under TMP."])
        out = ['"""' + _FROZEN_RULES[0], *_FROZEN_RULES[1:], *how, '"""', "import os", "import sys", "import tempfile"]
        out += (["from pathlib import Path", "", *syspath] if syspath else [])
        out += ["", 'TMP = tempfile.mkdtemp(prefix="frozen-test-")  # files the code writes go here; set env vars before the first load']
        out += loaders
        names = [f"test_case_{i}" for i in range(1, n + 1)]
        for nm in names:
            out += ["", "", f"def {nm}():",
                    f"    # {FROZEN_MARK}: rename this test after the behaviour it pins (and rename it in TESTS)",
                    f"    got = None  # {FROZEN_MARK}: got = {call}",
                    f'    assert got == "{FROZEN_MARK}: the exact expected value", f"got {{got!r}}"']
        return "\n".join(out + py_harness(names)) + "\n"
    if lang == "node":
        esm = tf.endswith((".mjs", ".mts")) or (not tf.endswith((".cjs", ".cts")) and _pkg_is_esm(cwd, tf))
        out = [f"// {ln}" for ln in _FROZEN_RULES]
        out += (["import { test } from 'node:test';", "import assert from 'assert';"] if esm
                else ["const { test } = require('node:test');", "const assert = require('assert');"])
        for i, f in enumerate(mine):
            rel = os.path.relpath(f, tdir).replace(os.sep, "/")
            rel = rel if rel.startswith(".") else "./" + rel
            out.append(f"const load{i} = () => import('{rel}');" if esm else f"const load{i} = () => require('{rel}');")
        call = (f"(await load0()).{fns[0] if fns else '<function>'}(...)" if esm else f"load0().{fns[0] if fns else '<function>'}(...)") \
            if mine else f"{fns[0] if fns else '<function>'}(...)"
        for i in range(1, n + 1):
            out += ["", f"test('case {i}', {'async ' if esm else ''}() => {{",
                    f"  // {FROZEN_MARK}: rename this case after the behaviour it pins",
                    f"  const got = undefined;  // {FROZEN_MARK}: const got = {call};",
                    f"  assert.strictEqual(got, '{FROZEN_MARK}: the exact expected value');", "});"]
        return "\n".join(out) + "\n"
    return None


def _pkg_is_esm(cwd, tf):
    """ESM when the nearest package.json says "type": "module"; otherwise when the code around tf already uses ESM syntax, which
    node runs in a .js/.ts file without that key (resell-tracker: no "type", every test `import`s node:test; a `require` template
    there is a global script to tsc, so the frozen test itself failed the typecheck gate)."""
    root = Path(cwd).resolve()
    d = (root / tf).parent.resolve()
    for p in [d, *d.parents]:
        if (p / "package.json").is_file():
            try:
                if json.loads((p / "package.json").read_text()).get("type") == "module":
                    return True
            except ValueError:
                pass
            break
        if p == root:
            break
    return _code_uses_esm(root, d, (root / tf).resolve())


_ESM_SYNTAX = re.compile(r"^\s*(?:import\s[^\n]*\bfrom\s*['\"]|import\s*['\"]|export\s)", re.M)
_CJS_SYNTAX = re.compile(r"\brequire\(\s*['\"]|\bmodule\.exports\b|^\s*exports\.\w+\s*=", re.M)
_JS_EXT = (".js", ".ts", ".jsx", ".tsx")
_JS_SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "coverage", ".venv"}


def _code_uses_esm(root, d, skip, cap=60):
    """Module syntax of the JS/TS files next to the file (its own directory decides when it has any), else of the first `cap` in the
    repo: ESM when more files use only import/export than only require/module.exports. A file using both counts for neither."""
    def tally(files):
        esm = cjs = 0
        for f in files[:cap]:
            try:
                t = f.read_text(errors="replace")[:20000]
            except OSError:
                continue
            e, c = bool(_ESM_SYNTAX.search(t)), bool(_CJS_SYNTAX.search(t))
            esm, cjs = esm + (e and not c), cjs + (c and not e)
        return esm, cjs
    near = [f for f in (sorted(d.iterdir()) if d.is_dir() else []) if f.suffix in _JS_EXT and f.is_file() and f.resolve() != skip]
    esm, cjs = tally(near)
    if not (esm or cjs):
        far = []
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(x for x in dns if x not in _JS_SKIP_DIRS)
            far += [Path(dp, f) for f in sorted(fns) if f.endswith(_JS_EXT) and Path(dp, f).resolve() != skip]
            if len(far) >= cap:
                break
        esm, cjs = tally(far)
    return esm > cjs


def skeleton(cwd, spec, allowed):
    """Phase A starting point for a test_file that does not exist: correct imports of every allowed file of the
    test's language (path relative to the test file, real extension) plus placeholder tests. It has no assertions and
    carries SKELETON_MARK, so test_gate rejects it until the model has filled it in. None for an unknown language. When
    TASK.md's python example goes through the package's public API (example_entry_points), the first python placeholder asks
    for a test that runs the example that way (test_gate's user_view_gap checks for one)."""
    tf = spec["test_file"]
    lang, tdir = lang_of(tf), (str(Path(tf).parent) or ".")
    fns = spec.get("target_fn") or []
    fns = [fns] if isinstance(fns, str) else list(fns)
    n = max(2, int(spec.get("min_tests", 2)))
    mine = [f for f in allowed if lang_of(f) == lang]
    names = ", ".join(f"`{f}`" for f in fns) or "the code under test"
    head = "Skeleton generated by mini-swe-runner: fill it in (keep the imports), do not create another test file."
    if lang == "py":
        syspath, loaders, mods = _py_loads(tf, mine, cwd)
        direct = py_direct_run(spec)  # run as a plain script: the skeleton carries the RESULT harness (TESTS list + __main__)
        out = [f"# {head}"] + (["import os"] if direct else []) + (["import sys"] if syspath or direct else [])
        out += (["from pathlib import Path", *syspath] if syspath else []) + loaders + [""]
        try:  # the public API TASK.md's example uses: the first placeholder runs the example through it (user_view_gap)
            tm = Path(cwd, "TASK.md")
            uv = example_entry_points(tm.read_text(errors="replace"), cwd, allowed) if tm.is_file() else []
        except Exception:
            uv = []
        pub = "; ".join(f"`{imp}` (import it inside the test)" + (", then " + ", ".join(f"`{c}`" for c in calls[:4]) if calls else "")
                        for imp, calls, _w in uv[:2])
        for i in range(1, n + 1):
            first = i == 1 and pub
            out += ["", f"def test_todo_case_{i}():",
                    f"    # {SKELETON_MARK}: replace this placeholder. "
                    + (f"Run TASK.md's example through the public API it uses: {pub}" if first else f"Call {names} with concrete inputs from TASK.md"),
                    *([f"    # (modules are lazy: {', '.join(_py_loader_name(m) + '()' for m in mods)})"] if mods else []),
                    "    # and check exactly what the user sees: the output, the error raised or the value returned (it must FAIL on "
                    "the current code)." if first else "    # and check the exact expected value (it must FAIL on the current code).",
                    "    pass"]
        if direct:
            out += py_harness([f"test_todo_case_{i}" for i in range(1, n + 1)])
        return "\n".join(out) + "\n"
    if lang == "node":
        esm = tf.endswith((".mjs", ".mts")) or (not tf.endswith((".cjs", ".cts")) and _pkg_is_esm(cwd, tf))
        out = [f"// {head}"]
        out += (["import { test } from 'node:test';", "import assert from 'assert';"] if esm
                else ["const { test } = require('node:test');", "const assert = require('assert');"])
        for i, f in enumerate(mine):
            rel = os.path.relpath(f, tdir).replace(os.sep, "/")
            rel = rel if rel.startswith(".") else "./" + rel
            out.append(f"const load{i} = () => import('{rel}');" if esm else f"const load{i} = () => require('{rel}');")  # lazy: loaded inside each test
        for i in range(1, n + 1):
            out += ["", f"test('TODO case {i}', {'async ' if esm else ''}() => {{",
                    *([f"  // (modules are lazy: {', '.join(('await ' if esm else '') + f'load{j}()' for j in range(len(mine)))})"] if mine else []),
                    f"  // {SKELETON_MARK}: replace this placeholder. Call {names} with concrete inputs from TASK.md",
                    "  // and check the exact expected value with `assert` (it must FAIL on the current code).", "});"]
        return "\n".join(out) + "\n"
    return None


def probe_test(cwd, spec, allowed):
    """A throwaway test for --probe: load every allowed module of the test's language, call each target_fn with no
    arguments (python: None for each required one) inside try/catch (load errors are printed as PROBE-LOAD so the crash gate sees them), then fail with
    assert.fail('probe'). If the fixture is sound this is exactly a red test that reaches the target. None for an unknown language."""
    tf = spec["test_file"]
    lang, tdir = lang_of(tf), (str(Path(tf).parent) or ".")
    fns = spec.get("target_fn") or []
    fns = [fns] if isinstance(fns, str) else list(fns)
    mine = [f for f in allowed if lang_of(f) == lang]
    if lang == "py":
        out = ["import sys", "from pathlib import Path", f"sys.path.insert(0, str(Path(__file__).resolve().parent / {os.path.relpath('.', tdir)!r}))"]
        out += ["", "def test_probe():", "    mods = []"]
        for f in mine:
            out += ["    try:", f"        import importlib; mods.append(importlib.import_module({re.sub(r'/', '.', f[:-3])!r}))",
                    "    except BaseException as e:", f"        print('PROBE-LOAD {f}:', type(e).__name__, e)"]
        out += ["    print('PROBE-EXPORTS', sum(1 for m in mods for k in dir(m) if not k.startswith('_')))"]
        for fn in fns:
            out += ["    import inspect",
                    "    def _args(g):",
                    "        try:",
                    "            return [None] * len([q for q in inspect.signature(g).parameters.values() if q.default is q.empty and q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)])",
                    "        except (TypeError, ValueError):",
                    "            return []",
                    "    cands = []",
                    "    for m in mods:",
                    f"        f = getattr(m, {fn!r}, None)",
                    "        if callable(f):",
                    "            cands.append(f)",
                    f"            cands += [c.cell_contents for c in (getattr(f, '__closure__', None) or ()) if callable(c.cell_contents) and getattr(c.cell_contents, '__name__', None) == {fn!r}]  # decorator without functools.wraps",
                    "        for k in dir(m):  # methods of a class defined in the module",
                    "            c = getattr(m, k, None)",
                    f"            if isinstance(c, type) and c.__module__ == m.__name__ and callable(getattr(c, {fn!r}, None)):",
                    "                try:",
                    "                    inst = c(*_args(c.__init__)[1:])",
                    f"                    cands.append(getattr(inst, {fn!r}))",
                    "                except BaseException as e:",
                    f"                    print('PROBE-CALL {fn}:', type(e).__name__, e)",
                    "    if not cands:",
                    f"        print('PROBE-NOCALL {fn}')",
                    "    for f in cands:",
                    "        try:",
                    "            f(*_args(f))",
                    "        except BaseException as e:",
                    f"            print('PROBE-CALL {fn}:', type(e).__name__, e)"]
        # a plain-script test_cmd (`python3 test_x.py`) runs the probe too; under pytest __name__ is the module name
        return "\n".join(out + ["    raise AssertionError('probe')", "", "", 'if __name__ == "__main__":', "    test_probe()"]) + "\n"
    if lang == "node":
        esm = tf.endswith((".mjs", ".mts")) or (not tf.endswith((".cjs", ".cts")) and _pkg_is_esm(cwd, tf))
        out = (["import { test } from 'node:test';", "import assert from 'assert';"] if esm
               else ["const { test } = require('node:test');", "const assert = require('assert');"])
        out += ["test('probe', async () => {", "  const mods = [];"]
        for f in mine:
            rel = os.path.relpath(f, tdir).replace(os.sep, "/")
            rel = rel if rel.startswith(".") else "./" + rel
            out += ["  try {", f"    mods.push({'await import' if esm else 'require'}({rel!r}));",
                    "  } catch (e) {", f"    console.log('PROBE-LOAD {f}: ' + e.name + ': ' + e.message);", "  }"]
        out += ["  console.log('PROBE-EXPORTS ' + mods.reduce((n, m) => n + (m ? Object.keys(m).length + (typeof m === 'function' ? 1 : 0) : 0), 0));"]
        for fn in fns:
            q = repr(fn)
            out += ["  {", "    const cands = [];",
                    "    for (const m of mods) {",
                    f"      if (m && typeof m[{q}] === 'function') cands.push([m[{q}], m]);",
                    f"      if (typeof m === 'function' && m.name === {q}) cands.push([m, undefined]);  // module.exports = fn",
                    "      for (const C of [m, m && m.default]) {",
                    f"        if (typeof C !== 'function') continue;",
                    f"        if (C !== m && C.name === {q}) cands.push([C, undefined]);  // export default function fn / CJS module.exports = fn under ESM import",
                    f"        if (C !== m && typeof C[{q}] === 'function') cands.push([C[{q}], C]);  // static method",
                    f"        if (C.prototype && typeof C.prototype[{q}] === 'function') {{  // method of a class: build an instance with no args",
                    f"          try {{ cands.push([C.prototype[{q}], new C()]); }} catch (e) {{ console.log('PROBE-CALL {fn}: ' + e.name + ': ' + e.message); }}",
                    "        }",
                    "      }",
                    "    }",
                    f"    if (!cands.length) console.log('PROBE-NOCALL {fn}');",
                    "    for (const [f, self] of cands) {",
                    f"      try {{ await f.call(self); }} catch (e) {{ console.log('PROBE-CALL {fn}: ' + e.name + ': ' + e.message); }}",
                    "    }", "  }"]
        return "\n".join(out + ["  assert.fail('probe');", "});"]) + "\n"
    return None


GUARD_LINE_RE = re.compile(r"^\s*(?:(?:\}\s*)?else\s+)?(?:if\b.*\b(?:return|throw|continue)\b|(?:return|throw)\b)")


def probe_check(cwd, spec, allowed):
    """--probe: run probe_test() through the real test_cmd with full reach and judge the fixture, no model.
    Returns (problems, info). Checks: no crash/load error, a plain assertion failure, every target_fn ran, every
    reach_anchor line exists (info: how many early return/throw guards precede it)."""
    tf = spec["test_file"]
    txt = probe_test(cwd, spec, allowed)
    if txt is None:
        return [f"cannot probe: unknown language for {tf}"], {}
    Path(cwd, tf).parent.mkdir(parents=True, exist_ok=True)
    Path(cwd, tf).write_text(txt)
    targets = [t for t in allowed if lang_of(t) and lang_of(t) == lang_of(tf)]
    rc, out, reach = run_cmd(spec["test_cmd"], cwd, spec, probe_targets=targets, full_reach=True)
    info = {"rc": rc, "reach": sorted((reach or {}).get("fns", ())), "anchors": {}}
    load = re.search(r"^[#\s]*(PROBE-LOAD [^\n]*)", out, re.M)
    if load:
        return ["a module cannot be loaded: " + load.group(1)[:200]], info
    if CRASH_RE.search(out):
        return ["the probe crashes instead of failing on its assertion: " + CRASH_RE.search(out).group(0)], info
    if rc == 0 or not FAILASSERT_RE.search(out):
        return [f"the probe did not fail on its assertion (rc={rc}): the test runner is not running {tf}"], info
    info["warn"] = []
    br = re.findall(r"^[#\s]*PROBE-CALL (\S+?): ReferenceError: (document|window|chrome|navigator) is not defined", out, re.M)
    if br:
        info["browser_only"] = sorted({fn for fn, _ in br})
        return [f"browser-only (not-local): `{'`, `'.join(info['browser_only'])}` throws ReferenceError on `{br[0][1]}` under node; "
                "extract the DOM/chrome access behind a pure helper before targeting it"], info
    if reach is None:
        return ["no reach data (coverage unavailable for this test_cmd)"], info
    want = [spec["target_fn"]] if isinstance(spec.get("target_fn"), str) else list(spec.get("target_fn") or [])
    if not want:
        return ["no target_fn in the spec"], info
    miss = [w for w in want if w not in reach["fns"]]
    if miss:
        srcs = [Path(cwd, t).read_text(errors="replace") for t in targets if Path(cwd, t).is_file()]
        exp = re.search(r"^[#\s]*PROBE-EXPORTS (\d+)", out, re.M)
        exported = bool(exp and int(exp.group(1)) > 0)
        defined = [w for w in miss if any(_defines(t, w) for t in srcs)]
        hard = [w for w in miss if w not in defined]
        if hard or not exported:
            why = "nothing is exported from the module" if not exported and not hard else "not defined in the allowed files" if hard else ""
            return [f"target_fn `{'`, `'.join(hard or miss)}` never ran (functions that ran: {sorted(reach['fns']) or 'none'}): "
                    + (why + " (not exported or no such function)" if why else "not exported or no such function")], info
        info["warn"].append(f"probe unable to call `{'`, `'.join(defined)}` directly (defined in the source but not exported/callable as a plain "
                            "module member: an internal helper, a class method, a wrapped function); a real test must reach it through the exported API")
    bad = []
    for anc in as_list(spec.get("reach_anchor")):
        for t in targets:
            src = Path(cwd, t).read_text(errors="replace")
            n = anchor_line(src, anc)
            if n is None:
                continue
            guards = sum(1 for ln in src.splitlines()[:n - 1] if GUARD_LINE_RE.match(ln))
            info["anchors"][anc] = {"file": t, "line": n, "guards_before": guards}
            break
        else:
            bad.append(f'reach_anchor "{anc}" not found in {targets}')
    return bad, info


def phase_a_prompt(spec, allowed, rejected=None, skel=False, cwd=None):
    tf, targets = spec["test_file"], ", ".join(f"`{a}`" for a in allowed)
    s = (f"PHASE 1 OF 2: WRITE A FAILING TEST. Do NOT fix the code in this phase.\n\n"
         f"Write ONE new test file `{tf}` that checks the behaviour TASK.md (below) asks for, using the repo's real code "
         f"({targets}), and that FAILS on the current code because that behaviour is missing or wrong. Run it with:\n\n"
         f"    {spec['test_cmd']}\n\n"
         "Rules:\n"
         + (f"- `{tf}` already exists as a SKELETON: correct imports of the code under test plus placeholder tests marked "
            f"`{SKELETON_MARK}`. Fill that file in: keep the imports, replace EVERY placeholder with a real test, and delete every "
            f"`{SKELETON_MARK}` marker. Do not create a different test file. An unedited skeleton is rejected.\n"
            + ("- The file ends with `TESTS = [...]` and an `if __name__ == \"__main__\":` block that runs them: keep both, and keep "
               "TESTS listing exactly your test functions (rename them there too).\n" if lang_of(tf) == "py" and py_direct_run(spec) else "")
            if skel else "")
         + f"- Create or edit only `{tf}`. Do not edit any other file.\n"
         f"- Call the real code under test. You may fake its dependencies (browser page, network, env, temp dirs), "
         f"but never mock, stub or replace {targets} or any function in it. A fake behaves like the real dependency: when TASK.md "
         "names an exception (an HTTPError, a timeout), make the fake RAISE it the way the real call does, never return an error "
         "status or object in its place.\n"
         "- Assert on what the code DOES (return values, raised errors, files or output it writes), never on its source text: "
         "reading the code under test (open/read_text/inspect.getsource/ast.parse) and asserting a string or call is in it is rejected.\n"
         # The user's view (2026-11-06, real-issue bench isort r3 b01deeeb6d94): TASK.md gave an import block, the output isort
         # printed and the one it should print; the skeleton loaded the diagnosed isort/parse.py, both tests asserted a key of
         # parse.file_contents()'s internal comment dict, and the fix turned them green while isort still printed the wrong
         # output (review reject). A test of TASK.md's own example through the public call stays red until the output is right.
         "- When TASK.md shows an example (an input and the output or error it gives, or should give), at least one test feeds "
         "that input (or a smaller one with the same bug) through the public function, method or command TASK.md uses (or the "
         "package's top-level call for it) and asserts the result the user sees: the returned text or value, the raised error, "
         "the written file. Import that public API next to the file's other imports. A test of an internal helper or data "
         "structure (a parsed dict, a private attribute) may come in addition, never instead: it can pass while the user still "
         "gets the wrong result.\n"
         # Scope (2026-11-06, real-issue bench moto r2 dd520bba2e08): the issue asked for RecoveryPeriodInDays in the ENABLED
         # response; the test also asserted it after PITR was disabled, an existing test pins that response, and both fix attempts
         # had to edit that test (scope reject). The fix prompt already keeps unmentioned inputs; the test must not demand them.
         "- Test only the cases TASK.md describes. An input, flag or branch it does not mention keeps today's result after the fix, "
         "so never assert a new result for one: existing tests pin those cases, and a test that asks them to change cannot pass "
         "alongside them.\n"
         f"- At least {spec.get('min_tests', 2)} test cases. Each must fail on an assertion about a concrete value "
         "(AssertionError), not on a crash, import error or timeout. No `assert.ok(true)`, no `>= 0` bounds, no skipped tests.\n"
         "- When TASK.md adds a method, attribute, parameter or protocol, assert it exists before the test uses it (python: "
         "`assert hasattr(Cls, \"name\")`, `assert \"param\" in inspect.signature(fn).parameters`; JS: "
         "`assert.strictEqual(typeof obj.name, \"function\")`), so the current code fails on that assert, not with a TypeError or "
         "AttributeError at the call.\n"
         "- For an 'at most N' cap, feed MORE than N inputs and assert the exact count (=== N), or pair the cap with >= 1 in the same test. "
         "Never assert a bare <=. Every assertion must fail on the baseline for the stated reason; a test that passes at baseline is useless.\n"
         + ("- Guards (optional): a test of behaviour the fix must KEEP (TASK.md says it stays the same: 'still', 'unchanged', 'as "
            "before', idempotent) is listed in " + guards_hint(tf)
            + ". When TASK.md adds a value and says other values stay the same, a guard feeds near-misses of the new value (for a "
            "new 'x': 'x2', 'xs', 'my-x') and asserts today's result for each. "
            "A guard must PASS on the current code and after the fix, so it never asserts today's result for an input whose "
            "result TASK.md changes: a guard of a side property (a slot released, a temp file removed) feeds inputs TASK.md "
            "leaves unchanged, or asserts only that property. The runner runs guards now, so a wrong expected value "
            "shows up before the fix. Never add a bug assertion to a guard to make it fail. Guards do not count toward the "
            "failing cases above.\n" if spec.get("guards") is not False else "")
         + "- No real network and no real sleeps; every test must finish in a few seconds.\n"
         "- Look at one existing test in the repo first and follow its style and imports.\n"
         + ("- Do not add code that changes sys.path, sys.modules or the working directory to the test file: pytest imports it while it collects "
            "the whole suite, so such a change breaks other test files. Import the code under test by its package path.\n"
            if lang_of(tf) == "py" else ""))
    # Budget + no-fix-research (2026-11-05, real-issue bench sqlmeta r1): both phase A attempts spent all 30 commands working out HOW
    # to fix the bug (how the AST tells bracket quoting from double quotes) and never wrote the test; the retry's "be faster" did not
    # help. Phase B already states its deadline (fix_step_limit); phase A now states its own: the fail-fast (FF_TEST_STEP) or the cap.
    n = min(int(spec.get("test_steps", FF_TEST_STEP)), FF_TEST_STEP)
    s += (f"- Budget: {n} commands. The attempt is ended at command {n} if `{tf}` still has no real test in it, so write a complete "
          f"first version of `{tf}` by command {max(8, n // 3)} and run it, then improve it from the test output.\n"
          "- You do not need to know HOW to fix the bug: the test only states the behaviour TASK.md expects. Do not research or plan "
          "the fix in this phase (a later phase does that). Once you know which call shows the bug and what it should return, write "
          "the test.\n")
    if spec.get("target_fn"):
        fns = [spec["target_fn"]] if isinstance(spec["target_fn"], str) else spec["target_fn"]
        anchors = as_list(spec.get("reach_anchor"))
        if anchors:
            s += (f"- The test must drive the real code far enough that it runs: {', '.join(f'`{f}`' for f in fns)}, and executes the "
                  f"line(s) containing {', '.join(f'`{a}`' for a in anchors)} (checked by line coverage; a call that returns at an "
                  "earlier guard does not count).\n")
        else:
            dls = spec.get("diag_lines") if _line_list(spec.get("diag_lines")) and allowed else []
            s += (f"- The test must drive the real code far enough that it runs: {', '.join(f'`{f}`' for f in fns)}, and executes at least "
                  f"{int(float(spec.get('min_fn_cov', 0.3)) * 100)}% of each one's body lines"
                  + (f" or one of the lines the diagnosis names as the fault in it (line{'s' if len(dls) > 1 else ''} "
                     f"{', '.join(map(str, dls))} of `{allowed[0]}`)"
                     if dls else "")
                  + " (checked by line coverage; a call that returns at the first guard does not count).\n")
    if "node --test" in spec.get("test_cmd", "") or "node:test" in spec.get("test_cmd", ""):
        s += ("- Use `const { test } = require('node:test')` (or import) with ONE `test()` per case and plain `assert` inside it. "
              "Do NOT write your own runner, try/catch that prints FAIL, or one big test wrapping several cases: "
              "a failed assertion must surface as an AssertionError, and the gate needs at least 2 tests. "
              "Make assertions on the feature's observable output (e.g. assert a file exists with `fs.existsSync`), "
              "never let a missing directory crash a `readdir`.\n")
    if spec.get("symptom"):
        s += f"- The failure output must match the regex /{spec['symptom']}/.\n"
    s += f"- Done when `{spec['test_cmd']}` runs your tests and they FAIL for that reason. Then submit.\n\n"
    if rejected:
        s += (f"Your previous attempt (still in `{tf}`) was REJECTED by the gate: {'; '.join(rejected)}. "
              "Fix the test so this no longer holds.\n\n")
    if cwd is not None and spec.get("target_fn"):
        s += fn_slice(cwd, allowed, spec["target_fn"], spec.get("reach_anchor"))
    return s


def fix_step_limit(cwd, allowed):
    """The step N by which the real allowed file must differ (same function the fail-fast watcher uses)."""
    return ff_fix_step({f: Path(cwd, f).read_text(errors="replace") for f in allowed if cwd is not None and Path(cwd, f).is_file()})


def _short_cmd(cmd, n=300):
    """A command for a prompt line: a long test-file list is cut (the runner runs the whole command)."""
    cmd = " ".join(str(cmd).split())
    return cmd if len(cmd) <= n else cmd[:n].rsplit(" ", 1)[0] + " ..."


def phase_b_prompt(spec, allowed, test_text, rejected=None, cwd=None, cleaned=None, suite_hint=None):
    """suite_hint: (seconds, finished) for the suite at baseline (the background baselines may still be running). A slow suite is
    one the model must not run itself: it costs minutes of its budget, and the gate runs it anyway."""
    tf = spec["test_file"]
    s = (f"PHASE 2 OF 2: MAKE THE FAILING TEST PASS.\n\n`{tf}` (shown below) currently fails. It is the acceptance test: "
         f"do NOT edit, move or delete it. Change the code so it passes. Edit only: {', '.join(allowed)}.\n\n"
         f"Run after every edit:\n\n    {spec['test_cmd']}\n\n")
    for k, what in (("suite_cmd", "repo suite"), ("typecheck_cmd", "type check")):
        if not spec.get(k):
            continue
        if k == "suite_cmd" and suite_hint and suite_hint[0] >= SUITE_SLOW_S:
            secs, done = suite_hint
            took = f"which took {secs:.0f}s at baseline" if done else f"which has run {secs:.0f}s so far at baseline"
            s += (f"The repo suite must not gain new failures. The runner runs it for you after you submit (`{_short_cmd(spec[k])}`, "
                  f"{took}): do NOT run the whole suite yourself (it takes minutes, and the runner runs it anyway). Run the test "
                  "command above, plus an existing test file that imports the code you change.\n")
        else:
            s += f"The {what} must not gain new failures: `{spec[k]}`\n"
    if spec.get("update_tests") and spec.get("update_tests_strict"):
        s += (f"Existing tests in {', '.join(as_list(spec['update_tests']))} call the code you change. If one of their test functions "
              "asserts exactly the behaviour the task changes, you may update that function's expectation to what the task asks for "
              f"(at most {UPD_EDIT_MAX} functions). The runner checks each edited test fails against your fix without the edit, passes "
              "with it, and keeps its assertions. Do not edit any other test, rename or delete one, or change other code in those files.\n")
    elif spec.get("update_tests"):
        s += (f"You may also update these existing tests, which assert the old behaviour: {', '.join(as_list(spec['update_tests']))}. "
              "Keep every test case; change only expectations the task changes.\n")
    s += "The changed files must contain every literal in TASK.md's `## Must contain` list, and must still contain every literal on a `Must keep:` line.\n"
    if cwd is not None and Path(cwd, "TASK.md").is_file() and parse_must_not_contain(Path(cwd, "TASK.md").read_text(errors="replace")):
        s += ("They must no longer contain any literal in TASK.md's `## Must not contain` list (stale text the task replaces: "
              "reword or remove it).\n")
    s += ("The test must actually execute the lines you change (checked by line coverage): fix the code path the test drives, "
          "do not add unused helpers or dead branches, and leave no import or variable unused (checked by a linter).\nSubmit when the test passes.\n\n")
    s += (f"Edit the real file ({', '.join(allowed)}) in place, in small edits, and run the test after each. Staging a rewrite in /tmp does not count: "
          f"the attempt is ended if the file is still unchanged at step {fix_step_limit(cwd, allowed)}.\n\n")
    if rejected:
        s += f"A previous attempt was REJECTED by the gate: {'; '.join(rejected)}. The code is as that attempt left it.\n\n"
    if cleaned:
        s += (f"The runner deleted these stray files the previous attempt created outside the allowed files: {', '.join(cleaned)}. "
              "Do not create files in the repository (e.g. `.bak` copies); keep scratch files in /tmp.\n\n")
    body = test_text if len(test_text) < 12000 else test_text[:12000] + "\n... (truncated; read the rest by line range)"
    s += f"--- {tf} ---\n{body}\n--- end {tf} ---\n\n"
    if needs_info_active(spec):
        s += NEEDS_RULE
    if cwd is not None and spec.get("target_fn"):
        s += fn_slice(cwd, allowed, spec["target_fn"], spec.get("reach_anchor"))
    return s


# ---- breadth retry (spec breadth_retry, set by auto when mini_mutate scores the fix) ---------------------------------------------
# Bench 2026-11-03/04: a prefix/substring match where the Problem named exact values was the top reject class (libseed escape,
# libseed2, sample3), although TASK.fix.md asks for exact matches up front. A green fix whose mini_mutate run shows a NARROWING
# `breadth` survivor (startswith/endswith/in -> ==) gets ONE more local attempt that names those lines, before any Claude review.
BREADTH_STEPS = 20


def narrow_survivors(detail, base=None):
    """[(where, before, after)] for the `breadth` survivors in a mutation gate detail that narrow a match the fix makes to an exact
    one (mini_mutate writes those as `((x) == S)` / `((x) != S)`): the frozen test cannot tell the fix's prefix, suffix or substring
    match from an exact one. The widening kind (`x == S` -> `str(x).startswith(S)`: an exact fix whose test has no near-miss) is
    left out: an exact match is the safe side. base ({file: baseline text}) drops a match whose source text (mini_mutate prints it
    verbatim) is already in the baseline file: mini_mutate mutates whole changed lines, so a match the original code already made on
    a line the fix touched shows up too (bench-libseed2: the baseline's own `label.startswith('sonarr')`), and it is not the fix's."""
    m = re.search(r"; survivors: (.*)$", detail or "", re.S)
    out = []
    for x in (m.group(1).split(" ;; ") if m else []):
        b = re.match(r"(\S+:\d+) breadth: (.+?) -> (\(\(.*)$", x.strip(), re.S)
        if b and not (b.group(2) in ((base or {}).get(b.group(1).rsplit(":", 1)[0]) or "")):
            out.append(b.groups())
    return out


def breadth_prompt(spec, allowed, narrow, diff):
    tf = spec["test_file"]
    s = ("PHASE 2 OF 2, LAST CHECK: IS EACH STRING MATCH IN YOUR FIX AS WIDE AS TASK.md SAYS, AND NO WIDER?\n\n"
         f"Your fix makes `{tf}` pass. A check then turned each prefix, suffix or substring match below into an exact match, and `{tf}` "
         "still passed. So the test does not show how wide these matches must be: only TASK.md does.\n\n")
    s += "".join(f"- `{w}`: `{b}` (the test also passes with `{a}`)\n" for w, b, a in narrow)
    s += ("\nA prefix, suffix or substring match also matches values nobody named: `name.startswith('tmp')` also matches 'tmpl' and "
          "'tmp2', and `'log' in path` also matches 'catalog/x.txt' and 'blog.txt'.\n\nFor each match above:\n"
          "1. Find what TASK.md says about which values count, and which values it says keep their current behaviour.\n"
          "2. If TASK.md names exact values (a name, a label, a directory, a file-name prefix), or says other values are unchanged, make "
          "the code match exactly what TASK.md names and nothing more: compare the whole value, or one path component, not any part "
          "of the string.\n"
          "3. If TASK.md says every value with that prefix, suffix or substring counts, leave the line as it is.\n\n"
          f"Rules: do NOT edit, move or delete `{tf}`. Edit only {', '.join(allowed)}, and only the matches listed above: a match the "
          f"original code already made stays exactly as it is. Run `{spec['test_cmd']}` after each edit: it must still pass.\n\n"
          "Before you submit, write one line per match in your message: `BREADTH <file>:<line> narrowed: \"<the TASK.md words that "
          "decide it>\"` or `BREADTH <file>:<line> kept: \"<the TASK.md words that decide it>\"`.\n\n")
    return s + f"--- your fix (diff against the original code) ---\n{diff[:8000]}\n--- end diff ---\n\n"


def breadth_said(traj):
    """The model's `BREADTH <file:line> narrowed|kept: ...` lines from its messages in the trajectory (the last one per place),
    one line each with no table pipes, for the packet."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return []
    got = {}
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        if m.get("role") not in ("assistant", "exit") or not isinstance(c, str):
            continue
        for w, how, why in re.findall(r"^[\s`*-]*BREADTH\s+(\S+?)\s+(narrowed|kept)\b\s*:?\s*(.*)$", c, re.M | re.I):
            w = w.strip("`*")
            if w and "<" not in w:
                got[w] = f"{w} {how.lower()}: {why.strip().strip('`* ').replace('|', '/')[:200]}"
    return list(got.values())


# ---- needs_info channel (MINI_NEEDS_INFO=off|shadow|on, default off) --------------------------------------------------------
# The model may write NEEDS.md (one question, evidence, two branches) instead of guessing. A gate-approved ask parks the job
# (exit 3, pause_reason needs_info: not a failure, no done.json); `mini-answer <job> "text"` writes the answers sidecar and
# resumes it; prepare() folds the sidecar into TASK.md and the run starts from scratch against the SAME frozen test.
NEEDS_FLAG_FILE = Path(os.path.expanduser("~/.config/mini-swe/needs_info"))
NEEDS_SHADOW_LOG = Path(os.path.expanduser("~/.ollama-queue/logs/needs-info-shadow.jsonl"))
NEEDS_HEADS = ("Question", "Tried", "If answer is A", "If answer is B")
NEEDS_RULE = (
    "NEEDS-INFO (optional, rarely right): if the task depends on a FACT that is not in the repo, TASK.md or the test (an external API value, a business rule, "
    "a choice between two valid behaviours the test does not pin down) you MAY stop and ask ONE question instead of guessing.\n"
    "- Before asking you MUST search: at least 2 `grep`/`rg`/`find`/`cat` commands aimed at the answer.\n"
    "- To ask, write `NEEDS.md` in this directory with these headings, then submit: `## Question` (one question), `## Tried` (the exact commands you ran, "
    "in backticks, and what they showed), `## If answer is A` and `## If answer is B` (what you would change in each case).\n"
    "- Do NOT ask about anything the repo, TASK.md or the test can answer, for permission, for style preferences, or to have the test changed. "
    "A rejected question costs you an attempt.\n\n")


DIAG_NEEDS_RULE = NEEDS_RULE.replace("what you would change in each case", "what your diagnosis would say in each case").replace(
    "then submit:", "INSTEAD of DIAGNOSIS.md, then submit:")


def needs_info_mode():
    """off (default, also for any bad value) | shadow | on. Env MINI_NEEDS_INFO wins when set; else the flag file (MINI_NEEDS_INFO_FILE or
    ~/.config/mini-swe/needs_info), read on every call so the switch flips without restarting the queue daemon."""
    v = os.environ.get("MINI_NEEDS_INFO")
    if v is None:
        try:
            v = Path(os.environ.get("MINI_NEEDS_INFO_FILE") or NEEDS_FLAG_FILE).read_text()
        except OSError:
            v = ""
    v = v.strip().lower()
    return v if v in ("shadow", "on") else "off"


def answers_path(spec):
    """<job-local spec task>.answers.md, or None when the task is not a job-local absolute file (nowhere durable to keep answers)."""
    t = spec.get("task")
    if not isinstance(t, str) or is_inline_task(t) or not Path(os.path.expanduser(t)).is_absolute():
        return None
    return Path(os.path.expanduser(t) + ".answers.md")


def needs_info_active(spec):
    """The channel is offered for this spec: mode != off, a durable answers location exists, and it is not a steps chain (out of scope)."""
    return needs_info_mode() != "off" and "_guard" not in spec and answers_path(spec) is not None


def ask_count(spec):
    """Questions already answered for this job = `### Q<n>` entries in the sidecar (on disk: in-process counters reset on every relaunch)."""
    p = answers_path(spec)
    try:
        return len(re.findall(r"^### Q\d+", p.read_text(errors="replace"), re.M)) if p else 0
    except OSError:
        return 0


def answers_test_sha(spec):
    """The frozen-test sha256 recorded by the latest answered ask (`<!-- test-sha: HEX -->`), or None."""
    p = answers_path(spec)
    try:
        m = re.findall(r"<!-- test-sha: ([0-9a-f]+) -->", p.read_text(errors="replace")) if p else []
    except OSError:
        return None
    return m[-1] if m else None


_NEEDS_READ_RE = re.compile(r"(?:^|[|;&(\s])(?:grep|egrep|rg|ag|find|cat|nl|sed|head|tail|awk|less)\b|git\s+grep")


def _needs_sections(text):
    secs, cur = {}, None
    for ln in text.splitlines():
        m = re.match(r"##\s+(.+?)\s*$", ln)
        if m:
            cur = m.group(1).strip().lower()
            secs.setdefault(cur, [])
        elif cur is not None:
            secs[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in secs.items()}


def check_needs(cwd, steps, text, ask_n=0, cap=None):
    """Question-quality gate for NEEDS.md. steps = traj_steps() [(command, output)]. Returns (ok, why)."""
    cap = int(os.environ.get("MINI_NEEDS_INFO_MAX", "1")) if cap is None else cap
    if ask_n >= cap:
        return False, f"ask cap reached ({ask_n}/{cap} already answered)"
    secs = _needs_sections(text)
    miss = [h for h in NEEDS_HEADS if h.lower() not in secs]
    if miss:
        return False, "missing heading(s): " + ", ".join(f"## {h}" for h in miss)
    q = secs["question"]
    if not q or len(q) > 600:
        return False, "the Question is empty" if not q else f"the Question is {len(q)} chars (max 600)"
    cmds = [c for c, _ in steps]
    if sum(1 for c in cmds if _NEEDS_READ_RE.search(c)) < 2:
        return False, "fewer than 2 search/read commands before asking"
    tried = secs["tried"]
    quoted = re.findall(r"`([^`\n]{4,})`", tried) + [ln.strip().lstrip("$ ").strip() for ln in tried.splitlines() if ln.strip().startswith("$ ")]
    sq = [_squash(c) for c in cmds]
    if not any(_squash(x) in c or c in _squash(x) for x in quoted for c in sq if c):
        return False, "## Tried quotes no command that appears in the run log (no fabricated evidence)"
    seen = "\n".join(c + "\n" + o for c, o in steps)
    if re.search(r"\b(?:test|assert)", q, re.I) and re.search(r"\b(?:chang|remov|relax|edit|modif|delet|loosen|skip)", q, re.I):
        return False, "asks about changing the test (the test is frozen)"
    for tok in dict.fromkeys(re.findall(r"`([^`\n]{2,120})`", q) + re.findall(r"[\w./-]+\.[A-Za-z]{1,5}\b", q)):
        f = Path(cwd, tok)
        if tok.startswith("/") or ".." in tok:
            continue
        files = [tok] if f.is_file() else []
        if not files and re.fullmatch(r"[A-Za-z_]\w{3,}", tok):  # an identifier: the files that define/use it
            files = [x for x in git(cwd, "grep", "-lwI", "-m1", "--", tok).splitlines()[:8] if x]
        if files and not any(x in seen or Path(x).name in seen for x in files):
            return False, f"the Question names `{tok}` which is in the repo ({files[0]}) but was never read in the run log"
    return True, ""


def needs_info_shadow_row(why_ok, why, text, spec):
    try:
        log = Path(os.environ.get("MINI_NEEDS_INFO_SHADOW_LOG") or NEEDS_SHADOW_LOG)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "accepted": why_ok, "why": why, "question": _needs_sections(text).get("question", "")[:300],
                                 "task": str(spec.get("task") or "")}) + "\n")
    except OSError:
        pass


def needs_info_step(cwd, spec, traj, test_sha):
    """After a Phase B run: handle NEEDS.md (always removed, it must never reach the gates or the commit).
    -> None (nothing to do / off / shadow: carry on exactly as without), ("reject", why) or ("park", meta)."""
    np_ = Path(cwd, "NEEDS.md")
    if not np_.is_file():
        return None
    text = np_.read_text(errors="replace")
    np_.unlink()
    mode = needs_info_mode() if needs_info_active(spec) else "off"
    if mode == "off":
        return None
    ok, why = check_needs(cwd, traj_steps(traj), text, ask_count(spec))
    secs = _needs_sections(text)
    if mode == "shadow":
        print(f"[mini-runner] NEEDS-INFO-SHADOW {'accepted' if ok else 'rejected: ' + why}", flush=True)
        needs_info_shadow_row(ok, why, text, spec)
        return None
    if not ok:
        print(f"[mini-runner] NEEDS-INFO rejected: {why}", flush=True)
        return ("reject", why)
    return ("park", {"question": secs["question"], "tried": secs["tried"], "branches": {"A": secs["if answer is a"], "B": secs["if answer is b"]},
                     "ask_n": ask_count(spec) + 1, "test_sha": test_sha, "test_file": spec.get("test_file"),
                     "task": str(Path(os.path.expanduser(spec["task"]))), "answers": str(answers_path(spec))})


# ---- single-shot repair (spec fix_mode "single_shot", Phase B) --------------------------------------------------------
# One request = TASK.md + the frozen test + the target function (line-numbered) + the last failure; the model returns the whole
# new function in ONE fenced block, the runner splices it over the function's span, checks it parses and runs the unchanged
# Phase B gates. Up to `single_shot_retries` (2) retries carry the rejection reason; `best_of` (1) samples per attempt; after the
# attempts (or at once when the fix needs a second function) the file is restored and the normal mini loop runs.

SS_HELPER = MINI_EXT / "single_shot_call.py"
SS_TAGS = {"py": {"", "python", "py", "python3"},
           "node": {"", "js", "javascript", "jsx", "mjs", "cjs", "node", "ts", "typescript", "tsx", "mts", "cts"}}
SS_MARK = "@@SS@@ "
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w+#.-]*)[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
NEEDS_MORE_RE = re.compile(r"^\s*NEEDS_MORE:\s*(.*)$", re.M)


class NeedsMore(Exception):
    """The fix cannot be one function: go straight to the mini loop."""


def ss_target(cwd, spec, allowed):
    """(file, fn, first, last, text) of the single function single-shot may replace, or (None, reason)."""
    fns = as_list(spec.get("target_fn"))
    if spec.get("update_tests") and not spec.get("update_tests_strict"):  # strict (auto): the tests may stay as they are
        return None, "single_shot cannot edit the update_tests files"
    if len(fns) != 1:
        return None, f"single_shot needs exactly one target_fn (got {len(fns)})"
    hits = []
    for f in allowed:
        p = Path(cwd, f)
        if p.is_file() and lang_of(f):
            d = functions(f, p.read_text(errors="replace")).get(fns[0])
            if d:
                hits.append((f, d))
    if len(hits) != 1:
        return None, f"`{fns[0]}` is defined in {len(hits)} allowed files; single_shot needs exactly one"
    f, (a, b) = hits[0]
    if b is None:
        return None, f"end of `{fns[0]}` in {f} cannot be determined"
    cap = int(spec.get("single_shot_max_lines", 200))
    if b - a + 1 > cap:
        return None, f"`{fns[0]}` is {b - a + 1} lines (single_shot_max_lines {cap})"
    return (f, fns[0], a, b, "\n".join(Path(cwd, f).read_text(errors="replace").splitlines()[a - 1:b])), None


def ss_prompt(spec, tgt, cwd, task_md, test_text, last_out=None, last_code=None, reasons=None):
    f, fn, a, b, body = tgt
    lang = "python" if lang_of(f) == "py" else ("typescript" if Path(f).suffix in (".ts", ".mts", ".cts", ".tsx") else "javascript")
    lines = Path(cwd, f).read_text(errors="replace").splitlines()
    s = (f"You are fixing ONE function in `{f}`. Reply with the COMPLETE new version of `{fn}` -- from its first line to its last, "
         f"same indentation, same name and signature unless TASK.md says otherwise -- in a single fenced code block tagged ```{lang}, "
         "and nothing after the block (a few lines of reasoning before it are fine). Do NOT reply with a diff or SEARCH/REPLACE "
         "blocks, and do not include imports, line numbers or other functions. If the fix cannot be made by changing only this "
         "function (it needs another function, a new helper or another file), reply with ONE line `NEEDS_MORE: <what>` and no code block.\n\n"
         f"The acceptance test `{spec['test_file']}` (below) currently fails and must pass after your change; it is frozen. "
         "The new function must contain every literal in TASK.md's `## Must contain` list and keep every `Must keep:` literal.\n\n"
         "--- TASK.md ---\n" + task_md.rstrip() + "\n--- end TASK.md ---\n\n")
    tb = test_text if len(test_text) < 12000 else test_text[:12000] + "\n... (truncated)"
    s += f"--- {spec['test_file']} ---\n{tb}\n--- end {spec['test_file']} ---\n\n"
    imps = _import_lines(lines, min(a - 1, 80))
    if imps:
        s += "Imports of the file:\n" + "\n".join(f"{n:>5}\t{t}" for n, t in imps) + "\n\n"
    s += f"`{fn}` as it is now (`{f}` lines {a}-{b}; the numbers are NOT part of the code):\n" + "\n".join(f"{n:>5}\t{lines[n - 1]}" for n in range(a, b + 1)) + "\n"
    other = [f"{n}:{x}-{y or x}" for n, (x, y) in functions(f, "\n".join(lines)).items() if n != fn][:40]
    if other:
        s += "Other functions in the file (name:first-last): " + ", ".join(other) + "\n"
    if last_code is not None:
        s += (f"\nYour previous version of `{fn}` was REJECTED: {'; '.join(reasons or ['rejected'])}.\n"
              "```\n" + last_code.rstrip() + "\n```\nThe code shown above is still the ORIGINAL; write a corrected complete replacement.\n")
        if last_out:
            s += "Output of the test run against your previous version (trimmed):\n```\n" + last_out[-2500:].strip() + "\n```\n"
    else:
        base = (spec.get("_ss_baseline_out") or "").strip()
        if base:
            s += "\nThe acceptance test's current failure (trimmed):\n```\n" + base[-2500:] + "\n```\n"
    return s + f"\nReply with the new `{fn}` in one ```{lang} block.\n"


def ss_extract(content, path, fn):
    """The function text out of a reply, or raise ValueError(reason) / NeedsMore(reason)."""
    txt = content or ""
    if "</think>" in txt:
        txt = txt.rsplit("</think>", 1)[1]
    elif re.match(r"\s*(?:<think>|Thinking Process)", txt) and not FENCE_RE.search(txt):
        raise ValueError("no answer: the reply was only reasoning (cut off); answer directly with the one code block")
    m = NEEDS_MORE_RE.search(txt)
    blocks = [(t.lower(), c) for _, t, c in FENCE_RE.findall(txt)]
    if m and not blocks:
        raise NeedsMore(m.group(1).strip()[:200] or "the model says the fix needs more than this function")
    if not blocks:
        raise ValueError("no fenced code block: reply with exactly one ```<lang> block holding the whole new function")
    ok = SS_TAGS[lang_of(path)]
    good = [c for t, c in blocks if t in ok]
    if not good:
        raise ValueError(f"the code block is tagged ```{blocks[-1][0]} (expected a {'python' if lang_of(path) == 'py' else 'javascript/typescript'} block); "
                         "reply with exactly one ```<lang> block holding the whole new function, not a diff or shell snippet")
    code = good[-1].strip("\n")
    if re.search(r"^(?:@@ .*@@|<{7}|={7}|>{7}|---\s+\S+|\+\+\+\s+\S+)", code, re.M):
        raise ValueError("the block looks like a diff / SEARCH-REPLACE; return the whole new function instead")
    if not _defines(code, fn):
        raise ValueError(f"the block does not define `{fn}`")
    return code


def _is_comment(ln):
    t = ln.strip()
    return not t or t.startswith(("//", "/*", "*", "#", "@"))


def ss_toplevel_extra(code, path, fn):
    """What else the reply holds at top level besides the target function: names of other functions/classes, and `<statement>` descriptions of
    any other top-level statement (assignment, expression, import, `module.exports.x = ...`). Any of it means the fix needs more than this function."""
    ded = textwrap.dedent(code)
    if lang_of(path) == "py":
        try:
            tree = ast.parse(ded)
        except SyntaxError:
            return []
        out = []
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if n.name != fn:
                    out.append(n.name)
            elif not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str)):  # a bare docstring-like string is harmless
                seg = (ast.get_source_segment(ded, n) or "").strip().splitlines()
                out.append(f"<{type(n).__name__} statement: {seg[0][:40] if seg else ''}>")
        return out
    lines = code.splitlines()
    ind = len(code) - len(code.lstrip(" \t"))
    out, span = [], None
    fs = functions("x.js", code)
    for name, (first, _) in fs.items():
        ln = lines[first - 1]
        if name != fn and len(ln) - len(ln.lstrip(" \t")) <= ind:
            out.append(name)
    if fn in fs and fs[fn][1] is not None:
        span = fs[fn]
    if span:
        inblock = False
        for k, ln in enumerate(lines, 1):  # top-level lines outside the target's span (comments / JSDoc / closing text aside)
            if span[0] <= k <= span[1]:
                continue
            t = ln.strip()
            if "/*" in t and "*/" not in t:
                inblock = True
            if inblock:
                inblock = "*/" not in t
                continue
            if _is_comment(ln) or len(ln) - len(ln.lstrip(" \t")) > ind or t in ("}", "};", ");"):
                continue
            if not any(f in out for f in [n for n, (a_, b_) in fs.items() if a_ <= k <= (b_ or a_)]):
                out.append(f"<statement: {t[:40]}>")
    return out


def _string_interior_lines(code, path=""):
    """0-based indexes of the lines that START inside a multi-line string literal (Python triple-quoted / JS template): re-indenting must not touch them."""
    inside = set()
    if lang_of(path) != "node":
        import io
        import tokenize
        try:
            for tok in tokenize.generate_tokens(io.StringIO(code).readline):
                if tok.start[0] < tok.end[0] and tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
                    inside.update(range(tok.start[0], tok.end[0]))  # rows are 1-based: rows start+1..end -> indexes start..end-1
            return inside
        except (tokenize.TokenError, IndentationError, SyntaxError):
            if lang_of(path) == "py":
                return inside
            inside = set()
    state, i, row = None, 0, 0
    while i < len(code):
        c = code[i]
        if c == "\n":
            row += 1
            if state == "`":
                inside.add(row)
            if state in ("'", '"', "//"):
                state = None
        elif state is None:
            if c in "'\"`":
                state = c
            elif code.startswith("//", i):
                state = "//"
            elif code.startswith("/*", i):
                state, i = "/*", i + 1
        elif state == "/*":
            if code.startswith("*/", i):
                state, i = None, i + 1
        elif state in ("'", '"', "`"):
            if c == "\\":
                i += 1
            elif c == state:
                state = None
        i += 1
    return inside


def ss_splice(text, a, b, code, path=""):
    """text with lines a..b (1-based, inclusive) replaced by the reply. Re-indented only when the reply's first line is not already at the
    original first line's indentation, by that one uniform difference, and never inside multi-line string literals or on blank lines. Python
    decorators of the original that the reply left out are kept."""
    lines = text.split("\n")
    first = lines[a - 1]
    ind = first[:len(first) - len(first.lstrip(" \t"))]
    cl = code.split("\n")
    fnb = next((l for l in cl if l.strip()), "")
    rind = fnb[:len(fnb) - len(fnb.lstrip(" \t"))]
    if rind != ind:
        inside = _string_interior_lines(code, path)
        out = []
        for k, l in enumerate(cl):
            if not l.strip() or k in inside:
                out.append(l)
            elif len(rind) > len(ind):
                cut = min(len(rind) - len(ind), len(l) - len(l.lstrip(" \t")))
                out.append(l[cut:])
            else:
                out.append(ind[len(rind):] + l)
        cl = out
    cl = [l if l.strip() else "" for l in cl]
    if lang_of(path) != "node" and fnb.lstrip().startswith(("def ", "async def ")) and first.lstrip().startswith("@"):
        k = a - 1
        while k < b and not lines[k].lstrip().startswith(("def ", "async def ")):
            k += 1
        cl = lines[a - 1:k] + cl  # the original's decorators
    return "\n".join(lines[:a - 1] + cl + lines[b:])


def parse_check(cwd, f):
    """None when the file parses, else a short message. Python: compile(); JS: node --check; TS: node's type stripping (syntax errors throw)."""
    p = Path(cwd, f)
    if lang_of(f) == "py":
        try:
            compile(p.read_text(), f, "exec")
        except (SyntaxError, ValueError) as e:
            return f"SyntaxError: {getattr(e, 'msg', e)} (line {getattr(e, 'lineno', '?')})"
        return None
    node = shutil.which("node")
    if not node:
        return None
    try:
        if p.suffix in (".ts", ".mts", ".cts", ".tsx"):
            r = subprocess.run([node, "-e", "const m=require('node:module');m.stripTypeScriptTypes(require('fs').readFileSync(process.argv[1],'utf8'))", str(p)],
                               capture_output=True, text=True, timeout=60)
        else:
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "the syntax check timed out after 60 s"
    if r.returncode == 0:
        return None
    err = [l for l in (r.stderr or "").splitlines() if "Error" in l]
    return (err[0] if err else (r.stderr or "syntax error").strip().splitlines()[-1])[:200]


def ss_call(model, local, messages, no_think, timeout, max_tokens=None):
    """One completion via mini_ext/single_shot_call.py in the mini venv (litellm, overlay sampling, honours no_think).
    MINI_SINGLE_BIN overrides the command (tests). Returns the helper's dict; {"error": ...} on any failure."""
    argv = shlex.split(os.environ["MINI_SINGLE_BIN"]) if os.environ.get("MINI_SINGLE_BIN") else [str(Path(MINI).parent / "python"), str(SS_HELPER)]
    kdir, kpath = write_key_file(local.get("api_key", ""))
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "MINI_KEY_FILE")}
    env["MINI_KEY_FILE"] = kpath
    req = json.dumps({"model": model, "base_url": local.get("base_url", ""), "messages": messages, "overlay": str(OVERLAY),
                      "no_think": bool(no_think), "timeout": int(timeout),
                      **({"max_tokens": int(max_tokens)} if max_tokens else {})})
    try:
        proc = _popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, errors="replace")
        try:
            out, _ = proc.communicate(req, timeout=max(5, timeout + 30))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            return {"error": f"timeout after {int(timeout)}s"}
        finally:
            _CHILDREN.discard(proc.pid)
    except OSError as e:
        return {"error": f"cannot start the model helper: {e}"}
    finally:
        shutil.rmtree(kdir, ignore_errors=True)
        _SECRET_FILES.discard(kdir)
    for ln in reversed((out or "").splitlines()):
        if ln.startswith(SS_MARK):
            try:
                return json.loads(ln[len(SS_MARK):])
            except ValueError:
                break
    return {"error": "no result from the model helper: " + (out or "").strip()[-200:].replace(local.get("api_key", "\0"), "<key>")}


def single_shot_fix(cwd, spec, allowed, base_texts, test_sha, base_tests, base_suite, base_tc, pre, task_md, test_text, ev, usage,
                    model, local, deadline, call=None):
    """Try fix_mode single_shot. Returns dict: green (bool), gates, out, final (str), fallback (reason or None), attempts (int).
    On any non-green end the allowed files are restored to the baseline so the mini loop starts clean."""
    call = call or ss_call
    res = {"green": False, "gates": {}, "out": "", "final": "", "fallback": None, "attempts": 0}
    tgt, why = ss_target(cwd, spec, allowed)
    if tgt is None:
        res["fallback"] = why
        print(f"[mini-runner] single-shot not used: {why}; mini loop", flush=True)
        return res
    f, fn, a, b, _ = tgt
    spec = {**spec, "_ss_baseline_out": ev.get("baseline_out", "")}
    nth, wall = no_think_for(spec, "single"), failfast_wall(spec, int(spec.get("fix_wall", 900)))
    tf = spec.get("test_file")
    base_texts = {k: v for k, v in base_texts.items() if k != tf}  # the frozen test is never swapped (None would delete it); precheck also rejects tf in allowed
    tries, best = 1 + int(spec.get("single_shot_retries", 2)), max(1, int(spec.get("best_of", 1)))
    last_out = last_code = reasons = None
    notes = []
    for att in range(1, tries + 1):
        green_here = False
        for k in range(1, best + 1):
            left = deadline - _now()
            if left < 45:
                notes.append("no time left")
                break
            swap_texts(cwd, base_texts)  # always start from the baseline function: the line span is stable
            res["attempts"] += 1
            msgs = [{"role": "user", "content": ss_prompt(spec, tgt, cwd, task_md, test_text, last_out, last_code, reasons)}]
            t0 = time.time()
            r = call(model, local, msgs, nth, min(left, wall))
            u = r.get("usage") or {}
            if u:
                usage.rows.append((int(time.time()), int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))))
                usage.secs += max(time.time() - t0, 0)
            tag = f"single-shot {att}/{tries}" + (f" sample {k}/{best}" if best > 1 else "")
            if r.get("error"):
                reasons, last_code, last_out = [f"model call failed: {r['error']}"], None, None
                print(f"[mini-runner] {tag}: {reasons[0]}", flush=True)
                continue
            res["final"] = (r.get("content") or "")[-2000:]
            print(f"[mini-runner] {tag}: {u.get('prompt_tokens', '?')} in / {u.get('completion_tokens', '?')} out, {r.get('secs', '?')}s, finish={r.get('finish_reason')}", flush=True)
            try:
                if r.get("finish_reason") == "length":
                    raise ValueError("the reply was cut off at the token limit before the code block finished; think less and answer with the one code block")
                code = ss_extract(r.get("content"), f, fn)
                extra = ss_toplevel_extra(code, f, fn)
                if extra:
                    raise NeedsMore(f"the block also defines {extra}: the fix needs more than this function")
            except NeedsMore as e:
                res["fallback"] = f"needs more than one function: {e}"
                print(f"[mini-runner] {tag}: {res['fallback']}; mini loop", flush=True)
                swap_texts(cwd, base_texts)
                return res
            except ValueError as e:
                reasons, last_code, last_out = [str(e)], (r.get("content") or "")[-1500:], None
                print(f"[mini-runner] {tag}: rejected: {reasons[0]}", flush=True)
                continue
            Path(cwd, f).write_text(ss_splice(base_texts[f], a, b, code, f))
            bad = parse_check(cwd, f)
            if bad:
                reasons, last_code, last_out = [f"the file no longer parses after splicing your function: {bad}"], code, None
                print(f"[mini-runner] {tag}: rejected: {reasons[0]}", flush=True)
                continue
            rc, out, _ = run_cmd(spec["test_cmd"], cwd, spec)  # quick red check; the full gates only run on a green test
            if rc != 0:
                reasons, last_code, last_out = [f"test_green: the acceptance test still fails (rc={rc})"], code, out
                print(f"[mini-runner] {tag}: {reasons[0]}", flush=True)
                continue
            gates, out = fix_gate(cwd, spec, allowed, base_texts, test_sha, base_tests, base_suite, base_tc, "", pre)
            red = [f"{g}: {d}" for g, (ok, d) in gates.items() if not ok]
            if not red:
                res.update(green=True, gates=gates, out=out)
                print(f"[mini-runner] {tag}: phase B gate GREEN", flush=True)
                return res
            reasons, last_code, last_out = red, code, out
            res["gates"], res["out"] = gates, out
            print(f"[mini-runner] {tag}: phase B gate RED: {' | '.join(red)}", flush=True)
        if notes:
            break
    swap_texts(cwd, base_texts)
    res["fallback"] = f"{res['attempts']} single-shot attempt(s) failed" + (f" ({notes[0]})" if notes else "") + (f"; last: {reasons[0][:160]}" if reasons else "")
    # res["gates"]/["out"] keep the last RED gates (if a candidate got that far): with no time for the mini loop they are the honest result
    print(f"[mini-runner] single-shot: {res['fallback']}; mini loop", flush=True)
    return res


# ---- mutation-score gate (spec mutation_gate, after Phase B) ------------------------------------------------------------
# Optional, default off. After every Phase B gate is green, mutate ONLY the target function's lines in a throwaway copy of the work
# dir (StrykerJS `--mutate file:L1-L2` through its command runner; mutmut 3 selecting the function's mutants for Python) and score how
# many mutants the frozen test kills. The score goes in RESULT; `fail_below` turns a low score into a FAIL gate, `warn_below` only warns.
# A missing tool or an unparsable run is "skipped" (not a failure) unless `required` is set. Nothing is installed by the runner.
#   "mutation_gate": {"warn_below": 0.6, "fail_below": 0.4, "timeout": 600, "min_mutants": 3, "required": false,
#                     "tool": "auto"|"stryker"|"mutmut", "cmd": "<custom command>", "format": "stryker"|"mutmut"|"line"}
# `cmd` (tests, other tools) runs in the copy with {file} {first} {last} {fn} {test_cmd} {report} substituted and MUT_* in the env; `format`
# says how its output is read: a Stryker mutation.json at {report}, mutmut `results` text on stdout, or one `MUTATION killed=K survived=S ...` line.

MUT_DETECTED = ("killed", "timeout")
MUT_VALID = MUT_DETECTED + ("survived", "nocoverage", "suspicious")
MUT_SURV_CAP = 8  # survivors named in the gate detail (and so in the packets)  # compile/runtime errors and ignored mutants are not counted


def mut_counts_stryker(report):
    """{status: n} from a Stryker mutation-testing-elements report (files -> mutants -> status)."""
    c = {}
    for f in (report.get("files") or {}).values():
        for m in f.get("mutants") or []:
            k = str(m.get("status", "")).lower()
            c[k] = c.get(k, 0) + 1
    return c


def mutmut_name_re(fns):
    """Regex for mutant names of the given functions in mutmut 3 style: module-level `pkg.mod.x_fn__mutmut_N` and methods
    `pkg.mod.xǁClassǁfn__mutmut_N` (UNVERIFIED against a real mutmut: not installed here; see mut_selector_default)."""
    alt = "|".join(re.escape(f) for f in fns)
    return re.compile(rf"(?:^|\.)(?:x_(?:{alt})|xǁ\w+ǁ(?:{alt}))__mutmut_\d+$")


def mut_counts_mutmut(text, fns=None):
    """{status: n} from `mutmut results` lines like `    pkg.mod.x_fn__mutmut_3: survived` (killed ones appear with --all true).
    With `fns`, only mutants of those functions count. Mutants mutmut did not run ("not checked") are not valid mutants and stay out of the score."""
    c, want = {}, (mutmut_name_re(fns) if fns else None)
    for ln in text.splitlines():
        m = re.match(r"\s*([\w.]+__mutmut_\d+):\s*([\w ]+?)\s*$", ln)
        if m and (want is None or want.search(m.group(1))):
            k = m.group(2).lower().replace(" ", "")
            k = {"nottested": "nocoverage", "notests": "nocoverage", "notchecked": "unselected"}.get(k, k)
            c[k] = c.get(k, 0) + 1
    return c


def mut_selector_default(f0, fn):
    """Best guess at the `mutmut run` selector for fn in file f0 (mutmut 3 drops a leading `src.` from the module name; methods are
    `xǁClassǁmethod`). Only a guess: the gate uses it only when spec mutation_gate.mutmut_selector sets it explicitly."""
    mod = Path(f0).with_suffix("").as_posix().replace("/", ".")
    mod = mod[4:] if mod.startswith("src.") else mod
    return [f"{mod}.x_{fn}__mutmut_*", f"{mod}.xǁ*ǁ{fn}__mutmut_*"]


def mut_counts_line(text):
    for ln in reversed(text.splitlines()):
        if ln.startswith("MUTATION "):
            return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", ln)}
    return {}


def mut_score(c):
    """(score or None, detected, valid)."""
    valid = sum(c.get(k, 0) for k in MUT_VALID)
    det = sum(c.get(k, 0) for k in MUT_DETECTED)
    return (det / valid if valid else None), det, valid


def mut_verdict(c, cfg):
    """(ok, detail, score) for counts and a mutation_gate config: fail_below fails, warn_below and too few mutants only warn."""
    score, det, valid = mut_score(c)
    if score is None or valid < int(cfg.get("min_mutants", 3)):
        return True, f"WARN too few mutants to judge ({valid} valid, need {cfg.get('min_mutants', 3)}): {dict(sorted(c.items()))}", score
    d = f"score={score:.2f} ({det}/{valid} killed; {dict(sorted(c.items()))})"
    fb, wb = cfg.get("fail_below"), cfg.get("warn_below", 0.6)
    if fb is not None and score < float(fb):
        return False, d + f" < fail_below {fb}: the test does not pin the function's behaviour", score
    if wb is not None and score < float(wb):
        return True, "WARN " + d + f" < warn_below {wb}", score
    return True, d, score


def mut_tool_bin(tool, cwd, files):
    """Path of the mutation tool, or None. Env override MINI_STRYKER_BIN / MINI_MUTMUT_BIN, then node_modules/.bin/stryker walking up from
    the target files to cwd, a .venv in cwd, then PATH. Never npx (it would download)."""
    envv = os.environ.get("MINI_STRYKER_BIN" if tool == "stryker" else "MINI_MUTMUT_BIN")
    if envv:
        return envv if os.access(envv, os.X_OK) else None
    root = Path(cwd).resolve()
    if tool == "stryker":
        for f in files:
            d = (root / f).parent
            while True:
                if os.access(d / "node_modules/.bin/stryker", os.X_OK):
                    return str(d / "node_modules/.bin/stryker")
                if d == root or root not in d.parents:
                    break
                d = d.parent
        return shutil.which("stryker")
    for cand in (root / ".venv/bin/mutmut",):
        if os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which("mutmut")


def mut_targets(cwd, spec, allowed):
    """[(file, fn, first, last)] for every target_fn with a known span; ([], reason) pieces for the rest."""
    out, bad = [], []
    for fn in as_list(spec.get("target_fn")):
        for f in allowed:
            p = Path(cwd, f)
            d = functions(f, p.read_text(errors="replace")).get(fn) if p.is_file() and lang_of(f) else None
            if d:
                if d[1] is None:
                    bad.append(f"end of `{fn}` unknown")
                else:
                    out.append((f, fn, d[0], d[1]))
                break
        else:
            bad.append(f"`{fn}` not found")
    return out, bad


def _clone_tree(src, dst):
    if sys.platform == "darwin" and subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True).returncode == 0:
        return
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)


def run_mutation_gate(cwd, spec, allowed, budget):
    """-> (ok, detail, score|None). Runs in a throwaway clone of cwd; cwd itself is never written."""
    cfg = spec.get("mutation_gate")
    cfg = {} if cfg is True else cfg
    tgts, bad = mut_targets(cwd, spec, allowed)
    if not tgts:
        return not cfg.get("required"), "skipped: no target_fn span to mutate (" + "; ".join(bad or ["no target_fn"]) + ")", None
    tmo = int(min(cfg.get("timeout", 600), budget))
    if tmo < 30:
        return not cfg.get("required"), "skipped: no time left", None
    tool = cfg.get("tool", "auto")
    py = lang_of(tgts[0][0]) == "py"
    tool = ("mutmut" if py else "stryker") if tool == "auto" else tool
    clone = Path(tempfile.mkdtemp(prefix="mini-mut-")) / "w"
    try:
        _clone_tree(cwd, clone)
        rep = clone / ".mini-mut-report.json"
        fmt, argv, files = cfg.get("format"), None, [t[0] for t in tgts]
        env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}, "PYTHONDONTWRITEBYTECODE": "1", "MUT_FILE": tgts[0][0], "MUT_FIRST": str(tgts[0][2]), "MUT_LAST": str(tgts[0][3]), "MUT_FN": tgts[0][1], "MUT_REPORT": str(rep)}
        env.pop("OPENAI_API_KEY", None)
        if cfg.get("cmd"):
            cmd = cfg["cmd"].format(file=shlex.quote(tgts[0][0]), first=tgts[0][2], last=tgts[0][3], fn=shlex.quote(tgts[0][1]),
                                    test_cmd=shlex.quote(spec["test_cmd"]), report=shlex.quote(str(rep)))
            fmt = fmt or "line"
        elif tool == "stryker":
            binp = mut_tool_bin("stryker", cwd, files)
            if not binp:
                return not cfg.get("required"), "skipped: StrykerJS not installed (looked for node_modules/.bin/stryker and PATH; install it in the repo's devDependencies to use the gate)", None
            conf = clone / ".mini-stryker.json"
            conf.write_text(json.dumps({"testRunner": "command", "commandRunner": {"command": spec["test_cmd"]}, "coverageAnalysis": "off",
                                        "mutate": [f"{f}:{a}-{b}" for f, _, a, b in tgts], "reporters": ["json"],
                                        "jsonReporter": {"fileName": str(rep.relative_to(clone))}, "concurrency": 2, "timeoutMS": 20000,
                                        "tempDirName": ".stryker-tmp", "cleanTempDir": True, "ignorePatterns": [".git"]}))
            cmd, fmt = f"{shlex.quote(binp)} run {shlex.quote(str(conf))}", "stryker"
        elif tool == "mutmut":
            binp = mut_tool_bin("mutmut", cwd, files)
            if not binp:
                return not cfg.get("required"), "skipped: mutmut (3.x) not installed (looked for .venv/bin/mutmut and PATH)", None
            f0 = tgts[0][0]
            if cfg.get("mutmut_mode") == "indir":
                # Verified against mutmut 3.8.1 (2026-10-31): mutmut's module key is the path relative to ITS cwd, and tests usually import the
                # module by bare name from the directory they run in. So run mutmut in the target file's own directory with a generated
                # setup.cfg (source_paths = the one file, also_copy = ".", test selection = the frozen test file) and keys `<stem>.x_<fn>__mutmut_N`.
                # mutmut runs pytest in ITS venv, so the repo's pytest-visible deps must be importable there. mutmut_pythonpath (repo-relative dirs) is
                # added to PYTHONPATH as absolute paths in the clone.
                mdir = (clone / f0).parent
                tf = Path(spec["test_file"])
                try:
                    tsel = (clone / tf).relative_to(mdir).as_posix()
                except ValueError:
                    return not cfg.get("required"), f"skipped: mutmut_mode indir needs the test file under {mdir.relative_to(clone).as_posix() or '.'}", None
                (mdir / "setup.cfg").write_text(f"[mutmut]\nsource_paths={Path(f0).name}\nalso_copy=\n    .\npytest_add_cli_args_test_selection={tsel}\n")
                pp = [str(clone / d) for d in (cfg.get("mutmut_pythonpath") or [])] + [str(mdir)]
                env["PYTHONPATH"] = os.pathsep.join(pp + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
                mod = Path(f0).stem
                sel = " ".join(shlex.quote(f"{mod}.x_{fn}__mutmut_*") for _, fn, _, _ in tgts)
                cmd, fmt = f"cd {shlex.quote(str(mdir))} && {shlex.quote(binp)} run {sel} && {shlex.quote(binp)} results --all true", "mutmut"
            else:
                if not cfg.get("mutmut_selector"):
                    return not cfg.get("required"), ("skipped: mutmut selector unverified (mutmut 3 is not installed here, so its mutant naming was never checked); set "
                                                     "mutation_gate.mutmut_selector, e.g. " + repr(" ".join(mut_selector_default(f0, tgts[0][1])))), None
                cfgp = clone / "setup.cfg"
                if "[mutmut]" not in (cfgp.read_text() if cfgp.exists() else ""):
                    with open(cfgp, "a") as fh:
                        fh.write(f"\n[mutmut]\npaths_to_mutate={f0}\ntests_dir={Path(spec['test_file']).parent.as_posix() or '.'}\n")
                mod = Path(f0).with_suffix("").as_posix().replace("/", ".")
                sel = " ".join(shlex.quote(cfg["mutmut_selector"].format(mod=mod, fn=fn)) for _, fn, _, _ in tgts)
                cmd, fmt = f"{shlex.quote(binp)} run {sel} && {shlex.quote(binp)} results --all true", "mutmut"
        else:
            return not cfg.get("required"), f"skipped: unknown mutation tool {tool!r}", None
        r = _bash(cmd, str(clone), env, tmo)
        out = r[1] if isinstance(r, tuple) and len(r) > 1 else ""
        if fmt == "stryker":
            counts = mut_counts_stryker(json.loads(rep.read_text())) if rep.is_file() else {}
        elif fmt == "mutmut":
            counts = mut_counts_mutmut(out, [t[1] for t in tgts])
        else:
            counts = mut_counts_line(out)
        if not counts:
            return not cfg.get("required"), f"skipped: {tool} produced no readable result (rc={r[0] if isinstance(r, tuple) else '?'}): {out.strip()[-200:]}", None
        ok, detail, score = mut_verdict(counts, cfg)
        surv = [ln.strip()[len("survived "):] for ln in out.splitlines() if ln.startswith("survived ")] if fmt == "line" else []
        if surv:  # mini_mutate names each survivor: the packets show what the test leaves open (" ;; " joins, no "|" for the table)
            detail += "; survivors: " + " ;; ".join(x.replace("|", "/")[:160] for x in surv[:MUT_SURV_CAP]) + (
                f" ;; (+{len(surv) - MUT_SURV_CAP} more)" if len(surv) > MUT_SURV_CAP else "")
        return ok, f"{'custom' if cfg.get('cmd') else tool} on {', '.join(f'{f}:{a}-{b}' for f, _, a, b in tgts)}: {detail}", score
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return not cfg.get("required"), f"skipped: {type(e).__name__}: {str(e)[:160]}", None
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)


def write_review(path, spec, allowed, ev, gates, test_text, diff, final_out, result, final_msg="", ctx=(), rejected=None, task_text="",
                 fallback=None):
    """fallback: the direct-fix fallback's section text (stage 22): it goes first, and the evidence and gate headings say whose."""
    rows = "\n".join(f"| {k} | {'ok' if ok else 'FAIL'} | {d} |" for k, (ok, d) in gates.items())
    head = f"# REVIEW packet: {result}\n\nallowed: {allowed}\ntest: `{spec['test_file']}`  cmd: `{spec['test_cmd']}`\n\n"
    if fallback:
        head += "## Direct-fix fallback (read first)\n\n" + fallback + "\n\n"
    ev_title = "Baseline evidence (the fallback's test, allowed files at baseline)" if fallback else "Phase A evidence (test at baseline)"
    gates_title = "Fallback gates (test and fix from one run)" if fallback else "Phase B gates"
    if rejected:  # phase A never produced an acceptable test
        head += "## Phase A REJECTED (no fix attempted)\n\nGate(s) that failed on the last attempt:\n\n" + "".join(f"- {r}\n" for r in rejected) + "\n"
    musts = [ln.strip() for ln in task_text.splitlines()
             if re.match(r"\s*(?:[-*]\s+)?Must keep\s*:", ln, re.I)]
    sec = re.search(r"(Behaviou?r that must NOT change:?.*?)(?=\n##\s|\Z)", task_text, re.S | re.I)
    if sec or musts:
        head = (head.split("\n\n", 1)[0] + "\n\n## Constraints from TASK.md (check the diff against each)\n\n"
                + (sec.group(1).strip() + "\n\n" if sec else "") + "".join(f"{m}\n" for m in musts) + "\n\n"
                + head.split("\n\n", 1)[1])
    Path(path).write_text(
        head + f"## {ev_title}\n\nrc={ev.get('baseline_rc')} tests={ev.get('tests')} "
        f"negative_control_rc={ev.get('negative_control_rc')} target functions that ran: {ev.get('reach')} "
        f"body-line coverage: {ev.get('fn_cov')}"
        + (f" (coverage floor waived: {'; '.join(f'`{w}`: {v}' for w, v in ev['cov_waived'].items())})" if ev.get("cov_waived") else "")
        + "\n\n"
        f"```\n{(ev.get('baseline_out') or '')[-1500:]}\n```\n\n"
        + ("## Test expectations found in neither TASK.md nor the code (accepted on phase A's last attempt: check each against "
           "the Problem; one it does not ask for is a reject)\n\n" + "".join(f"- {u}\n" for u in ev["ungrounded"]) + "\n"
           if ev.get("ungrounded") else "")
        + ("## No test goes through the public API that TASK.md's example uses (accepted on phase A's last attempt: check that "
           "the fix changes what the example shows, not only the internal code the tests call)\n\n"
           + "".join(f"- {u}\n" for u in ev["user_view"]) + "\n" if ev.get("user_view") else "")
        + (f"## {gates_title}\n\n| gate | | detail |\n|---|---|---|\n{rows}\n\n" if gates else "")
        + f"## Test file{' (rejected)' if rejected else ''}\n\n```\n{test_text}\n```\n\n"
        + (f"## Diff (allowed files)\n\n```diff\n{diff}\n```\n\n## Final test output (tail)\n\n```\n{final_out[-1500:]}\n```\n\n" if not rejected else "")
        + f"## Model's final message\n\n```\n{final_msg or '(none)'}\n```\n\n"
        + ("## Context per run\n\n" + "".join(f"- {c}\n" for c in ctx) + "\n" if ctx else "")
        + "Review asks: does the test encode TASK.md (not a weaker reading)? does the diff do only what TASK.md asks?\n")


def in_browser_only(cwd, allowed, fns):
    """target_fn names that only ever run inside page.evaluate(): node coverage can never see them."""
    src = "\n".join(Path(cwd, f).read_text() for f in allowed if Path(cwd, f).is_file() and f.endswith((".js", ".ts", ".mjs", ".cjs")))
    out = []
    for fn in fns:
        uses = [m for m in re.finditer(rf"\b{re.escape(fn)}\b", src)
                if not re.search(r"(function\s*\*?\s*|(?:const|let|var)\s+)$", src[max(0, m.start() - 20):m.start()])
                and not re.match(r"\s*=\s*(?:async\s*)?(?:function|\()", src[m.end():m.end() + 20])]
        if uses and all(re.search(r"(?:evaluate|evaluateAll|evaluateHandle|\$eval|\$\$eval)\(\s*$", src[max(0, m.start() - 40):m.start()]) for m in uses):
            out.append(fn)
    return out


def resolve_task(cwd, spec):
    """TASK.md text for a chain step (None = leave the file prepare() produced): the step's own task, plus a one-paragraph
    summary of the steps already done in this working tree."""
    t = spec.get("_task_text")
    rel = spec.get("_task_rel")
    if t is None and rel and rel != "TASK.md":
        if not Path(cwd, rel).is_file():
            raise ValueError(f"task file {rel} not found in the working tree")
        t = Path(cwd, rel).read_text()
    if t is None and not spec.get("_prev_summary"):
        return None
    if t is None:
        t = Path(cwd, "TASK.md").read_text()
    if spec.get("_prev_summary"):
        t = t.rstrip() + "\n\n## Already done in earlier steps\n\n" + spec["_prev_summary"] + "\n"
    return t


def failfast_wall(spec, wall):
    """Lever 4a (OFF by default): with MINI_FAILFAST_CAP=1 the Phase B wall limit is min(wall, cap) where cap = 2 x the PASS p90 of the spec's
    fix_steps bucket, read from MINI_FAILFAST_CAPS_FILE (default ~/.ollama-dispatch/failfast-caps.json, written by `mini-scorecard caps --write`).
    No file, no bucket or the flag unset/anything else: returns wall unchanged. Never below 120s."""
    if os.environ.get("MINI_FAILFAST_CAP") != "1":
        return wall
    try:
        tab = json.loads(Path(os.path.expanduser(os.environ.get("MINI_FAILFAST_CAPS_FILE") or "~/.ollama-dispatch/failfast-caps.json")).read_text())
        fs = int(spec.get("fix_steps") or 0)
        cap = tab["buckets"][str(((fs + 19) // 20) * 20)]["cap_s"]
        return int(min(wall, max(120, cap)))
    except (OSError, ValueError, KeyError, TypeError):
        return wall


def testdraft_accept(spec, cwd, tf, run_dir, tag=None):
    """Lever 1: save the drafted test beside the run and run spec['draft_accept_cmd'] ({draft} is replaced by its path).
    rc 0 = ACCEPT. Returns (rc, RESULT line). PASS here means 'draft accepted', nothing else; the draft is never landed by the runner.
    tag (the attempt, e.g. A2): also keep this attempt's draft and check output under that name."""
    cmd = spec.get("draft_accept_cmd")
    keep = Path(run_dir) / ("draft_" + Path(tf).name)
    shutil.copy2(Path(cwd, tf), keep)
    if not cmd:
        return RC_FAIL, "FAIL mode=test-draft bad-spec no draft_accept_cmd"
    try:
        p = subprocess.run(cmd.replace("{draft}", shlex.quote(str(keep))), shell=True, capture_output=True, text=True, timeout=3600,
                           env={**os.environ, "PATH": os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + os.environ.get("PATH", "")})
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "TIMEOUT", 124
    (Path(run_dir) / "DRAFT-CHECK.txt").write_text(out)
    if tag:
        (Path(run_dir) / f"DRAFT-CHECK.{tag}.txt").write_text(out)
        shutil.copy2(keep, Path(run_dir) / f"draft_{tag}_{Path(tf).name}")
    head = re.sub(r"\n--- draft vs ref:.*?\n--- end ---", "", out, flags=re.S)  # the test's own output is not a verdict line
    tail = " | ".join(ln.strip() for ln in head.splitlines() if re.search(r"mutation:|V\d|ACCEPT|REJECT|REFUSED", ln))[:400]
    # indented: the check output carries the draft's own test output, which must never pass for a RESULT line
    print("[mini-runner] draft check:\n" + textwrap.indent(out[-4000:], "  | "), flush=True)
    return (RC_OK, f"PASS mode=test-draft accepted draft={keep} {tail}") if rc == 0 else (RC_FAIL, f"FAIL mode=test-draft rejected rc={rc} draft={keep} {tail}")


def draft_feedback(out):
    """mini-testdraft check output -> (items for the next drafting attempt's prompt, retry). The test's own output with the ref
    applied goes last. retry is False when nothing the model can change failed (a refused spec, a check timeout, a tool problem)."""
    if re.search(r"^(REFUSED|TIMEOUT$|no draft file)", out, re.M):
        return [out.strip()[-300:]], False
    v = {m.group(1): (m.group(2) == "ok", m.group(3).strip()) for m in re.finditer(r"^\s+s\d+ (V\d) [^:]*: (ok|FAIL) ?(.*)$", out, re.M)}
    mm = re.search(r"^\s+mutation: (ok|FAIL) ?(.*)$", out, re.M)
    blk = re.search(r"^--- draft vs ref: test output \(tail, rc=([^)]*)\) ---\n(.*?)\n--- end ---$", out, re.S | re.M)
    bad = lambda k: k in v and not v[k][0]
    mdet = mm.group(2) if mm else ""
    ref_red = (bad("V4") and not re.match(r"(skipped|no reference)", v["V4"][1])) or "fails on the unmutated ref" in mdet
    items, retry = [], False
    for k, what in (("V2", "static rules"), ("V3", "red for the right reason")):
        if bad(k):
            items.append(f"{what}: {v[k][1][:300]}")
            retry = True
    v6 = v["V6"][1] if bad("V6") else ""
    nt = re.search(r"(\d+) tests \(need 2\.\.(\d+)\)", v6)
    if re.search(r"changed ref lines >|fix_steps formula \d+ >|spec fix_steps \d+ <", v6):  # the ref's size: no draft passes V6
        items.append(f"size (the reference fix's, not something the test can change): {v6[:300]}")
    elif nt and int(nt.group(1)) > int(nt.group(2)):
        # cutting tests to fit dropped assertions (0.982 -> 0.702 mutation on settings-profile-post): merge them instead
        items.append(f"too many test functions ({nt.group(1)}, at most {nt.group(2)}): merge related tests into one function each and "
                     "keep EVERY assertion; dropping assertions weakens the test and the mutation check will reject it")
        retry = True
    elif nt:
        items.append(f"too few test functions ({nt.group(1)}): split the checks into at least 2 test functions")
        retry = True
    elif v6:
        items.append(f"size: {v6[:300]}")
    if ref_red:
        items.append("with the reference fix applied the test still FAILS, and it must PASS there: it expects something that fix does "
                     "not do (a function, name, value or message the reference diff in TASK.md never produces). Assert only what that "
                     "diff makes true")
        retry = True
    if bad("V5"):
        items.append("the test does not need every part of the reference fix: " + v["V5"][1][:400]
                     + ". Add an assertion that fails whenever that part is missing")
        retry = True
    if bad("V7") and not ref_red:
        items.append(f"the tests fail when run in reverse order ({v['V7'][1][:200]}): each test must set up its own state and never "
                     "rely on an earlier test")
        retry = True
    if mm and mm.group(1) == "FAIL" and not ref_red:
        if "canary survived" in mdet:
            items.append(f"the test never loads the code from the directory it runs in ({mdet[:200]}): import it relative to the test file")
            retry = True
        elif "score=" in mdet:
            items.append(f"assertions too weak: {mdet[:600]}. Each survivor is a change to the fixed code that no assertion noticed: "
                         "assert the exact values those lines produce")
            retry = True
        else:
            items.append("mutation check: " + mdet[:300])
    if bad("V1"):
        items.append("precheck: " + v["V1"][1][:300])
    if blk:
        items.append(f"your test's own output with the reference fix applied (rc={blk.group(1)}):\n{blk.group(2)[-1800:]}")
    if not items:
        items.append("the acceptance check rejected the test: " + out.strip()[-400:])
    return items, retry


def review_result(ok, summary, trip, soft=None):
    """Build the REVIEW header string, appending trip reason when kill was fail-fast. soft: the failing SOFT_GATES of a
    needs-review pass (final_result's verdict), so the packet does not say FAIL for a run the queue counts as passed."""
    if soft:
        return f"PASS needs-review soft={','.join(soft)} {summary}"
    base = f"{"PASS" if ok else "FAIL"} {summary}"
    if not ok and isinstance(trip, str) and trip.startswith("killed by fail-fast"):
        return base + " " + trip
    return base


STEP_REFS = "step-refs"   # run_dir/step-refs/s<N>/: a passing chain step's files, what `mini-finish` replays in a continuation
REF_MANIFEST = "MANIFEST.json"


def save_step_ref(cwd, dest, files):
    """Copy a passing chain step's files (allowed + update_tests + its test) to dest/<path>; files the step deleted are listed in
    dest/MANIFEST.json so a replay deletes them too. Best effort: a copy error is printed, never fails the run."""
    try:
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        have, gone = [], []
        for f in dict.fromkeys(files):
            if Path(cwd, f).is_file():
                (dest / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(cwd, f), dest / f)
                have.append(f)
            else:
                gone.append(f)
        (dest / REF_MANIFEST).write_text(json.dumps({"files": have, "deleted": gone}))
    except OSError as e:
        print(f"[mini-runner] could not save the step ref {dest}: {e}", flush=True)


def ref_manifest(sd):
    """{'files': [...], 'deleted': [...]} of a saved step ref, {} for a hand-written ref dir (no manifest)."""
    try:
        m = json.loads(Path(sd, REF_MANIFEST).read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def replay_dir(spec, chain):
    """The refs dir when this chain step is REPLAYED (spec `replay`: {"refs": DIR, "through": K}, written by `mini-finish` for a
    continuation): steps 1..K apply DIR/s<N> and run the real phase-B gates with no model call. None otherwise."""
    rp = spec.get("replay")
    if chain is None or not isinstance(rp, dict) or not rp.get("refs"):
        return None
    try:
        through = int(rp.get("through") or 0)
    except (TypeError, ValueError):
        return None
    return str(Path(os.path.expanduser(str(rp["refs"])))) if chain["n"] <= through else None


def step_diff(cwd, spec, allowed, base_texts):
    """Unified diff of the allowed files against where this step started (a later chain step) or HEAD, plus new files in full."""
    if spec.get("_step_base") is not None:  # later chain step: the diff against where THIS step started
        diff = "".join("".join(difflib.unified_diff((base_texts[f] or "").splitlines(True), (Path(cwd, f).read_text() if Path(cwd, f).is_file() else "").splitlines(True),
                                                    f"a/{f}", f"b/{f}")) for f in allowed)
    else:
        diff = git(cwd, "diff", "--", *allowed)
    return diff + "".join(
        f"\n+++ new file {f}\n{Path(cwd, f).read_text()}" for f in allowed if base_texts[f] is None and Path(cwd, f).exists())


# ---- baselines: the suite and typecheck runs a fix is judged against, run beside phase A ----------------------------------------
# operator 2026-11-06: "any way to speed up the gates so we're not waiting on them after a gpu run?" The baseline suite and typecheck ran
# in the foreground before phase A's first model call (conan1r2's and scrapyr1's whole suites timed out at 600 s each, optuna's took
# 240 s), and every phase-B gate ran the whole suite again. Now the baselines run in a background thread on a copy of the work dir
# made before anything is written, and phase B waits for them only at its first gate (Baselines; base_ready in _two_phase). A whole
# suite that times out at baseline is replaced by spec suite_narrow when there is one (auto: the existing tests nearest the change),
# so a gate run checks something in minutes instead of timing out again. A slow suite (SUITE_SLOW_S at baseline) is skipped on an
# attempt whose own test is red, and the phase-B prompt tells the model not to run it itself (suite_gates, phase_b_prompt).
SUITE_SLOW_S = 120          # seconds at baseline
AUTO_NARROW_MAX = 40        # test files in auto's suite_narrow
BG_COPY_MAX_FILES = 100000  # a bigger work dir runs its baselines in the foreground
BG_COPY_MAX_BYTES = 2 << 30


def _names_path(text, roots):
    """True when text names one of the paths in roots (a whole path component, not a prefix of a longer name)."""
    return any(r and re.search(re.escape(r) + r"(?![\w.-])", text) for r in roots)


def _links_into(cwd):
    """Why a copy of cwd would still load cwd's own code, or None: an editable install in cwd/.venv that names cwd (a .pth,
    .egg-link or __editable__ finder), or a node_modules link into cwd outside node_modules (a workspace package)."""
    roots = {str(cwd), os.path.realpath(cwd), str(Path(cwd).resolve())}
    for sp in sorted(Path(cwd).glob(".venv/lib/python3*/site-packages")):
        for p in sorted([*sp.glob("*.pth"), *sp.glob("*.egg-link"), *sp.glob("__editable__*.py")]):
            try:
                t = p.read_text(errors="replace")
            except OSError:
                continue
            if _names_path(t, roots):
                return f"the editable install in .venv ({p.name}) loads the work dir itself"
    nm = Path(cwd, "node_modules")
    if nm.is_dir():
        real, inner = os.path.realpath(cwd), os.path.realpath(nm)
        try:
            ents = [e for d in [nm, *(x for x in nm.iterdir() if x.name.startswith("@") and x.is_dir())] for e in d.iterdir()]
        except OSError:
            return None
        for e in ents:
            if e.is_symlink():
                r = os.path.realpath(e) + os.sep
                if r.startswith(real + os.sep) and not r.startswith(inner + os.sep):
                    return f"node_modules/{e.relative_to(nm)} links into the work dir (a workspace package)"
    return None


def bg_reason(cwd, spec, check_only=False, ref_mode=None):
    """Why the baselines must run in the foreground, in the work dir itself, or None: they run on a copy, beside phase A."""
    if check_only or ref_mode:
        return "a model-free check"
    if contain_runs(spec):
        return "the runs are contained (a drafted test)"
    if not (spec.get("suite_cmd") or spec.get("typecheck_cmd")):
        return "no suite or typecheck command"
    if spec.get("bg_baseline", True) is False:
        return "spec bg_baseline is false"
    if Path(cwd, ".git").is_file():
        return "the work dir is a git worktree (its .git file names it)"
    roots, name = {str(cwd), os.path.realpath(cwd), str(Path(cwd).resolve())}, Path(cwd).name
    for t in [str(spec.get(k) or "") for k in ("suite_cmd", "typecheck_cmd", "suite_narrow")] + [str(v) for v in (spec.get("env") or {}).values()]:
        if _names_path(t, roots):
            return "a command or env value names the work dir's absolute path"
        if re.search(rf"\.\./{re.escape(name)}(?![\w.-])", t):
            return f"a command or env value names ../{name}"
    return _links_into(cwd)


def _unalias(copy, cwd):
    """str -> str that puts cwd's paths back where the copy's are (same length: a cut summary line is cut at the same place)."""
    pairs = sorted({(str(copy), str(cwd)), (os.path.realpath(copy), os.path.realpath(cwd))}, key=lambda p: -len(p[0]))

    def f(s):
        for x, y in pairs:
            s = s.replace(x, y)
        return s
    return f


def run_baselines(cwd, spec, on_start=None, stop=None, live=None, alias=None):
    """The baseline suite_cmd and typecheck_cmd runs in cwd -> {key: (rc, failure lines, seconds)}, plus "_narrow" (the same for
    spec suite_narrow) when the suite timed out and suite_narrow is a different command. alias: the work dir a copy stands for (its
    paths are put back in the output first, so a red line naming a path matches the gate's run in the work dir). Prints nothing (it
    may run in the background); live (a dict) is filled as each command ends; stop (an Event) ends it between commands."""
    res = live if live is not None else {}
    timeout = int(spec.get("cmd_timeout", 300)) * 2
    un = _unalias(cwd, alias) if alias is not None else (lambda s: s)
    halted = lambda: stop is not None and stop.is_set()
    for key in ("suite_cmd", "typecheck_cmd"):
        if halted():
            break
        if not spec.get(key):
            continue
        t0 = time.time()
        rc, out, _ = run_cmd(spec[key], cwd, spec, timeout=timeout, on_start=on_start)
        res[key] = (rc, fail_lines(rc, un(out)), time.time() - t0)
        nar = str(spec.get("suite_narrow") or "").strip()
        if key == "suite_cmd" and rc == 124 and nar and nar != str(spec[key]).strip() and not halted():
            t0 = time.time()
            rc2, out2, _ = run_cmd(nar, cwd, spec, timeout=timeout, on_start=on_start)
            res["_narrow"] = (rc2, fail_lines(rc2, un(out2)), time.time() - t0)
    return dict(res)


def apply_baselines(spec, res):
    """Print run_baselines' results as the foreground baselines always have (mini-validate's suite_red_at_baseline reads the
    `baseline suite_cmd: rc=N failure lines=N` line and the `baseline red:` line under it), put suite_narrow in suite_cmd when the
    whole suite timed out and the narrow run did not (the whole one stays in spec _suite_full), and set spec _suite_secs/_suite_rc.
    -> (base_suite, base_tc)."""
    out = {"suite_cmd": set(), "typecheck_cmd": set()}
    nar = res.get("_narrow")
    for key in ("suite_cmd", "typecheck_cmd"):
        if key not in res:
            continue
        rc, fl, secs = res[key]
        if key == "suite_cmd" and nar is not None:
            full, narrow = str(spec["suite_cmd"]), str(spec.get("suite_narrow") or "").strip()
            if nar[0] != 124:
                try:
                    n = max(0, len(shlex.split(narrow)) - len(shlex.split(full)))
                except ValueError:
                    n = narrow.count(".py")
                print(f"[mini-runner] baseline suite_cmd (whole suite) timed out after {secs:.0f}s: the suite gate runs the {n} existing "
                      f"test file(s) nearest the change instead: `{_short_cmd(narrow)}`", flush=True)
                spec["_suite_full"], spec["suite_cmd"] = full, narrow
                rc, fl, secs = nar
            else:
                print(f"[mini-runner] WARNING: the baseline suite timed out after {secs:.0f}s and suite_narrow timed out too "
                      f"({nar[2]:.0f}s): the suite gate keeps the whole suite", flush=True)
        print(f"[mini-runner] baseline {key}: rc={rc} failure lines={len(fl)} ({secs:.0f}s)", flush=True)
        for _l in sorted(fl)[:12]:
            print(f"[mini-runner]   baseline red: {_l}", flush=True)  # a red baseline weakens the no-new-failures gate: show it
        out[key] = fl
        if key == "suite_cmd":
            spec["_suite_secs"], spec["_suite_rc"] = secs, rc
    return out["suite_cmd"], out["typecheck_cmd"]


def _sibling(cwd):
    """A free path beside cwd whose name is as long as cwd's (work -> wor0), or None."""
    n = Path(cwd).name
    for c in "0123456789_":
        p = Path(cwd).with_name(n[:-1] + c) if n else None
        if p is not None and p.name != n and not os.path.lexists(p):
            return p
    return None


class Baselines:
    """One two-phase run's baselines, in a background thread on a copy of the work dir made when start() is called (before the
    skeleton, phase A or any edit). The copy sits beside the work dir under a name of the same length (_sibling), so the output's
    paths can be put back (run_baselines alias) without moving where pytest cuts a long summary line. .venv and node_modules dirs
    are linked to where they really are, not copied (lane_remote's walk-up import loads the copy's own source). start() -> False
    when the copy cannot be made (err says why); join() -> (results, or None when the run failed, seconds waited); stop() kills
    the run and removes the copy, and is safe to call twice."""

    def __init__(self, cwd, spec, tf):
        self.cwd, self.sp, self.copy = Path(cwd), dict(spec), _sibling(cwd)
        tp = Path(cwd, tf)
        self.tf0 = tp.read_bytes() if tp.is_file() else None  # the test as the copy has it: a fallback run in the work dir uses it
        self.stop_ev, self.live = threading.Event(), {}
        self.pid = self.res = self.err = self.th = self.t0 = None
        self.made = self.stopped = False

    def start(self):
        if self.copy is None:
            self.err = "no free name beside the work dir"
            return False
        seen, links = [0, 0], []

        def ignore(d, names):
            seen[0] += len(names)
            if seen[0] > BG_COPY_MAX_FILES:
                raise OSError(f"over {BG_COPY_MAX_FILES} files")
            skip = []
            for n in names:
                if n == "__pycache__":
                    skip.append(n)
                elif n in (".venv", "node_modules"):
                    skip.append(n)
                    links.append(Path(d, n))
                else:
                    try:
                        st = os.lstat(os.path.join(d, n))
                        seen[1] += st.st_size if stat.S_ISREG(st.st_mode) else 0
                    except OSError:
                        pass
            if seen[1] > BG_COPY_MAX_BYTES:
                raise OSError(f"over {BG_COPY_MAX_BYTES >> 30} GiB")
            return skip
        try:
            os.mkdir(self.copy)  # fails when the name was taken meanwhile: never write into (or remove) a dir this run did not make
            self.made = True
            _SCRATCH_DIRS.add(self.copy)
            shutil.copytree(self.cwd, self.copy, symlinks=True, ignore=ignore, dirs_exist_ok=True)
            for src in links:
                dst = self.copy / src.relative_to(self.cwd)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(os.path.realpath(src))
        except (OSError, shutil.Error) as e:
            self.err = f"{type(e).__name__}: {str(e)[:200]}"
            self._rm()
            return False
        self.t0 = time.time()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        return True

    def _run(self):
        try:
            self.res = run_baselines(self.copy, self.sp, on_start=self._started, stop=self.stop_ev, live=self.live, alias=self.cwd)
        except Exception as e:  # noqa: BLE001 -- a broken background run means "run them in the work dir", never a crash
            self.err = f"{type(e).__name__}: {e}"

    def _started(self, pid):
        self.pid = pid
        if self.stop_ev.is_set():  # stop() came between two commands
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

    def running(self):
        return self.th is not None and self.th.is_alive()

    def join(self):
        t = time.time()
        if self.th is not None:
            self.th.join()
        waited = time.time() - t
        res = None if (self.err or self.stop_ev.is_set() or not self.res) else self.res
        self.stop()
        return res, waited

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.stop_ev.set()
        pid = self.pid
        if pid is not None and pid in _CHILDREN:  # still running (_bash drops a finished command's pid): never a reused pid
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.th is not None:
            self.th.join(timeout=10)
        self._rm()

    def _rm(self):
        if self.made and self.copy is not None:
            subprocess.run(["chmod", "-R", "u+w", str(self.copy)], capture_output=True)
            shutil.rmtree(self.copy, ignore_errors=True)
            self.made = False
        _SCRATCH_DIRS.discard(self.copy)

    def suite_hint(self):
        """(seconds, finished) of the baseline suite so far, for the phase-B prompt; None without a suite."""
        if not self.sp.get("suite_cmd") or self.t0 is None:
            return None
        got = self.live.get("suite_cmd")
        return (got[2], True) if got else (time.time() - self.t0, False)


_BG = []  # the Baselines of the two_phase calls in progress: the wrapper stops each one (and removes its copy) on every return


def two_phase(a, spec, allowed, local, check_only=False, chain=None, sub=None):
    """One two-phase run. chain (a dict from run_steps) makes it step n of a `steps` chain: no prepare after step 1,
    shared Usage, its own REVIEW file, and the RESULT line is left to the chain driver. sub (a dict from auto()) makes it the
    last stage of an auto run: the deadline is auto's, and the result line goes to sub["line"] instead of RESULT. Whatever way it
    ends, the background baselines it started are stopped and their copy of the work dir removed."""
    n = len(_BG)
    try:
        return _two_phase(a, spec, allowed, local, check_only, chain, sub)
    finally:
        while len(_BG) > n:
            _BG.pop().stop()


def _two_phase(a, spec, allowed, local, check_only=False, chain=None, sub=None):
    """two_phase's body (see two_phase)."""
    cwd = a.cwd
    first = chain is None or chain["first"]
    pfx = f"s{chain['n']}" if chain else ""

    def result(line):
        if chain is not None:
            chain["why"] = line
            print(f"[mini-runner] step {chain['n']} result: {line}", flush=True)
        elif sub is not None:
            sub["line"] = line
            print(f"[mini-runner] auto stage two-phase result: {line}", flush=True)
        else:
            print(f"RESULT {tag_result(line)}", flush=True)

    def check_mutation(gates, on):
        """mini-finish's check (--mutation): the spec's mutation_gate on a green candidate, as phase B runs it on the model's fix."""
        if on and spec.get("mutation_gate") and all(v[0] for v in gates.values()):
            mok, mdet, _ = run_mutation_gate(cwd, spec, allowed, deadline - _now())
            gates["mutation"] = (mok, mdet)

    def breadth_retry(gates, out, last_msg, score):
        """spec breadth_retry: ONE local attempt at the narrowing `breadth` survivors of a green, mutated fix (see narrow_survivors).
        The model decides each match from TASK.md, narrows it or keeps it, and names the TASK.md words that decide it. A red result,
        an edited test or a stray file puts the green fix back byte for byte. The "breadth" row is always ok: it tells the reviewer
        what was asked and what the model claimed. -> (gates, out, last_msg, score) for the packet."""
        base_ready()
        narrow = narrow_survivors(gates["mutation"][1], base_texts)
        asked = "; ".join(f"{w} {b}" for w, b, _ in narrow).replace("|", "/").replace("\n", " ")[:300]
        left = deadline - reserve - _now()
        if left < 600:
            gates["breadth"] = (True, f"not asked (only {int(left)}s left): {asked}")
            return gates, out, last_msg, score
        keep = list(dict.fromkeys([*allowed, tf]))
        read = lambda: {f: (Path(cwd, f).read_bytes() if Path(cwd, f).is_file() else None) for f in keep}
        snap, tracked0, before = read(), snapshot(cwd), untracked_files(cwd)  # bytes: the put-back is exact
        print(f"[mini-runner] breadth retry: {len(narrow)} narrowing survivor(s) ({int(left)}s)", flush=True)
        traj = run_dir / f"{Path(cwd).name}.{pfx}BN.traj.json"
        log, _ = run_mini(model, prefix + breadth_prompt(spec, allowed, narrow, step_diff(cwd, spec, allowed, base_texts)) + "--- TASK.md ---\n"
                          + task_md, traj, cwd, env, key, left - 300, usage, BREADTH_STEPS, tag=f"{pfx}BN", wall=int(min(600, left - 300)),
                          watch=make_watch("BN", spec, cwd, test_sha=test_sha),  # TEST-SUSPECT + the repeat rule; no no-edit rule
                          no_think=no_think_for(spec, "B"))                    # ("kept" is a valid answer)
        revert_bytecode(cwd)
        said = " ;; ".join(breadth_said(traj)) or "no BREADTH line"
        now, tracked1, new = read(), snapshot(cwd), sorted(untracked_files(cwd) - set(before))
        if now == snap and tracked1 == tracked0 and not new:
            gates["breadth"] = (True, f"asked: {asked}; fix unchanged; model: {said}")
            return gates, out, last_msg, score
        g2, out2 = ((fix_gate(cwd, spec, allowed, base_texts, test_sha, ev.get("tests"), base_suite, base_tc, log, pre, traj=traj)
                     if now[tf] == snap[tf] else ({"test_unchanged": (False, "the frozen test was edited")}, out)))
        bad = [f"{k}: {d}" for k, (ok, d) in g2.items() if not ok]
        if bad:
            for f, t in snap.items():
                if t is None:
                    Path(cwd, f).unlink(missing_ok=True)
                else:
                    Path(cwd, f).write_bytes(t)
            for c in sorted((set(tracked0) | set(tracked1)) - set(snap)):  # a tracked file outside allowed: as it was before
                if c not in tracked0:
                    run_git(cwd, "checkout", "--", c)
                elif tracked0[c] is None:
                    Path(cwd, c).unlink(missing_ok=True)
                else:
                    Path(cwd, c).write_bytes(tracked0[c])
            clean_new_untracked(cwd, before)
            revert_bytecode(cwd)
            why = "; ".join(bad).replace("|", "/").replace("\n", " ")[:300]
            gates["breadth"] = (True, f"asked: {asked}; the changed fix failed ({why}), so the green fix was put back; model: {said}")
            return gates, out, last_msg, score
        mok, mdet, score = run_mutation_gate(cwd, spec, allowed, deadline - reserve - _now())
        g2["mutation"] = (mok, mdet)
        print(f"[mini-runner] mutation gate after the breadth retry: {'ok' if mok else 'FAIL'} {mdet}", flush=True)
        g2["breadth"] = (True, f"asked: {asked}; fix changed (the diff is the result); model: {said}")
        return g2, out2, final_message(traj) or last_msg, score

    try_test = getattr(a, "try_test", None)
    try_fix = [x.split("=", 1) for x in (getattr(a, "try_fix", None) or [])]
    for k in ("test_file", "test_cmd"):
        if not spec.get(k):
            print(f"[mini-runner] two-phase spec needs {k!r}", flush=True)
            result(f"FAIL bad-spec missing {k}")
            return RC_FAIL
    tf, run_dir = spec["test_file"], Path(cwd).parent
    total = int(spec.get("timeout", 2400))
    t_start = _now()
    deadline = t_start + total
    if not check_only and chain is None and sub is None:
        install_signal_handlers(total, int(spec.get("deadline_grace", 120)))
    reserve = int(spec.get("gate_reserve", 240))
    # phase_a_only (lever 1) has no phase B: phase A may use all but the reserve, so a rejected draft can be retried
    budget_a = int(spec.get("test_timeout", total - max(reserve, 300) if spec.get("phase_a_only") else total * 0.35))
    print(f"[mini-runner] two-phase budgets: total={total}s test={budget_a}s x{spec.get('test_attempts', 2)} "
          f"fix=rest-minus-{reserve}s x{spec.get('fix_attempts', 2)}", flush=True)
    if first:
        prepare(cwd, spec)
        for d in spec.get("drop", [] if spec.get("verify") else ["verify.sh", "verify.test.ts", "check_literals.py"]):
            Path(cwd, d).unlink(missing_ok=True)  # a stale hand oracle must not anchor qwen's own test
    if chain is not None:
        try:
            tt = resolve_task(cwd, spec)
        except ValueError as e:
            print(f"[mini-runner] precheck FAIL: {e}", flush=True)
            result(f"FAIL bad-spec {e}")
            return RC_FAIL
        if tt is not None:
            tm = Path(cwd, "TASK.md")
            if tm.exists():
                tm.chmod(0o644)
                tm.unlink()
            tm.write_text(tt)
            spec["_task_written"] = True
        if not first:
            spec["_step_base"] = snapshot(cwd)  # earlier steps' edits stay; only this step's are judged
    ref_mode = ref_root = None  # a step applied from a reference dir instead of the model: mini-submit C6, or a mini-finish replay
    if chain is not None and check_only and getattr(a, "try_fix_chain", None):
        ref_mode, ref_root = "try-fix-chain", a.try_fix_chain
    elif replay_dir(spec, chain):
        ref_mode, ref_root = "replay", replay_dir(spec, chain)
        rt = Path(ref_root, f"s{chain['n']}", tf)
        if rt.is_file():  # the step's own test as it passed (a model-written test is not in the baseline)
            Path(cwd, tf).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rt, Path(cwd, tf))
    err, warn = precheck(spec, cwd)  # mandatory: every provable spec problem fails here, before any model call
    why = report_precheck(err, warn)
    if err:
        result(f"FAIL bad-spec {why}")
        return RC_FAIL
    task_md = Path(cwd, "TASK.md").read_text()
    base_texts = {f: (Path(cwd, f).read_text() if Path(cwd, f).is_file() else None) for f in allowed}
    spec["_upd_base"] = {f: Path(cwd, f).read_text(errors="replace") for f in as_list(spec.get("update_tests")) if Path(cwd, f).is_file()}
    pre = tuple(ln[3:].strip() for ln in git(cwd, "status", "--short", "--untracked-files=all").splitlines() if ln.startswith("??"))
    base_suite = base_tc = set()
    spec.pop("_collect_base", None)  # collect_check's baseline belongs to this run's tree
    if spec.get("_suite_full"):  # a later chain step starts from the whole suite again
        spec["suite_cmd"] = spec.pop("_suite_full")
    spec.pop("_suite_secs", None)
    spec.pop("_suite_rc", None)
    bg, why_fg = None, bg_reason(cwd, spec, check_only, ref_mode)
    if why_fg is None:
        b0 = Baselines(cwd, spec, tf)
        if b0.start():
            bg = b0
            _BG.append(bg)
            print(f"[mini-runner] baselines run in the background on a copy of the work dir ({b0.copy.name}) while phase A works", flush=True)
        else:
            why_fg = f"the copy failed ({b0.err})"
    if bg is None:
        if not check_only and (spec.get("suite_cmd") or spec.get("typecheck_cmd")):
            print(f"[mini-runner] baselines run first: {why_fg}", flush=True)
        base_suite, base_tc = apply_baselines(spec, run_baselines(cwd, spec))

    def base_ready(wait=True):
        """The baseline sets, before a gate needs them: joins the background run (wait=False: only when it has finished). A failed
        background run (the copy broke, a crash) runs here instead, with the base files swapped back in for it. In an auto run the
        wait moves the fix budget's end, as the foreground baselines' shift does."""
        nonlocal bg, base_suite, base_tc, deadline
        if bg is None or (not wait and bg.running()):
            return
        res, waited = bg.join()
        if res is None:
            print(f"[mini-runner] background baselines failed ({bg.err or 'stopped'}): running them here", flush=True)
            t, tp = time.time(), Path(cwd, tf)
            now_tf = tp.read_bytes() if tp.is_file() else None
            prev = swap_texts(cwd, {**{f: base_texts[f] for f in allowed}, **(spec.get("_upd_base") or {})})
            try:
                if bg.tf0 is None:
                    tp.unlink(missing_ok=True)
                else:
                    tp.write_bytes(bg.tf0)
                res = run_baselines(cwd, spec)
            finally:
                swap_texts(cwd, prev)
                if now_tf is None:
                    tp.unlink(missing_ok=True)
                else:
                    tp.write_bytes(now_tf)
            waited += time.time() - t
        bg = None
        base_suite, base_tc = apply_baselines(spec, res)
        if waited >= 1:
            room = (_DEADLINE[0] - time.time()) - (deadline - _now()) - 60 if sub is not None and _DEADLINE[0] else 0.0
            ext = max(0.0, min(waited, room))
            deadline += ext
            print(f"[mini-runner] waited {waited:.0f}s for the baselines" + (f": the fix budget ends {ext:.0f}s later" if ext >= 1 else ""), flush=True)
    prefix = (f"WORKING DIRECTORY: {Path(cwd).resolve()}. The code you must edit is in this directory (your shell starts here). "
              "Use relative paths. Do NOT search `/`, cd elsewhere, or read or edit any file outside this directory; "
              "a copy of the repo elsewhere (e.g. /private/tmp) is NOT yours.\n\n") + READ_HINT + graph_lookup_note(cwd, spec)
    usage, model, key = (chain["usage"] if chain else Usage()), a.model or DEFAULT_MODEL, local.get("api_key", "")
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}, "OPENAI_API_BASE": local.get("base_url", ""),
           "MSWEA_COST_TRACKING": "ignore_errors", "MSWEA_CONFIGURED": "true",
           "PYTHONDONTWRITEBYTECODE": "1"}  # key: launcher file, never env
    done_line = lambda ok, why: result(f"{'PASS' if ok else 'FAIL'} mode=two-phase {why}")
    show_usage = lambda: print(usage.summary(), flush=True) if chain is None else None
    ctx0 = len(usage.ctx)
    review = chain["review"] if chain else run_dir / "REVIEW.md"
    review.unlink(missing_ok=True)

    def review_a(why, final_msg):
        tp = Path(cwd, tf)
        write_review(review, spec, allowed, ev, {}, tp.read_text(errors="replace") if tp.exists() else "(not written)", "", "",
                     f"FAIL phase=test {why[:200]}", final_msg, usage.ctx[ctx0:], rejected=rejected or [why], task_text=task_md)
        print(f"[mini-runner] REVIEW packet: {review}", flush=True)

    def direct_fallback(why, after, orig_line, failing=()):
        """Stage 22 (2026-11-06): one plain run, test and fix together, when the structured path stops with budget left (see
        fallback_on and the block comment at FB_MIN_S). -> (rc, RESULT line) or None when it did not run: off, a chain step, a
        check-only or test-draft run, or too little time. A PASS line is `PASS needs-review soft=fallback,...`; a FAIL line is the
        structured path's own plus ` fallback=direct FAIL (<why>)`, so the failure class is unchanged."""
        nonlocal deadline
        if chain is not None or check_only or spec.get("phase_a_only") or not fallback_on(spec):
            return None
        if fail_class(orig_line) == "INFRA":  # the provider, the host or the queue stopped it: another run meets the same wall
            print("[mini-runner] direct fallback not run: the stop is infra, not a gate", flush=True)
            return None
        left = deadline - reserve - _now()
        room = (_DEADLINE[0] - time.time()) - (deadline - _now()) - 120 if sub is not None and _DEADLINE[0] else 0.0
        if left < FB_WANT_S and room > 0:
            ext = min(room, FB_WANT_S - left)
            deadline += ext
            left += ext
            print(f"[mini-runner] direct fallback: the stage deadline moves {ext:.0f}s later (job room)", flush=True)
        gate_s = 120 + 2 * float(spec.get("_suite_secs") or 0)  # three test runs and one more suite run after the model
        if left < FB_MIN_S + gate_s:
            print(f"[mini-runner] direct fallback not run: only {int(left)}s left (needs {int(FB_MIN_S + gate_s)}s)", flush=True)
            return None
        print(f"[mini-runner] direct fallback after {after} ({int(left)}s): {why[:300]}", flush=True)
        main = run_dir / "REVIEW-main.md"
        if review.exists():
            review.replace(main)  # the stopped path's packet stays next to the fallback's
        tp = Path(cwd, tf)
        notes = []
        if after == "fix":
            if not tp.is_file() or sha(tp) != test_sha:  # phase B edited or deleted the frozen test: the fallback starts from the frozen one
                tp.write_text(test_text)
            d = step_diff(cwd, spec, allowed, base_texts)
            notes.append("Its fix (reverted; the diff below) failed these gates: " + "; ".join(failing)[:800]
                         + "\n\n```diff\n" + d[:2500] + ("\n... (cut)" if len(d) > 2500 else "") + "\n```")
        prior_test = tp.read_text(errors="replace") if tp.is_file() else None
        if prior_test is not None:
            notes.append(f"Its test `{tf}` (in the work dir now) "
                         + ("passed the test gate (red at baseline for the right reason, reaching the target): keep it unless it is wrong."
                            if after == "fix" else f"was REJECTED ({why[:600]}): repair or replace it.")
                         + f"\n\n```\n{prior_test[:6000]}" + ("\n... (cut)" if len(prior_test) > 6000 else "") + "\n```")
        tsha0 = sha(tp) if tp.is_file() else None
        swap_texts(cwd, {f: base_texts[f] for f in allowed})  # the start state: the code at baseline, the test as it was left
        for f, t in (spec.get("_upd_base") or {}).items():
            Path(cwd, f).write_text(t)
        clean_new_untracked(cwd, pre, keep={tf, *allowed})
        revert_bytecode(cwd)
        budget = left - gate_s
        traj = run_dir / f"{Path(cwd).name}.{pfx}F.traj.json"
        log, _ = run_mini(model, prefix + fallback_prompt(spec, allowed, why, "\n\n".join(notes), cwd=cwd) + "--- TASK.md ---\n" + task_md,
                          traj, cwd, env, key, budget, usage, int(spec.get("fallback_steps", spec.get("fix_steps", 45))), tag=f"{pfx}F",
                          wall=int(min(int(spec.get("fallback_wall", spec.get("fix_wall", 900))), budget)), watch=make_watch("F", spec, cwd),
                          no_think=no_think_for(spec, "B"))
        last = final_message(traj)
        revert_bytecode(cwd)
        base_ready()
        ctx = usage.ctx[ctx0:]

        def give_up(reason):
            g0 = {"fallback": (False, f"direct fix after {after}: {why[:200]}; ended: {reason}")}
            txt = tp.read_text(errors="replace") if tp.is_file() else "(not written)"
            write_review(review, spec, allowed, {}, g0, txt, "", "", f"FAIL fallback={reason}", last, ctx, task_text=task_md,
                         fallback=f"The structured path stopped after {after} ({why[:300]}). The direct-fix fallback then ran and produced "
                                  f"nothing to check: {reason}. No fix is offered; the stopped path's packet is {main}.")
            print(f"[mini-runner] direct fallback: {reason}; REVIEW packet: {review}", flush=True)
            show_usage()
            return RC_FAIL, f"{orig_line} fallback=direct FAIL ({reason})"

        if not tp.is_file() or not tp.read_text(errors="replace").strip():
            return give_up("no test written")
        now = {f: (Path(cwd, f).read_text(errors="replace") if Path(cwd, f).is_file() else None) for f in allowed}
        if all(now[f] == base_texts[f] for f in allowed):
            return give_up("no fix written")
        tsha_m = sha(tp)  # the model's test, before the gate's runner repair (test_unchanged compares this one)
        # stage 27 (holoviews/unyt/pydap fallbacks): a test other than the one phase A's gate passed gets the canonical runner
        fbfix = None if (after == "fix" and tsha_m == tsha0) else _fb_runner_fix(cwd, spec, tf)
        if fbfix:
            print(f"[mini-runner] direct fallback: the gate {fbfix} in {tf} with the canonical runner (RESULT lines, exit 1 on a failure)", flush=True)
        tsha = sha(tp)
        sp2 = {k: v for k, v in spec.items() if k != "_base_red"}  # the test is this run's own: no same-tests check against phase A
        sp2.update(fn_scope="off", min_diff_cov=0.0)
        g, out = fix_gate(cwd, sp2, allowed, base_texts, tsha, None, base_suite, base_tc, log, pre, traj=traj)
        same = tsha0 is not None and tsha_m == tsha0
        g["test_unchanged"] = (True, "not frozen: test and fix come from one run" + (" (the test is the stopped path's, unchanged)" if same else "")
                               + (f" (the gate {fbfix} with the canonical runner)" if fbfix else ""))
        prev = swap_texts(cwd, {f: base_texts[f] for f in allowed})
        try:
            rc2, out2, _ = run_cmd(spec["test_cmd"], cwd, spec)
        finally:
            swap_texts(cwd, prev)
        _remember_task(spec, cwd, allowed)
        per2 = parse_test_results(out2)
        n2 = test_count(out2)
        fev = {"baseline_rc": rc2, "baseline_out": (out2 or "")[-3000:], "tests": n2, "reach": None, "fn_cov": {},
               "per_test": [f"{nm} {st} {t or ''}".strip() for nm, st, t, _m in per2]}
        red = sorted(nm for nm, st, _t, _m in per2 if st == "FAIL")
        wrong = [f"{nm}: {fb_red_problem(t, m, spec)}" for nm, st, t, m in per2 if st == "FAIL" and fb_red_problem(t, m, spec)]
        crash = crash_hit(out2, per2)  # stage 25: positional once per-test lines parsed (see crash_hit)
        if rc2 == 0:
            g["fb_red_reason"] = (False, "the test passes at baseline: no evidence of the bug")
        elif crash:
            g["fb_red_reason"] = (False, f"at baseline the test crashes instead of failing on an assertion: {crash.group(0)}")
        elif per2 and wrong:
            g["fb_red_reason"] = (False, f"at baseline rc={rc2}, red {red}; wrong-reason: {wrong[:4]}")
        elif not per2 and not spec.get("allow_error_fail") and not FAILASSERT_RE.search(out2 or ""):
            g["fb_red_reason"] = (False, f"at baseline rc={rc2} but no assertion failure and no per-test line (wrong-reason failure)")
        else:
            g["fb_red_reason"] = (True, f"at baseline rc={rc2}, red: {red or 'an assertion failure (no per-test lines)'}")
        n, per = test_count(out), parse_test_results(out)
        zero = _zero_tests_ran(tf, tp.read_text(errors="replace"), out, per, spec["test_cmd"])
        g["fb_tests_ran"] = (((n or 0) >= 1 or bool(per)) and not zero and (n is None or n2 is None or n >= n2),
                             f"tests={n} per-test lines={len(per)} (at baseline {n2})" + ("; zero tests ran" if zero else ""))
        locus = fn_scope_check(cwd, spec, allowed, base_texts) if spec.get("target_fn") else (True, "no target_fn")
        g["fix_locus"] = (True, ("inside the diagnosed function" if locus[0] else "OUTSIDE the diagnosed function (information, not a gate here)")
                          + f": {str(locus[1])[:300]}")
        mut_score, cfg = None, spec.get("mutation_gate")
        if cfg and gate_verdict(g)[0] != "FAIL":
            req = isinstance(cfg, dict) and bool(cfg.get("required"))
            if not locus[0]:
                g["mutation"] = (not req, "skipped: the fix is not inside the diagnosed function, so its mutants say nothing about this fix")
            elif deadline - reserve - _now() < 90:
                g["mutation"] = (not req, "skipped: no time left")
            else:
                mok, mdet, mut_score = run_mutation_gate(cwd, spec, allowed, deadline - reserve - _now())
                g["mutation"] = (mok, mdet)
                print(f"[mini-runner] mutation gate (fallback): {'ok' if mok else 'FAIL'} {mdet}", flush=True)
        if gate_verdict(g)[0] != "FAIL" and deadline - reserve - _now() > 60:
            st = style_pass(cwd, spec, [f for f in allowed if f != tf], base_texts, check=lambda: _style_still_green(cwd, spec, n))
            if st:
                g["style"] = (True, f"fix: {st}")
        g = {"fallback": (True, f"direct fix after {after}: {why[:200]}; skipped: {FB_SKIPPED}"), **g}
        verdict, bad = gate_verdict(g)
        ok = verdict != "FAIL"
        summary = " ".join(f"{k}={'ok' if v[0] else 'FAIL'}" for k, v in g.items())
        soft = ["fallback", *bad] if ok else None
        write_review(review, spec, allowed, fev, g, tp.read_text(errors="replace"), step_diff(cwd, spec, allowed, base_texts), out,
                     review_result(ok, summary, usage.trip, soft), last, ctx, task_text=task_md,
                     fallback=fallback_section(after, why, g, fev, same, main))
        print(f"[mini-runner] direct fallback: {verdict}" + (f" (failing: {', '.join(bad)})" if bad else "") + f"; REVIEW packet: {review}", flush=True)
        show_usage()
        if ok:
            return RC_OK, (f"PASS needs-review soft={','.join(soft)} mode=two-phase {summary} test={tf} fallback=direct after={after}"
                           + (f" mutation_score={mut_score:.2f}" if mut_score is not None else ""))
        return RC_FAIL, f"{orig_line} fallback=direct FAIL ({','.join(bad)})"

    # phase A: a test supplied in the baseline (untracked, kept by prepare) skips the model if it passes the gate
    rejected, ev = None, {}
    supplied = Path(cwd, tf).exists()
    skel_txt = None if supplied else skeleton(cwd, spec, allowed)  # phase A fills a skeleton in instead of inventing a file
    if skel_txt is not None:
        Path(cwd, tf).write_text(skel_txt)
        print(f"[mini-runner] wrote test skeleton {tf} ({len(skel_txt.splitlines())} lines)", flush=True)
    if supplied:
        rejected, ev = test_gate(cwd, spec, allowed)
        print(f"[mini-runner] supplied {tf}: {'ACCEPTED' if not rejected else 'rejected: ' + '; '.join(rejected)}", flush=True)
    else:
        rejected = ["not written yet"]
    if check_only:
        if getattr(a, "probe", False):  # model-free fixture check: can ANY test reach target_fn here?
            bad, info = probe_check(cwd, spec, allowed)
            anc = "; ".join(f"`{k}` line {v['line']} ({v['guards_before']} guard line(s) before it)" for k, v in info.get("anchors", {}).items())
            verdict = "FAIL " + "; ".join(bad) if bad else ("WARN " + "; ".join(info["warn"]) if info.get("warn") else "ok")
            print("PROBE " + verdict + (f" reach={info.get('reach')}" if not bad else "") + (f" anchors: {anc}" if anc else ""), flush=True)
            return RC_OK if not bad else RC_FAIL
        if try_test:  # dry run of the gates on a hand-supplied candidate test (and fix), no model
            shutil.copy2(try_test, Path(cwd, tf))
            rejected, ev = test_gate(cwd, spec, allowed)
            print(f"[mini-runner] try-test {try_test}: {'ACCEPTED' if not rejected else 'REJECTED: ' + '; '.join(rejected)} "
                  f"reach={ev.get('reach')} fn_cov={ev.get('fn_cov')}", flush=True)
            if getattr(a, "draft_check", False) and not rejected:  # test_draft spec: the external acceptance check too (mini-finish)
                rc, line = testdraft_accept(spec, cwd, tf, run_dir)
                print(f"[mini-runner] draft-check: {line}", flush=True)
                return rc
            if try_fix and not rejected:
                tsha = sha(Path(cwd, tf))
                for dst, src in try_fix:
                    Path(cwd, dst).parent.mkdir(parents=True, exist_ok=True)  # a new file in a new directory
                    shutil.copy2(src, Path(cwd, dst))
                gates, _ = fix_gate(cwd, spec, allowed, base_texts, tsha, ev.get("tests"), base_suite, base_tc, "", pre)
                check_mutation(gates, getattr(a, "mutation", False))
                bad = [f"{k}: {d}" for k, (ok, d) in gates.items() if not ok]
                print(f"[mini-runner] try-fix: {'GREEN' if not bad else 'RED: ' + ' | '.join(bad)}", flush=True)
                for k, (ok, d) in gates.items():
                    print(f"[mini-runner]   {k}: {'ok' if ok else 'FAIL'} {d}", flush=True)
                return RC_OK if not bad else RC_FAIL
            return RC_OK if not rejected else RC_FAIL
    if ref_mode:  # model-free step (C6 proof or a continuation's replay): apply the s<N> reference cumulatively, run the REAL phase-B gates
        n = chain["n"]
        spec_pfx = "bad-spec " if ref_mode == "replay" else ""  # a replay that stops passing is the continuation spec's fault, never the model's
        if rejected:
            print(f"[mini-runner] {ref_mode} step {n}: test REJECTED: {'; '.join(rejected)}", flush=True)
            result(f"FAIL {spec_pfx}{ref_mode} test-rejected {'; '.join(rejected)[:300]}")
            return RC_FAIL
        sd = Path(os.path.expanduser(str(ref_root))) / f"s{n}"
        gone = set(ref_manifest(sd).get("deleted") or []) & set(edit_scope(spec, allowed))  # files the step deleted (saved refs only)
        missing = [f for f in allowed if f not in gone and not (sd / f).is_file()]
        if missing:
            print(f"[mini-runner] {ref_mode} step {n}: no reference {missing} in {sd}", flush=True)
            result(f"FAIL {spec_pfx}{ref_mode} no-ref {missing}")
            return RC_FAIL
        tsha = sha(Path(cwd, tf))
        for f in edit_scope(spec, allowed):  # update_tests files ride along when the reference carries them
            if f in gone:
                Path(cwd, f).unlink(missing_ok=True)
            elif (sd / f).is_file():
                Path(cwd, f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sd / f, Path(cwd, f))
        gates, gout = fix_gate(cwd, spec, allowed, base_texts, tsha, ev.get("tests"), base_suite, base_tc, "", pre, ws_clean=False)
        try:
            last = n == int((spec.get("replay") or {}).get("through") or 0)
        except (TypeError, ValueError):
            last = False
        check_mutation(gates, getattr(a, "mutation", False) and check_only and ref_mode == "replay" and last)
        bad = [f"{k}: {d}" for k, (ok, d) in gates.items() if not ok]
        print(f"[mini-runner] {ref_mode} step {n}: {'GREEN' if not bad else 'RED: ' + ' | '.join(bad)}", flush=True)
        for k, (ok, d) in gates.items():
            print(f"[mini-runner]   {k}: {'ok' if ok else 'FAIL'} {d}", flush=True)
        if bad:
            result(f"FAIL {spec_pfx}{ref_mode} RED {' | '.join(bad)[:300]}")
            return RC_FAIL
        if not check_only:  # a real replayed step: its packet says no model ran
            write_review(review, spec, allowed, ev, gates, Path(cwd, tf).read_text(errors="replace"), step_diff(cwd, spec, allowed, base_texts),
                         gout, f"PASS replayed from {sd} (no model call)", task_text=task_md)
            chain["replayed"] = True
            result(f"PASS mode=two-phase replayed from {sd}")
        chain["changed"] = step_changed(cwd, spec)
        chain.setdefault("guard", {}).update(guard_hashes(cwd, tf, allowed))
        return RC_OK
    if check_only:
        print("[mini-runner] check-only: phase A prompt follows\n" + prefix + phase_a_prompt(spec, allowed, skel=bool(skel_txt), cwd=cwd)[:1800], flush=True)
        return RC_OK
    # The baselines (and a supplied test's gate) are the lane's work, not the model's: in an auto run, whose deadline has room past
    # this stage's, the phase budgets start after them. Elsewhere the run's own alarm is the job's contract and nothing moves.
    # 2026-11-05: scrapy's baseline suite timed out at 600 s inside phase A's 840 s (A1 cut at step 24, A2 had 10 s); optuna's
    # took 255 s and left A2 no time.
    took = _now() - t_start
    room = (_DEADLINE[0] - time.time()) - (deadline - _now()) - 60 if sub is not None and _DEADLINE[0] else 0.0
    shift = max(0.0, min(took, room))
    if shift >= 1:
        t_start, deadline = t_start + shift, deadline + shift
        print(f"[mini-runner] the baselines took {took:.0f}s: the phase budgets start {shift:.0f}s later", flush=True)
    tries, last_msg, td = 0, "", None
    while rejected and tries < int(spec.get("test_attempts", 2)) and _now() < t_start + budget_a:
        tries += 1
        td = None
        prior = None if rejected == ["not written yet"] else rejected
        print(f"[mini-runner] phase A attempt {tries}", flush=True)
        traj = run_dir / f"{Path(cwd).name}.{pfx}A{tries}.traj.json"
        log, _ = run_mini(model, prefix + phase_a_prompt(spec, allowed, prior, skel=bool(skel_txt) and prior is None, cwd=cwd) + "--- TASK.md ---\n" + task_md, traj,
                          cwd, env, key, t_start + budget_a - _now(), usage, spec.get("test_steps", 30), tag=f"{pfx}A{tries}",
                          wall=int(spec.get("test_wall", 600)), watch=make_watch("A", spec, cwd, {tf: skel_txt}),
                          no_think=no_think_for(spec, "A"), overlay=TEST_OVERLAY, draft=test_draft(cwd, spec, tf, bool(skel_txt)))
        last_msg = final_message(traj)
        revert_bytecode(cwd)
        stray = [c for c in step_changed(cwd, spec) if c != tf]
        extra = untracked_violations(git(cwd, "status", "--short", "--untracked-files=all").splitlines(), extra_ok=(tf, *pre))
        if stray:
            restore_paths(cwd, spec, stray)
        for x in extra:
            Path(cwd, x.strip()).unlink(missing_ok=True)
        if said_missing(log, traj):
            show_usage()
            review_a("agent reported MISSING:", last_msg)
            done_line(False, "phase=test missing=True")
            return RC_FAIL
        rejected, ev = test_gate(cwd, spec, allowed, ground="soft" if tries >= int(spec.get("test_attempts", 2)) else "hard",
                                 fix_runner=True)
        if usage.trip and rejected:
            rejected = [usage.trip, *rejected]  # the next attempt's prompt says why the last one was cut short
        if stray or extra:
            print(f"[mini-runner] phase A touched other files (reverted): {stray + extra}", flush=True)
        print(f"[mini-runner] phase A gate: {'ACCEPTED' if not rejected else 'REJECTED: ' + '; '.join(rejected)}", flush=True)
        if not rejected and spec.get("phase_a_only"):  # lever 1: the acceptance check is part of the attempt, so a REJECT is retried
            td = testdraft_accept(spec, cwd, tf, run_dir, tag=f"{pfx}A{tries}")
            if td[0] != RC_OK:
                rejected, retry = draft_feedback(Path(run_dir, "DRAFT-CHECK.txt").read_text(errors="replace"))
                print(f"[mini-runner] draft check REJECTED{'' if retry else ' (not retryable)'}: {'; '.join(rejected)[:900]}", flush=True)
                if not retry:
                    break
    if rejected:
        show_usage()
        if td is not None:  # the last draft passed the test gate and the acceptance check rejected it
            review_a("test-draft: " + "; ".join(rejected), last_msg)
            done_line(False, "phase=test-draft " + td[1][:200])
            result(td[1])
            return RC_FAIL
        review_a("test-gate: " + "; ".join(rejected), last_msg)
        fb = direct_fallback("phase A rejected the test: " + "; ".join(rejected), "test",
                             f"FAIL mode=two-phase phase=test test-gate: {'; '.join(rejected)[:300]}")
        if fb is not None:
            result(fb[1])
            return fb[0]
        done_line(False, f"phase=test test-gate: {'; '.join(rejected)[:300]}")
        return RC_FAIL
    style_notes = []
    if chain is None and not supplied and not spec.get("phase_a_only") and ev.get("per_test"):
        st = style_pass(cwd, spec, [tf], {}, check=lambda: _style_same_red(cwd, spec, ev), test_file=tf)
        if st:
            style_notes.append(f"test: {st}")
            print(f"[mini-runner] style (test): {st}", flush=True)
    test_sha, test_text = sha(Path(cwd, tf)), Path(cwd, tf).read_text()
    print(f"[mini-runner] test frozen: {tf} sha={test_sha[:12]} reach={ev.get('reach')} tests={ev.get('tests')}", flush=True)
    parked_sha = answers_test_sha(spec) if chain is None else None
    if parked_sha and supplied and parked_sha != test_sha:  # the test must not move while the job waits for its answer
        show_usage()
        done_line(False, f"phase=fix test-changed-while-parked (was {parked_sha[:12]}, now {test_sha[:12]})")
        return RC_FAIL

    if spec.get("phase_a_only"):  # lever 1 (test_draft): the model only drafts the test; an external gate (mini-testdraft check) decides
        show_usage()
        rc, line = td if td is not None else testdraft_accept(spec, cwd, tf, run_dir)  # td: checked inside the attempt loop
        done_line(rc == RC_OK, "phase=test-draft " + line[:200])
        result(line)
        return rc

    # phase B: fresh context per attempt (TASK.md + the frozen test only)
    gates, out, prior, last_msg = {}, "", None, ""
    fix_mode = "mini"
    if spec.get("fix_mode") == "single_shot":
        base_ready()
        ss = single_shot_fix(cwd, spec, allowed, base_texts, test_sha, ev.get("tests"), base_suite, base_tc, pre, task_md, test_text, ev, usage,
                             model, local, deadline - reserve)
        usage.ctx.append(f"{pfx}B-single-shot: attempts={ss['attempts']} green={ss['green']}" + (f" fallback={ss['fallback']}" if ss["fallback"] else ""))
        if ss["green"]:
            gates, out, last_msg, fix_mode = ss["gates"], ss["out"], ss["final"], "single_shot"
        else:
            fix_mode = "single_shot>mini"
    cleaned = None
    for tries in (range(1, int(spec.get("fix_attempts", 2)) + 1) if fix_mode != "single_shot" else ()):
        left = deadline - reserve - _now()
        if left < 60:
            break
        print(f"[mini-runner] phase B attempt {tries} ({int(left)}s)", flush=True)
        untracked0 = untracked_files(cwd)
        traj = run_dir / f"{Path(cwd).name}.{pfx}B{tries}.traj.json"
        base_ready(wait=False)
        hint = bg.suite_hint() if bg else ((spec["_suite_secs"], True) if "_suite_secs" in spec else None)
        log, _ = run_mini(model, prefix + phase_b_prompt(spec, allowed, test_text, prior, cwd=cwd, cleaned=cleaned, suite_hint=hint) + "--- TASK.md ---\n" + task_md,
                          traj, cwd, env, key, left, usage, spec.get("fix_steps", 45), tag=f"{pfx}B{tries}",
                          wall=failfast_wall(spec, int(spec.get("fix_wall", 900))), watch=make_watch("B", spec, cwd, {f: (Path(cwd, f).read_text(errors="replace") if Path(cwd, f).is_file() else None) for f in base_texts}, test_sha=test_sha),
                          no_think=no_think_for(spec, "B"))
        last_msg = final_message(traj)
        revert_bytecode(cwd)
        ni = needs_info_step(cwd, spec, traj, test_sha) if chain is None else None  # NEEDS.md is removed here, before any gate or diff
        if ni and ni[0] == "park":
            show_usage()
            print("RESULT PAUSED needs-info", flush=True)
            return park_run(run_dir / f"{Path(cwd).name}.needs-info.json", "needs_info", ni[1],
                            f"[mini-runner] PAUSED: needs-info: {ni[1]['question'][:200]}; answer with `mini-answer <job> \"text\"` (not graded, not a failure)")
        if Path(cwd, tf).exists() and sha(Path(cwd, tf)) != test_sha:
            print("[mini-runner] phase B edited the frozen test", flush=True)
            suspect = run_dir / (f"{pfx}-test-proposal.diff" if pfx else "test-proposal.diff")
            try:  # G6: keep the model's proposed test change for review, then judge the frozen test
                suspect.write_text("".join(difflib.unified_diff(test_text.splitlines(True), Path(cwd, tf).read_text(errors="replace").splitlines(True),
                                                                f"a/{tf} (frozen)", f"b/{tf} (phase B edit)")))
                print(f"[mini-runner] TEST-SUSPECT: proposal saved to {suspect}", flush=True)
            except OSError:
                pass
        base_ready()
        gates, out = fix_gate(cwd, spec, allowed, base_texts, test_sha, ev.get("tests"), base_suite, base_tc, log, pre, traj=traj)
        prior = [f"{k}: {d}" for k, (ok, d) in gates.items() if not ok]
        if usage.trip and prior:
            prior.insert(0, usage.trip)
        if ni and prior:  # a rejected question counts as a normal failed attempt; the next prompt says why
            prior.insert(0, f"needs-info question rejected: {ni[1]}")
        print(f"[mini-runner] phase B gate: {'GREEN' if not prior else 'RED: ' + ' | '.join(prior)}", flush=True)
        if not prior or not gates["no_missing"][0] or not gates["test_unchanged"][0]:
            break  # green, or a failure another attempt cannot undo
        cleaned = clean_new_untracked(cwd, untracked0, keep={tf, *allowed}) or None  # a stray .bak would fail the next attempt's scope gate
        if cleaned:
            print(f"[mini-runner] removed stray files from the failed attempt: {cleaned}", flush=True)
    if not gates and fix_mode == "single_shot>mini" and ss.get("gates"):
        gates, out = ss["gates"], ss["out"]  # the mini loop never ran (no time): report the single-shot candidate's RED gates, not "no-time-left"
    show_usage()
    calls0 = len(usage.rows)
    if not gates:
        done_line(False, "phase=fix no-time-left")
        return RC_FAIL
    mut_score_v = None
    if spec.get("mutation_gate") and all(v[0] for v in gates.values()):  # only a green fix is worth mutating; the score is advisory unless fail_below
        mok, mdet, mut_score_v = run_mutation_gate(cwd, spec, allowed, deadline - reserve - _now())
        gates["mutation"] = (mok, mdet)
        print(f"[mini-runner] mutation gate: {'ok' if mok else 'FAIL'} {mdet}", flush=True)
    if spec.get("breadth_retry") and chain is None and "mutation" in gates and all(v[0] for v in gates.values()) \
            and narrow_survivors(gates["mutation"][1], base_texts):
        gates, out, last_msg, mut_score_v = breadth_retry(gates, out, last_msg, mut_score_v)
    if chain is None and fix_hunks_on(spec) and gate_verdict(gates)[0] != "FAIL":  # advisory: never fed back to the model
        fh = fix_hunk_check(cwd, spec, allowed, base_texts, base_tc, deadline - reserve - _now())
        fh = merge_soft(fh, cast_check(cwd, spec, allowed, base_texts, deadline - reserve - _now()))
        if fh is not None:
            gates["fix_hunks"] = fh
            print(f"[mini-runner] fix_hunks: {'ok' if fh[0] else 'NEEDS-REVIEW'} {fh[1]}", flush=True)
    if chain is None and doc_pass_on(spec) and gate_verdict(gates)[0] != "FAIL" and deadline - reserve - _now() > DOC_PASS_TIMEOUT + 120:
        try:
            dp = doc_pass(cwd, spec, [f for f in allowed if f != tf], base_texts, task_md, model, local, usage,
                          deadline - reserve - _now() - 60, check=lambda: _style_still_green(cwd, spec, ev.get("tests")))
        except Exception as e:  # a review aid: never at the green fix's cost (doc_pass puts each file back itself)
            dp = (True, f"skipped: {type(e).__name__}: {str(e)[:100]}")
        if dp is not None:
            gates["docstring"] = dp
            print(f"[mini-runner] docstring: {dp[1]}", flush=True)
    if chain is None and gate_verdict(gates)[0] != "FAIL" and deadline - reserve - _now() > 60:
        st = style_pass(cwd, spec, [f for f in allowed if f != tf], base_texts, check=lambda: _style_still_green(cwd, spec, ev.get("tests")))
        if st:
            style_notes.append(f"fix: {st}")
            print(f"[mini-runner] style (fix): {st}", flush=True)
    if len(usage.rows) > calls0:
        show_usage()  # the breadth retry or the doc pass called the model after the summary above (the queue API reads the last one)
    if style_notes:
        gates["style"] = (True, "; ".join(style_notes))   # informational, never fails: the diff below is the restyled one
    ok = all(v[0] for v in gates.values())
    diff = step_diff(cwd, spec, allowed, base_texts)
    summary = " ".join(f"{k}={'ok' if v[0] else 'FAIL'}" for k, v in gates.items())
    if "test_unchanged" in gates and not gates["test_unchanged"][0]:
        summary += " TEST-SUSPECT"
    verdict, failing = gate_verdict(gates)
    write_review(review, spec, allowed, ev, gates, test_text, diff, out,
                 review_result(ok, summary, usage.trip, failing if verdict == "NEEDS-REVIEW" and chain is None else None),
                 last_msg, usage.ctx[ctx0:], task_text=task_md)
    print(f"[mini-runner] REVIEW packet: {review}", flush=True)
    if chain is not None:
        chain["changed"] = step_changed(cwd, spec)
        chain.setdefault("guard", {}).update(guard_hashes(cwd, tf, allowed))
    rc, line = final_result(gates, f"{summary} test={tf}", (f" fix_mode={fix_mode}" if spec.get("fix_mode") else "")
                            + (f" mutation_score={mut_score_v:.2f}" if mut_score_v is not None else ""), chain is not None)
    if rc != RC_OK and gates.get("no_missing", (True, ""))[0]:  # not after the model said the task cannot be done (MISSING:)
        fb = direct_fallback("phase B's fix failed its gates: " + ", ".join(failing), "fix", line,
                             [f"{k}: {d}" for k, (ok_, d) in gates.items() if not ok_])
        if fb is not None:
            rc, line = fb
    result(line)
    return rc


def _demote(md):
    """Markdown headings one level down (outside code fences) so a step's packet nests under '## Step n'."""
    out, fence = [], False
    for ln in md.splitlines():
        if ln.lstrip().startswith("```"):
            fence = not fence
        out.append("#" + ln if not fence and ln.startswith("#") else ln)
    return "\n".join(out)


def write_chain_review(path, rows, verdict):
    """One REVIEW.md for the chain: a status table, then a section per step (its own packet, headings demoted)."""
    body = f"# REVIEW packet (steps chain): {verdict}\n\n| step | test | result |\n|---|---|---|\n"
    body += "".join(f"| {i} | `{tf}` | {res} |\n" for i, tf, res, _ in rows) + "\n"
    for i, tf, res, text in rows:
        body += f"## Step {i}: `{tf}` ({res})\n\n" + (_demote(text) if text else "(no review packet for this step)") + "\n\n"
    Path(path).write_text(body)


def run_steps(a, spec, local, check_only=False):
    """`steps` chain: each step is a full two-phase run (fresh context) on the tree the previous step left. The first
    failing step stops the chain (RESULT FAIL step-<n> <reason>); earlier steps' edits and tests stay in the work dir."""
    steps, cwd = merged_steps(spec), a.cwd
    run_dir = Path(cwd).parent
    bad = validate_steps(steps, spec.get("steps"))
    if getattr(a, "try_test", None):
        bad.append("--try-test is not supported with steps")
    if bad:
        for b in bad:
            print(f"[mini-runner] precheck FAIL: {b}", flush=True)
        print("RESULT " + tag_result(f"FAIL bad-spec {'; '.join(bad)[:400]}"), flush=True)
        return RC_FAIL
    err, warn = precheck_all(spec)  # the whole chain, before the first step spends model time
    why = report_precheck(err, warn)
    if err:
        print("RESULT " + tag_result(f"FAIL bad-spec {why}"), flush=True)
        return RC_FAIL
    total = sum(int(sp.get("timeout", 2400)) for sp in steps)
    if not check_only:
        install_signal_handlers(total, int(spec.get("deadline_grace", 120)))
    usage = Usage()
    ch = {"usage": usage}
    rows, prior_cmds, summary, replayed = [], [], "", []
    review = run_dir / "REVIEW.md"
    review.unlink(missing_ok=True)
    refs = run_dir / STEP_REFS
    if not check_only:
        shutil.rmtree(refs, ignore_errors=True)  # this run's passing steps only
    for i, sp in enumerate(steps, 1):
        sp = dict(sp)
        t = sp.get("task")
        if isinstance(t, str):
            if is_inline_task(t):
                sp["_task_text"] = t
                sp.pop("task")  # prepare() must not treat prose as a path
            elif Path(os.path.expanduser(t)).is_absolute():
                sp["_task_text"] = Path(os.path.expanduser(t)).read_text()
            else:
                sp["_task_rel"] = t
        sp["_guard"] = dict(ch.get("guard") or {})  # content hashes of earlier steps' new files: this step must not edit them
        sp["_prior_tests"] = list(prior_cmds)
        sp["_prev_summary"] = summary
        ch.update(n=i, first=i == 1, why="", changed=[], review=run_dir / f"REVIEW.step{i}.md", replayed=False)
        print(f"[mini-runner] === step {i}/{len(steps)}: {sp['test_file']} ===", flush=True)
        rc = two_phase(a, sp, list(sp["allowed"]), local, check_only=check_only, chain=ch)
        text = ch["review"].read_text() if ch["review"].exists() else ""
        ch["review"].unlink(missing_ok=True)
        rows.append((i, sp["test_file"], ("PASS (replayed)" if ch["replayed"] else "PASS") if rc == RC_OK and not check_only
                     else ("ok" if rc == RC_OK else "FAIL"), text))
        if rc != RC_OK:
            rows[-1] = (i, sp["test_file"], "FAIL " + re.sub(r"^FAIL\s+(mode=two-phase\s+)?", "", ch["why"])[:200], text)
            rows += [(j, steps[j - 1]["test_file"], "not run", "") for j in range(i + 1, len(steps) + 1)]
            if usage.rows:
                print(usage.summary(), flush=True)
            if not check_only:
                write_chain_review(review, rows, f"FAIL step-{i}")
                print(f"[mini-runner] REVIEW packet: {review}", flush=True)
            reason = re.sub(r"^FAIL\s+(mode=two-phase\s+)?", "", ch["why"]) or "failed"
            print("RESULT " + tag_result(f"FAIL step-{i} {reason}"), flush=True)
            return RC_FAIL
        if not check_only:  # what `mini-finish` replays when a later step fails and Claude finishes it
            save_step_ref(cwd, refs / f"s{i}", [*edit_scope(sp, sp["allowed"]), sp["test_file"]])
        if ch["replayed"]:
            replayed.append(i)
        prior_cmds.append(sp["test_cmd"])
        first_line = next((ln.strip("# ").strip() for ln in (sp.get("_task_text") or Path(cwd, "TASK.md").read_text()).splitlines() if ln.strip()), "")
        summary = (summary + " " if summary else "Earlier steps are already done in this working tree and must stay working. ") + (
            f"Step {i} ({first_line[:160]}) changed {', '.join(ch['changed']) or 'no tracked files'} and added the test "
            f"`{sp['test_file']}`; do not edit or undo that.")
    if check_only:
        print(f"[mini-runner] check-only: {len(steps)} steps ok", flush=True)
        return RC_OK
    if usage.rows:
        print(usage.summary(), flush=True)
    write_chain_review(review, rows, f"PASS ({len(steps)} steps)")
    print(f"[mini-runner] REVIEW packet: {review}", flush=True)
    print(f"RESULT PASS mode=two-phase steps={len(steps)} " + " ".join(f"step-{i}=ok" for i, *_ in rows)
          + (f" replayed_through={max(replayed)}" if replayed else ""), flush=True)
    return RC_OK


# ---- diagnose mode (read-only root-cause report, gated by diag_check.py) ---------------------------------

DIAG_CONTRACT = """THIS IS A READ-ONLY DIAGNOSIS. Do NOT edit, create or delete any file except DIAGNOSIS.md in this directory.
Do NOT apply or try your fix here (a later stage writes the fix from your report): an edited source file is detected and put back,
and a report that quotes your edited code is rejected. To try a fix idea, copy first (cp -r . /tmp/tryfix && cd /tmp/tryfix) and
edit the copy; DIAGNOSIS.md here quotes the ORIGINAL files.
Investigate with grep/sed/nl (read only), then write the report with ONE shell command:
  cat > DIAGNOSIS.md <<'EOF'
  ...report...
  EOF
and then submit. The file must contain ONLY the report (no narration such as "Let me..." in it), >= 800 bytes, with these H2 headings:

## Code path
At least 3 fenced quotes copied VERBATIM from the code, each fence's info string exactly `<lang> <path>:<N>[-<M>]`, e.g.
```<lang> <path>:475-490
<the real lines 475-490 of <path>, copied exactly>
```
<path> is the file's FULL path relative to this directory, exactly as `git ls-files` prints it (never a bare file name, never a
guessed or remembered prefix; find it with `git ls-files | grep NAME`). Every line you quote must really be in <path> within lines N..M (+-5). Append each quote mechanically with `sed -n 'N,Mp' FILE >> DIAGNOSIS.md` (<= 10 lines per block; write the fence lines around it with printf) rather than typing code, and cite the same N-M. A quote that is not in the file rejects the whole report. Walk the failing path step by step
(caller -> callee -> the line that throws or decides wrongly). Bare `path:N` mentions in prose must exist too, with the same full path.
## Root cause
ONE sentence, at most 400 characters, naming the function and the faulty decision.
## Second cause
A contributing cause (e.g. what stops the caller recovering), or `None found` with a reason.
## Minimal fix
A File: line, then one Function: line per function whose code the fix changes (usually one; when the same fault is copied in
parallel functions of that file, e.g. a radarr and a sonarr twin, one line for each, at most 3), each holding only the name;
then a short description of the change:
- File: `path/to/file.js`
- Function: `functionName`
When the fix edits a module-level value (a dict, list, set or constant assigned at the top of the file, e.g. a table of known
keys), add a `- Constant: `NAME`` line for it, and put on the Function: line the function whose decision reads that value (the
regression test calls it). A Function: line never names a value.
Code you propose (the fix's or the test's new lines) goes in a fence with only the language (```python, ```js), never with
`<lang> <path>:<N>`: that info string says the lines are in the file now.
## Regression test
Which test to add, its setup, and the assertion that fails on the current code.
## Not verified
What you did not read or could not confirm. Label guesses as guesses.

"""

DIAG_POLISH_STEPS = 8  # the one extra attempt when only the report's form failed (diag_form_only)
DIAG_FORM_FAILS = re.compile(r"Root cause is \d+ chars \(> \d+\)|Root cause must be ONE sentence|report starts with narration")


def diag_form_only(fails):
    """True when every gate failure is about the report's form, not its content: the trigger for one short extra attempt
    after the last one (ranges 6414eb3abf32: three attempts, the last report rejected only for a 507-char Root cause)."""
    return bool(fails) and all(DIAG_FORM_FAILS.match(f) for f in fails)


DIAG_POLISH_PLACEHOLDER = "WRITE YOUR ONE SENTENCE HERE"
DIAG_POLISH_RECIPE = ("python3 - <<'PY'\n"
                      "import pathlib, re\n"
                      "p = pathlib.Path('DIAGNOSIS.md')\n"
                      f"new = r'''{DIAG_POLISH_PLACEHOLDER}'''.strip()\n"
                      "assert new and not new.isupper(), 'put your sentence in place of the placeholder'\n"
                      "t = p.read_text()\n"
                      "t, k = re.subn(r'(?msi)^(## Root cause[^\\n]*\\n).*?(?=^## |\\Z)', lambda m: m.group(1) + '\\n' + new + '\\n\\n', t, count=1)\n"
                      "assert k == 1, 'no ## Root cause heading'\n"
                      "m = re.search(r'(?m)^## ', t)\n"
                      "p.write_text(t[m.start():] if m else t)\n"
                      "print('written')\n"
                      "PY\n")


def diag_polish_note(text, fails, dc):
    """The form-repair prompt (diag_form_only): the rejected Root cause inline and a one-command recipe that replaces it (and
    drops any narration before the first heading), with nothing to investigate. bench-dupes3 69c0a9690916: the repair attempt
    got the diagnose contract and TASK.md again, re-read the source for all 8 steps and never edited the report."""
    cur = (dc.parse(text)[0].get("Root cause") or "").strip()
    lim = getattr(dc, "ROOT_MAX", 400)
    return (f"DIAGNOSIS.md passed every content check; only its FORM was rejected: {'; '.join(fails)}.\n\n"
            f"The current text under ## Root cause:\n\n{cur[:3000]}\n\n"
            f"Rewrite it as ONE sentence of at most {lim} characters (aim for {lim - 100}): no list, no code block, no second paragraph. "
            "Keep the function name and the faulty decision; drop what the Code path already shows (line numbers, secondary "
            "effects, extra examples). Do NOT read, grep or list any source file, and do not change any other section: everything "
            "you need is above.\n\n"
            f"Step 1: run this ONE command with your sentence in place of {DIAG_POLISH_PLACEHOLDER} (it replaces the section and "
            "drops any text before the first heading):\n\n```bash\n" + DIAG_POLISH_RECIPE + "```\n\n"
            "Step 2: print the section back with `awk '/^## /{p=/^## Root cause/} p' DIAGNOSIS.md` and check it is ONE sentence. "
            f"Step 3: submit. You have {DIAG_POLISH_STEPS} steps.\n")


def diag_polish_drift(before, after, dc):
    """Sections other than Root cause that a form repair changed (whitespace-normalized): the repair may only reword that one
    section, so any other change fails the attempt rather than reach the derive stage unreviewed."""
    norm = lambda s: " ".join((s or "").split())  # noqa: E731
    a, b = dc.parse(before)[0], dc.parse(after)[0]
    return sorted(h for h in set(a) | set(b) if h != "Root cause" and norm(a.get(h)) != norm(b.get(h)))


DIAG_STEPS = 80  # default step cap per attempt; 40 ran out mid-reading on all 3 attempts of cc6618122c9b (~9 s/step, so 80 fits diag_wall 900)


def diag_draft_step(steps):
    """The step by which the diagnose prompt asks for a complete first draft (diag_budget) and the nudge first fires."""
    return max(8, min(20, steps // 2))


DIAG_SAY = ("Write your COMPLETE first draft of DIAGNOSIS.md NOW with one cat command (all six headings, quotes from lines you have "
            "already read, open questions under ## Not verified; if part of it is written, append the missing sections with one "
            "`cat >> DIAGNOSIS.md <<'EOF'` command), even if you are not finished; then keep investigating and improve it. "
            "At the step cap a report without all six headings fails the job.")


DIAG_FIX_SAY = ("Fix exactly the problems the rejection listed, in DIAGNOSIS.md, NOW, from what you have already read: edit those lines "
                "(python or sed), or rewrite the file with one `cat > DIAGNOSIS.md <<'EOF'` command that keeps every part that was not "
                "rejected. Read code only when a listed problem needs it. If this attempt ends with the report unchanged, the job fails.")
DIAG_FIX_STEP = 8  # a retry that starts from a rejected report: the step by which its prompt asks for the fix, and the nudge's first


def diag_draft(cwd, spec, steps, first, heads=(), rejected=""):
    """The diagnose nudge for one attempt (not the form repair): NEEDS.md also counts as written when the needs-info channel is on;
    heads (diag_check.HEADINGS): a report missing one of them is not written yet (paramr2 f4f87714860e); rejected: the sha256 of the
    previous attempt's rejected report, which does not count as written until this attempt changes it (sqlmetar3 f28c586f4214:
    D2 re-explored for 59 steps and never touched its rejected report), with DIAG_FIX_SAY as the instruction."""
    ni = needs_info_active(spec)
    return draft_nudge(Path(cwd, "DIAGNOSIS.md"), first, max(5, steps // 8), steps,
                       (DIAG_FIX_SAY if rejected else DIAG_SAY)
                       + (" (Or NEEDS.md instead, if TASK.md cannot be diagnosed without an answer.)" if ni else ""),
                       alt=[Path(cwd, "NEEDS.md")] if ni else (), heads=heads, sha=rejected)


def diag_missing_note(fails):
    """Retry text for a report that lacks headings: append exactly those sections in one command, before any other fix. ''
    when no heading is missing. paramr2 f4f87714860e: attempts 2 and 3 got 'Edit it; do not rewrite' for a report holding only
    ## Code path, and re-explored the repo instead of adding the five missing sections."""
    miss = [m.group(1) for m in (re.match(r"missing section '## (.+)'$", f) for f in fails) if m]
    if not miss:
        return ""
    return (f"DIAGNOSIS.md has no {', '.join('## ' + h for h in miss)} section{'s' if len(miss) > 1 else ''}. FIRST, within your "
            f"first 8 steps, append {'them' if len(miss) > 1 else 'it'} with ONE command, from what the report and the previous "
            "attempt's notes already say (the rules below say what each section holds):\n"
            "  cat >> DIAGNOSIS.md <<'EOF'\n" + "".join(f"  ## {h}\n  ...\n" for h in miss) + "  EOF\n"
            "Then read the whole report back and fix the other listed problems, if any.\n\n")


def diag_budget(steps, fix=False):
    """Step-budget paragraph: draft early, refine, submit before the cap (cc6618122c9b read code for 40 steps and never wrote).
    fix: a retry that starts from a rejected report: fix it early, no new investigation (sqlmetar3 f28c586f4214 D2 was told "do not
    re-explore" next to the first attempt's "keep investigating" budget, re-explored and never edited the report)."""
    if fix:
        return (f"STEP BUDGET: you have at most {steps} steps (one command each), and less time than the first attempt had. "
                f"DIAGNOSIS.md is already written: fix the listed problems in it by step {DIAG_FIX_STEP}, then read it back and submit. "
                "Read code only when a listed problem needs it (a quote to copy, the one file the fix changes). An attempt that ends "
                "with the report unchanged FAILS the job.\n\n")
    draft = diag_draft_step(steps)
    return (f"STEP BUDGET: you have {steps} steps (one command each). At the cap the run is killed and an unwritten report FAILS the job, "
            f"however much you read. By step {draft} at the latest, write a COMPLETE first draft of DIAGNOSIS.md with the cat command "
            "(all six headings, quotes from lines you have already read, open questions under ## Not verified), even if you are not "
            "finished. Then keep investigating and rewrite it with the same cat command as you learn more. Stop reading and submit by "
            f"step {max(draft + 1, steps - 5)}. Answer the task's questions from the code you read; you do not need to read every related file.\n\n")


def diag_carry(traj, cwd, limit=2000, wrote=False):
    """Summary of a previous attempt: files/line ranges it read, searches it ran, its own notes. '' when the trajectory is
    unreadable or empty. Fed to the next attempt so it does not restart the exploration. wrote: that attempt did write a
    (rejected) report, so the header must not say it ran out of steps before writing one (cfgkeys 2cdee16a8b71 said both)."""
    try:
        msgs = json.loads(Path(traj).read_text()).get("messages", [])
    except (OSError, ValueError):
        return ""
    roots = sorted({str(Path(cwd).resolve()), str(Path(cwd).absolute())}, key=len, reverse=True)  # /var vs /private/var on macOS
    reads, greps, notes = [], [], []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        txt = re.sub(r"</?think>", "", str(m.get("content") or "")).strip()
        if len(txt) > 80 and not (re.match(r"(Let me|Now let me|Good|I'll)\b", txt) and len(txt) < 160):  # skip pure narration
            notes.append(" ".join(txt.split())[:300])
        for act in (m.get("extra") or {}).get("actions") or []:
            c = str(act.get("command", ""))
            for root in roots:
                c = c.replace(root + "/", "").replace(root, ".")
            r = re.search(r"([\w./-]+\.\w+)\s*\|\s*sed -n\s*['\"]?(\d+),(\d+)p", c)
            if r:
                reads.append(f"{r.group(1)}:{r.group(2)}-{r.group(3)}")
            elif re.match(r"\s*(grep|rg|git grep|find|ls)\b", c):
                greps.append(" ".join(c.split())[:120])
    if not (reads or greps or notes):
        return ""
    out = ("The previous attempt already did this exploration; do NOT redo it:\n" if wrote else
           "The previous attempt ran out of steps BEFORE writing DIAGNOSIS.md. It already did this; do NOT redo the exploration:\n")
    if reads:
        out += "Read: " + ", ".join(dict.fromkeys(reads)) + "\n"
    if greps:
        out += "Searched: " + " ; ".join(list(dict.fromkeys(greps))[-8:]) + "\n"
    if notes:
        body, picked = "", []
        for n in reversed(notes):  # latest notes first-in: they carry the most understanding
            if len(body) + len(n) > limit:
                break
            picked.insert(0, n)
            body += n
        out += "Its notes (unverified leads; re-read only the exact lines you will quote):\n" + "".join(f"- {n}\n" for n in picked)
    return out + "Write the full draft DIAGNOSIS.md within your first 8 steps, then refine it.\n\n"


def _diag_checker():
    import importlib.util
    sp = importlib.util.spec_from_file_location("diag_check", os.environ.get("DIAG_CHECK") or Path(os.path.realpath(__file__)).parent / "diag_check.py")
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def diag_starting_points(cwd, spec):
    """`mini-spec WT --locate P...` output (graph block included), capped at 60 lines; '' when unavailable or it fails."""
    pats = [p for p in as_list(spec.get("locate")) if p]
    if not pats:
        return ""
    exe = os.environ.get("MINI_SPEC_BIN") or str(Path(os.path.realpath(__file__)).parent / "mini-spec")
    try:
        r = subprocess.run([sys.executable, exe, str(Path(cwd).resolve()), *sum((["--locate", p] for p in pats), [])],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = r.stdout.splitlines()
    if r.returncode != 0 or not lines:
        return ""
    more = f"\n... ({len(lines) - 60} more lines cut)" if len(lines) > 60 else ""
    return ("## Starting points (mechanical grep/graph hits for the symptom: leads, not findings. Read the code before you cite it.)\n\n"
            + "\n".join(lines[:60]) + more + "\n\n")


def diag_reject_note(cwd, fails, limit=3000):
    """Rejection text for the retry: a citation whose quote is not in the file is paired with the REAL lines of that range
    (the invented quote is dropped, so the model does not re-copy it). Repeated problems are said once, the non-citation ones
    first, and the text is cut between problems with a count of what was cut (cfgkeys 2cdee16a8b71: eight per-line citation
    repeats filled the 3000-char cap and cut the one `Function: DEFAULTS` problem out of the retry prompt)."""
    head, cites, seen = [], [], set()
    for f in fails:
        m = re.match(r"citation (\S+?):(\d+)-(\d+) \(report line (\d+)\): quoted line not found", f)
        if not m:
            head.append(f)
            continue
        path, n, e = m.group(1), int(m.group(2)), int(m.group(3))
        at = re.search(r"\((it is at lines? [\d, .]+)\)$", f)
        cites.append(f"citation {path}:{n}-{e} (report line {m.group(4)}): " + (
            f"a quoted line is outside the cited range ({at.group(1)}): change the range to cover it, or quote only the cited lines"
            if at else "a quoted line is not in the file (invented text removed)"))
        if (path, n, e) in seen:
            continue
        seen.add((path, n, e))
        try:
            src = Path(cwd, path).read_text(errors="replace").splitlines()
        except OSError:
            continue
        lo, hi = max(1, n - 5), min(len(src), e + 5)
        cites.append(f"REAL lines {lo}-{hi} of {path} (copy from these, or run sed -n '{n},{e}p' {path} >> DIAGNOSIS.md):\n"
                     + "\n".join(f"{i}\t{src[i - 1]}" for i in range(lo, hi + 1)))
    items = list(dict.fromkeys(head + cites))
    out = ""
    for k, x in enumerate(items):
        add = (" | " if out else "") + x
        if len(out) + len(add) > limit:
            if not out:
                return x[:limit]
            return out + f" | (+{len(items) - k} more problem(s) not shown: fix these first, the gate lists the rest)"
        out += add
    return out


def diagnose(a, spec, local, check_only=False, sub=None):
    """mode "diagnose": the model writes DIAGNOSIS.md, diag_check.py gates it, a fresh mini run edits it on rejection.
    A source edit is restored and the report is judged against the real code (an edit is a rejected attempt, not the end);
    first, open citation fences are closed, shortened paths the report resolves are written in full (a placeholder or
    wrong-directory citation path takes the one file that holds its quoted lines), a quoted line mis-copied only in its quote
    characters, backslashes or whitespace takes the source line's text, a Minimal fix or Regression test fence that cites a file
    but holds code that is not in it is un-cited (proposed code), and citation ranges that drifted from real quotes are moved
    (diag_check close_fences, fix_paths, fix_quotes, fix_proposed, fix_ranges). RESULT PASS|FAIL mode=diagnose; never exits 3. sub (a dict from auto()): first
    stage of an auto run, the deadline is auto's and the result line goes to sub["line"] instead of RESULT."""
    cwd = a.cwd
    run_dir = Path(cwd).parent
    total = int(spec.get("timeout", 1800))
    t_start = _now()
    deadline = t_start + total
    def result(ok, why):
        line = f"{'PASS' if ok else 'FAIL'} mode=diagnose {why}"
        if sub is not None:
            sub["line"] = line
            print(f"[mini-runner] auto stage diagnose result: {line}", flush=True)
        else:
            print(f"RESULT {line}", flush=True)
    if not check_only and sub is None:
        install_signal_handlers(total, int(spec.get("deadline_grace", 120)))
    prepare(cwd, spec)
    tm = Path(cwd, "TASK.md")
    if not tm.is_file():
        print("[mini-runner] diagnose: no TASK.md (spec task)", flush=True)
        result(False, "bad-spec no task")
        return RC_FAIL
    task_md = tm.read_text()
    pre_untracked = sorted(untracked_files(cwd))
    dspec = {"must_mention": as_list(spec.get("must_mention")), "untracked_ok": pre_untracked}
    wd = (f"WORKING DIRECTORY: {Path(cwd).resolve()}. The code you must read is in this directory (your shell starts here). "
          "Use relative paths. Do NOT search `/`, cd elsewhere, or read any file outside this directory; "
          "a copy of the repo elsewhere (e.g. /private/tmp) is NOT yours.\n\n")
    banner = wd + READ_HINT + graph_lookup_note(cwd, spec)
    steps = int(spec.get("diag_steps", DIAG_STEPS))
    starting = diag_starting_points(cwd, spec)
    contract = DIAG_CONTRACT + (DIAG_NEEDS_RULE if needs_info_active(spec) else "")
    base_prompt = banner + contract + diag_budget(steps) + starting + "--- TASK.md ---\n" + task_md
    try_diag = getattr(a, "try_diagnosis", None)
    if check_only and try_diag:  # model-free: gate a hand-finished report exactly as a model's would be (mini-finish)
        try:
            dc = _diag_checker()
            shutil.copy2(os.path.expanduser(try_diag), Path(cwd, "DIAGNOSIS.md"))
        except Exception as e:
            print(f"[mini-runner] try-diagnosis: cannot run the check: {type(e).__name__}: {e}", flush=True)
            return RC_FAIL
        ok, fails = dc.check(cwd, "DIAGNOSIS.md", dspec)
        print(f"[mini-runner] try-diagnosis {try_diag}: {'ACCEPTED' if ok else 'REJECTED: ' + '; '.join(fails)[:1500]}", flush=True)
        return RC_OK if ok else RC_FAIL
    if check_only:
        print("[mini-runner] check-only: diagnose prompt follows\n" + base_prompt[:2500], flush=True)
        return RC_OK
    try:
        dc = _diag_checker()
    except Exception as e:
        print(f"[mini-runner] cannot load diag_check.py: {type(e).__name__}: {e}", flush=True)
        result(False, "bad-spec diag_check unavailable")
        return RC_FAIL
    usage, model, key = Usage(), a.model or DEFAULT_MODEL, local.get("api_key", "")
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}, "OPENAI_API_BASE": local.get("base_url", ""),
           "MSWEA_COST_TRACKING": "ignore_errors", "MSWEA_CONFIGURED": "true", "PYTHONDONTWRITEBYTECODE": "1"}
    review = run_dir / "REVIEW.md"
    review.unlink(missing_ok=True)
    report = Path(cwd, "DIAGNOSIS.md")
    attempts, history, fails, last_msg, stray_all = int(spec.get("diag_attempts", 3)), [], [], "", []

    def write_review(verdict):
        body = report.read_text(errors="replace") if report.is_file() else "(not written)"
        review.write_text(
            f"# REVIEW packet: {verdict}\n\nmode: diagnose  allowed: [] (read-only)\n\n## Attempts\n\n"
            + "".join(f"- attempt {i}: {h}\n" for i, h in enumerate(history, 1))
            + ("\n## Remaining gate failures\n\n" + "".join(f"- {f}\n" for f in fails) if fails else "")
            + f"\n## DIAGNOSIS.md\n\n```\n{body}\n```\n\n## Model's final message\n\n```\n{last_msg or '(none)'}\n```\n\n"
            + ("## Context per run\n\n" + "".join(f"- {c}\n" for c in usage.ctx) + "\n" if usage.ctx else "")
            + "Review asks: do the cited lines really show the claimed mechanism? is the root cause the cause, not a symptom? does the fix target the right function?\n")
        print(f"[mini-runner] REVIEW packet: {review}", flush=True)

    def finish(ok, why, verdict):
        if usage.rows:
            print(usage.summary(), flush=True)
        write_review(verdict)
        result(ok, why)
        return RC_OK if ok else RC_FAIL

    def diag_watch(tj):  # either diagnose attempt: the same command 3 times in a row with identical output ends it (failfast_reason)
        return failfast_reason(traj_steps(tj), "D", {}, cwd)

    n, polish, trip = 0, False, None
    while True:
        n += 1
        if n > attempts:  # every attempt used: one short extra attempt only when the report is rejected for its form alone
            if polish or not (report.is_file() and diag_form_only(fails)):
                break
            polish = True
        left = deadline - _now()
        if left < 60:
            history.append("no time left")
            break
        if polish:
            before = report.read_text(errors="replace")
            note = diag_polish_note(before, fails, dc)
        elif fails and report.is_file():
            note = diag_missing_note(fails) + (f"DIAGNOSIS.md was REJECTED: {diag_reject_note(cwd, fails)}. Edit it; do not rewrite from scratch. "
                    "(It is in this directory; read it, fix exactly the listed problems with targeted edits, then submit. "
                    "Do not re-explore: the files you need are already read.)\n\n"
                    + diag_carry(run_dir / f"{Path(cwd).name}.D{n - 1}.traj.json", cwd, limit=800, wrote=True))
        elif fails:
            note = (f"The previous attempt left no usable DIAGNOSIS.md ({' | '.join(fails)[:600]}). Write it now with the single cat command.\n\n"
                    + diag_carry(run_dir / f"{Path(cwd).name}.D{n - 1}.traj.json", cwd))
        else:
            note = ""
        if trip:  # the previous attempt was cut short: say why first (cfgkeys 2cdee16a8b71 ran one grep ~20 times in a row)
            note = f"The previous attempt was STOPPED: {trip}. Never re-run a command whose output you already have.\n\n" + note
        # the previous attempt's rejected report: this attempt must change it (DIAG_FIX_SAY, the fix budget); not the form repair
        rejected = hashlib.sha256(report.read_bytes()).hexdigest() if n > 1 and not polish and fails and report.is_file() else ""
        print(f"[mini-runner] diagnose attempt {n}/{attempts}{' (form repair)' if polish else ''} ({int(left)}s left)", flush=True)
        traj = run_dir / f"{Path(cwd).name}.D{n}.traj.json"
        prompt = (wd + note if polish  # the repair investigates nothing: no read hint, contract or TASK.md (dupes3 69c0a9690916)
                  else banner + note + contract + diag_budget(steps, fix=bool(rejected)) + starting + "--- TASK.md ---\n" + task_md
                  if n > 1 else base_prompt)
        log, timed_out = run_mini(model, prompt, traj, cwd, env, key, left, usage, DIAG_POLISH_STEPS if polish else steps, tag=f"D{n}",
                                  wall=min(300, int(spec.get("diag_wall", 900))) if polish else int(spec.get("diag_wall", 900)),
                                  no_think=no_think_for(spec, "A"), watch=diag_watch, overlay=POLISH_OVERLAY if polish else DIAG_OVERLAY,
                                  draft=None if polish else diag_draft(cwd, spec, steps, DIAG_FIX_STEP if n > 1 and (rejected or not report.is_file()
                                                                       or diag_missing_note(fails)) else diag_draft_step(steps),
                                                                       heads=getattr(dc, "HEADINGS", ()), rejected=rejected))
        trip = usage.trip if usage.trip and usage.trip.startswith("fail-fast") else None
        if trip:
            print(f"[mini-runner] diagnose attempt {n} stopped: {trip}", flush=True)
        last_msg = final_message(traj)
        ex = exit_status(traj) or ("killed" if timed_out else "none")
        revert_bytecode(cwd)
        ni = needs_info_step(cwd, spec, traj, None)  # NEEDS.md (if any) is removed here
        if ni and ni[0] == "park":
            if usage.rows:
                print(usage.summary(), flush=True)
            print("RESULT PAUSED needs-info", flush=True)
            return park_run(run_dir / f"{Path(cwd).name}.needs-info.json", "needs_info", ni[1],
                            f"[mini-runner] PAUSED: needs-info: {ni[1]['question'][:200]}; answer with `mini-answer <job> \"text\"` (not graded, not a failure)")
        tracked = changed_files(cwd)
        scope_note = None
        if tracked:  # restored, then the report is judged against the real code: an edit is a rejected attempt, not the end
            restore_paths(cwd, {}, tracked)
            scope_note = (f"scope: you edited tracked source {tracked}; it was restored. Diagnosis is read-only: never edit, create "
                          "or delete source files; every citation must quote the file as it is now")
            print(f"[mini-runner] diagnose: tracked source edited and restored: {tracked}", flush=True)
        written = report.read_text(errors="replace") if polish and report.is_file() else None  # the model's text, before the fixups below
        closed = getattr(dc, "close_fences", lambda *_: [])(cwd, "DIAGNOSIS.md") if report.is_file() else []
        if closed:  # bench-dupes2 7938a1ad1fec: a script wrote the quotes without their closing ``` (the quoted lines are the file's)
            print(f"[mini-runner] diagnose: open citation fences closed after their cited lines: {'; '.join(closed)}", flush=True)
        full = getattr(dc, "fix_paths", lambda *_: [])(cwd, "DIAGNOSIS.md") if report.is_file() else []
        if full:  # scrapy r2 5f2fbdc72e8e: D1 rejected for `crawl.py:33` beside its own citations of scrapy/commands/crawl.py
            print("[mini-runner] diagnose: paths resolved: " + "; ".join(f"{o} -> {n_}" for o, n_ in full), flush=True)
        quotes = getattr(dc, "fix_quotes", lambda *_: [])(cwd, "DIAGNOSIS.md") if report.is_file() else []
        if quotes:  # bibtexr2 5329a2a546bb: a regex and a quote-heavy string copied with one quote character extra or missing
            print("[mini-runner] diagnose: quoted lines restored to the file's text: "
                  + "; ".join(f"{c}: {o.strip()[:80]!r} -> {n_.strip()[:80]!r}" for c, o, n_ in quotes), flush=True)
        proposed = getattr(dc, "fix_proposed", lambda *_: [])(cwd, "DIAGNOSIS.md") if report.is_file() else []
        if proposed:  # conan2 r1 014d9fdb192b: the fix's new validation loop cited as `python ...profile_loader.py:249-249`
            print("[mini-runner] diagnose: proposed-code fences un-cited (the fix's or the test's new code): " + "; ".join(proposed), flush=True)
        moved = dc.fix_ranges(cwd, "DIAGNOSIS.md") if report.is_file() else []
        if moved:  # the quotes are real, only the line numbers drifted (cited from memory or from a since-restored edit)
            print(f"[mini-runner] diagnose: citation ranges moved to where the quoted lines are: "
                  + "; ".join(f"{o} -> {n_}" for o, n_ in moved), flush=True)
        ok, fails = dc.check(cwd, "DIAGNOSIS.md", dspec)
        drift = diag_polish_drift(before, written, dc) if written is not None else []
        if drift:  # the repair may reword the Root cause only; anything else it changed has not been through an investigation
            ok, fails = False, [f"form repair changed other sections: {', '.join(drift)}"] + fails
        if scope_note and not ok:
            fails.insert(0, scope_note)
        if ni and not ok:
            fails.insert(0, f"needs-info question rejected: {ni[1]}")
        history.append(("form repair: " if polish else "") + f"exit={ex} " + ("stopped: repeated command " if trip else "")
                       + (f"scope: edited {tracked} (restored) " if scope_note else "")
                       + (f"fences closed: {len(closed)} " if closed else "") + (f"paths resolved: {len(full)} " if full else "")
                       + (f"quotes restored: {len(quotes)} " if quotes else "")
                       + (f"proposed code un-cited: {len(proposed)} " if proposed else "")
                       + (f"ranges moved: {len(moved)} " if moved else "") + f"report={'%dB' % report.stat().st_size if report.is_file() else 'none'} " + ("ACCEPTED" if ok else f"REJECTED ({len(fails)} problem(s))"))
        print(f"[mini-runner] diagnose gate: {'ACCEPTED' if ok else 'REJECTED: ' + '; '.join(fails)[:1500]}", flush=True)
        if ok:
            return finish(True, f"attempts={n} bytes={report.stat().st_size}", "PASS")
        for x in sorted(untracked_files(cwd) - set(pre_untracked) - {"DIAGNOSIS.md", "TASK.md", "ANSWER.md"}):  # stray files: removed, the report is kept
            if Path(cwd, x).is_file():
                Path(cwd, x).unlink()
    why = "; ".join(fails)[:400] if fails else "no report"
    return finish(False, f"check attempts={len(history) - polish}/{attempts}{' +form repair' if polish else ''}: {why}", "FAIL check")


# ---- mode "auto": a description in, a reviewed test + fix out ---------------------------------------------------------------
# Claude writes the symptom (optionally a file / function hint). Stage 1 diagnose (read-only, DIAGNOSIS.md names File:/Function:
# under ## Minimal fix), stage 2 derive a two-phase spec with mini-spec's build() for that one file and function, stage 3 two-phase
# (the model writes the test, it is frozen, a fresh context writes the fix, every two-phase gate). One queue job, one RESULT line
# (mode=auto) and one compact packet for the reviewer (REVIEW-AUTO.md). The model writes BOTH the oracle and the answer here, so
# every auto run is reviewed by Claude (mini-scorecard picks 100%) and the must-not-offload areas of
# docs/offload-increase-plan.md section 4 are refused before any model call: by repo / path / function name (AUTO_FORBID) and by
# the description's words (AUTO_FORBID_TEXT). mini-submit runs the same check at the door.

AUTO_FORBID = re.compile(
    r"secret|keychain|passw|passkey|credential|crypt|oauth|migrat|bfmr|resell|canary|scorecard|diag.check|ollama.queue|queue.events|"
    r"bloomgauge|payment|payout|billing|invoice|refund|sale.?pric"
    r"|(?:^|[^a-z0-9])(?:tax(?!on)|pnl|auth(?!or)|token(?!i[sz])|jwt|login|gates?(?:[^a-z]|$)|mini[-_]|intake|throttle)", re.I)
AUTO_FORBID_TEXT = re.compile(
    r"\b(?:resell|bfmr|tax(?:es)?|p&l|pnl|auth(?:entication|orization)?|secrets?|keychain|passwords?|passkeys?|credentials?|"
    r"api[ _-]?keys?|(?:api|auth|access|refresh|bearer|session|csrf|id)[ _-]?tokens?|oauth|jwt|login|migrations?|encrypt\w*|decrypt\w*|"
    r"payments?|payouts?|billing|invoices?|refunds?|sale[ _-]?price)\b", re.I)
AUTO_TIMEOUT = 5400       # whole job (diagnose + test + fix); the two-phase stage gets what is left, at most its own default
AUTO_DIAG_TIMEOUT = 2700  # diagnose stage: room for all three attempts. At 1500 (real-issue bench, 2026-11-05..07) 9 of 12
# lost diagnoses never started their third (D2 was killed at ~570 s); the two-phase stage still gets its own 2400 s.
AUTO_PASS_KEYS = ("env", "keep", "test_attempts", "fix_attempts", "test_steps", "fix_steps", "test_wall", "fix_wall", "gate_reserve",
                  "fix_mode", "min_tests", "min_fn_cov", "no_think", "cmd_timeout", "test_cmd_timeout", "deadline_grace", "ground",
                  "bg_baseline", "user_view", "direct_fallback", "fallback_steps", "fallback_wall")


def _auto_words(s):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s or ""))  # checkAuth -> check_Auth, so camelCase names hit the word rules


def auto_forbidden(names=(), text=""):
    """[(what, value, hit)] for must-not-offload hits. names: [(what, repo name | repo-relative path | function name)];
    text: the description. [] when the job may run."""
    hits = []
    for what, v in names:
        m = AUTO_FORBID.search(_auto_words(v)) if v else None
        if m:
            hits.append((what, str(v), m.group(0).strip("_-./ ")))
    m = AUTO_FORBID_TEXT.search(text or "")
    if m:
        hits.append(("description", m.group(0), m.group(0)))
    return hits


def auto_repo_names(wt):
    """Names that identify worktree wt's repo: its own dir, the main checkout (git common dir) and origin's last path part
    (a `git clone --no-local` scratch copy keeps its source as origin)."""
    out = [Path(wt).resolve().name]
    for args in (("rev-parse", "--git-common-dir"), ("config", "--get", "remote.origin.url")):
        try:
            r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        v = r.stdout.strip()
        if r.returncode or not v:
            continue
        if args[0] == "rev-parse":
            v = Path(os.path.join(str(wt), v)).resolve().parent.name  # <main>/.git -> <main>
        else:
            v = re.sub(r"\.git$", "", v.rstrip("/")).split("/")[-1].split(":")[-1]
        out.append(v)
    return list(dict.fromkeys(x for x in out if x))


def auto_resolve(f, files):
    """Tracked repo-relative path for a hint or a diagnosis' File: (exact, else the one tracked path ending in /f), else None."""
    f = re.sub(r":\d+(?:-\d+)?$", "", re.sub(r"^\./", "", str(f or "").strip().strip("`'\"")))
    if not f:
        return None
    if f in files:
        return f
    c = [x for x in files if x.endswith("/" + f)]
    return c[0] if len(c) == 1 else None


def md_h2(text):
    """{lower-case H2 title: body} of a markdown report; `## ` lines inside code fences are not titles."""
    out, cur, fence = {}, None, False
    for ln in (text or "").splitlines():
        if ln.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"##\s+(.+?)\s*#*\s*$", ln)
        if m:
            cur = m.group(1).strip().lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def md_sec(secs, key):
    """A section's body by title prefix, as diag_check matches headings (`## Regression test idea` is the Regression test)."""
    return next((v for k, v in (secs or {}).items() if k.startswith(key)), "")


def auto_target(text):
    """(file, [function], [extra]) from DIAGNOSIS.md's `## Minimal fix` (diag_check.fix_target, the gate's parser): the File:
    value, the first name of each Function: line, and the names a line adds with `and`/`,` (auto() keeps the extras the file
    defines). None / [] when absent; dotted names keep their last part."""
    fp, firsts, extras = _diag_checker().fix_target(md_sec(md_h2(text), "minimal fix"))
    return (fp[0] if fp else None), [x.split(".")[-1] for x in firsts], [x.split(".")[-1] for x in extras]


def auto_consts(text):
    """The `Constant:` names of DIAGNOSIS.md's `## Minimal fix` (diag_check.fix_consts): module-level values the fix edits.
    auto() keeps those the file assigns at top level and puts them in the fix spec's new_names (fn_scope counts their statements)."""
    return list(getattr(_diag_checker(), "fix_consts", lambda _b: [])(md_sec(md_h2(text), "minimal fix")))


def _fn_list(fns):
    fns = [fns] if isinstance(fns, str) else list(fns)
    q = [f"`{x}`" for x in fns]
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " and " + q[-1]


def auto_diag_task(name, desc, hints, fn_hint):
    hl = [f"- File: `{f}`" for f in hints] + ([f"- Function: `{fn_hint}`"] if fn_hint else [])
    return "\n".join([
        f"# Diagnose: {name}", "", "## Problem (as reported)", "", desc.strip(), "",
        "## Reporter's hints (leads, not findings: confirm them in the code)", "", *(hl or ["None: find the code from the symptom."]), "",
        "## What to deliver", "",
        "The root cause of the Problem, and the function whose code must change to fix it (when the same fault is copied in "
        "parallel functions of one file, every copy: one `Function:` line each, at most 3).",
        "- `## Minimal fix`: the `File:` and `Function:` lines name that function exactly as it is defined at baseline (an existing "
        "function, in the file that defines it). Name the function that makes the wrong decision; name a caller only when the fix "
        "belongs in the caller. When the fix edits a module-level value (e.g. a table of known keys), name it on a `Constant:` "
        "line and the function that reads it on the `Function:` line. The fix changes source code only, never a data file (JSON, "
        "YAML, a schema): when the wrong result comes from such data, name the function that reads it.",
        "- `## Regression test`: one concrete input that shows the bug through that function, what it returns (or raises) now, and "
        "what the Problem says it should return instead. A test is written from this, so be exact.",
        "- If neither the Problem nor the code says what the right result is, say so under `## Not verified`.", ""])


def _auto_cap(s, n):
    return s if len(s) <= n else s[:n] + "\n... (cut here; the full text is in REVIEW.md)"


def auto_fix_task(name, desc, secs, f, fns, b, ms, consts=(), upd=()):
    """TASK.md for the derived two-phase stage: the description is the requirement, the diagnosis is context. fns: the target
    function(s): one, or a few parallel copies of the same fault in one file. consts: module-level values (the diagnosis's
    Constant: lines) the fix may edit; the functions read them. upd: the existing test files the fix may update (update_tests)."""
    fns = [fns] if isinstance(fns, str) else list(fns)
    consts = list(consts or ())
    _fn, tf, tcmd = b["tests"][0]

    def where(fn):
        span = ((b.get("defined") or {}).get(fn) or (None, (None, None)))[1] or (None, None)
        return f" (about lines {span[0]}-{span[1]})" if span[0] and span[1] else ""
    names = _fn_list(fns)
    text = (b.get("texts") or {}).get(f, "")
    L = [f"# TASK: {name}", "", "## Problem (as reported: this is the requirement)", "", desc.strip(), ""]
    if secs:
        L += ["## Diagnosis (a read-only model run: its code quotes were checked against the files, its reasoning was not)", ""]
        for k, title, cap in (("root cause", "Root cause", 600), ("minimal fix", "Minimal fix", 1500),
                              ("regression test", "Regression test idea", 1500), ("second cause", "Second cause", 600),
                              ("code path", "Code path", 3500)):
            v = md_sec(secs, k).strip()
            if v:
                L += [f"### {title}", "", _demote(v if len(v) <= cap else v[:cap] + "\n... (cut)"), ""]
    def at(c):
        m = re.search(rf"^(?:export\s+)?(?:(?:const|let|var)\s+)?{re.escape(c)}\s*(?::[^=\n]+)?=(?!=)", text, re.M)
        return f" (line {text.count(chr(10), 0, m.start()) + 1})" if m else ""
    cn = _fn_list(consts) if consts else ""
    L += ["## Entry point", "", f"`{f}` " + ", ".join(f"`{x}`{where(x)}" for x in fns) + "."
          + (f" Module-level value{'s' if len(consts) > 1 else ''}: " + ", ".join(f"`{c}`{at(c)}" for c in consts) + "." if consts else ""),
          "", "## Required change", "",
          (f"The diagnosis puts the fix in the module-level value{'s' if len(consts) > 1 else ''} {cn}, which {names} "
           f"read{'s' if len(fns) == 1 else ''}: change {cn} (and {names} only where the value alone cannot give the Problem's "
           "behaviour) so the behaviour matches the Problem section above. " if consts else
           f"Change {names} so the behaviour matches the Problem section above. ")
          + (f"The same fault is in each of these {len(fns)} functions: fix every one of them. " if len(fns) > 1 else "")
          + "Where the diagnosis and the Problem disagree, the Problem wins, and a diagnosis suggestion the Problem does not ask "
          "for (an extra key, value or message, \"or similar\") is not part of the change. Fix the root cause with the smallest change; do not "
          "refactor, rename or reformat. A value the Problem adds is matched exactly (after any normalisation the code already "
          "applies, e.g. lower-casing), never as a prefix or substring, unless the Problem asks for that. If a docstring or comment "
          "(the module docstring too) describes the behaviour you change, update that sentence so it stays true.", "",
          "Behaviour that must NOT change:"]
    other = [x for x in functions(f, text) if x not in fns]
    if other:
        L.append(f"- Other functions in `{f}` keep their behaviour: " + ", ".join(f"`{x}`" for x in other[:15])
                 + (" ..." if len(other) > 15 else "") + ".")
    try:
        ex, envs = ms.exports(f, text), sorted(set(ms.env_reads(text)))
    except Exception:
        ex, envs = [], []
    if ex:
        L.append(f"- The public names of `{f}` stay as they are: " + ", ".join(f"`{x}`" for x in ex[:15]) + ".")
    if b.get("callers"):
        L.append("- Callers are not edited: " + ", ".join(f"`{c}`" for c in b["callers"][:8]) + ".")
    L += ["- Inputs the Problem does not mention keep their current results, near-misses of a new value too (for a new 'x': "
          "'x2', 'xs', 'my-x').", ""]
    keep = [m.group(0) for m in (re.search(rf"(?:\bdef\s+|\bfunction\s*\*?\s*){re.escape(x)}\s*\(", text) for x in fns) if m]
    if keep:
        L += ["## Must keep", ""] + [f"- `{k}`" for k in keep] + [""]
    L += ["## Test notes", "", f"New test file `{tf}` (a new file; the runner writes a skeleton with the module loads). Run with `{tcmd}`. "
          + str(b.get("runner_desc") or ""),
          (f"Each test calls `{fns[0]}`" if len(fns) == 1 else f"Each test calls one of {names}, and every one of them is called by at least one test")
          + " (directly, or through the public function that calls it) with an input from the Problem and asserts "
          "the exact value the Problem asks for (every expected value comes from the Problem or from what the code returns "
          "today: one found in neither is rejected), so it FAILS on the current code with an AssertionError. Every case must fail at baseline "
          "(a case that already passes is rejected), except guards: a test of behaviour the Problem says must stay the same is listed in "
          + guards_hint(tf)
          + "; it must PASS on the current code and after the fix (the runner checks it now, so a wrong idea of today's behaviour "
          "is caught before the fix). When the Problem adds a value and says other values stay the same, one guard feeds "
          "near-misses of the new value (for a new 'x': 'x2', 'xs', 'my-x') and asserts today's result for each. "
          "The repo's existing tests also run (suite gate)."]
    if envs:
        L.append(f"The code reads env vars {', '.join(f'`{e}`' for e in envs)}: set what it needs inside the test before loading the module.")
    rows = [r for r in (b.get("impact") or {}).get("rows", []) if r.get("tier") != "WEAK"]
    upd = list(upd or ())
    if rows or upd:
        L += ["", "## Existing tests near this code", "", "These existing tests load this code and may assert its current behaviour. The repo "
              "suite runs them and must not gain failures; " + ("do not edit them." if not upd else
              f"do not edit them, with one exception: when a test function in {', '.join(f'`{u}`' for u in upd)} asserts exactly "
              "the behaviour the Problem changes, update that test function's expectation to what the Problem asks for (only such "
              "a function; the runner checks that each edited test fails against your fix without the edit, passes with it, and "
              "keeps its assertions). Do not edit any other test, and add no other change to those files.")]
        L += [f"- `{r['test']}` ({r.get('via', '')})" for r in rows[:6]]
        if len(rows) > 6:
            L.append(f"- and {len(rows) - 6} more (the suite runs them too)")
        L += [f"- `{u}` (calls the target)" for u in upd if u not in {r["test"] for r in rows[:6]}]
    L += ["", "## Scope", "", f"Only edit `{f}` (plus the new test file in the test phase"
          + (", and an existing test function the Problem's change makes wrong, as above" if upd else "") + ").", ""]
    return "\n".join(L)


def _mini_spec_lib():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    p = os.environ.get("MINI_SPEC_LIB") or str(Path(os.path.realpath(__file__)).parent / "mini-spec")
    ld = SourceFileLoader("mini_spec_lib", p)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("mini_spec_lib", ld))
    ld.exec_module(m)
    return m


AUTO_IMPACT_MAX = 24  # bench-cfgkeys2: 9 existing tests load the config module; mini-spec's default 6 stopped the derive stage


def auto_build(ms, wt, run_dir, name, task_path, f, fns):
    """mini-spec build() for one file and its target function(s), one test file; an ambiguous test location falls back to a
    sibling test file. Up to AUTO_IMPACT_MAX impact tests are allowed: past mini-spec's IMPACT_MAX the build only succeeds when
    the suite gate runs every one of them (listed, or the whole run), so each still has to stay green after the fix."""
    ns = dict(worktree=wt, task=str(task_path), name=name, allowed=[f], fn=([fns] if isinstance(fns, str) else list(fns)), no_fn=False,
              single=True, runner=None, test_style=None,
              runs_dir=str(Path(run_dir) / "derive"), reach_anchor=None, override=None, tried=None, graph=None, no_impact=False,
              impact_ignore=None, impact_max=AUTO_IMPACT_MAX)
    try:
        return ms.build(argparse.Namespace(**ns))
    except ms.Ambiguous as e:
        first = e
    try:
        return ms.build(argparse.Namespace(**{**ns, "test_style": "sibling"}))
    except ms.Ambiguous:
        raise first


PYTEST_FILE_RE = re.compile(r"(?:test_[^/]*|[^/]*_test)\.py")  # the file names pytest collects by default
WHOLE_PYTEST_RE = re.compile(r"(\S+) -m pytest -q -p no:cacheprovider")  # mini-spec's whole-pytest-run suite (fullmatch)
AUTO_UPD_MAX = 3  # existing test files an auto fix may update (update_tests_strict; gate update_flip checks every edit)


def _auto_related(ms, wt, files, f):
    """{path: text} of the tracked pytest files that import the python file f (mini-spec's suite rule: imports_stem on its
    load_stems names)."""
    stems, out = ms.load_stems(files, [f], "py"), {}
    for t in files:
        if not (t.endswith(".py") and PYTEST_FILE_RE.fullmatch(Path(t).name) and ms.is_test_path(t)):
            continue
        try:
            tt = Path(wt, t).read_text(errors="replace")
        except OSError:
            continue
        if any(ms.imports_stem(tt, s, "py") for s in stems):
            out[t] = tt
    return out


def _near_key(ms, f):
    """Sort key: test files nearest f first (the module's name in the test's name, then more directory names shared with f)."""
    stem, parts = ms._stem(f), set(Path(f).parent.parts)
    return lambda t: (stem not in Path(t).stem, -len(parts & set(Path(t).parent.parts)), len(Path(t).parts), t)


def _impact_tests(b):
    """The impact scan's STRONG then MEDIUM test files (mini-spec build's impact rows; WEAK ones left out)."""
    rows = [r for r in (b.get("impact") or {}).get("rows", []) if r.get("tier") != "WEAK" and r.get("test")]
    rows.sort(key=lambda r: 0 if r.get("tier") == "STRONG" else 1)
    return list(dict.fromkeys(r["test"] for r in rows))


def auto_update_tests(ms, wt, files, f, fns, b, related):
    """update_tests for auto (with update_tests_strict): existing pytest files the suite runs that call a target function by name,
    the impact scan's STRONG/MEDIUM tests first, then the other files that import f, nearest first; at most AUTO_UPD_MAX. Canvas r2
    (2026-11-05): the fix the issue asks for broke tests/test_util.py::test_obj_or_str_invalid_obj_type, which asserted the
    TypeError the issue removes, and auto never let a fix update a test. Gate update_flip lets only a test the fix makes fail
    change, and REVIEW-AUTO.md shows each edit. [] for a non-python target or no suite."""
    suite = str(b.get("suite") or "")
    if not f.endswith(".py") or not suite:
        return []
    whole, tracked = bool(WHOLE_PYTEST_RE.fullmatch(suite)), set(files)
    skip = {f, b["tests"][0][1], *(b.get("drop") or [])}
    out = []
    for t in dict.fromkeys([*_impact_tests(b), *sorted(related, key=_near_key(ms, f))]):
        if t in skip or t not in tracked or not PYTEST_FILE_RE.fullmatch(Path(t).name) or not (whole or Path(t).name in suite):
            continue
        try:
            text = related.get(t) or Path(wt, t).read_text(errors="replace")
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(x)}\b", text) for x in fns):
            out.append(t)
            if len(out) >= AUTO_UPD_MAX:
                break
    return out


def auto_narrow_suite(ms, wt, files, f, b, first=None, related=None):
    """spec suite_narrow for auto's whole-pytest-run suite (mini-spec runs everything when more than MAX_SUITE existing test files
    import the target): first (the update_tests), the impact scan's STRONG/MEDIUM tests, then the other pytest files that import
    f, nearest first; at most AUTO_NARROW_MAX. The gate runs it only when the whole suite times out at baseline (run_baselines).
    None for any other suite, a non-python target, more impact tests than fit, or nothing to list."""
    m = WHOLE_PYTEST_RE.fullmatch(str(b.get("suite") or ""))
    if not m or not f.endswith(".py"):
        return None
    tracked, skip = set(files), {b["tests"][0][1], *(b.get("drop") or [])}
    ok = lambda t: t in tracked and t not in skip and bool(PYTEST_FILE_RE.fullmatch(Path(t).name)) and Path(wt, t).is_file()
    must = [t for t in dict.fromkeys([*(first or []), *_impact_tests(b)]) if ok(t)]
    if len(must) > AUTO_NARROW_MAX:  # every impact test must stay in the gate: past the cap the whole suite stays
        return None
    rel = related if related is not None else _auto_related(ms, wt, files, f)
    sel = [*must, *sorted((t for t in rel if ok(t) and t not in must), key=_near_key(ms, f))][:AUTO_NARROW_MAX]
    return f"{m.group(1)} -m pytest -q -p no:cacheprovider " + " ".join(shlex.quote(t) for t in sel) if sel else None


def auto_diag_lines(dtext, f, fns, wt):
    """Lines of f inside the target functions' bodies that the accepted diagnosis's Root cause / Second cause cite
    (diag_check.cited_lines), at most 40: spec.diag_lines, for a spec with no reach_anchor. test_gate waives the body-coverage
    floor for a function when the phase A test runs one of them (diag_line_hit). loguru r3 bf6183725d19: emit is 84 lines whose
    six format_map calls sit on alternative branches, so the test of the cited KeyError path ran 26% (< 30%) and phase A spent
    its last attempt chasing coverage."""
    try:
        cited = _diag_checker().cited_lines(dtext, f)
        tree = ast.parse(Path(wt, f).read_text(errors="replace"))
    except Exception:  # noqa: BLE001 -- an older diag_check, an unreadable or unparsable file: no lines, the floor stays
        return []
    names = {str(x).rsplit(".", 1)[-1] for x in fns}
    spans = [sp for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
             for sp in [_py_fn_body_span(n)] if sp]
    return [n for n in cited if any(a <= n <= b for a, b in spans)][:40]


def auto_mutation_cfg(wt, f, spec):
    """Advisory mutation_gate for the derived spec (warn only; the score goes in RESULT and the packet). Python: mini_mutate mutates
    the lines the model's fix changed (base = the worktree's file). JS/TS: the runner's StrykerJS path (skipped when not installed).
    spec auto_mutation=false turns it off."""
    if spec.get("auto_mutation") is False:
        return None
    if f.endswith(".py"):
        mm = Path(os.path.realpath(__file__)).parent / "mini_mutate.py"
        if not mm.is_file():
            return None
        q = lambda s: shlex.quote(str(s)).replace("{", "{{").replace("}", "}}")
        return {"cmd": f"{q(sys.executable)} {q(mm)} --work . --pair {{file}}={q(Path(wt, f))} --test-cmd {{test_cmd}} --max 40 --jobs 4",
                "format": "line", "warn_below": 0.6, "timeout": 480}
    return {"tool": "stryker", "warn_below": 0.6, "timeout": 420}


def auto_breadth_retry(mg, spec):
    """spec breadth_retry for the derived spec: on when mini_mutate scores the fix (its `line` output names the breadth survivors that
    narrow_survivors reads; Stryker's does not), unless the job spec says breadth_retry: false."""
    return bool(mg) and mg.get("format") == "line" and spec.get("breadth_retry") is not False


def _auto_survivors(run_dir):
    """The mutation survivors that the phase-B packet (run_dir/REVIEW.md) names for the fix ([] when none or no packet)."""
    try:
        t = Path(run_dir, "REVIEW.md").read_text(errors="replace")
    except OSError:
        return []
    m = re.search(r"^\| mutation \| \w+ \| .*?; survivors: (.*?) \|\s*$", t, re.M)
    return [x.strip() for x in m.group(1).split(" ;; ") if x.strip()] if m else []


def _auto_breadth(run_dir):
    """The phase-B packet's `breadth` row (what the breadth retry asked and what the model claimed), or ""."""
    try:
        t = Path(run_dir, "REVIEW.md").read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"^\| breadth \| \w+ \| (.*?) \|\s*$", t, re.M)
    return m.group(1).strip() if m else ""


def _auto_doc(run_dir):
    """The phase-B packet's `docstring` row (what the doc pass asked about and what came of it), or ""."""
    try:
        t = Path(run_dir, "REVIEW.md").read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"^\| docstring \| \w+ \| (.*?) \|\s*$", t, re.M)
    return m.group(1).strip() if m else ""


def auto_review(run_dir, cwd, name, desc, line, target=None, hint=None, secs=None, fspec=None, base_text=None):
    """REVIEW-AUTO.md: the compact packet `mini-scorecard review brief` prints inline (the full packets stay next to it)."""
    L = [f"# AUTO review: {name}", "", f"RESULT {line}", "", "## Problem (as given)", "", _auto_cap(desc.strip(), 2500), ""]
    fb = _auto_fallback(run_dir) if "fallback=direct" in (line or "") else ""  # stage 22: a fallback candidate says so first
    if fb:
        L += ["## Direct-fix fallback (read first)", "", _auto_cap(fb, 3000), ""]
    if target and target[0]:
        L += [f"Diagnosed target: `{target[0]}` {_fn_list(target[1] or ['?'])}" + (f" (reporter's hint: {hint})" if hint else ""), ""]
    rc = md_sec(secs, "root cause").strip()
    if rc:
        L += ["## Root cause (model's diagnosis)", "", _auto_cap(rc, 800), ""]
    stopped = bool(secs) and not fspec  # an accepted diagnosis but no test or fix (derive failed, or the target was refused)
    ud = ""  # the fix's edits to existing tests (update_tests), as a diff
    if stopped:  # Claude fixes it directly: the packet carries what that needs, so the fallback starts from the diagnosis
        for key in ("minimal fix", "regression test"):
            s = md_sec(secs, key).strip()
            if s:
                L += [f"## {key.capitalize()} (model's diagnosis)", "", _auto_cap(s, 1500), ""]
    if fspec:
        tf, f = fspec["test_file"], fspec["allowed"][0]
        tp = Path(cwd, tf)
        frozen = "written in the SAME run as the fix: NOT frozen, never passed phase A's gate" if fb else "frozen before the fix"
        L += [f"## Test `{tf}` (model-written, {frozen})", "", "```", _auto_cap(tp.read_text(errors="replace") if tp.is_file() else "(not written)", 9000), "```", ""]
        try:
            ung = ungrounded_expectations(tp.read_text(errors="replace"), tf, desc, cwd, override={f: base_text or ""}) if tp.is_file() else []
        except Exception as e:  # a review aid: never let it break the packet
            ung = [(f"(grounding check failed: {type(e).__name__})", "", 0)]
        if ung:
            L += ["## Expected values found in neither the Problem nor the baseline code nor the test's inputs", ""]
            L += [f"- `{t}` (test line {n}): {s[:100]!r}" for t, s, n in ung]
            L += ["", "Each is the model's own choice: unless the Problem implies it, an expectation it does not ask for is a reject.", ""]
        try:
            gap = user_view_gap(tp.read_text(errors="replace"), tf, desc, cwd, as_list(fspec.get("allowed")) or [f],
                                override={f: base_text or ""}) if tp.is_file() else []
        except Exception as e:  # a review aid: never let it break the packet
            gap = []
            L += [f"(user-view check failed: {type(e).__name__})", ""]
        if gap:
            L += ["## The test never goes through the public API the Problem's example uses", ""]
            L += [f"- {_uv_line(imp, calls)}" for imp, calls in gap]
            L += ["", "Every test calls internal code, so the fix can pass them while the user still sees the bug. Check in the code "
                  "that the fix also changes what the Problem's example shows (follow its calls from the public API to the fixed "
                  "code); a fix that only changes the internal path the tests call is a reject.", ""]
        now = Path(cwd, f).read_text(errors="replace") if Path(cwd, f).is_file() else ""
        d = "".join(difflib.unified_diff((base_text or "").splitlines(True), now.splitlines(True), f"a/{f}", f"b/{f}", n=3))
        L += ["## Fix diff", "", "```diff", _auto_cap(d, 9000) if d else "(no change)", "```", ""]
        for t in fspec.get("update_tests") or []:
            bp, cp = Path(str(fspec.get("baseline_from") or ""), t), Path(cwd, t)
            old, new = (p.read_text(errors="replace") if p.is_file() else "" for p in (bp, cp))
            ud += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{t}", f"b/{t}", n=3))
        if ud:
            L += ["## Existing tests the fix updated", "", "```diff", _auto_cap(ud, 6000), "```", "",
                  "Gate update_flip checked that each edited test fails on the fix without the edit and passes with it. Whether the new "
                  "expectation is right is yours to decide: it must be what the Problem asks for, or the edit only hides a wrong fix "
                  "(a reject).", ""]
        surv = _auto_survivors(run_dir)
        if surv:
            L += ["## Mutation survivors (variants of the fix that the frozen test cannot tell apart from it)", ""]
            L += [f"- `{x}`" for x in surv] + ["", "Decide each from the Problem: one the Problem decides against the fix is a reject, "
                  "e.g. a `breadth` survivor (an exact vs a prefix/substring match) where the Problem says other values are unchanged.", ""]
        br = _auto_breadth(run_dir)
        if br:
            L += ["## Breadth retry (one local attempt at the fix's prefix/substring matches, before this review)", "", f"- {br}", "",
                  "The model's BREADTH claims are its own reading: check each against the Problem. A match it kept that the Problem "
                  "does not ask for is still a reject.", ""]
        dr = _auto_doc(run_dir)
        if dr:
            L += ["## Docstring check (one local question per docstring the fix left as it was, before this review)", "", f"- {dr}", "",
                  "A correction the model made is in the fix diff above: check it is true and says no more than the code does.", ""]
    L += ["## Files", "", f"- packets: {run_dir}/REVIEW.md (test + fix gates), {run_dir}/REVIEW-D.md (diagnosis gate), {run_dir}/DIAGNOSIS.md, "
          f"{run_dir}/TASK.fix.md (what the model was told), {run_dir}/spec.auto.json"
          + (f", {run_dir}/REVIEW-main.md (the stopped structured path's packet)" if fb else ""), f"- work dir (test + fix in place): {cwd}", ""]
    if stopped:
        L += ["## Next", "", "No test or fix was written. Fix it directly from this diagnosis: diag_check verified every quote in "
              f"{run_dir}/DIAGNOSIS.md against the code, but the reasoning is the model's, so confirm the root cause in the code "
              "first. Write the regression test above (red on the baseline), then the minimal fix.", ""]
    else:
        L += ["## Review asks", "",
              "1. Does the test assert what the Problem asks for? The model wrote it from its own reading: a wrong expectation is a reject.",
              "2. Is the diff the minimal correct fix of the root cause, with no unrelated change?",
              "3. Could an input the Problem does not mention now behave differently? Try near-misses on every condition the fix adds "
              "or widens (for a new value 'x': 'x2', 'xs', 'my-x', another case, None): where the Problem says other values are "
              "unchanged they must behave as at baseline, so a prefix or substring match is a reject unless the Problem asks for one.",
              "4. Does every docstring and comment that describes the changed behaviour (the module docstring too) still hold? A stale "
              "one is a repair (update it), not a pass."]
        if fb:
            L.insert(L.index("## Review asks") + 2,
                     "0. FALLBACK CANDIDATE: the test and the fix come from one plain run after the structured path stopped; the test "
                     "never passed phase A's gate. Check first that it fails at baseline for the Problem's reason (the fallback section "
                     "above) and asserts the Problem's expectation, not the fix's behaviour: a test written to fit the fix is a reject.")
        if ud:
            L.append("5. If the fix updated an existing test (section above), is each new expectation exactly what the Problem asks for?")
        L.append("")
    (Path(run_dir) / "REVIEW-AUTO.md").write_text("\n".join(L))


def auto(a, spec, local, check_only=False):
    """mode "auto" (see the block comment above). spec: baseline_from (a git worktree), task (the description file), optional files
    (hint paths), fn (hint function), locate (symptom regexes for the diagnosis), diagnose ("always" | "skip": needs one file hint and
    fn), timeout (the whole job), diag_timeout, fix_timeout, auto_mutation, auto_update_tests (default true: a python fix may
    update the existing tests auto_update_tests picks, under gate update_flip), doc_pass (default true for a python target: after a
    green fix, one local question per docstring the fix left as it was; false, or env MINI_DOC_PASS=off, turns it off), and the
    two-phase knobs in AUTO_PASS_KEYS."""
    cwd, run_dir = a.cwd, Path(a.cwd).parent
    total = int(spec.get("timeout", AUTO_TIMEOUT))
    deadline = _now() + total
    name = re.sub(r"[^\w.-]", "-", str(spec.get("name") or run_dir.name))[:80] or "auto"
    wt = os.path.abspath(os.path.expanduser(str(spec.get("baseline_from") or "")))
    try:
        desc = Path(os.path.expanduser(str(spec["task"]))).read_text(errors="replace").strip()
    except (KeyError, OSError) as e:
        print("RESULT " + tag_result(f"FAIL bad-spec mode=auto cannot read the task ({type(e).__name__})"), flush=True)
        return RC_FAIL

    def done(line, rc, **kw):
        if not check_only:
            auto_review(run_dir, cwd, name, desc, line, **kw)
            print(f"[mini-runner] REVIEW packet: {run_dir / 'REVIEW-AUTO.md'}", flush=True)
        print("RESULT " + tag_result(line), flush=True)
        return rc

    if not desc:
        return done("FAIL bad-spec mode=auto the task (description) is empty", RC_FAIL)
    if not (spec.get("baseline_from") and Path(wt).is_dir() and git(wt, "rev-parse", "--show-toplevel").strip()
            and os.path.realpath(git(wt, "rev-parse", "--show-toplevel").strip()) == os.path.realpath(wt)):
        return done("FAIL bad-spec mode=auto baseline_from must be the root of a git worktree", RC_FAIL)
    files = [x for x in git(wt, "ls-files").split("\n") if x]
    raw_hints = [str(h) for h in as_list(spec.get("files")) if str(h).strip()]
    hints = [auto_resolve(h, files) or h for h in raw_hints]
    fn_hint = str(spec["fn"]).split(".")[-1] if spec.get("fn") else None
    hits = auto_forbidden([("repo", x) for x in auto_repo_names(wt)] + [("file hint", h) for h in hints] + [("function hint", fn_hint)], desc)
    if hits:
        return done("FAIL bad-spec mode=auto phase=refused must-not-offload: " + "; ".join(f"{w} {v!r} ({h})" for w, v, h in hits)[:300], RC_FAIL)
    os.environ["MINI_NEEDS_INFO"] = "off"  # one job, one RESULT: no parked questions inside an auto run
    if not check_only:
        install_signal_handlers(total, int(spec.get("deadline_grace", 120)))
    t0 = _now()
    skip = spec.get("diagnose") == "skip"
    one = len(hints) == 1 and hints[0] in files and bool(fn_hint)
    if skip and not one:
        return done("FAIL bad-spec mode=auto diagnose=skip needs exactly one tracked file hint and a fn hint", RC_FAIL)
    secs, hint_txt = {}, ", ".join([*hints, *([fn_hint] if fn_hint else [])]) or None
    # A queue re-launch after a provider pause (exit 3) starts the whole run again: an accepted diagnosis of the same baseline
    # commit and description is reused instead of spending another ~20 GPU minutes on it.
    stp, head = run_dir / "auto-state.json", git(wt, "rev-parse", "HEAD").strip()
    key = {"head": head, "desc": hashlib.sha256(desc.encode()).hexdigest()[:16], "hints": hint_txt}
    try:
        prev = json.loads(stp.read_text()) if stp.is_file() else {}
    except ValueError:
        prev = {}
    reuse = (not skip and not check_only and prev.get("diagnose") == "PASS" and all(prev.get(k) == v for k, v in key.items())
             and Path(run_dir, "DIAGNOSIS.md").is_file())
    dtext = ""
    if skip:
        target = (hints[0], [fn_hint], [])
    elif reuse:
        dtext = Path(run_dir, "DIAGNOSIS.md").read_text(errors="replace")
        secs = md_h2(dtext)
        df, dfns, dx = auto_target(dtext)
        target = (auto_resolve(df, files) or df, dfns, dx)
        print(f"[mini-runner] auto: reusing the diagnosis accepted by an earlier launch of this run ({run_dir / 'DIAGNOSIS.md'})", flush=True)
    else:
        tdp = run_dir / "TASK-D.md"
        tdp.write_text(auto_diag_task(name, desc, hints, fn_hint))
        dspec = {"mode": "diagnose", "allowed": [], "baseline_from": wt, "task": str(tdp),
                 "timeout": int(min(int(spec.get("diag_timeout", AUTO_DIAG_TIMEOUT)), max(300, deadline - _now() - 1200))),
                 **{k: spec[k] for k in ("locate", "env", "keep", "diag_steps", "diag_attempts", "diag_wall", "no_think") if k in spec}}
        st = {}
        rc = diagnose(a, dspec, local, check_only, sub=st)
        rv = run_dir / "REVIEW.md"
        if rv.is_file() and not check_only:
            rv.replace(run_dir / "REVIEW-D.md")
        if check_only:
            if rc != RC_OK or not one:
                print("[mini-runner] check-only auto: diagnose stage checked; the derive stage needs a diagnosis (or one file + fn hint)", flush=True)
                return rc
            target = (hints[0], [fn_hint], [])
        else:
            rep = Path(cwd, "DIAGNOSIS.md")
            if rc != RC_OK or not rep.is_file():
                why = re.sub(r"^(?:PASS|FAIL) mode=diagnose ", "", st.get("line") or "no result")
                return done(f"FAIL mode=auto phase=diagnose {why}"[:600], RC_FAIL)
            shutil.copy2(rep, run_dir / "DIAGNOSIS.md")
            stp.write_text(json.dumps({"diagnose": "PASS", **key}) + "\n")
            dtext = rep.read_text(errors="replace")
            secs = md_h2(dtext)
            df, dfns, dx = auto_target(dtext)
            target = (auto_resolve(df, files) or df, dfns, dx)
    if check_only and spec.get("remote"):  # derive (mini-spec probes the repo's own python) and fix run project code: CPU runner only
        print("[mini-runner] check-only auto (remote): the derive and fix stages run on the CPU runner, not here", flush=True)
        return RC_OK
    t_d = int(_now() - t0)
    f, fns, extras = target
    kw = dict(target=(f, fns), hint=hint_txt, secs=secs)
    if not (f and fns):
        return done("FAIL mode=auto phase=derive the diagnosis names no File:/Function: under ## Minimal fix", RC_FAIL, **kw)
    if f not in files:
        return done(f"FAIL mode=auto phase=derive File: {f} is not a tracked file of the repo", RC_FAIL, **kw)
    raw_consts = auto_consts(dtext) if dtext else []
    hits = auto_forbidden([("diagnosed file", f)] + [("diagnosed function", x) for x in [*fns, *extras]]
                          + [("diagnosed constant", x) for x in raw_consts])
    if hits:
        return done("FAIL bad-spec mode=auto phase=refused must-not-offload: " + "; ".join(f"{w} {v!r} ({h})" for w, v, h in hits)[:300], RC_FAIL, **kw)
    src = Path(wt, f).read_text(errors="replace")
    gone = [x for x in fns if not _defines(src, x)]
    if gone:
        return done(f"FAIL mode=auto phase=derive Function: {gone[0]} is not defined in {f}", RC_FAIL, **kw)
    fns = fns + [x for x in extras if x not in fns and _defines(src, x)]  # `- Function: \`a\` (and \`b\`)`: b when f defines it
    fns = list(dict.fromkeys(fns))
    consts = [c for c in raw_consts if _binds_top(src, c) and c not in fns]
    kw["target"] = (f, fns)
    mx = getattr(_diag_checker(), "MAX_FNS", 3) if len(fns) > 1 else 1  # a skip/hint run never loads diag_check.py
    if len(fns) > mx:
        return done(f"FAIL mode=auto phase=derive the diagnosis names {len(fns)} functions ({', '.join(fns)}); at most {mx}", RC_FAIL, **kw)
    fn = fns[0] if len(fns) == 1 else fns  # a one-function spec stays a string, as before
    try:
        ms = _mini_spec_lib()
        b = auto_build(ms, wt, run_dir, name, spec["task"], f, fns)
    except Exception as e:
        msg = str(e) if type(e).__name__ == "Ambiguous" else f"{type(e).__name__}: {e}"
        return done(f"FAIL mode=auto phase=derive mini-spec: {msg}"[:600], RC_FAIL, **kw)
    intake = b.get("intake") or {}
    cls, rs = intake.get("class"), intake.get("reasons")
    rs = (rs.get(cls) if isinstance(rs, dict) else rs) or []
    _x, tf, tcmd = b["tests"][0]
    print(f"[mini-runner] auto derive: target {f}:{','.join(fns)}{' constants ' + ','.join(consts) if consts else ''} test {tf} :: {tcmd} "
          f"suite={b.get('suite') or '-'} typecheck={b.get('typecheck') or '-'} intake={cls} {'; '.join(map(str, rs))[:300]}", flush=True)
    if cls in ("not-local", "needs-stubs"):
        return done(f"FAIL mode=auto phase=derive intake={cls}: {'; '.join(map(str, rs))}"[:600], RC_FAIL, **kw)
    related = _auto_related(ms, wt, files, f) if f.endswith(".py") else {}
    upd = auto_update_tests(ms, wt, files, f, fns + consts, b, related) if spec.get("auto_update_tests", True) is not False else []
    if upd:
        print(f"[mini-runner] auto: the fix may update existing tests in {', '.join(upd)} (gate update_flip checks each edit)", flush=True)
    (run_dir / "TASK.fix.md").write_text(auto_fix_task(name, desc, secs, f, fns, b, ms, consts, upd=upd))
    left = int(deadline - _now() - 60)
    fspec = {"mode": "two-phase", "baseline_from": wt, "task": str(run_dir / "TASK.fix.md"), "allowed": [f], "drop": b["drop"],
             "test_file": tf, "test_cmd": tcmd, "target_fn": fn, "red_ok": [], "baseline_green": [], "fn_scope": "enforce",
             "timeout": max(300, min(left, int(spec.get("fix_timeout", b["timeout"]))))}
    if b.get("suite"):
        fspec["suite_cmd"] = b["suite"]
    if b.get("typecheck"):
        fspec["typecheck_cmd"] = b["typecheck"]
    lits = list(dict.fromkeys(x for v in (b.get("anchors") or {}).values() for x in v))
    if lits:
        fspec["reach_anchor"] = lits[0] if len(lits) == 1 else lits
    elif dtext and f.endswith(".py"):
        dl = auto_diag_lines(dtext, f, fns, wt)
        if dl:
            fspec["diag_lines"] = dl
            print(f"[mini-runner] auto: lines of {f} the diagnosis names as the fault inside {','.join(fns)}: {dl} (a phase A test "
                  "that runs one of them meets the body-coverage floor)", flush=True)
    fspec.update({k: spec[k] for k in AUTO_PASS_KEYS if k in spec})
    if upd:
        fspec["update_tests"], fspec["update_tests_strict"] = upd, True
    nar = auto_narrow_suite(ms, wt, files, f, b, first=upd, related=related) if fspec.get("suite_cmd") == b.get("suite") else None
    if nar:
        fspec["suite_narrow"] = nar
    if consts:  # the diagnosis's Constant: values: their top-level statements are in fn_scope (cfgkeys 2cdee16a8b71: DEFAULTS)
        fspec["new_names"] = consts
    mg = auto_mutation_cfg(wt, f, spec)
    if mg:
        fspec["mutation_gate"] = mg
    if auto_breadth_retry(mg, spec):
        fspec["breadth_retry"] = True
    if f.endswith(".py") and spec.get("doc_pass") is not False:
        fspec["doc_pass"] = True
    if spec.get("direct_fallback") is not False:  # stage 22: the spec field is the switch a remote run sees
        fspec["direct_fallback"] = True
    (run_dir / "spec.auto.json").write_text(json.dumps(fspec, indent=1) + "\n")
    st = {}
    rc = two_phase(a, fspec, [f], local, check_only, sub=st)
    if check_only:
        return rc
    line = st.get("line") or "FAIL mode=two-phase no result"
    line = line.replace("mode=two-phase", "mode=auto", 1) if "mode=two-phase" in line else re.sub(r"^(PASS|FAIL)\b", r"\1 mode=auto", line, count=1)
    if line.startswith("FAIL") and "phase=" not in line:
        line = line.replace("mode=auto", "mode=auto phase=" + ("derive" if "bad-spec" in line else "fix"), 1)
    line += f" target={f}:{','.join(fns)}{'+' + ','.join(consts) if consts else ''} diag_s={t_d} fix_s={int(_now() - t0) - t_d}"
    return done(line, rc, fspec=fspec, base_text=(b.get("texts") or {}).get(f, ""), **kw)


# ---- remote lane: an auto job whose project code runs on the CPU runner ------------------------------------------------------
# A spec with "remote": {"python": "3.11"|"3.12"|"3.13", "deps_lock": path|null, "max_calls": N, "setup_timeout": S} (mini-auto new
# --remote) runs auto() on a dispatch-cpu-runner instead of here. remote_auto() makes the must-not-offload checks on this host,
# then submits one runner job with this file, its helpers and the job itself (lane/) as tools: lane_remote.py setup builds the
# project's venv on the runner (with network), lane_remote.py run runs auto() there with no network and the model only through
# the queue's relay, which adds the key (the key never leaves this host). What comes back is data and is never run here: the
# run's review files (their paths rewritten to this host's) and the files the run changed, of which only the job's own (the fix
# file, the test, update_tests, drop) land in the local work dir, rebuilt from baseline_from first. The diagnosed target meets
# the must-not-offload rules again here, and REVIEW-AUTO.md is written here. A lane failure is remote-infra (INFRA, never the
# model's); a changed file the local scope refuses is remote-scope. A model hold or a provider outage parks the run (exit 3) as a
# local outage does, and the re-launch ships the accepted diagnosis back, so it is not paid for twice.
REMOTE_TOOLS = ("mini-swe-runner.py", "mini-spec", "mini-precheck", "mini_mutate.py", "diag_check.py", "mini_graph.py", "mini-graph",
                "mini_rule_coverage.py", "model_profile.py", "model_profiles.yaml", "infra_incidents.py", "lane_remote.py",
                "mini_ext/mini_ctx.py", "mini_ext/single_shot_call.py")
REMOTE_RUN_FILE = re.compile(r"^(?:REVIEW[\w.-]*\.md|DIAGNOSIS\.md|TASK-D\.md|TASK\.fix\.md|spec\.auto\.json|auto-state\.json|[\w.-]+\.traj\.json)$",
                             re.A)
REMOTE_KEEP = ("auto-state.json", "DIAGNOSIS.md")  # shipped back on a re-launch, byte for byte: auto() reuses an accepted diagnosis
# 3.11 (stage 27b, dmcg3): the CPU runner image has no standalone 3.11, but its uv (UV_PYTHON_DOWNLOADS=never) finds Debian's
# /usr/bin/python3.11, the agent's own interpreter; the runner's self-test passes with a 3.11.2 venv as the project's python
REMOTE_PYTHONS = ("3.11", "3.12", "3.13")
REMOTE_SETUP_S, REMOTE_WALL_EXTRA, REMOTE_MAX_CALLS, REMOTE_CLAIM_S, REMOTE_OUTAGE_S = 1800, 1500, 1500, 1800, 1200
# PAUSE (2026-11-05): the remote wall gives back the provider waits mini_ctx credits (a Darkbloom Sandbox hold the relay
# waits out inside a call, an outage), up to REMOTE_HOLD_ALLOW_S, so a hold pauses the run instead of failing it. The job
# timeout makes room for the allowance within REMOTE_JOB_MAX_S (cpu_lane runner-config max_job_timeout_s; the runner
# clamps a job's timeout to it). A wait past the allowance stops the run at its wall, and remote_auto parks it.
REMOTE_HOLD_ALLOW_S, REMOTE_JOB_MAX_S = 5400, 10800
REMOTE_FILE_MAX, REMOTE_TOTAL_MAX, REMOTE_MEMBERS_MAX, REMOTE_MANIFEST_MAX = 20 << 20, 160 << 20, 20000, 8 << 20
REMOTE_JUNK = re.compile(r"(?:^|/)(?:\.coverage(?:\.[\w.-]+)?|coverage\.xml|htmlcov|\.mypy_cache|\.ruff_cache|\.hypothesis|\.tox|\.nox|"
                         r"\.eggs|[\w.-]+\.egg-info)(?:/|$)")
REMOTE_API = queue_config.queue_api()[0]
REMOTE_HOLD_TEXT = "darkbloom sandbox hold"
REMOTE_LOG_PASS = ("Building agent config from specs:", "[mini-runner] ")  # the queue's progress views read these unprefixed
REMOTE_LOG_PASS_RE = re.compile(r"mini-swe-agent \(step \d+,|Retrying \S+ in [\d.]+ seconds as it raised |\[provider-(?:outage|hold)\]")
REMOTE_LOG_SEPS = {ord(c): " " for c in "\r\x0b\x0c\x1c\x1d\x1e\x85  \x00"}  # line breaks to str.splitlines()


class _Preempted(BaseException):
    """A second SIGTERM/SIGINT while the remote job is being cancelled."""


def _remote_text(s, n=600):
    """s as one printable line (control, separator and format characters -> spaces), at most n characters: remote text that
    goes into our RESULT line or the log can never start a line of its own."""
    return "".join(c if c == " " or not unicodedata.category(c).startswith(("C", "Z")) else " " for c in str(s))[:n]


def _remote_rel(p):
    """p when it is a safe repo-relative path: no absolute, empty, '.' or '..' part, no backslash or control/format character,
    nothing under .git or .venv (in any case or Unicode form); else None."""
    if not isinstance(p, str) or not 0 < len(p) <= 1024 or p.startswith("/") or "\\" in p:
        return None
    if any(unicodedata.category(c).startswith("C") for c in p):
        return None
    for part in p.split("/"):
        if part in ("", ".", "..") or unicodedata.normalize("NFC", part).casefold() in (".git", ".venv"):
            return None
    return p


def _remote_parent(root, rel, create=False):
    """The real directory that holds root/rel, walked part by part without following a symlink; None when a part is missing
    (created 0755 when create) or is not a directory."""
    d = str(root)
    for part in rel.split("/")[:-1]:
        d = os.path.join(d, part)
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(d, 0o755)
            continue
        if not stat.S_ISDIR(st.st_mode):
            return None
    return d


def _remote_lexists(root, rel):
    """Whether root/rel exists here as anything (a symlink too), looked up without following symlinks; a file or symlink where
    a parent directory would be counts as existing (the path collides with it)."""
    d, parts = str(root), rel.split("/")
    for i, part in enumerate(parts):
        d = os.path.join(d, part)
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            return False
        except NotADirectoryError:
            return True
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            return True
    return True


def _remote_read(root, rel, limit=REMOTE_FILE_MAX):
    """(bytes, or None when larger than limit; mode bits) of the regular file root/rel, opened without following a symlink;
    None when it is missing or not a regular file."""
    d = _remote_parent(root, rel)
    if d is None:
        return None
    try:
        fd = os.open(os.path.join(d, rel.split("/")[-1]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > limit:
            return None, st.st_mode & 0o777
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read(limit + 1), st.st_mode & 0o777
    finally:
        os.close(fd)


def _remote_write(root, rel, data, mode):
    """root/rel := data as a new regular file (0755 when mode has an exec bit, else 0644), replacing a file or symlink there and
    never writing through one; ValueError when a directory or a non-directory parent is in the way."""
    d = _remote_parent(root, rel, create=True)
    if d is None:
        raise ValueError(f"{rel}: a parent is not a directory")
    p = os.path.join(d, rel.split("/")[-1])
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            raise ValueError(f"{rel}: a directory is in the way")
        os.unlink(p)
    m = 0o755 if mode & 0o111 else 0o644
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, m)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), m)
        fh.write(data)


def _remote_unlink(root, rel):
    """Remove the file or symlink root/rel (looked up without following symlinks); nothing when it is not there; ValueError on a
    directory."""
    d = _remote_parent(root, rel)
    if d is None:
        return
    p = os.path.join(d, rel.split("/")[-1])
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{rel}: a directory, not a file")
    os.unlink(p)


def _remote_tolerated(rel):
    """A path the local scope gate never counts: an IGNORE_UNTRACKED name as a path part, test/type-check caches, build metadata."""
    return any(p in IGNORE_UNTRACKED for p in rel.split("/")) or bool(REMOTE_JUNK.search(rel))


LANE_TOOLS = os.environ.get("MINI_LANE_TOOLS", "ruff==0.16.3")   # exact pins a remote job's setup adds to the mini venv ("" or off: none)


def _lane_tools_lock():
    """LANE_TOOLS (name==version pins, space or comma separated) as the requirements file for lane_remote.py setup --tools-lock,
    or None when it names none."""
    pins = [p for p in re.split(r"[\s,]+", LANE_TOOLS or "") if re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", p)]
    return ("\n".join(pins) + "\n").encode() if pins else None


def _remote_mini_lock():
    """This host's mini-swe-agent venv as pins (name==version of every installed dist): the runner builds the same one, --no-deps.
    ValueError when a dist cannot be pinned (installed from a path or URL, unreadable metadata) or mini-swe-agent is missing."""
    venv = Path(MINI).parent.parent
    pins = {}
    for di in sorted((venv / "lib").glob("python3*/site-packages/*.dist-info")):
        if (di / "direct_url.json").is_file():
            raise ValueError(f"{di.name} was installed from a path or URL, not from the index")
        name = ver = None
        try:
            head = (di / "METADATA").read_text(errors="replace")
        except OSError:
            head = ""
        for ln in head.splitlines():
            if not ln.strip():
                break
            if name is None and ln.startswith("Name:"):
                name = ln[5:].strip()
            elif ver is None and ln.startswith("Version:"):
                ver = ln[8:].strip()
        pin = f"{name}=={ver}"
        if not (name and ver and re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9._+!-]+", pin)):
            raise ValueError(f"{di.name}: no readable name and version")
        pins[re.sub(r"[-_.]+", "-", name).lower()] = pin
    if "mini-swe-agent" not in pins:
        raise ValueError(f"no mini-swe-agent in {venv}")
    return ("\n".join(sorted(pins.values())) + "\n").encode()


class _RemoteLog:
    """on_log for the remote job: its output becomes our log, line by line. Progress lines the queue's views read pass as they
    are; every other line gets a "[remote] " prefix, so nothing the remote run prints can pass for our own RESULT or [worker]
    markers. outage: the remote run reported a provider outage."""

    def __init__(self):
        self.dec, self.buf, self.outage = codecs.getincrementaldecoder("utf-8")("replace"), "", False

    def __call__(self, data):
        txt = self.dec.decode(data) if isinstance(data, (bytes, bytearray)) else str(data)
        *lines, self.buf = (self.buf + txt).split("\n")
        for ln in lines:
            self._emit(ln)

    def flush(self):
        rest, self.buf = self.buf + self.dec.decode(b"", final=True), ""
        if rest:
            self._emit(rest)

    def _emit(self, ln):
        ln = ln.translate(REMOTE_LOG_SEPS)
        if "[provider-outage]" in ln or "[provider-hold]" in ln:
            self.outage = True
        keep = (ln.startswith(REMOTE_LOG_PASS) and not ln.startswith("[mini-runner] REVIEW packet:")) or REMOTE_LOG_PASS_RE.match(ln)
        print(ln if keep else "[remote] " + ln, flush=True)


def _remote_extract(tgz):
    """The remote job's artifacts .tgz read as data (nothing is extracted to disk or run) -> (manifest, {work rel: bytes},
    {run file: bytes}). ValueError on a member that is not a regular file or a directory, an unsafe, unknown or duplicate
    path, or past a size cap."""
    man, work, run, seen, total, n = None, {}, {}, set(), 0, 0
    with tarfile.open(tgz, "r:gz") as tf:
        for m in tf:
            n += 1
            if n > REMOTE_MEMBERS_MAX:
                raise ValueError("too many members")
            name = m.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", ".") or m.isdir():
                continue
            if not m.isreg():
                raise ValueError(f"{name[:120]!r} is not a regular file")
            if m.size > REMOTE_FILE_MAX:
                raise ValueError(f"{name[:120]!r} has {m.size} bytes")
            total += m.size
            if total > REMOTE_TOTAL_MAX:
                raise ValueError("the artifacts are past the size cap")
            fh = tf.extractfile(m)
            data = fh.read(m.size + 1) if fh else b""
            if len(data) != m.size:
                raise ValueError(f"{name[:120]!r}: short read")
            if name == "manifest.json":
                if man is not None or m.size > REMOTE_MANIFEST_MAX:
                    raise ValueError("manifest.json twice or too large")
                man = json.loads(data)
            elif name.startswith("work/"):
                rel = _remote_rel(name[5:])
                key = unicodedata.normalize("NFC", rel).casefold() if rel else None
                if rel is None or key in seen:
                    raise ValueError(f"{name[:120]!r}: an unsafe or duplicate path")
                seen.add(key)
                work[rel] = data
            elif name.startswith("run/"):
                f = name[4:]
                if not REMOTE_RUN_FILE.match(f) or f.casefold() in {x.casefold() for x in run}:
                    raise ValueError(f"{name[:120]!r} is not a run file")
                run[f] = data
            else:
                raise ValueError(f"{name[:120]!r}: an unknown member")
    if not isinstance(man, dict):
        raise ValueError("no manifest.json")
    return man, work, run


def _remote_manifest(man):
    """None when the manifest has every field remote_auto reads, of its type; else which one is wrong."""
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def strs(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    if man.get("v") != 1:
        return "v"
    if man.get("rc") is not None and not (isinstance(man["rc"], int) and not isinstance(man["rc"], bool)):
        return "rc"
    if man.get("result") is not None and not isinstance(man["result"], str):
        return "result"
    if not all(isinstance(man.get(k), bool) for k in ("wall_hit", "terminated")):
        return "wall_hit/terminated"
    if not num(man.get("elapsed_s")):
        return "elapsed_s"
    real = man.get("real")
    if not (isinstance(real, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in real.items())):
        return "real"
    if not all(isinstance(man.get("remote_" + k), str) for k in ("run_dir", "work", "checkout", "home")):
        return "remote paths"
    if "error" in man:
        return None if isinstance(man["error"], str) else "error"
    if not all(strs(man.get(k)) for k in ("changed", "deleted", "run_files")):
        return "file lists"
    md = man.get("modes")
    if not (isinstance(md, dict) and all(isinstance(v, int) and not isinstance(v, bool) for v in md.values())):
        return "modes"
    sk = man.get("skipped")
    if not (isinstance(sk, list) and all(isinstance(x, dict) and isinstance(x.get("path"), str) and isinstance(x.get("why"), str)
                                         for x in sk)):
        return "skipped"
    return None


def _remote_rewriter(man, pairs):
    """str -> str turning the remote run's absolute paths (man remote_<key> and real[<key>]) into this host's; pairs: [(key,
    local path)]. A path is replaced only where it ends (never as the prefix of a longer name); the longest one wins."""
    subs = {}
    for key, local in pairs:
        for v in (man.get("remote_" + key), (man.get("real") or {}).get(key)):
            if isinstance(v, str) and v.startswith("/") and v.rstrip("/"):
                subs.setdefault(v.rstrip("/"), str(local))
    if not subs:
        return lambda s: s
    rx = re.compile("(?:" + "|".join(re.escape(k) for k in sorted(subs, key=len, reverse=True)) + r")(?![\w-])")
    return lambda s: rx.sub(lambda m: subs[m.group(0)], s)


def _rewrite_json(o, rw):
    if isinstance(o, str):
        return rw(o)
    if isinstance(o, list):
        return [_rewrite_json(x, rw) for x in o]
    if isinstance(o, dict):
        return {k: _rewrite_json(v, rw) for k, v in o.items()}
    return o


REMOTE_FSPEC_PATHS = ("allowed", "test_file", "drop", "update_tests", "target_fn", "new_names")


def _remote_fspec(raw, rw, wt, run_dir, remote, files):
    """The remote run's spec.auto.json (bytes) as this host's fix spec -> (spec, None) or (None, why it cannot be used). The
    file fields are checked as they came (the fix file one tracked file, the rest safe repo-relative paths) and kept as they
    are; every other string gets its paths rewritten. remote is kept in it, so mini-finish and the queue never run its tests here."""
    try:
        fs = json.loads(raw)
    except Exception:  # noqa: BLE001 -- ValueError or RecursionError: unusable either way
        return None, "spec.auto.json does not parse"
    if not isinstance(fs, dict) or fs.get("mode") != "two-phase":
        return None, "spec.auto.json is not a two-phase spec"
    al = fs.get("allowed")
    if not (isinstance(al, list) and len(al) == 1 and _remote_rel(al[0]) and al[0] in files):
        return None, "its allowed is not one tracked file"
    if not _remote_rel(fs.get("test_file")):
        return None, "its test_file is not a repo-relative path"
    for k in ("drop", "update_tests"):
        v = fs.get(k)
        if v is not None and not (isinstance(v, list) and all(_remote_rel(x) for x in v)):
            return None, f"its {k} is not a list of repo-relative paths"
    ut = fs.get("update_tests") or []
    if len(ut) > AUTO_UPD_MAX or any(x not in files or not TESTFILE_RE.search(x) or x in (al[0], fs.get("test_file")) for x in ut):
        return None, f"its update_tests are not at most {AUTO_UPD_MAX} tracked existing test files"
    t = fs.get("target_fn")
    if not ((isinstance(t, str) and t) or (isinstance(t, list) and t and all(isinstance(x, str) and x for x in t))):
        return None, "its target_fn is not a function name or a list of them"
    nn = fs.get("new_names")
    if nn is not None and not (isinstance(nn, list) and all(isinstance(x, str) for x in nn)):
        return None, "its new_names is not a list of names"
    if fs.get("diag_lines") is not None and not _line_list(fs["diag_lines"]):
        return None, "its diag_lines is not a list of line numbers"
    out = {**_rewrite_json(fs, rw), **{k: fs[k] for k in REMOTE_FSPEC_PATHS if k in fs}}
    out.update(baseline_from=str(wt), task=str(Path(run_dir) / "TASK.fix.md"), remote=remote)
    return out, None


def _remote_overlay(cwd, man, work, fspec):
    """Land the job's own files of the remote run's changes in the local work dir (just rebuilt from baseline_from) ->
    (violations, notes, infra). violations: local files the run changed, deleted or replaced outside the job's files (the
    local scope refuses them); notes: new files it left that are not landed; infra: why a job file cannot land, else None."""
    mine = {fspec["allowed"][0], fspec["test_file"], *(fspec.get("update_tests") or [])}
    drop = set(fspec.get("drop") or [])
    modes, viol, notes = man["modes"], [], []
    for rel in sorted(work):
        data, mode = work[rel], modes.get(rel, 0o644)
        cur = _remote_read(cwd, rel)
        if cur is not None and cur[0] == data and bool(cur[1] & 0o111) == bool(mode & 0o111):
            continue
        if rel in mine:
            _remote_write(cwd, rel, data, mode)
        elif _remote_tolerated(rel):
            continue
        elif _remote_lexists(cwd, rel):
            viol.append(rel)
        else:
            notes.append(rel)
    for rel in man["deleted"]:
        if not _remote_rel(rel):
            return viol, notes, f"an unsafe deleted path {_remote_text(rel, 80)!r}"
        if not _remote_lexists(cwd, rel):
            continue
        if rel in mine or rel in drop:
            _remote_unlink(cwd, rel)
        elif not _remote_tolerated(rel):
            viol.append(rel)
    for sk in man["skipped"]:
        rel = sk["path"]
        if rel.startswith("run/"):
            notes.append(rel)
        elif rel in mine:
            return viol, notes, f"{rel} came back without its content ({_remote_text(sk['why'], 60)})"
        elif _remote_rel(rel) and _remote_lexists(cwd, rel) and not _remote_tolerated(rel):
            viol.append(rel)
        else:
            notes.append(_remote_text(rel, 120))
    return viol, notes, None


def _cpu_job_lib():
    """The dispatch-cpu-runner client next to this file (CPU_JOB_LIB: another copy)."""
    import importlib.util
    p = os.environ.get("CPU_JOB_LIB") or str(Path(os.path.realpath(__file__)).parent / "cpu_job.py")
    sp = importlib.util.spec_from_file_location("_lane_cpu_job", p)
    m = importlib.util.module_from_spec(sp)
    sys.modules["_lane_cpu_job"] = m  # dataclasses look their module up
    sp.loader.exec_module(m)
    return m


def _remote_park(why):
    """Exit PAUSED (see provider_paused): the queue re-launches the run once Darkbloom serves again."""
    return park_run(Path(PAUSE_FILE[0] or "mini-provider-pause.json"), "chat_request_failed",
                    {"lane_cause": f"darkbloom capacity wait: remote: {why}"},
                    f"[mini-runner] PAUSED: remote: {why}; the queue resumes this run when Darkbloom serves again (not graded, not a failure)")


def remote_auto(a, spec):
    """mode "auto" with spec["remote"] (see the block comment above): one runner job, one RESULT line (with lane=remote:<job>)."""
    cwd, run_dir = os.path.abspath(a.cwd), Path(os.path.abspath(a.cwd)).parent
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", str(spec.get("name") or run_dir.name))[:80].lstrip(".") or "auto"
    wt = os.path.abspath(os.path.expanduser(str(spec.get("baseline_from") or "")))
    try:
        desc = Path(os.path.expanduser(str(spec["task"]))).read_text(errors="replace").strip()
    except (KeyError, OSError) as e:
        print("RESULT " + tag_result(f"FAIL bad-spec mode=auto cannot read the task ({type(e).__name__})"), flush=True)
        return RC_FAIL

    def done(line, rc, **kw):
        line = _remote_text(line, 2000)
        auto_review(run_dir, cwd, name, desc, line, **kw)
        print(f"[mini-runner] REVIEW packet: {run_dir / 'REVIEW-AUTO.md'}", flush=True)
        print("RESULT " + tag_result(line), flush=True)
        return rc

    # the checks auto() makes before its first model call, here, where the rules are this host's
    if not desc:
        return done("FAIL bad-spec mode=auto the task (description) is empty", RC_FAIL)
    if not (spec.get("baseline_from") and Path(wt).is_dir() and git(wt, "rev-parse", "--show-toplevel").strip()
            and os.path.realpath(git(wt, "rev-parse", "--show-toplevel").strip()) == os.path.realpath(wt)):
        return done("FAIL bad-spec mode=auto baseline_from must be the root of a git worktree", RC_FAIL)
    files = [x for x in git(wt, "ls-files").split("\n") if x]
    hints = [auto_resolve(h, files) or h for h in (str(h) for h in as_list(spec.get("files")) if str(h).strip())]
    fn_hint = str(spec["fn"]).split(".")[-1] if spec.get("fn") else None
    hits = auto_forbidden([("repo", x) for x in auto_repo_names(wt)] + [("file hint", h) for h in hints] + [("function hint", fn_hint)], desc)
    if hits:
        return done("FAIL bad-spec mode=auto phase=refused must-not-offload: " + "; ".join(f"{w} {v!r} ({h})" for w, v, h in hits)[:300], RC_FAIL)
    if spec.get("diagnose") == "skip" and not (len(hints) == 1 and hints[0] in files and fn_hint):
        return done("FAIL bad-spec mode=auto diagnose=skip needs exactly one tracked file hint and a fn hint", RC_FAIL)
    hint_txt = ", ".join([*hints, *([fn_hint] if fn_hint else [])]) or None
    rcfg = spec.get("remote")

    def int_in(v, lo, hi):
        return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi

    why = None
    if not isinstance(rcfg, dict):
        why = "remote must be an object"
    elif str(rcfg.get("python")) not in REMOTE_PYTHONS:
        why = f"remote.python must be one of {', '.join(REMOTE_PYTHONS)}"
    elif rcfg.get("deps_lock") is not None and not (isinstance(rcfg["deps_lock"], str)
                                                    and Path(os.path.expanduser(rcfg["deps_lock"])).is_file()):
        why = "remote.deps_lock is not a file"
    elif not int_in(rcfg.get("max_calls", REMOTE_MAX_CALLS), 1, 2000):
        why = "remote.max_calls must be 1..2000"
    elif not int_in(rcfg.get("setup_timeout", REMOTE_SETUP_S), 60, 1800):
        why = "remote.setup_timeout must be 60..1800 s"
    elif not int_in(spec.get("timeout", AUTO_TIMEOUT), 300, 4 * 3600):
        why = "timeout must be 300..14400 s"
    else:  # the job ships every untracked file git does not ignore: a venv or node_modules would go too, and break the setup
        heavy = []
        for x in git(wt, "ls-files", "-o", "--exclude-standard").split("\n"):  # a quoted name (core.quotePath) keeps its parts
            parts = (x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x).split("/")
            i = next((n for n, p in enumerate(parts) if p in (".venv", "venv", "node_modules") and (n < len(parts) - 1 or p == ".venv")), None)
            if i is not None and "/".join(parts[:i + 1]) not in heavy:
                heavy.append("/".join(parts[:i + 1]))
        if heavy:
            why = (f"baseline_from has {', '.join(heavy[:3])} untracked and not ignored (the remote job would ship it): "
                   "list it in the repo's info/exclude or .gitignore")
    if why:
        return done(f"FAIL bad-spec mode=auto {why}", RC_FAIL, hint=hint_txt)
    py, lock = str(rcfg["python"]), rcfg.get("deps_lock")
    total = spec.get("timeout", AUTO_TIMEOUT)
    here = Path(os.path.realpath(__file__)).parent
    try:
        tools = {f"bin/{x}": (here / x).read_bytes() for x in REMOTE_TOOLS}
        for p in (OVERLAY, DIAG_OVERLAY, TEST_OVERLAY, POLISH_OVERLAY):
            tools[f"bin/configs/{p.name}"] = Path(p).read_bytes()
        tools["lane/mini.lock"] = _remote_mini_lock()
        if _lane_tools_lock():
            tools["lane/tools.lock"] = _lane_tools_lock()
        if lock:
            tools["lane/deps.lock"] = Path(os.path.expanduser(lock)).read_bytes()
        tools["lane/TASK.md"] = (desc + "\n").encode()
        tools["lane/spec.json"] = (json.dumps({**spec, "name": name}, indent=1) + "\n").encode()
        for f in REMOTE_KEEP:
            p = run_dir / f
            if p.is_file() and not p.is_symlink():
                tools[f"lane/{f}"] = p.read_bytes()
    except (OSError, ValueError) as e:
        return done(f"FAIL mode=auto remote-infra cannot pack the job: {type(e).__name__}: {e}"[:600], RC_FAIL, hint=hint_txt)
    model = a.model or DEFAULT_MODEL
    wall = total + REMOTE_WALL_EXTRA
    allow = max(0, min(REMOTE_HOLD_ALLOW_S, REMOTE_JOB_MAX_S - wall - 600))
    py_sel = 'PY=/opt/python/bin/python3; [ -x "$PY" ] || PY=python3; '
    setup_cmd = (py_sel + 'mkdir -p "$HOME" && ln -sfn "$JOB_TOOLS/bin" "$HOME/bin" && "$PY" "$JOB_TOOLS/bin/lane_remote.py" setup '
                 f"--python={shlex.quote(py)}" + (' --deps-lock="$JOB_TOOLS/lane/deps.lock"' if lock else "")
                 + ' --mini-lock="$JOB_TOOLS/lane/mini.lock"' + (' --tools-lock="$JOB_TOOLS/lane/tools.lock"' if "lane/tools.lock" in tools else ""))
    run_cmd = (py_sel + f'exec "$PY" "$JOB_TOOLS/bin/lane_remote.py" run --name={shlex.quote(name)} --model={shlex.quote(model)} '
               f"--wall={wall} --hold-allow={allow} --outage-max={REMOTE_OUTAGE_S}")
    log, stop, held, phase = _RemoteLog(), [None], [None], ["submit"]

    def on_sig(signum, _frame):
        if phase[0] != "submit":
            _abort("preempted")
        if stop[0]:
            raise _Preempted()
        stop[0] = signal.Signals(signum).name  # cancel_if hands it to cpu_job, which cancels the runner job

    def prog(ph, text):
        t = _remote_text(text or "", 200)
        held[0] = (held[0] or time.time()) if ph == "waiting" and REMOTE_HOLD_TEXT in t else None
        print(f"[mini-runner] remote lane: {_remote_text(ph or '', 40)}: {t}", flush=True)

    def cancel():
        if stop[0]:
            return stop[0]
        if held[0] and time.time() - held[0] > REMOTE_OUTAGE_S:
            return "model hold"
        return None

    old = {s: signal.signal(s, on_sig) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        print(f"[mini-runner] remote lane: submitting {name} (python {py}, model {model}, wall {wall}s + up to {allow}s "
              f"of provider waits, "
              f"{'deps.lock' if lock else 'deps resolved on the runner'})", flush=True)
        try:
            res = _cpu_job_lib().submit_cpu_job(
                wt, run_cmd, wall + 600 + allow, stage="lane", label=f"lane {name}", tools=tools,
                api=os.environ.get("CPU_RUNNER_API") or REMOTE_API, claim_timeout_s=REMOTE_CLAIM_S, fallback=False,
                setup={"cmd": setup_cmd, "timeout_s": rcfg.get("setup_timeout", REMOTE_SETUP_S)},
                llm={"models": [model], "max_calls": rcfg.get("max_calls", REMOTE_MAX_CALLS)}, artifacts=True,
                artifacts_dest=str(run_dir / "remote-artifacts.tgz"), on_log=log, on_progress=prog, cancel_if=cancel,
                owner=f"mini-runner auto {name}")
        except _Preempted:
            raise
        except Exception as e:  # noqa: BLE001 -- the lane failed, not the model
            log.flush()
            if stop[0]:
                _abort("preempted")
            return done(f"FAIL mode=auto remote-infra {type(e).__name__}: {e}", RC_FAIL, hint=hint_txt)
        phase[0] = "local"
        log.flush()
        try:
            return _remote_finish(res, log, stop, spec, name, desc, wt, cwd, run_dir, files, hints, fn_hint, hint_txt, wall, done)
        except Exception as e:  # noqa: BLE001 -- a bug here must still end in one RESULT line (and never as the model's failure)
            import traceback
            for ln in traceback.format_exc().splitlines():
                print("[mini-runner] remote lane bug: " + _remote_text(ln, 400), flush=True)
            print("RESULT " + tag_result(_remote_text(f"FAIL mode=auto remote-infra internal error {type(e).__name__}: {e}", 600)), flush=True)
            return RC_FAIL
    except _Preempted:
        log.flush()
        _abort("preempted")
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def _remote_finish(res, log, stop, spec, name, desc, wt, cwd, run_dir, files, hints, fn_hint, hint_txt, wall, done):
    """remote_auto after the runner job: what came back -> run files, the local work dir and the RESULT line."""
    jid = str(getattr(res, "job_id", None) or "?")
    tag = f" lane=remote:{_remote_text(jid, 12)}"
    ie = _remote_text(res.infra_error, 400) if getattr(res, "infra_error", None) else None
    llm = res.llm if isinstance(getattr(res, "llm", None), dict) else {}
    print(f"[mini-runner] remote lane: job {_remote_text(jid, 40)} ended: exit {res.exit_code}{' (timed out)' if res.timed_out else ''}"
          f"{'; ' + ie if ie else ''}; llm {llm.get('calls', '?')} calls, {llm.get('errors', '?')} errors, {llm.get('held', 0)} held", flush=True)
    if stop[0]:
        _abort("preempted")
    if ie and ie.startswith("cancelled: model hold"):
        return _remote_park(f"the model hold outlasted {REMOTE_OUTAGE_S}s before a runner took the job")
    if ie and ie.startswith("cancelled:"):
        _abort("preempted")
    if ie:
        return done(f"FAIL mode=auto remote-infra {ie}{tag}", RC_FAIL, hint=hint_txt)
    arts = getattr(res, "artifacts", None)
    if not getattr(res, "artifacts_path", None) or not isinstance(arts, dict) or arts.get("error"):
        why = _remote_text(arts.get("error"), 200) if isinstance(arts, dict) and arts.get("error") else "none came back"
        return done(f"FAIL mode=auto remote-infra artifacts: {why}{tag}", RC_FAIL, hint=hint_txt)
    try:
        man, work, run = _remote_extract(res.artifacts_path)
        bad = _remote_manifest(man)
    except Exception as e:  # noqa: BLE001 -- tar, gzip, JSON, size or path errors: the artifacts are unusable
        return done(f"FAIL mode=auto remote-infra artifacts: {type(e).__name__}: {e}"[:600] + tag, RC_FAIL, hint=hint_txt)
    if bad:
        return done(f"FAIL mode=auto remote-infra the manifest is malformed ({bad}){tag}", RC_FAIL, hint=hint_txt)
    if man.get("error"):
        return done(f"FAIL mode=auto remote-infra the remote run's files: {_remote_text(man['error'], 300)}{tag}", RC_FAIL, hint=hint_txt)
    if set(man["changed"]) != set(work) or set(man["run_files"]) != set(run):
        return done(f"FAIL mode=auto remote-infra the artifacts do not match the manifest{tag}", RC_FAIL, hint=hint_txt)
    # the run's review files, paths rewritten (the accepted diagnosis and its key byte for byte: they are shipped back as they came)
    rw = _remote_rewriter(man, [("work", cwd), ("run_dir", run_dir), ("checkout", wt), ("home", HOME)])
    fspec, fs_why = None, "no spec.auto.json came back"
    try:
        sa = run_dir / "spec.auto.json"
        if sa.is_file() or sa.is_symlink():
            sa.unlink()  # a stale one from an earlier launch must not stand for this one
        for f, data in sorted(run.items()):
            if f.casefold() in ("review-auto.md", "spec.auto.json"):
                continue
            if f in REMOTE_KEEP or f.endswith(".traj.json"):
                out = data
            elif f.endswith(".json"):
                try:
                    out = (json.dumps(_rewrite_json(json.loads(data), rw), indent=1) + "\n").encode()
                except Exception:  # noqa: BLE001 -- unparsable: kept as it came
                    out = data
            else:
                out = rw(data.decode("utf-8", "replace")).encode()
            _remote_write(run_dir, f, out, 0o644)
        if "spec.auto.json" in run:
            fspec, fs_why = _remote_fspec(run["spec.auto.json"], rw, wt, run_dir, spec.get("remote"), files)
        if fspec:
            _remote_write(run_dir, "spec.auto.json", (json.dumps(fspec, indent=1) + "\n").encode(), 0o644)
    except (OSError, ValueError) as e:
        return done(f"FAIL mode=auto remote-infra cannot write the run files: {type(e).__name__}: {e}"[:600] + tag, RC_FAIL, hint=hint_txt)
    if fs_why and "spec.auto.json" in run:
        print(f"[mini-runner] remote lane: the remote spec.auto.json is not used: {fs_why}", flush=True)
    dtext = rw(run["DIAGNOSIS.md"].decode("utf-8", "replace")) if "DIAGNOSIS.md" in run else ""
    kw = {"hint": hint_txt, "secs": md_h2(dtext) if dtext else {}}
    if fspec:
        kw["target"] = (fspec["allowed"][0], as_list(fspec["target_fn"]))
    elif dtext:
        try:
            df, dfns, _dx = auto_target(dtext)
            kw["target"] = (auto_resolve(df, files) or df, dfns)
        except Exception:  # noqa: BLE001 -- a review aid only
            pass
    elif spec.get("diagnose") == "skip" and hints:
        kw["target"] = (hints[0], [fn_hint])
    if fspec:  # this host's must-not-offload rules on what the remote run diagnosed (its own check ran there)
        hits = auto_forbidden([("diagnosed file", fspec["allowed"][0])] + [("diagnosed function", x) for x in as_list(fspec["target_fn"])]
                              + [("diagnosed constant", x) for x in fspec.get("new_names") or []])
        if hits:
            return done("FAIL bad-spec mode=auto phase=refused must-not-offload: "
                        + "; ".join(f"{w} {v!r} ({h})" for w, v, h in hits)[:300] + tag, RC_FAIL, **kw)
    # PAUSE: a run that hit its wall after its provider waits were all given back (credited_s under hold_allow_s) ran out of
    # its own time, a FAIL like any other; one whose waits were not (past the allowance, or a remote from before the credit)
    # was stopped by the provider and is parked. Parking every wall hit that met a hold would re-run a slow model forever
    # once holds are common.
    cr, ha = man.get("credited_s"), man.get("hold_allow_s")
    repaid = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (cr, ha)) and cr < ha
    if man["rc"] == RC_PAUSED or ((man["terminated"] or (man["wall_hit"] and not repaid))
                                  and (log.outage or (llm.get("held") or 0) > 0)):
        return _remote_park("the remote run paused for a provider outage" if man["rc"] == RC_PAUSED
                            else "the remote run was stopped during a provider outage or model hold")
    viol, notes, oinfra = [], [], None
    if fspec:
        tfix = run_dir / "TASK.fix.md"
        try:
            prepare(cwd, {"baseline_from": wt, "task": str(tfix if tfix.is_file() else spec["task"]),
                          **({"keep": spec["keep"]} if isinstance(spec.get("keep"), list) else {})})
            cur = _remote_read(cwd, fspec["allowed"][0])
            base_text = cur[0].decode("utf-8", "replace") if cur and cur[0] is not None else ""
            viol, notes, oinfra = _remote_overlay(cwd, man, work, fspec)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            oinfra = f"cannot land the files: {type(e).__name__}: {e}"
        if notes:
            print(f"[mini-runner] remote lane: left on the runner (not landed): {_remote_text(', '.join(notes), 400)}", flush=True)
        if not oinfra:
            kw.update(fspec=fspec, base_text=base_text)
    res_line = re.sub(r"^RESULT\s+", "", _remote_text(man.get("result") or "", 2000).strip())
    res_line = re.sub(r" class=\S+", "", res_line)
    rc = RC_FAIL
    if man["wall_hit"]:
        cr = man.get("credited_s")
        line = f"FAIL mode=auto timeout-total remote wall {wall}s" + (
            f" (+{int(cr)}s of provider waits)" if isinstance(cr, (int, float)) and not isinstance(cr, bool) and cr >= 1 else "")
    elif man["terminated"]:
        line = "FAIL mode=auto remote-infra the runner stopped the remote run"
    elif not re.match(r"(?:PASS|FAIL)\b", res_line):
        line = f"FAIL mode=auto remote-infra the remote run ended without a RESULT (exit {man['rc']})"
    elif res_line.startswith("FAIL"):
        line = res_line
    elif not re.match(r"PASS (?:needs-review soft=\S+ )?mode=auto\b", res_line) or man["rc"] != RC_OK:
        line = f"FAIL mode=auto remote-infra a PASS that is not an auto PASS (exit {man['rc']}): {res_line[:200]}"
    elif not fspec:
        line = f"FAIL mode=auto remote-infra a PASS without a usable spec.auto.json ({fs_why})"
    elif oinfra:
        line = f"FAIL mode=auto remote-infra {oinfra}"
    elif viol:
        line = f"FAIL mode=auto phase=fix remote-scope {', '.join(viol)}"[:600]
    else:
        line, rc = res_line, RC_OK
    if oinfra and not line.startswith("FAIL mode=auto remote-infra"):
        print(f"[mini-runner] remote lane: {_remote_text(oinfra, 400)}", flush=True)
    return done(line + tag, rc, **kw)


def main(argv=None):
    try:
        return _main(argv)
    except ProviderDown as e:
        return provider_paused(e)
    except ModelGone as e:  # the model was swapped out mid-run: infrastructure, never graded as the model's failure
        _incident("model-not-served", detail=str(e)[:300])
        _kill_children()
        _drop_secrets()
        print(f"[mini-runner] {e}. Darkbloom serves other models now (infrastructure, not the model); "
              f"resubmit or resume the job once the model is served again.", flush=True)
        print("RESULT " + tag_result(f"FAIL model-not-served {e}"), flush=True)
        return RC_FAIL
    except GitStall as e:  # a filesystem that stopped answering: infrastructure, never graded as the model's failure
        _incident("git-stall", detail=str(e)[:300])
        _kill_children()
        _drop_secrets()
        print(f"[mini-runner] git stalled: {e}. The disk under the work dir or its source repo is not answering "
              f"(infrastructure, not the model); resume the job once git answers there again.", flush=True)
        print("RESULT " + tag_result(f"FAIL git-stall {e}"), flush=True)
        return RC_FAIL


def provider_paused(e):
    """Exit with the queue's PAUSED code: the queue stamps the row paused with this lane_cause (an infra pause, never charged to an
    attempt/resume budget or the failure ledger) and re-launches the whole run once Darkbloom's /health is OK again. No gate is run."""
    return park_run(Path(PAUSE_FILE[0] or "mini-provider-pause.json"), "chat_request_failed", {"lane_cause": f"darkbloom capacity wait: {e}"},
                    f"[mini-runner] PAUSED: {e}; the queue resumes this run when Darkbloom serves again (not graded, not a failure)")


def park_run(pf, reason, meta, msg):
    """Write the pause marker the queue reads (pause_reason/pause_meta), print the RESUMABLE TRANSCRIPT line naming it, return RC_PAUSED."""
    _kill_children()
    _drop_secrets()
    try:
        pf.write_text(json.dumps({"pause_reason": reason, "pause_meta": meta}))
        print(f"[worker] RESUMABLE TRANSCRIPT: {pf}", flush=True)
    except OSError as oe:
        print(f"[mini-runner] could not write the pause marker: {oe}", flush=True)
    print(msg, flush=True)
    return RC_PAUSED


PAUSE_FILE = [None]


def validate_spec_job(a):
    """--validate-spec (a validate job on a remote queue): the checks mini-finish's continuation and mini-submit's C5/C6 run on the
    submitting machine when the queue is local, run here instead -- mini-validate V1-V8 on a copy of the task file, then the
    runner's own --check-only (C5) and, for a steps spec, --try-fix-chain <ref> (C6), each in a fresh scratch dir beside the run dir.
    VALIDATION.md and the (possibly field-filled) spec copy are written into --cwd, so the default job result carries them. No model."""
    me = Path(os.path.realpath(__file__))
    cwd = Path(a.cwd).resolve()
    run = cwd.parent
    src = Path(a.task_file).resolve()
    vdir = run / "validate"
    shutil.rmtree(vdir, ignore_errors=True)
    vdir.mkdir(parents=True)
    sp = vdir / "spec.json"
    shutil.copy2(src, sp)
    try:
        spec = json.loads(sp.read_text())
        assert isinstance(spec, dict)
    except (OSError, ValueError, AssertionError):
        print("RESULT " + tag_result("FAIL bad-spec validate-spec: the task file is not a JSON object"), flush=True)
        return RC_FAIL
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MINI_RUNNER": str(me)}
    steps = [("V1-V8 mini-validate", [sys.executable, str(me.parent / "mini-validate"), str(sp)], None)]
    steps.append(("C5 runner --check-only", [sys.executable, str(me), "--check-only", "--task-file", str(sp)], "validate-c5"))
    if spec.get("steps"):
        refd = Path(os.path.expanduser(str(spec.get("ref") or "ref")))
        refd = refd if refd.is_absolute() else src.parent / refd
        steps.append(("C6 runner --try-fix-chain", [sys.executable, str(me), "--check-only", "--task-file", str(sp),
                                                    "--try-fix-chain", str(refd)], "validate-c6"))
    failed = None
    for name, cmd, scratch in steps:
        if scratch:
            sd = run / scratch
            shutil.rmtree(sd, ignore_errors=True)
            sd.mkdir()
            cmd = cmd[:3] + ["--cwd", str(sd)] + cmd[3:]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, cwd=str(vdir), env=env)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, "TIMEOUT"
        if scratch:
            shutil.rmtree(run / scratch, ignore_errors=True)
        keep = [ln for ln in out.splitlines() if re.match(r"\s+s\d+ V\d|\[mini-validate\] (PASS|FAIL)", ln)] if rc == 0 else out.splitlines()[-40:]
        print(f"[mini-runner] validate-spec {name}: {'ok' if rc == 0 else 'FAIL rc=%d' % rc}", flush=True)
        for ln in keep[-40:]:
            print("  | " + ln[:400], flush=True)
        if rc != 0:
            failed = name
            break
    for src_f, dst_n in ((vdir / "VALIDATION.md", "VALIDATION.md"), (sp, "validated-spec.json")):
        if src_f.is_file():
            shutil.copy2(src_f, cwd / dst_n)
    if failed:
        print("RESULT " + tag_result(f"FAIL validate-spec {failed}"), flush=True)
        return RC_FAIL
    print("RESULT PASS validate-spec V1-V8" + " C5" + (" C6" if spec.get("steps") else ""), flush=True)
    return RC_OK


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model")
    ap.add_argument("--host")
    ap.add_argument("--num-ctx", default=None)
    ap.add_argument("--cwd")
    ap.add_argument("--task-file")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--self-test-scope", action="store_true", help="only the fn_scope_check fixtures (no model, no network)")
    ap.add_argument("--check-verify", "--check-only", dest="check_verify", action="store_true",
                    help="prepare the scratch dir and prove the oracle (two-phase: baselines + gate a supplied test), then exit (no model)")
    ap.add_argument("--probe", action="store_true",
                    help="with --check-only (two-phase): write a throwaway probe test, run it with full reach and print PROBE ok / PROBE FAIL <reason> "
                         "(modules load, target_fn runs, anchor exists), no model")
    ap.add_argument("--try-test", help="with --check-only (two-phase): gate this candidate test file instead of the model's")
    ap.add_argument("--try-fix", action="append", metavar="DST=SRC",
                    help="with --try-test: copy SRC over DST in the scratch copy, then run the phase B gates")
    ap.add_argument("--try-fix-chain", metavar="REFDIR",
                    help="with --check-only on a steps spec: apply REFDIR/s<N>/<allowed> cumulatively and run the real phase B gates per step (every step must be GREEN)")
    ap.add_argument("--draft-check", action="store_true",
                    help="with --check-only --try-test on a test_draft (phase_a_only) spec: also run the spec's draft_accept_cmd on the candidate")
    ap.add_argument("--mutation", action="store_true",
                    help="with --check-only: also run the spec's mutation_gate on a green --try-fix candidate and on the LAST step of a "
                         "replay (mini-finish's check, so a finish meets the same gates as the job)")
    ap.add_argument("--try-diagnosis", metavar="FILE",
                    help="with --check-only on a diagnose spec: gate FILE as DIAGNOSIS.md with diag_check.py, no model")
    ap.add_argument("--validate-spec", action="store_true",
                    help="a validate job (remote queue): mini-validate V1-V8 on a copy of --task-file, then --check-only and, for a steps "
                         "spec, --try-fix-chain; VALIDATION.md and the filled spec go to --cwd. No model")
    a = ap.parse_args(argv)
    _RUN_CTX.update({k: v for k, v in (("cwd", a.cwd), ("task_file", a.task_file)) if v})
    if a.model and ":" in a.model:
        # 2026-10-25: the runner talks straight to the local Darkbloom daemon, which 503s
        # ("Provider capacity is temporarily unavailable") on any model id it doesn't serve --
        # a legacy Ollama tag (qwen3.8:27b-q4_K_M) stalled a job ~9h behind a fake outage.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import model_profile as _mp
            a.model = _mp.resolve_alias(a.model, "darkbloom")[0]
        except Exception:
            pass
    if a.self_test:
        return _scratch_incidents(self_test)
    if a.self_test_scope:
        return _scratch_incidents(self_test_scope)
    if not (a.cwd and a.task_file):
        ap.error("--cwd and --task-file are required")
    if a.validate_spec:
        return validate_spec_job(a)
    held = None if a.check_verify else finish_guard(a.cwd)
    if held:  # before the endpoint config is read or prepare() runs: a re-run of a claimed job must not touch its work dir
        print(f"[mini-runner] {held}", flush=True)
        print("RESULT " + tag_result(f"FAIL bad-cwd {held}"), flush=True)
        return RC_FAIL
    PAUSE_FILE[0] = str(Path(a.cwd).resolve().parent / (Path(a.cwd).resolve().name + ".provider-pause.json"))
    try:
        spec = json.loads(Path(a.task_file).read_text())
        chain = bool(spec.get("steps"))
        allowed = list(spec["allowed"]) if "allowed" in spec or not (chain or spec.get("mode") == "auto") else []
        local = {} if (a.check_verify and (chain or spec.get("mode") in ("two-phase", "diagnose", "auto"))) else _read_local()
    except (OSError, ValueError, KeyError) as e:
        print(f"[mini-runner] bad spec or endpoint config: {type(e).__name__}", flush=True)
        return RC_FAIL
    if local and not a.check_verify and not a.self_test:
        # Fail fast if the daemon doesn't serve this model id: it 503s "Provider capacity is temporarily
        # unavailable" for unknown ids, which the outage wait would otherwise retry for days (2026-10-25).
        try:
            _rq = urllib.request.Request(local["base_url"].rstrip("/") + "/models",
                                         headers={"Authorization": "Bearer " + local.get("api_key", "")})
            _served = [m["id"] for m in json.loads(urllib.request.urlopen(_rq, timeout=15).read())["data"]]
        except Exception:
            _served = []   # endpoint unreachable/odd: leave it to the normal outage handling
        if _served and (a.model or DEFAULT_MODEL) not in _served:
            print(f"[mini-runner] model {(a.model or DEFAULT_MODEL)!r} is not served by the local Darkbloom daemon (serves {_served})", flush=True)
            print("RESULT " + tag_result("FAIL model-not-served"), flush=True)
            return RC_FAIL
    if a.try_fix_chain and not (a.check_verify and chain):
        print("[mini-runner] --try-fix-chain needs --check-only and a `steps` spec", flush=True)
        return RC_FAIL
    if a.draft_check and not (a.check_verify and a.try_test):
        print("[mini-runner] --draft-check needs --check-only and --try-test", flush=True)
        return RC_FAIL
    if a.try_diagnosis and not (a.check_verify and spec.get("mode") == "diagnose"):
        print("[mini-runner] --try-diagnosis needs --check-only and a diagnose spec", flush=True)
        return RC_FAIL
    bad = cwd_guard(a.cwd, spec, a.task_file)
    if bad:
        print(f"[mini-runner] {bad}", flush=True)
        print("RESULT " + tag_result(f"FAIL bad-cwd {bad}"), flush=True)
        return RC_FAIL
    if spec.get("remote"):  # the project's code runs on the CPU runner (remote_auto); only an auto job can go there
        if chain or spec.get("mode") != "auto":
            print("RESULT " + tag_result("FAIL bad-spec remote needs mode auto"), flush=True)
            return RC_FAIL
        if not a.check_verify:
            return remote_auto(a, spec)
    if chain:
        return run_steps(a, spec, local, check_only=a.check_verify)
    if spec.get("mode") == "two-phase":
        return two_phase(a, spec, allowed, local, check_only=a.check_verify)
    if spec.get("mode") == "diagnose":  # spec must carry "allowed": [] (read at the top of main)
        return diagnose(a, spec, local, a.check_verify)
    if spec.get("mode") == "auto":  # description -> diagnose -> derived two-phase spec -> model test -> model fix (one RESULT)
        return auto(a, spec, local, a.check_verify)
    cwd = a.cwd
    prepare(cwd, spec)
    err, warn = precheck(spec, cwd, sound=not spec.get("skip_soundness"))
    why = report_precheck(err, warn)
    if err:
        print("RESULT " + tag_result(f"FAIL bad-spec {why}"), flush=True)
        return RC_FAIL
    if not spec.get("skip_soundness"):
        print("[mini-runner] oracle sound: fails on baseline, passes with refimpl", flush=True)
    if a.check_verify:
        return RC_OK
    tname = spec.get("task", "TASK.md")
    task_text = (Path(cwd) / ("TASK.md" if Path(os.path.expanduser(tname)).is_absolute() else tname)).read_text()
    task_text = (f"WORKING DIRECTORY: {Path(cwd).resolve()}. The code you must edit is in this directory (your shell starts here). "
                 "Use relative paths. Do NOT search `/`, cd elsewhere, or read or edit any file outside this directory; "
                 "a copy of the repo elsewhere (e.g. /private/tmp) is NOT yours.\n\n"
                 f"A `{spec.get('verify', 'verify.sh')}` is in the working directory. Run `bash {spec.get('verify', 'verify.sh')}` "
                 "to check your work. Do not submit until it passes.\n\n") + READ_HINT + graph_lookup_note(cwd, spec) + task_text
    gone = preflight_missing(cwd, task_text)
    if gone:
        print(f"[mini-runner] preflight FAIL: TASK.md references missing in baseline: {gone}", flush=True)
        print("RESULT FAIL preflight class=SPEC", flush=True)
        return RC_FAIL
    traj = Path(cwd).parent / (Path(cwd).name + ".mini.traj.json")
    env = {**os.environ, "OPENAI_API_BASE": local["base_url"],  # key goes through the launcher file, never env
           "MSWEA_COST_TRACKING": "ignore_errors", "MSWEA_CONFIGURED": "true"}
    env.pop("OPENAI_API_KEY", None)
    print(f"[mini-runner] start model={a.model} cwd={cwd}", flush=True)
    install_signal_handlers(int(spec.get("timeout", 900)) + 900, int(spec.get("deadline_grace", 120)))
    usage = Usage()
    log, timed_out = run_mini(a.model or DEFAULT_MODEL, task_text, traj, cwd, env, local["api_key"], int(spec.get("timeout", 900)), usage)
    if usage.rows:
        print(usage.summary(), flush=True)
    if timed_out:
        print("RESULT timeout", flush=True)
        return RC_FAIL
    missing = said_missing(log, traj)
    v_rc, _ = _bash(f"bash {shlex.quote(spec.get('verify', 'verify.sh'))}", cwd, {k: x for k, x in os.environ.items() if k != "OPENAI_API_KEY"}, remaining(900))
    v = types.SimpleNamespace(returncode=v_rc)
    revert_bytecode(cwd)
    changed = changed_files(cwd)
    out_scope = scope_violations(changed, allowed)
    extra = untracked_violations(git(cwd, "status", "--short").splitlines())
    ok = v.returncode == 0 and not out_scope and not extra and not missing
    print(f"RESULT {'PASS' if ok else 'FAIL'} verify_rc={v.returncode} changed={changed} "
          f"out_of_scope={out_scope} new_files={extra} missing={missing}", flush=True)
    return RC_OK if ok else RC_FAIL


# ---- self-test ------------------------------------------------------------------------------

STUB_MINI = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
a = sys.argv; task = a[a.index("-t") + 1]; traj = a[a.index("-o") + 1]
cwd = next(x.split("=", 1)[1] for x in a if x.startswith("environment.cwd="))
plan = json.load(open(os.environ["STUB_PLAN"]))
if "expect_key" in plan:  # the model client got the key in-process; an agent command (inherits os.environ) must not
    import litellm
    assert litellm.api_key == plan["expect_key"], "launcher did not hand the key to litellm"
    open(plan["env_dump"], "w").write(subprocess.run(["env"], capture_output=True, text=True, env=os.environ).stdout)
if "pidfile" in plan:
    open(plan["pidfile"], "w").write(str(os.getpid()))
if "argv_dump" in plan:  # which agent.* limits each phase's run was given
    open(plan["argv_dump"], "a").write(("A " if "PHASE 1 OF 2" in task else "B ") + " ".join(x for x in a if x.startswith("agent.")) + "\n")
if "task_dump" in plan:  # the prompt each phase's run was given
    open(plan["task_dump"], "a").write(task + "\n=====\n")
if "draft_dump" in plan:  # the draft nudge's MINI_DRAFT_* settings each run was given (A, B, or D for any other run)
    ph = "A" if "PHASE 1 OF 2" in task else ("B" if "PHASE 2 OF 2" in task else "D")
    open(plan["draft_dump"], "a").write(json.dumps({"phase": ph, "env": {k: v for k, v in os.environ.items() if k.startswith("MINI_DRAFT_")}}) + "\n")
if "raw_traj" in plan:  # a canned (mid-run) trajectory, then a long sleep: the runner's watcher should kill us
    json.dump(plan["raw_traj"], open(traj, "w"))
time.sleep(plan.get("sleep", 0))
for ln in plan.get("outage_lines", []):  # what mini_ctx.CtxLitellmModel prints while it waits out a provider outage
    print(ln, file=sys.stderr, flush=True)
if "exit_status" in plan:  # the agent ended abnormally: mini records an exit message, then the process dies
    json.dump({"messages": [{"role": "exit", "content": plan["exit_status"], "extra": {"exit_status": plan["exit_status"], "submission": ""}}]}, open(traj, "w"))
    sys.exit(1)
if "by_task" in plan:  # chain: first entry whose marker is in this run's task text
    plan = {**plan, **next(e for e in plan["by_task"] if e["match"] in task)}
step = plan["A"] if "PHASE 1 OF 2" in task else plan["B"]
for rel, txt in step.items():
    open(os.path.join(cwd, rel), "w").write(txt)
if "needs" in plan:  # needs_info: the model asks instead of fixing (plan "cmds" = the commands it ran, recorded in the trajectory)
    open(os.path.join(cwd, "NEEDS.md"), "w").write(plan["needs"])
now = int(time.time())
pre = []
for i, c in enumerate(plan.get("cmds", [])):
    pre += [{"role": "assistant", "content": "", "extra": {"actions": [{"command": c, "tool_call_id": f"t{i}"}]}}, {"role": "tool", "tool_call_id": f"t{i}", "content": "out"}]
json.dump({"messages": pre + [{"role": "assistant", "content": plan.get("say", "done"),
                         "extra": {"response": {"created": now, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}}}]}, open(traj, "w"))
print("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
'''

CALC = "function add(a, b) { return a - b; }\nfunction other() { return 1; }\nmodule.exports = { add, other };\n"
CALC_FIXED = CALC.replace("a - b", "a + b") + "// sumFix\n"
GOOD_T = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { add } = require('./calc');\n"
          "test('adds', () => { assert.strictEqual(add(1, 2), 3); });\ntest('adds neg', () => { assert.strictEqual(add(-1, 5), 4); });\n")


GATES_V2_FIXTURES = Path.home() / ".ollama-dispatch" / "mini-fixtures"


def _self_test_contain():
    """A drafted-test run (phase_a_only spec, or MINI_CONTAIN=1) gets a throwaway HOME and the sandbox: it can write its work dir (a
    mini-work dir under the real home) but nothing else under the real home. Any other spec keeps the real HOME. Every run carries
    DISPATCH_VERIFY_SANDBOX=1, which a spec env cannot clear."""
    pid = os.getpid()
    work = Path(tempfile.mkdtemp(prefix=".mini-runner-ct-", dir=Path.home()))
    outside = Path.home() / f".mini-runner-ct-outside-{pid}"
    # printenv, not $HOME: _contain rewrites $HOME and ~ in the command to the real home (a test_cmd's $HOME/bin must still resolve)
    probe = (f"echo home=$(printenv HOME); echo sbx=${{DISPATCH_VERIFY_SANDBOX:-}}; echo x > w.txt && echo work=ok; "
             f"(echo x > {shlex.quote(str(outside))}) 2>/dev/null && echo outside=WROTE || echo outside=denied")
    sandboxed = sys.platform == "darwin" and shutil.which("sandbox-exec") and os.environ.get("MINI_MUTATE_SANDBOX", "").lower() != "off"
    old = os.environ.pop("MINI_CONTAIN", None)
    try:
        rc, out, _ = run_cmd(probe, str(work), {"phase_a_only": True})
        assert rc == 0 and "work=ok" in out and "sbx=1" in out, (rc, out)
        assert f"home={Path.home()}\n" not in out, out
        if sandboxed:
            assert "outside=denied" in out and not outside.exists(), out
        rc, out, _ = run_cmd("echo home=$(printenv HOME); echo sbx=${DISPATCH_VERIFY_SANDBOX:-}", str(work), {"env": {"DISPATCH_VERIFY_SANDBOX": "0"}})
        assert f"home={Path.home()}\n" in out and "sbx=1\n" in out, out
        os.environ["MINI_CONTAIN"] = "1"
        rc, out, _ = run_cmd(probe, str(work), {})
        assert "work=ok" in out and "sbx=1" in out and f"home={Path.home()}\n" not in out, out
        if sandboxed:
            assert "outside=denied" in out and not outside.exists(), out
    finally:
        os.environ.pop("MINI_CONTAIN", None)
        if old is not None:
            os.environ["MINI_CONTAIN"] = old
        if outside.exists():
            outside.unlink()
        shutil.rmtree(work, ignore_errors=True)
    print(f"[self-test] contain ok ({'sandboxed' if sandboxed else 'HOME only'})")


def _self_test_source_checks():
    """Fable #4: a frozen python test that only checks the code's own text/AST is rejected (static: every assertion is a
    presence check; runtime: every red test is one); exec loading, sandbox copies, fixtures, found-it guards on executed
    text, absence checks and mixed files pass; spec.source_checks "allow" (with a reason) opts out."""
    code, st = ["calc.py"], ["calc"]
    msg = "every assertion checks the source text"
    flag = {
        "open-read": "import calc\ndef test_a():\n    src = open('calc.py').read()\n    assert 'retry' in src\n"
                     "    assert src.count('sleep(') == 2\nTESTS = [test_a]\n",
        "getsource": "import inspect, calc\ndef test_a():\n    src = inspect.getsource(calc.add)\n    assert 'return a + b' in src\n"
                     "    assert 'if b is None' in src\n",
        "ast-walk": "import ast, calc\ndef test_a():\n    tree = ast.parse(open(calc.__file__).read())\n"
                    "    assert any(isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'retry' for n in ast.walk(tree))\n"
                    "    names = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]\n"
                    "    assert 'sleep' in names\n",
        "helper": "import ast\nfrom pathlib import Path\nCODE = Path(__file__).parent / 'calc.py'\n"
                  "def _calls_in(fn_name):\n    tree = ast.parse(CODE.read_text())\n    for n in ast.walk(tree):\n"
                  "        if isinstance(n, ast.FunctionDef) and n.name == fn_name:\n"
                  "            return {c.func.id for c in ast.walk(n) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}\n"
                  "    return set()\ndef test_retry_wired():\n    assert 'retry' in _calls_in('fetch')\n"
                  "def test_sleep_wired():\n    assert 'sleep' in _calls_in('fetch')\nTESTS = [test_retry_wired, test_sleep_wired]\n",
        "unittest": "import unittest, calc\nfrom pathlib import Path\nclass T(unittest.TestCase):\n"
                    "    def test_a(self):\n        src = Path(calc.__file__).read_text()\n        self.assertIn('retry', src)\n"
                    "        self.assertRegex(src, r'sleep\\(\\d+\\)')\n",
        "if-raise": "import calc\nfrom pathlib import Path\ndef test_a():\n    src = Path('calc.py').read_text()\n"
                    "    if 'retry' not in src:\n        raise AssertionError('no retry')\n    if not src.count('sleep'):\n"
                    "        raise AssertionError('no sleep')\n",
    }
    for k, t in flag.items():
        bad = py_test_rules(t, st, code)
        assert any(b.startswith(msg) for b in bad), (k, bad)
        assert not any(b.startswith(msg) for b in py_test_rules(t, st, code, source_checks=False)), k
    ok = {
        "exec-load": "import types\nfrom pathlib import Path\nCODE = Path(__file__).parent / 'calc.py'\ncalc = types.ModuleType('calc')\n"
                     "exec(compile(CODE.read_text(), str(CODE), 'exec'), calc.__dict__)\n"
                     "def test_add():\n    assert calc.add(2, 3) == 5\n    assert calc.add(-1, 1) == 0\n",
        "fixture": "import json, calc\nfrom pathlib import Path\nFIX = Path(__file__).parent / 'fixtures' / 'calc.json'\n"
                   "def test_add():\n    data = json.loads(FIX.read_text())\n    assert calc.add(*data['args']) == data['want']\n"
                   "    assert calc.add(0, 0) == 0\n",
        "sandbox": "import shutil, subprocess, sys, tempfile\nfrom pathlib import Path\nCODE = Path(__file__).parent / 'calc.py'\n"
                   "def test_cli():\n    with tempfile.TemporaryDirectory() as d:\n        shutil.copy(CODE, d)\n"
                   "        out = subprocess.run([sys.executable, str(Path(d) / 'calc.py'), '2', '3'], capture_output=True, text=True).stdout\n"
                   "        assert out.strip() == '5'\n        assert 'error' not in out\n",
        "extract-guard": "import re\nfrom pathlib import Path\nCODE = Path(__file__).parent / 'calc.py'\ndef test_parse():\n"
                         "    src = CODE.read_text()\n    m = re.search(r'def parse\\(.*?(?=\\ndef |\\Z)', src, re.S)\n"
                         "    assert m, 'parse() not found'\n    ns = {}\n    exec(m.group(0), ns)\n"
                         "    assert ns['parse']('1,2') == [1, 2]\n    assert ns['parse']('') == []\n",
        "absence": "import re\nfrom pathlib import Path\nCODE = Path(__file__).parent / 'calc.py'\ndef test_gone():\n"
                   "    src = CODE.read_text()\n    assert 'legacy_add' not in src\n    assert re.search(r'import\\s+legacy', src) is None\n"
                   "    assert src.count('add(') == 1\n",
        "mixed": "import calc\nfrom pathlib import Path\ndef test_wired():\n    assert 'retry(' in Path(calc.__file__).read_text()\n"
                 "def test_behaviour():\n    assert calc.add(2, 3) == 5\nTESTS = [test_wired, test_behaviour]\n",
        "name-reuse": "import calc\nfrom pathlib import Path\nSRC = open('calc.py').read()\ndef test_src():\n"
                      "    for line in Path(calc.__file__).read_text().splitlines():\n        assert 'TODO' not in line\n"
                      "    assert 'legacy' not in SRC\ndef test_out():\n    line = calc.render(3)\n    assert '3' in line\n"
                      "    SRC = calc.render(4)\n    assert SRC.startswith('n=')\nTESTS = [test_src, test_out]\n",
    }
    for k, t in ok.items():
        bad = [b for b in py_test_rules(t, st, code) if "source text" in b]
        assert not bad, (k, bad)
    # runtime: every red test only checks the text
    red = lambda *ns: [(n, "FAIL", "AssertionError", "") for n in ns]
    rmsg = "only checks the source text"
    assert py_source_only_tests(flag["helper"], code) == {"test_retry_wired", "test_sleep_wired"}
    assert any(rmsg in b for b in per_test_red_problems(red("test_retry_wired", "test_sleep_wired"), {}, flag["helper"], "test_calc.py", code))
    assert not any(rmsg in b for b in per_test_red_problems(red("test_retry_wired", "test_sleep_wired"),
                                                           {"source_checks": "allow"}, flag["helper"], "test_calc.py", code))
    assert not any(rmsg in b for b in per_test_red_problems(red("test_wired", "test_behaviour"), {}, ok["mixed"], "test_calc.py", code))
    mixed_one_red = [("test_wired", "FAIL", "AssertionError", ""), ("test_behaviour", "PASS", None, "")]
    assert any(rmsg in b for b in per_test_red_problems(mixed_one_red, {"baseline_green": ["test_behaviour"]}, ok["mixed"], "test_calc.py", code))
    assert any(rmsg in b for b in per_test_red_problems(red("t.py::T::test_a"), {}, flag["unittest"], "t.py", code))
    assert not any(rmsg in b for b in per_test_red_problems(red("test_src", "test_out"), {}, ok["name-reuse"], "test_calc.py", code))
    assert not any(rmsg in b for b in per_test_red_problems(red("test_a"), {}, flag["open-read"], "test_calc.js", code))
    # precheck: "allow" needs a reason; only fail/allow
    assert any("source_checks" in e for e in precheck({"source_checks": "maybe", "allowed": []})[0])
    assert any("source_checks_reason" in e for e in precheck({"source_checks": "allow", "allowed": []})[0])
    assert not any("source_checks" in e for e in precheck({"source_checks": "allow", "source_checks_reason": "daemon wiring",
                                                         "allowed": []})[0])
    print(f"[self-test] source-checks ok ({len(flag)} flagged, {len(ok)} clean)")


def _self_test_gates_v2():
    """2026-10-24 gates G1-G7: synthetic cases, then the u2c/u2d must-reject replays (skipped when the fixtures are gone)."""
    import tempfile as _tf
    # G1 parsers
    r = parse_test_results("RESULT test_a PASS\nRESULT test_b FAIL AssertionError: x != y\nRESULT FAIL mode=two-phase\n")
    assert r == [("test_a", "PASS", None, ""), ("test_b", "FAIL", "AssertionError", "x != y")], r
    r = parse_test_results("OK test_a\nFAIL test_b: UnboundLocalError: cannot access local variable 'aw'\n")
    assert [x[2] for x in r] == [None, "UnboundLocalError"], r
    r = parse_test_results("PASSED t.py::test_a\nFAILED t.py::test_b - assert 1 == 2\nERROR t.py::test_c - NameError: name 'r' is not defined\n")
    assert [(x[0], x[2]) for x in r] == [("test_a", None), ("test_b", "AssertionError"), ("test_c", "NameError")], r
    r = parse_test_results("TAP version 13\nok 1 - adds\nnot ok 2 - subs\n  ---\n  error: 'Expected values to be strictly equal'\n  code: 'ERR_ASSERTION'\n  ...\n1..2\n")
    assert [(x[0], x[1], x[2]) for x in r] == [("adds", "PASS", None), ("subs", "FAIL", "AssertionError")], r
    assert parse_test_results("Traceback ...\nAssertionError\n") == []
    # node <23 (TAP): a test file that crashed while loading is one top-level `not ok` carrying node's stand-in error
    tap_crash = ("TAP version 13\n# /x/t.test.js:5\n# SyntaxError: Unexpected token ';'\n# Subtest: /x/t.test.js\nnot ok 1 - /x/t.test.js\n"
                 "  ---\n  duration_ms: 31.2\n  failureType: 'testCodeFailure'\n  exitCode: 1\n  signal: ~\n  error: 'test failed'\n"
                 "  code: 'ERR_TEST_FAILURE'\n  ...\n1..1\n")
    assert parse_test_results(tap_crash) == [], "a file that failed to load (TAP): whole-output checks"
    assert [x[1] for x in parse_test_results(tap_crash.replace("'test failed'", "'2 subtests failed'"))] == ["FAIL"]
    # node's spec reporter (its default since node 23): top-level verdicts, a suite takes its failing child's error, skips are out
    spec_out = ("\u2714 keeps (0.1ms)\n\u25b6 grp\n  \u2716 inner (0.2ms)\n\u2716 grp (0.3ms)\n\u2716 adds (0.4ms)\n"
                "\ufe63 skipped (0.1ms) # SKIP\n\u2139 tests 4\n\n\u2716 failing tests:\n\n"
                "test at t.test.js:4:3\n\u2716 inner (0.2ms)\n  TypeError: Cannot read properties of undefined (reading 'x')\n"
                "      at TestContext.<anonymous> (/x/t.test.js:4:30)\n\n"
                "test at t.test.js:6:1\n\u2716 adds (0.4ms)\n  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n  \n"
                "  20 !== 19\n  \n      at TestContext.<anonymous> (/x/t.test.js:6:9) {\n    code: 'ERR_ASSERTION'\n  }\n")
    r = parse_test_results(spec_out)
    assert r == [("keeps", "PASS", None, ""), ("grp", "FAIL", "TypeError", "Cannot read properties of undefined (reading 'x')"),
                 ("adds", "FAIL", "AssertionError", "Expected values to be strictly equal: 20 !== 19")], r
    assert parse_test_results("\u2716 t.test.js (36.6ms)\n\u2139 tests 1\n\u2139 fail 1\n") == [], "a file that failed to load: whole-output checks"
    assert parse_test_results("\u2716 t.test.js (36.6ms)\n\n\u2716 failing tests:\n\ntest at t.test.js:1:1\n\u2716 t.test.js (36.6ms)\n  'test failed'\n") == []
    # G1 reasons
    sp = {"red_ok": [{"type": "FileNotFoundError", "match": r"rescue\.json"}]}
    assert red_reason_problem("AssertionError", "", sp) is None
    assert red_reason_problem("UnboundLocalError", "x", sp) and red_reason_problem("NameError", "name 'r'", sp)
    assert red_reason_problem("AttributeError", "'NoneType' object has no attribute 'x'", sp)
    assert red_reason_problem("FileNotFoundError", "No such file: /t/rescue.json", sp) is None
    assert red_reason_problem("FileNotFoundError", "No such file: /t/other", sp)
    assert red_reason_problem("KeyError", "'x'", {"allow_error_fail": True}) is None
    tt = "TESTS = [test_a, test_b, test_c]\n"
    assert any("PASSES at baseline" in b for b in per_test_red_problems([("test_a", "PASS", None, "")], {}, "", "t.py"))
    assert per_test_red_problems([("test_a", "PASS", None, "")], {"baseline_green": ["test_a"]}, "", "t.py") == []
    assert any("declares 3" in b for b in per_test_red_problems([("test_a", "FAIL", "AssertionError", "")], {}, tt, "t.py"))
    # GUARDS: a listed test must pass at baseline, an unlisted passing one is still refused, guards do not count toward min_tests
    gt = "TESTS = [test_a, test_b, test_keep]\nGUARDS = [test_keep]\n"
    fa, fb = ("test_a", "FAIL", "AssertionError", "x"), ("test_b", "FAIL", "AssertionError", "y")
    assert declared_guards(gt, "t.py") == ["test_keep"] and declared_guards("TESTS = [t]\n", "t.py") == []
    assert declared_guards('const GUARDS = ["keeps the old rows", \'idempotent\'];\n', "t.test.js") == ["keeps the old rows", "idempotent"]
    assert per_test_red_problems([fa, fb, ("test_keep", "PASS", None, "")], {}, gt, "t.py") == []
    gbad = per_test_red_problems([fa, fb, ("test_keep", "FAIL", "AssertionError", "'a\\n' != 'a'")], {}, gt, "t.py")
    assert len(gbad) == 1 and "is in GUARDS" in gbad[0] and "'a\\n' != 'a'" in gbad[0], gbad
    assert any("only 1 test(s) outside GUARDS" in b for b in per_test_red_problems([fa, ("test_b", "PASS", None, ""), ("test_keep", "PASS", None, "")],
                                                                                    {}, gt.replace("[test_keep]", "[test_keep, test_b]"), "t.py"))
    # a green test outside GUARDS with too few red ones: the first rejection gives the count too, before GUARDS exists (moto r3)
    one = per_test_red_problems([fa, ("test_b", "PASS", None, "")], {}, "TESTS = [test_a, test_b]\n", "t.py")
    assert any("test_b PASSES at baseline" in b for b in one) and any("only 1 test(s) outside GUARDS" in b for b in one), one
    assert not any("outside GUARDS" in b for b in per_test_red_problems([fa, fb, ("test_c", "PASS", None, "")], {}, "", "t.py"))
    assert not any("outside GUARDS" in b for b in per_test_red_problems([fa, ("test_b", "PASS", None, "")], {"guards": False}, "", "t.py"))
    assert any("not tests that ran" in b for b in per_test_red_problems([fa, fb, ("test_keep", "PASS", None, "")], {}, gt.replace("[test_keep]", "[test_keep, test_zz]"), "t.py"))
    assert any("PASSES at baseline" in b for b in per_test_red_problems([fa, fb, ("test_keep", "PASS", None, "")], {"guards": False}, gt, "t.py"))
    assert any("GUARDS = []" in ln for ln in py_harness(["test_a"]))
    # G5 python test rules
    ok_t = ("import importlib.util, sys\nspec = importlib.util.spec_from_file_location('arr_webhook', 'arr-webhook.py')\naw = None\n"
            "def reload_aw():\n    global aw\n    aw = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(aw)\n    return aw\n"
            "def test_a():\n    reload_aw()\n    assert aw.x == 1\n    assert aw.y == 2\nTESTS = [test_a]\nfor t in TESTS:\n    try:\n        t()\n"
            "    except Exception as e:\n        print(f'FAIL {t.__name__}: {type(e).__name__}: {e}')\n")
    assert py_test_rules(ok_t, ["arr-webhook"]) == [], py_test_rules(ok_t, ["arr-webhook"])
    bad = py_test_rules(ok_t.replace("TESTS = [test_a]", "TESTS = [v for k, v in sorted(globals().items()) if k.startswith('test')]"), ["arr-webhook"])
    assert any("globals()" in b for b in bad), bad
    bad = py_test_rules(ok_t + "def test_b():\n    aw = reload_aw()\n    assert aw.x\n    assert r.y\n", ["arr-webhook"])
    assert any("without `global`" in b for b in bad) and any("`r` in test_b" in b for b in bad), bad
    assert any("bare `except:`" in b for b in py_test_rules(ok_t.replace("except Exception as e:", "except:"), ["arr-webhook"]))
    assert any("sys.exit(0)" in b for b in py_test_rules(ok_t + "def test_z():\n    sys.exit(0)\n", ["arr-webhook"]))
    _self_test_source_checks()
    _self_test_contain()
    # G7
    assert tag_result("FAIL bad-spec x") == "FAIL bad-spec x class=SPEC" and tag_result("PASS mode=two-phase") == "PASS mode=two-phase"
    assert fail_class("FAIL mode=two-phase test_unchanged=FAIL test_green=ok TEST-SUSPECT") == "TEST-SUSPECT"
    assert fail_class("FAIL mode=two-phase phase=test test-gate: x") == "TEST" and fail_class("FAIL mode=two-phase test_green=FAIL") == "MODEL"
    assert fail_class("FAIL APIConnectionError: boom") == "INFRA" and fail_class("FAIL step-2 LimitsExceeded") == "SIZE"
    assert fail_class("FAIL model-not-served") == "INFRA", "the endpoint not serving the model is infrastructure, not the model's work"
    assert fail_class("FAIL mode=auto phase=derive mini-spec: target files must all be python or all JS/TS") == "SPEC"
    assert fail_class("FAIL mode=auto phase=derive intake=not-local: x") == "SPEC"
    assert fail_class("FAIL mode=auto phase=derive the diagnosis names 4 functions (a, b, c, d); at most 3") == "SPEC"
    assert fail_class("FAIL mode=auto phase=derive Function: f is not defined in a.py") == "MODEL", "a wrong diagnosis is the model's"
    assert tag_result(tag_result("FAIL x")) == "FAIL x class=MODEL"
    # G3 new_names + G4 no_hacks on a synthetic python module
    base = "import os\n\nCAP = 6\n\n\ndef target(h):\n    return h\n\n\ndef other():\n    return 1\n"
    with _tf.TemporaryDirectory() as d:
        fixed = base.replace("CAP = 6\n", "CAP = 6\nSTATE_PATH = os.environ.get('P', '/x')\n").replace("    return h\n", "    _save()\n    return h.upper()\n") + \
            "\n\ndef _save():\n    pass\n"
        Path(d, "m.py").write_text(fixed)
        sp = {"target_fn": ["target"], "test_file": "test_m.py"}
        ok, det = fn_scope_check(d, sp, ["m.py"], {"m.py": base})
        assert not ok and "module level" in det and "new_names" in det, det
        ok, det = fn_scope_check(d, {**sp, "new_names": ["STATE_PATH"]}, ["m.py"], {"m.py": base})
        assert ok and "new_names 1" in det, det
        ok, det = fn_scope_check(d, {**sp, "fn_scope": "warn"}, ["m.py"], {"m.py": base})
        assert ok and det.startswith("WARN"), det
        Path(d, "m.py").write_text(fixed.replace("def other():\n    return 1", "def other():\n    return 2"))
        assert not fn_scope_check(d, {**sp, "new_names": ["STATE_PATH"]}, ["m.py"], {"m.py": base})[0]
        Path(d, "m.py").write_text(fixed)
        # an EXISTING module-level table the fix edits (bench-cfgkeys 2cdee16a8b71: a key added to DEFAULTS) counts via new_names too
        Path(d, "m.py").write_text(base.replace("CAP = 6\n", "CAP = 7\n"))
        assert not fn_scope_check(d, sp, ["m.py"], {"m.py": base})[0]
        ok, det = fn_scope_check(d, {**sp, "new_names": ["CAP"]}, ["m.py"], {"m.py": base})
        assert ok and "new_names 1" in det, det
        # JS/TS: one-name top-level const/let/var statements (_js_top_spans); a statement whose end is unclear stays outside
        jb = "'use strict';\nconst KNOWN = {\n  small: 1,\n};\n\nfunction sizeOf(n) {\n  return KNOWN[n];\n}\n\nfunction other() {\n  return 1;\n}\n"
        Path(d, "k.js").write_text(jb.replace("  small: 1,\n", "  small: 1,\n  large: 10,\n"))
        jsp = {"target_fn": ["sizeOf"], "test_file": "k.test.js"}
        assert not fn_scope_check(d, jsp, ["k.js"], {"k.js": jb})[0]
        ok, det = fn_scope_check(d, {**jsp, "new_names": ["KNOWN"]}, ["k.js"], {"k.js": jb})
        assert ok and "new_names 1" in det, det
        Path(d, "k.js").write_text(jb.replace("  small: 1,\n", "  small: 1,\n  large: 10,\n").replace("return 1;", "return 2;"))
        assert not fn_scope_check(d, {**jsp, "new_names": ["KNOWN"]}, ["k.js"], {"k.js": jb})[0]
        assert [(n, a, z) for n, a, z, _i in _js_top_spans(jb)] == [({"KNOWN"}, 2, 4)]
        for src, want in (("const X =\n  5;\nlet y = 2\n", [({"X"}, 1, 2), ({"y"}, 3, 3)]), ("const RE = /[(]/;\nfoo(1);\n", [({"RE"}, 1, 1)]),
                          ("const a = 1, b = 2;\n", []), ("const BAD = {\nfunction f() {}\n", []), ("  const inner = 1;\n", []),
                          ("export const T: Record<string, number> = {\n  a: 1,\n}\nfunction g() {}\n", [({"T"}, 1, 3)]),
                          ("const L = [\n  1,\n]\n  .map(x => x);\nconst M = 1\n", [({"L"}, 1, 4), ({"M"}, 5, 5)]),
                          ("const S = 'a{';\nconst U = `x${1}`; // (\n", [({"S"}, 1, 1), ({"U"}, 2, 2)])):
            assert [(n, a, z) for n, a, z, _i in _js_top_spans(src)] == want, (src, _js_top_spans(src))
        Path(d, "m.py").write_text(fixed)
        assert no_hacks_check(d, sp, ["m.py"], {"m.py": base})[0]
        for hack in ("    sys._getframe(1).f_globals['r'] = h\n", "    globals()['r'] = h\n", "    import __main__\n",
                     "    if os.environ.get('PYTEST_CURRENT_TEST'): return 1\n", "    x = 1  # for test compat\n", "    open('test_m.py')\n"):
            Path(d, "m.py").write_text(fixed.replace("    _save()\n", "    _save()\n" + hack))
            assert not no_hacks_check(d, sp, ["m.py"], {"m.py": base})[0], hack
        Path(d, "m.py").write_text(fixed.replace("    _save()\n", "    sys._getframe(1)\n"))
        assert no_hacks_check(d, {**sp, "no_hacks": "off"}, ["m.py"], {"m.py": base}) == (True, "off")
    # G4b lint (2026-11-03 sampled review: unused imports and dead locals passed every gate): only what the fix added counts
    with _tf.TemporaryDirectory() as d:
        if _ruff_bin():
            lb = "import os\nimport json\n\n\ndef target(h):\n    return os.path.join(h, 'x')\n"   # json: unused at baseline already
            Path(d, "m.py").write_text(lb)
            assert lint_check(d, {}, ["m.py"], {"m.py": lb}) == (True, "no new unused imports/variables")
            Path(d, "m.py").write_text(lb.replace("import json\n", "import json\nimport io\n").replace("    return", "    tmp = 1\n    return"))
            ok, det = lint_check(d, {}, ["m.py"], {"m.py": lb})
            assert not ok and "m.py:3 F401 `io`" in det and "F841" in det and "json" not in det, det
            Path(d, "m.py").write_text(lb.replace("os.path.join(h, 'x')", "h"))   # the fix removed the last use of os
            ok, det = lint_check(d, {}, ["m.py"], {"m.py": lb})
            assert not ok and "`os`" in det and "json" not in det, det
            assert lint_check(d, {"lint": "off"}, ["m.py"], {"m.py": lb}) is None
            Path(d, "pkg").mkdir()
            Path(d, "pkg/__init__.py").write_text("from os import path\n")
            assert lint_check(d, {}, ["pkg/__init__.py"], {"pkg/__init__.py": ""})[0], "a package's re-exports are not unused"
            Path(d, "n.py").write_text("import sys\n")
            ok, det = lint_check(d, {}, ["n.py"], {"n.py": None})
            assert not ok and "n.py:1 F401" in det, det   # a new file: every finding is the fix's
            two = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
            Path(d, "r.py").write_text("x = 0\n" + two.replace("def g", "def f"))   # F811 names a line: shifted by the fix, still old
            assert lint_check(d, {}, ["r.py"], {"r.py": two.replace("def g", "def f")})[0]
            assert gate_verdict({"test_green": (True, ""), "test_unchanged": (True, ""), "lint": (False, "x")}) == ("NEEDS-REVIEW", ["lint"])
        else:
            print("[self-test] lint: ruff not found; python lint cases skipped", flush=True)
        Path(d, "c.js").write_text("const x = 1;\n")
        assert lint_check(d, {"typecheck_cmd": "npx tsc --noEmit"}, ["c.js"], {"c.js": ""}) is None, "nothing to lint: no gate"
        assert _tsc_lint_cmd("npx tsc --noEmit") == "npx tsc --noEmit --noUnusedLocals --pretty false"
        assert _tsc_lint_cmd("cd web && npx tsc -p .") == "cd web && npx tsc -p . --noUnusedLocals --pretty false"
        assert not any(_tsc_lint_cmd(c) for c in ("npm run typecheck", "npx tsc -b", "tsc --noEmit --noUnusedLocals", "npx tsc --noEmit | head", "vue-tsc --noEmit", ""))
        Path(d, "src").mkdir()
        Path(d, "src/x.ts").write_text("import { y } from './y';\nexport const a = 1;\n")
        Path(d, "bin").mkdir()
        Path(d, "bin/tsc").write_text("#!/bin/sh\necho \"src/x.ts(1,10): error TS6133: 'y' is declared but its value is never read.\"\n"
                                      "echo \"src/x.ts(2,14): error TS6133: 'a' is declared but its value is never read.\"\n"
                                      "echo \"src/x.ts(1,1): error TS2304: Cannot find name 'q'.\"\nexit 2\n")
        os.chmod(Path(d, "bin/tsc"), 0o755)
        ok, det = lint_check(d, {"typecheck_cmd": f"{d}/bin/tsc --noEmit"}, ["src/x.ts"], {"src/x.ts": "export const a = 1;\n"})
        assert not ok and "src/x.ts:1 TS6133" in det and "src/x.ts:2" not in det and "TS2304" not in det, det   # added lines only
        assert lint_check(d, {"typecheck_cmd": f"{d}/bin/tsc --noEmit"}, ["src/x.ts"], {"src/x.ts": "import { y } from './y';\nexport const a = 1;\n"})[0]
    # a new top-level class the target instantiates is a helper: class line, methods and the import it needs are in scope (docsum-cli
    # 2026-11-03 replay). An unreferenced new class, a class rebinding a baseline name and an edit to a sibling stay outside.
    base = '"""doc"""\nimport os\nMAX = 3\n\n\ndef parse_args(argv):\n    raise NotImplementedError\n\n\ndef other():\n    return 1\n'
    fixed = base.replace("import os\n", "import argparse\nimport os\n").replace(
        "def parse_args(argv):\n    raise NotImplementedError\n",
        "class _P(argparse.ArgumentParser):\n    def error(self, message):\n        raise ValueError('E_USAGE')\n\n    def _code(self):\n"
        "        return _code_of(self)\n\n\ndef _code_of(p):\n    return 'E_USAGE'\n\n\n"
        "def parse_args(argv):\n    p = _P(add_help=False)\n    p.add_argument('pdf')\n    return p.parse_args(argv)\n")
    with _tf.TemporaryDirectory() as d:
        sp = {"target_fn": ["parse_args"], "fn_scope": "enforce"}
        chk = lambda text, b=base: (Path(d, "m.py").write_text(text), fn_scope_check(d, sp, ["m.py"], {"m.py": b}))[1]
        ok, det = chk(fixed)
        assert ok and "outside 0" in det and "_P" in det and "_code_of" in det and "import 1" in det and "error" not in det, det
        ok, det = chk(fixed + "\n\nclass Unused:\n    x = 1\n")
        assert not ok and "`module level`" in det and "`error`" not in det, det
        ok, det = chk(fixed.replace("p = _P(add_help=False)", "p = argparse.ArgumentParser(add_help=False)"))
        assert not ok and "`error`" in det and "`module level`" in det, det
        ok, det = chk(fixed.replace("    return 1\n", "    return 2\n"))
        assert not ok and "`other`" in det, det
        b2 = base.replace("MAX = 3\n", "MAX = 3\n_P = None\n")
        ok, det = chk(fixed.replace("MAX = 3\n", "MAX = 3\n_P = None\n"), b2)
        assert not ok and "`error`" in det, det
        # a caller in another allowed file updating its qualified call (`M.parse_args(`) is a 1-hop neighbour, capped by scope_slack
        # (settings-admin-key-removal 2026-11-03 replay); an unrelated function in that file stays outside
        Path(d, "m.py").write_text(fixed)
        srv = "import m as M\n\n\ndef do_post(argv):\n    return M.parse_args(argv)\n\n\ndef ping():\n    return 1\n"
        Path(d, "srv.py").write_text(srv.replace("M.parse_args(argv)", "M.parse_args(list(argv))"))
        two = lambda extra=None: fn_scope_check(d, {**sp, **(extra or {})}, ["m.py", "srv.py"], {"m.py": base, "srv.py": srv})
        ok, det = two()
        assert ok and "neighbor 1" in det, det
        ok, det = two({"scope_slack": 0})
        assert not ok and "`do_post`" in det and "scope_slack 0" in det, det
        Path(d, "srv.py").write_text(srv.replace("    return 1\n", "    return 2\n"))
        ok, det = two()
        assert not ok and "`ping`" in det, det
    assert any("fn_scope_reason" in e for e in precheck({"fn_scope": "warn", "allowed": []})[0])
    assert any("red_ok" in e for e in precheck({"red_ok": [{"type": "NameError", "match": "x"}], "allowed": []})[0])
    assert not any("fn_scope" in e or "red_ok" in e or "new_names" in e for e in
                   precheck({"fn_scope": "warn", "fn_scope_reason": "r", "new_names": ["A"], "red_ok": [{"type": "KeyError", "match": "a"}], "allowed": []})[0])
    # must-reject replays: the u2c (frame-hack fix, globals() discovery) and u2d (UnboundLocalError/NoneType test) runs
    fx = GATES_V2_FIXTURES
    if not fx.is_dir():
        print("[self-test] gates-v2 fixtures absent: replays skipped")
        return
    n = 0
    u2d = fx / "u2d"
    if (u2d / "baseline-out.txt").is_file() and (u2d / "test_auto_rescue.py").is_file():
        txt = (u2d / "test_auto_rescue.py").read_text()
        per = parse_test_results((u2d / "baseline-out.txt").read_text())
        bad = per_test_red_problems(per, {}, txt, "test_auto_rescue.py")
        assert any("UnboundLocalError" in b for b in bad) and any("NoneType" in b for b in bad), bad
        rules = py_test_rules(txt, ["arr-webhook"])
        assert any("module alias" in b for b in rules), rules
        n += 1
    u2c = fx / "u2c"
    if (u2c / "test_auto_rescue.py").is_file():
        rules = py_test_rules((u2c / "test_auto_rescue.py").read_text(), ["arr-webhook"])
        assert any("globals()" in b for b in rules), rules
        n += 1
    if (u2c / "arr-webhook.fixed.py").is_file() and (fx / "arr-webhook.base.py").is_file():
        with _tf.TemporaryDirectory() as d:
            shutil.copy2(u2c / "arr-webhook.fixed.py", Path(d, "arr-webhook.py"))
            base_t = {"arr-webhook.py": (fx / "arr-webhook.base.py").read_text()}
            sp = {"target_fn": ["_auto_rescue"], "test_file": "test_auto_rescue.py",
                  "new_names": ["RESCUE_STATE_PATH", "_load_rescue_state", "_save_rescue_state", "_rescue_attempts", "_rescue_gave_up"]}
            ok, det = no_hacks_check(d, sp, ["arr-webhook.py"], base_t)
            assert not ok and "frame introspection" in det, det
            ok, det = fn_scope_check(d, sp, ["arr-webhook.py"], base_t)
            assert not ok, det
        n += 1
    print(f"[self-test] gates-v2 ok ({n} fixture replay(s))")


def _self_test_ground():
    """ungrounded_expectations (phase A's grounding gate): bench-notset's invented key and its grounded twin, a name bound to an
    expected literal (and the same name used as an input), prose that never grounds or claims anything, unittest asserts, JS
    expected values (`.not` and messages aside), requirement_text dropping `## Diagnosis`."""
    with tempfile.TemporaryDirectory() as d:
        run_git(d, "init", "-q", check=True)
        Path(d, "acct.py").write_text("NOT_SET = ['rate', 'tier']\n\n\ndef not_set(cfg):\n    return [k for k in NOT_SET if k not in cfg]\n")
        Path(d, "price.js").write_text("function total(q) { return q > 10 ? 'bulk' : 'unit'; }\nmodule.exports = { total };\n")
        run_git(d, "add", "-A", check=True)
        task = ("## Problem\n\n`not_set` must also report `uec_rate` when it is missing.\n\n"
                "## Diagnosis\n\nAppend \"metered_days\" (or similar) as well.\n")
        ug = lambda text, tf="test_acct.py", t=task: [x for x, _s, _n in ungrounded_expectations(text, tf, t, d)]
        head = "from acct import not_set\n\n\n"
        bad = head + "def test_missing():\n    assert not_set({}) == ['rate', 'tier', 'uec_rate', 'metered_days']\n"
        assert ug(bad) == ["metered_days"], ug(bad)
        assert ug(bad.replace(", 'metered_days'", "")) == []
        assert ug(bad, t=task.replace("## Diagnosis", "## Notes")) == []  # the same words outside a Diagnosis section do ground it
        bound = head + "EXPECTED = ['rate', 'zebra_mode']\n\n\ndef test_b():\n    assert not_set({}) == EXPECTED\n"
        assert ug(bound) == ["zebra_mode"], ug(bound)
        assert ug(bound + "\n\ndef test_c():\n    cfg = dict.fromkeys(EXPECTED)\n    assert not_set(cfg) == []\n") == []  # also an input
        prose = (head + 'def test_p():\n    """checks quux_flag handling"""\n    print("frob_value")\n'
                 '    assert not_set({}) == ["rate", "tier"], "expected glorp_key too"\n')
        assert ug(prose) == [], ug(prose)
        exc = (head + "def test_z():\n    try:\n        not_set(None)\n    except TypeError as e:\n        assert str(e) == 'division by zero'\n"
               "    assert not_set({}) != ['a1b2c3d4e5f6', 'nowhere_key']\n    h = 'a1b2c3d4e5f6'\n    assert h == 'a1b2c3d4e5f6'\n")
        assert ug(exc) == [], ug(exc)  # prose words, a hash, and `!=` (not a claim) never need a source
        ut = ("import unittest\nfrom acct import not_set\n\n\nclass T(unittest.TestCase):\n    def test_u(self):\n"
              "        self.assertEqual(not_set({}), ['rate', 'ghost_key'], msg='ghost_msg_key')\n")
        assert ug(ut) == ["ghost_key"], ug(ut)
        js = ("const assert = require('node:assert');\nconst { test } = require('node:test');\nconst { total } = require('./price');\n"
              "test('bulk totals', () => {\n  assert.strictEqual(total(11), 'bulk');\n  assert.deepStrictEqual(total(12), { kind: 'bulkish' });\n"
              "  expect(total(1)).not.toBe('nonsense_value');\n  assert.strictEqual(total(2), 'unit', 'wanted zorch_msg');\n});\n")
        jt = "## Problem\n\n`total` must return 'bulk' from 11 items.\n"
        assert sorted(ug(js, "test-price.js", jt)) == ["bulkish", "kind"], ug(js, "test-price.js", jt)
        assert ug(js.replace("'bulkish'", "'bulk'").replace("{ kind: 'bulk' }", "'bulk'"), "test-price.js", jt) == []
        assert ug(bad, "notes.txt") == []  # no test language: nothing to check
        assert "found in neither TASK.md's Problem" in ground_problem([("metered_days", "metered_days", 5)])
    assert requirement_text("## Problem\nA\n\n## Diagnosis\nB\n\n## Scope\nC\n") == "## Problem\nA\n\n## Scope\nC\n"


def _self_test_user_view():
    """user_view_gap (phase A, stage 21): loguru r4's shape (every test calls internal code; TASK.md's example goes through the
    public API) is flagged; a reference to that API outside GUARDS clears it (a local from-import, an alias, the package's
    attribute, a lazy loader); nothing is asked without such an example, for a name the fix is to add, for code only in the
    Diagnosis, or for a non-python test. test_gate: hard rejects before the run, soft records, off and both kill switches skip
    it; the skeleton's first placeholder; the REVIEW.md and REVIEW-AUTO.md sections."""
    prev = os.environ.pop("MINI_USER_VIEW", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "logx").mkdir()
            (root / "logx/__init__.py").write_text("from logx._handler import Handler\n\nlogger = Handler()\n")
            handler = ("class Handler:\n    def __init__(self):\n        self.sinks = []\n\n    def add(self, sink, catch=True):\n"
                       "        self.sinks.append((sink, catch))\n\n    def info(self, msg, **kw):\n        for sink, _c in self.sinks:\n"
                       "            sink(msg.format(**kw))\n")
            (root / "logx/_handler.py").write_text(handler)
            task = ("## Problem\n\nWith catch=False a missing key raises a bare KeyError:\n\n```python\nfrom logx import logger\n\n"
                    "logger.add(print, catch=False)\nlogger.info(\"{x}\", y=1)\n```\n\n## Diagnosis\n\n`Handler.info` formats it unguarded.\n")
            (root / "TASK.md").write_text(task)
            tg = ["logx/_handler.py"]
            ent = example_entry_points(task, root, tg)
            assert ent == [("from logx import logger", ["logger.add", "logger.info"], {"logx.logger"})], ent
            load = "def _load_logx__handler():\n    import logx._handler\n    return logx._handler\n"
            internal = (load + "\n\ndef test_a():\n    h = _load_logx__handler().Handler()\n    out = []\n    h.add(out.append)\n"
                        "    h.info(\"{x}\", x=1)\n    assert out == [\"1\"]\n\n\ndef test_b():\n    h = _load_logx__handler().Handler()\n"
                        "    out = []\n    h.add(out.append)\n    h.info(\"{x}{x}\", x=2)\n    assert out == [\"22\"]\n")
            pub = ("\n\ndef test_c():\n    from logx import logger\n    out = []\n    logger.add(out.append, catch=False)\n"
                   "    logger.info(\"{x}\", x=3)\n    assert out == [\"3\"]\n")

            def gap(text, tf="test_logx.py", t=task):
                return user_view_gap(text, tf, t, root, tg)

            assert gap(internal) == [("from logx import logger", ["logger.add", "logger.info"])], gap(internal)
            assert gap(internal + pub) == []
            assert gap(internal + pub.replace("import logger", "import logger as lg").replace("logger.", "lg.")) == []
            assert gap(internal + pub.replace("from logx import logger", "import logx").replace("logger.", "logx.logger.")) == []
            lazy = "\n\ndef _load_logx():\n    import logx\n    return logx\n" + pub.replace("from logx import logger", "logger = _load_logx().logger")
            assert gap(internal + lazy) == []
            assert gap(internal + pub + "\n\nGUARDS = [test_c]\n") != []  # the public call only in a guard: no test of the bug uses it
            assert gap(internal + pub + "\n\nGUARDS = [test_a]\n") == []
            pkg_only = internal + "\n\ndef test_d():\n    import logx\n    assert logx._handler.Handler().sinks == []\n"
            assert gap(pkg_only) != []  # the package's internals are not the example's API
            # nothing to ask: no example import from the package, a name the fix is to add, code only in the Diagnosis, a JS test
            assert gap(internal, t="## Problem\n\n`logger.info('{x}')` raises KeyError.\n") == []
            assert gap(internal, t=task.replace("logger", "brand_new_logger")) == []
            assert gap(internal, t="## Problem\n\nIt raises.\n\n## Diagnosis\n\n```python\nfrom logx import logger\nlogger.info('{x}')\n```\n") == []
            assert gap(internal, tf="test_logx.js") == []
            # a backtick span; a doctest that imports the module: the name the example calls on it is what a test must reach
            assert gap(internal, t="## Problem\n\n`from logx import logger; logger.info('{x}')` raises.\n") != []
            doc = "## Problem\n\n```\n>>> import logx\n>>> logx.logger.info('{x}', y=1)\nTraceback (most recent call last):\nKeyError: 'x'\n```\n"
            assert example_entry_points(doc, root, tg) == [("import logx", ["logx.logger.info"], {"logx.logger"})], example_entry_points(doc, root, tg)
            assert gap(internal + pub, t=doc) == [] and gap(pkg_only, t=doc) == [("import logx", ["logx.logger.info"])], gap(pkg_only, t=doc)
            # test_gate: hard rejects before the run, soft records and runs, off and both kill switches skip it
            spec = {"test_file": "test_logx.py", "test_cmd": "exit 3", "target_fn": "info"}
            (root / "test_logx.py").write_text(internal)
            bad, ev = test_gate(root, dict(spec), tg, ground="hard")
            assert len(bad) == 1 and "public API" in bad[0] and "`from logx import logger`: `logger.add`, `logger.info`" in bad[0], (bad, ev)
            assert ev.get("user_view") == ["`from logx import logger`: `logger.add`, `logger.info`"] and "baseline_rc" not in ev, ev
            bad, ev = test_gate(root, dict(spec), tg, ground="soft")
            assert not any("public API" in b for b in bad) and ev.get("user_view") and "baseline_rc" in ev, (bad, ev)
            for sp, env, gr in ((dict(spec, user_view=False), None, "hard"), (dict(spec), "off", "hard"), (dict(spec), None, "off")):
                if env:
                    os.environ["MINI_USER_VIEW"] = env
                bad, ev = test_gate(root, sp, tg, ground=gr)
                os.environ.pop("MINI_USER_VIEW", None)
                assert not any("public API" in b for b in bad) and "user_view" not in ev, (sp, env, gr, bad, ev)
            (root / "test_logx.py").write_text(internal + pub)
            bad, ev = test_gate(root, dict(spec), tg, ground="hard")
            assert not any("public API" in b for b in bad) and "user_view" not in ev, (bad, ev)
            # the skeleton's first placeholder asks for the example through the public API; the others are unchanged
            ssp = {"test_file": "test_new.py", "test_cmd": "python3 -m pytest -q test_new.py", "target_fn": "info"}
            first, rest = skeleton(root, ssp, tg).split("def test_todo_case_2")
            assert "public API" in first and "`from logx import logger`" in first and "`logger.add`" in first, first
            assert "what the user sees" in first and "public API" not in rest and "Call `info` with concrete inputs" in rest, rest
            # the review packets: REVIEW.md records the soft pass, REVIEW-AUTO.md recomputes it on the frozen test
            write_review(root / "REVIEW.md", spec, tg, {"user_view": ["`from logx import logger`: `logger.add`"]}, {}, internal, "",
                         "", "PASS")
            assert "## No test goes through the public API" in (root / "REVIEW.md").read_text()
            run = root / "run"
            run.mkdir()
            fsp = {"test_file": "test_logx.py", "allowed": tg}
            auto_review(run, root, "uv", task, "PASS", fspec=fsp, base_text=handler)
            assert "public API the Problem's example uses" not in (run / "REVIEW-AUTO.md").read_text()
            (root / "test_logx.py").write_text(internal)
            auto_review(run, root, "uv", task, "PASS", fspec=fsp, base_text=handler)
            ra = (run / "REVIEW-AUTO.md").read_text()
            assert "## The test never goes through the public API the Problem's example uses" in ra and "`from logx import logger`" in ra, ra
            (root / "TASK.md").write_text("## Problem\n\n`info` raises KeyError on a missing key.\n")
            assert "public API" not in skeleton(root, ssp, tg)
    finally:
        os.environ.pop("MINI_USER_VIEW", None) if prev is None else os.environ.__setitem__("MINI_USER_VIEW", prev)


def _self_test_style():
    """style_pass (2026-11-05): the project's own ruff rules on what the model wrote, only where the project already follows them,
    put back when the re-run test says no; and the pins a remote job's setup installs."""
    global LANE_TOOLS
    keep = LANE_TOOLS
    try:
        LANE_TOOLS = "ruff==0.16.3, -e evil black==24.1.0 ruff>=1"
        assert _lane_tools_lock() == b"ruff==0.16.3\nblack==24.1.0\n", _lane_tools_lock()
        for off in ("", "off"):
            LANE_TOOLS = off
            assert _lane_tools_lock() is None
    finally:
        LANE_TOOLS = keep
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        assert style_pass(d, {}, ["gone.py", "x.js"], {}, lambda: (True, "")) is None, "nothing to restyle: no ruff needed"
    # stray_blanks (conan2 r2): a blank line added or dropped far from the fix goes back; one near the fix, and any blank
    # line inside a string, stays
    far = "import sys\nimport os\n\n\ndef g():\n    x=1\n    y=2\n\n    z=3\n    w=4\n    v=5\n    u=6\n    return x\n"
    fixed = far.replace("    return x\n", "\n    return x+y\n")
    stray = fixed.replace("    x=1\n", "    x=1\n\n").replace("\n\n    z=3", "\n    z=3")   # one blank added, one dropped
    assert stray_blanks(far, stray) == (fixed, 2), stray_blanks(far, stray)
    assert stray_blanks(far, fixed) == (fixed, 0), "the blank line next to the fix is its layout"
    near = fixed.replace("    v=5\n", "    v=5\n\n")   # one unchanged line from the fix: not stray
    assert stray_blanks(far, near) == (near, 0), stray_blanks(far, near)
    assert stray_blanks(far, far.replace("    y=2\n", "    y=2\n\n")) == (far, 1), "a file with no other change goes back whole"
    sbase = 'def h():\n    s = """a\nb"""\n    p = 1\n    q = 2\n    r = 3\n    t = 4\n    return s\n'
    sdata = sbase.replace('"""a\n', '"""a\n\n').replace("return s\n", "return s + p\n")
    assert stray_blanks(sbase, sdata) == (sdata, 0), "a blank line inside a string is data: the AST check keeps it"
    broken = stray.replace("x+y\n", "x+y)\n")
    assert stray_blanks(far, broken) == (broken, 0), "no parse, no change"
    g_ = globals()
    keep_rb = g_["_ruff_bin"]
    try:
        g_["_ruff_bin"] = lambda: None
        with _tf.TemporaryDirectory() as d:
            Path(d, "far.py").write_text(stray)
            det = style_pass(d, {}, ["far.py"], {"far.py": far}, lambda: (True, "checked"))
            assert det == "far.py (2 stray blank-line changes put back); checked (ruff not found: its rules skipped)", det
            assert Path(d, "far.py").read_text() == fixed
            Path(d, "far.py").write_text(stray)
            det = style_pass(d, {}, ["far.py"], {"far.py": far}, lambda: (False, "red"))
            assert det.startswith("reverted, red: far.py") and Path(d, "far.py").read_text() == stray, det
            Path(d, "far.py").write_text(fixed)
            assert style_pass(d, {}, ["far.py"], {"far.py": far}, lambda: (True, "")) == "skipped: ruff not found"
            Path(d, "t.py").write_text(stray)
            assert style_pass(d, {}, ["t.py"], {}, lambda: (True, "")) == "skipped: ruff not found", "a new file has no baseline"
    finally:
        g_["_ruff_bin"] = keep_rb
    if not _ruff_bin():
        print("[self-test] style: ruff not found; style cases skipped", flush=True)
        return
    ok = lambda: (True, "checked")   # noqa: E731
    with _tf.TemporaryDirectory() as d:
        Path(d, "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
        for sub_ in ("src", "tests", "legacy", "solo"):
            Path(d, sub_).mkdir()
        base = "import os\nimport sys\n\n\ndef f(a, b):\n    return os.path.join(a, b) + sys.prefix\n"
        Path(d, "src/m.py").write_text(base)
        Path(d, "tests/test_a.py").write_text('from src.m import f\n\n\ndef test_a():\n    assert f("a", "b")\n')
        legacy = "import sys\nimport os\n\n\ndef g():\n    x=1\n"   # follows none of the rules: the lane must not restyle it
        Path(d, "legacy/old.py").write_text(legacy)
        Path(d, "legacy/test_old.py").write_text(legacy)
        for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
            subprocess.run(["git", *c], cwd=d, capture_output=True, timeout=60)
        # a fix with an import out of order and unformatted lines: both rules, the baseline follows them
        fix = "import os\nimport sys\nimport json\n\n\ndef f(a,b):\n    return os.path.join(a,b) + sys.prefix + json.dumps(1)\n"
        Path(d, "src/m.py").write_text(fix)
        det = style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, ok)
        got = Path(d, "src/m.py").read_text()
        assert det == "src/m.py (import order, ruff format); checked", det
        assert got.startswith("import json\nimport os\nimport sys\n") and "def f(a, b):" in got and "json.dumps(1)" in got, got
        assert style_pass(d, {}, ["src/m.py"], {"src/m.py": got}, ok) is None, "unchanged since the baseline: left alone"
        assert style_pass(d, {"style": "off"}, ["src/m.py"], {"src/m.py": base}, ok) is None
        Path(d, "src/m.py").write_text(fix)
        det = style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, lambda: (False, "red"))
        assert det.startswith("reverted, red: src/m.py") and Path(d, "src/m.py").read_text() == fix, det
        Path(d, "src/m.py").write_text(fix.replace("import json\n", "import json, re\n").replace("+ sys.prefix", "+ sys.prefix + re.escape(a)"))
        det = style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, ok)
        assert det and "F841" not in det and "unused" not in det, det
        Path(d, "src/m.py").write_text("def f(:\n")
        assert style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, ok) is None, "ruff cannot judge a syntax error: left alone"
        Path(d, "src/m.py").write_text(fix.replace("    return", "    unused = 1\n    return"))
        det = style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, ok)
        assert "unused = 1" in Path(d, "src/m.py").read_text() and "unused locals" not in det, "F841 only on the test file"
        # a baseline that follows neither rule: nothing applies
        Path(d, "legacy/old.py").write_text(legacy + "y=2\n")
        assert style_pass(d, {}, ["legacy/old.py"], {"legacy/old.py": legacy}, ok) is None
        assert Path(d, "legacy/old.py").read_text() == legacy + "y=2\n"
        # ... but a stray blank line still goes back there: that is the project's own text, not a rule
        Path(d, "legacy/far.py").write_text(stray)
        det = style_pass(d, {}, ["legacy/far.py"], {"legacy/far.py": far}, ok)
        assert det == "legacy/far.py (2 stray blank-line changes put back); checked", det
        assert Path(d, "legacy/far.py").read_text() == fixed
        # a new test file: its tracked siblings decide; F841 drops the name and keeps the call
        t = "import os\nfrom src.m import f\nimport sys\n\n\ndef test_b():\n    r = f('a','b')\n    assert sys and os\n"
        Path(d, "tests/test_b.py").write_text(t)
        det = style_pass(d, {}, ["tests/test_b.py"], {}, ok, test_file="tests/test_b.py")
        got = Path(d, "tests/test_b.py").read_text()
        assert det == "tests/test_b.py (unused locals, import order, ruff format); checked", det
        assert '    f("a", "b")\n' in got and got.index("import os") < got.index("import sys") < got.index("from src.m"), got
        Path(d, "legacy/test_new.py").write_text(t)
        assert style_pass(d, {}, ["legacy/test_new.py"], {}, ok, test_file="legacy/test_new.py") is None, "its siblings do not follow the rules"
        Path(d, "solo/test_c.py").write_text(t)
        assert style_pass(d, {}, ["solo/test_c.py"], {}, ok, test_file="solo/test_c.py") is None, "no sibling: unknown style, left alone"
        Path(d, "tests/test_crlf.py").write_bytes(t.replace("\n", "\r\n").encode())
        assert style_pass(d, {}, ["tests/test_crlf.py"], {}, ok, test_file="tests/test_crlf.py") is None, "CRLF: left alone"
        # the project's own config: an excluded directory stays as it is
        Path(d, "pyproject.toml").write_text('[tool.ruff]\nline-length = 100\nextend-exclude = ["src"]\n')
        Path(d, "src/m.py").write_text(fix)
        assert style_pass(d, {}, ["src/m.py"], {"src/m.py": base}, ok) is None and Path(d, "src/m.py").read_text() == fix
    os.environ["MINI_STYLE"] = "off"
    try:
        assert style_pass(".", {}, ["mini-swe-runner.py"], {}, ok) is None
    finally:
        os.environ.pop("MINI_STYLE", None)


def _self_test_bg_baseline():
    """Background baselines: _sibling, bg_reason (when they must run in the work dir itself), the copy Baselines runs them on
    (.venv/node_modules linked, __pycache__ left out, made before any edit, its paths put back in the output, removed after),
    stop() twice, and apply_baselines' switch to suite_narrow when the whole suite times out."""
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as d:
        w = Path(d, "work")
        w.mkdir()
        assert _sibling(w) == Path(d, "wor0")
        Path(d, "wor0").mkdir()
        assert _sibling(w) == Path(d, "wor1")
        Path(d, "wor0").rmdir()
        plain = {"suite_cmd": "python -m pytest -q", "test_file": "t.py"}
        mc = os.environ.pop("MINI_CONTAIN", None)
        try:
            for spec, kw, frag in ((plain, {"check_only": True}, "a model-free check"), (plain, {"ref_mode": "replay"}, "a model-free check"),
                                   ({**plain, "phase_a_only": True}, {}, "the runs are contained"),
                                   ({"test_file": "t.py"}, {}, "no suite or typecheck command"),
                                   ({**plain, "bg_baseline": False}, {}, "bg_baseline is false"),
                                   ({**plain, "suite_cmd": f"cd {w} && pytest"}, {}, "absolute path"),
                                   ({**plain, "env": {"PYTHONPATH": f"{w}/src"}}, {}, "absolute path"),
                                   ({**plain, "typecheck_cmd": "tsc -p ../work/tsconfig.json"}, {}, "names ../work")):
                got = bg_reason(w, spec, **kw)
                assert got and frag in got, (spec, kw, got)
            assert bg_reason(w, plain) is None and bg_reason(w, {**plain, "suite_cmd": "pytest ../workers/t.py"}) is None
            assert bg_reason(w, {**plain, "suite_cmd": f"pytest {w}x/t.py"}) is None, "a longer name is not the work dir"
            Path(w, ".git").write_text("gitdir: /elsewhere\n")
            assert "git worktree" in (bg_reason(w, plain) or "")
            Path(w, ".git").unlink()
            sp = Path(w, ".venv/lib/python3.12/site-packages")
            sp.mkdir(parents=True)
            Path(sp, "__editable__.pkg-1.0.pth").write_text(f"{w}/src\n")
            assert "editable install in .venv (__editable__.pkg-1.0.pth)" in (bg_reason(w, plain) or "")
            Path(sp, "__editable__.pkg-1.0.pth").write_text("/opt/other/src\n")
            assert bg_reason(w, plain) is None
            Path(w, "node_modules/@s").mkdir(parents=True)
            Path(w, "pkgs/a").mkdir(parents=True)
            Path(w, "node_modules/@s/a").symlink_to(Path(w, "pkgs/a"))
            assert "node_modules/@s/a links into the work dir" in (bg_reason(w, plain) or "")
            Path(w, "node_modules/@s/a").unlink()
            Path(w, "node_modules/real").mkdir()
            Path(w, "node_modules/@s/a").symlink_to(Path(w, "node_modules/real"))
            assert bg_reason(w, plain) is None, "a link inside node_modules is copied as a link"
        finally:
            if mc is not None:
                os.environ["MINI_CONTAIN"] = mc
        # the copy: made at start(), before the work dir changes; the output names the work dir, never the copy
        Path(w, "m.py").write_text("A = 1\n")
        Path(w, "__pycache__").mkdir()
        Path(w, "__pycache__/m.cpython-312.pyc").write_bytes(b"x")
        line = 'echo "FAILED $PWD/t.py::x"'
        cmd = f"sleep 1; test -L .venv && test -L node_modules && test ! -e __pycache__ && grep -q 'A = 1' m.py && {line}; exit 1"
        b = Baselines(w, {"suite_cmd": cmd, "cmd_timeout": 30}, "t.py")
        assert b.start() and b.copy == Path(d, "wor0") and b.copy.is_dir() and b.copy in _SCRATCH_DIRS, b.err
        Path(w, "m.py").write_text("A = 2\n")
        res, _waited = b.join()
        want = fail_lines(1, f"FAILED {os.path.realpath(w)}/t.py::x\n")
        assert res is not None and res["suite_cmd"][0] == 1 and res["suite_cmd"][1] == want, (res, want)
        assert not Path(d, "wor0").exists() and b.copy not in _SCRATCH_DIRS
        # stop() kills the running command and removes the copy; a second stop() is a no-op; join() after stop() -> None
        pidf = Path(d, "suite.pid")
        b = Baselines(w, {"suite_cmd": f"echo $$ > {pidf}; sleep 30", "cmd_timeout": 60}, "t.py")
        assert b.start()
        t0 = time.time()
        while not (pidf.is_file() and pidf.read_text().strip()) and time.time() - t0 < 20:
            time.sleep(0.1)
        spid = int(pidf.read_text())
        t1 = time.time()
        b.stop()
        b.stop()
        assert time.time() - t1 < 8 and not b.copy.exists() and b.copy not in _SCRATCH_DIRS and not b.running()
        assert b.join()[0] is None
        try:
            os.kill(spid, 0)
            alive = True
        except OSError:
            alive = False
        assert not alive, "stop() left the suite running"
        # a whole suite that times out at baseline is replaced by suite_narrow when that one finishes; else it stays
        sp2 = {"suite_cmd": "sleep 5", "cmd_timeout": 1, "suite_narrow": "true -- t1.py t2.py"}
        res = run_baselines(w, sp2)
        assert res["suite_cmd"][0] == 124 and res["_narrow"][0] == 0, res
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bs, bt = apply_baselines(sp2, res)
        o = buf.getvalue()
        assert ("baseline suite_cmd (whole suite) timed out after 2s: the suite gate runs the 2 existing test file(s) nearest the change "
                "instead: `true -- t1.py t2.py`") in o and "baseline suite_cmd: rc=0 failure lines=0 (0s)" in o, o
        assert sp2["_suite_full"] == "sleep 5" and sp2["suite_cmd"] == "true -- t1.py t2.py" and sp2["_suite_rc"] == 0 and bs == bt == set(), sp2
        sp3 = {"suite_cmd": "sleep 5", "cmd_timeout": 1, "suite_narrow": "sleep 5; true t1.py"}
        res = run_baselines(w, sp3)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bs, _ = apply_baselines(sp3, res)
        o = buf.getvalue()
        assert "WARNING: the baseline suite timed out after 2s and suite_narrow timed out too (2s): the suite gate keeps the whole suite" in o, o
        assert "baseline suite_cmd: rc=124 failure lines=1 (2s)" in o and sp3["suite_cmd"] == "sleep 5" and "_suite_full" not in sp3, (o, sp3)
        assert sp3["_suite_rc"] == 124 and bs == {"rc=124"}, sp3
        sp4 = {"suite_cmd": "true", "suite_narrow": "true t1.py"}  # the suite did not time out: no narrow run
        assert "_narrow" not in run_baselines(w, sp4)


def _self_test_suite_pure():
    """The slow-suite prompt hint, suite_gates' rows (slow + red skip, WEAK, narrowed, typecheck), _py_tests, _pytest_rows."""
    sp = {"test_file": "t.py", "test_cmd": "python -m pytest -q t.py", "suite_cmd": "python -m pytest -q"}
    slow = phase_b_prompt(sp, ["a.py"], "x", suite_hint=(200, True))
    assert "(`python -m pytest -q`, which took 200s at baseline): do NOT run the whole suite yourself" in slow, slow[:900]
    assert "which has run 130s so far at baseline" in phase_b_prompt(sp, ["a.py"], "x", suite_hint=(130, False))
    fast = phase_b_prompt(sp, ["a.py"], "x", suite_hint=(10, True))
    assert "do NOT run the whole suite" not in fast and "The repo suite must not gain new failures: `python -m pytest -q`" in fast
    assert "do NOT run the whole suite" not in phase_b_prompt(sp, ["a.py"], "x")
    st = phase_b_prompt({**sp, "update_tests": ["tests/test_a.py"], "update_tests_strict": True}, ["a.py"], "x")
    assert "Existing tests in tests/test_a.py call the code you change" in st and "(at most 3 functions)" in st and "Keep every test case" not in st
    with tempfile.TemporaryDirectory() as d:
        red = "echo 'FAILED t.py::a'; exit 1"
        skip = "not run: the test is not green, and the suite took 130s at baseline (it runs on a green attempt)"
        for g0, secs, base, want in (({"test_green": (False, "")}, 130, set(), (True, skip)),
                                     ({"test_green": (True, ""), "test_unchanged": (False, "")}, 130, set(), (True, skip)),
                                     ({"test_green": (True, ""), "test_unchanged": (True, "")}, 130, set(), (False, "rc=1 new failures: ['FAILED t.py::a']")),
                                     ({"test_green": (False, "")}, 5, set(), (False, "rc=1 new failures: ['FAILED t.py::a']")),
                                     ({}, None, {"FAILED t.py::a"}, (True, "rc=1, no new failures (WEAK: baseline already had 1 red line(s))"))):
            g = dict(g0)
            suite_gates(g, d, {"suite_cmd": red, **({"_suite_secs": secs} if secs is not None else {})}, base, set())
            assert g["suite"] == want, (g0, secs, g)
        g = {}
        suite_gates(g, d, {"suite_cmd": "true", "_suite_full": "sleep 999", "_suite_secs": 3}, set(), set())
        assert g == {"suite": (True, "rc=0, no new failures (narrowed: the whole suite timed out at baseline; this runs the tests nearest the change)")}, g
        g = {"test_green": (False, "")}
        suite_gates(g, d, {"typecheck_cmd": "echo 'a.ts(1,2): error TS2322: x'; exit 2", "_suite_secs": 500}, set(), set())
        assert g == {"test_green": (False, ""), "typecheck": (False, "rc=2 new failures: ['a.ts(1,2): error TS2322: x']")}, g
    src = ("import pytest\nfrom m import label\n\nX = 1\n\n\ndef helper():\n    return 2\n\n\ndef test_a():\n    assert label(1) == '1'\n"
           "    assert label(2) == '2'\n\n\n@pytest.mark.parametrize('v', [1, 2])\ndef test_p(v):\n    with pytest.raises(TypeError):\n"
           "        label(None)\n\n\nclass TestK:\n    y = 3\n\n    def test_g(self):\n        self.assertEqual(label(1), '1')\n")
    pt = _py_tests(src)
    assert pt is not None and sorted(pt[0]) == ["TestK::test_g", "test_a", "test_p"], pt
    assert [pt[0][k][1] for k in ("test_a", "test_p", "TestK::test_g")] == [2, 1, 1], pt[0]
    assert pt[0]["test_p"][2].startswith("@pytest.mark.parametrize") and pt[0]["test_a"][2].endswith("assert label(2) == '2'\n"), pt[0]
    assert len(pt[1]) == 5 and _py_tests("def test_a(:\n") is None, pt[1]
    out = ("PASSED tests/test_m.py::test_a\nFAILED tests/test_m.py::test_p[1] - TypeError\nPASSED tests/test_m.py::test_p[2]\n"
           "ERROR tests/test_m.py::TestK::test_g - x\nPASSED other/test_m.py::test_b\n")
    assert _pytest_rows(out, "tests/test_m.py", "test_p") == {"FAILED", "PASSED"}
    assert _pytest_rows(out, "tests/test_m.py", "TestK::test_g") == {"ERROR"} and _pytest_rows(out, "tests/test_m.py", "test_b") == set()
    assert _pytest_rows("PASSED pkg/tests/test_m.py::test_a\n", "tests/test_m.py", "test_a") == {"PASSED"}
    assert _pytest_rows("PASSED test_m.py::test_a\n", "tests/test_m.py", "test_a") == {"PASSED"}
    assert _pytest_rows("PASSED xtest_m.py::test_a\n", "test_m.py", "test_a") == set() and _pytest_rows("", "t.py", "test_a") == set()


def _self_test_update_flip():
    """update_flip_check (spec update_tests_strict): an edited existing test must fail on the fix without the edit and pass with it;
    only test functions may change, at most UPD_EDIT_MAX of them, each naming the target and keeping its assertions. Needs pytest."""
    NONE0 = "def test_none():\n    with pytest.raises(TypeError):\n        label(None)\n"
    NONE1 = "def test_none():\n    assert label(None) == ''\n"
    T0 = ("import pytest\nfrom m import label\n\n\n" + NONE0 + "\n\ndef test_val():\n    assert label(1) == '1'\n\n\n"
          "def test_val2():\n    assert label(2) == '2'\n\n\ndef test_val3():\n    assert label(3) == '3'\n\n\n"
          "class TestK:\n    def test_g(self):\n        assert label(4) == '4'\n")
    T1 = T0.replace(NONE0, NONE1)
    py = f"{shlex.quote(sys.executable)} -m pytest -q"
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        (w / "tests").mkdir()
        (w / "m.py").write_text("def label(x):\n    if x is None:\n        return ''\n    return str(x)\n")  # the fix: None -> '' (was TypeError)
        (w / "pytest.ini").write_text("[pytest]\n")
        tp = w / "tests/test_m.py"
        spec = {"update_tests": ["tests/test_m.py"], "update_tests_strict": True, "_upd_base": {"tests/test_m.py": T0},
                "target_fn": "label", "suite_cmd": py, "cmd_timeout": 60}

        def flip(text, **kw):
            tp.write_text(text)
            return update_flip_check(w, {**spec, **kw})
        assert flip(T0) is None
        four = T0
        for i in "1234":
            four = four.replace(f" == '{i}'", f" == str({i})")
        for name, text, kw, want_ok, frag in (
                ("tests added", T0 + "\n\ndef test_new():\n    assert label(5) == '5'\n", {}, True, "tests/test_m.py: tests added, no existing test edited"),
                ("valid edit", T1, {}, True, "1 edited test(s) fail on the fix without the edit and pass with it: tests/test_m.py::test_none"),
                ("import added", "import os\n" + T1, {}, True, "1 edited test(s) fail on the fix"),
                ("unneeded edit", T0.replace("label(1) == '1'", "label(1) == str(1)"), {}, False,
                 "tests/test_m.py::test_val without the edit: PASSED on the fix (only a test the fix makes fail may be updated)"),
                ("wrong expectation", T0.replace(NONE0, NONE1.replace("== ''", "== 'none'")), {}, False, "tests/test_m.py::test_none with the edit: FAILED"),
                ("fewer asserts", T0.replace("    assert label(2) == '2'", "    label(2)"), {}, False, "tests/test_m.py::test_val2 has 0 assertion(s), 1 at baseline"),
                ("no target", T0.replace("assert label(3) == '3'", "assert str(3) == '3'"), {}, False, "tests/test_m.py::test_val3 names none of label"),
                ("constant added", T1 + "\nX = 1\n", {}, False, "code other than test functions changed"),
                ("removed", T0.replace("def test_val3():\n    assert label(3) == '3'\n", ""), {}, False, "test(s) removed or renamed: test_val3"),
                ("renamed", T0.replace("def test_val3(", "def test_val3b("), {}, False, "test(s) removed or renamed: test_val3"),
                ("four edits", four, {}, False, "4 existing test functions edited (at most 3)"),
                ("not pytest", T1, {"suite_cmd": "node --test"}, False, "suite_cmd is not `<python> -m pytest ...`"),
                ("parse error", T1 + "\ndef broken(:\n", {}, False, "tests/test_m.py does not parse"),
                ("class attribute", T0.replace("class TestK:\n", "class TestK:\n    y = 2\n\n"), {}, False, "code other than test functions changed"),
                ("narrowed suite", T1, {"suite_cmd": "true narrowed", "_suite_full": py}, True, "1 edited test(s) fail on the fix")):
            got = flip(text, **kw)
            assert got is not None and got[0] == want_ok and frag in got[1], (name, got)


def _self_test_doc_pass():
    """doc_pass (2026-11-06): one local question per docstring a green fix left as it was; a correction is laid out as the old
    docstring was, refused unless it is a small edit of that one docstring, and put back unless the re-run stays green."""
    import tempfile as _tf
    base = '''"""Module doc."""


def parse(s, strict=False):
    """Parse s as a number.

    :param s: the text
    :returns: the number, or None when s is empty
    """
    if not s:
        return None
    return int(s)


def keep(x):
    """Return x unchanged."""
    return x


class Box:
    """A box of one value; the value must be positive."""

    def __init__(self, v):
        if v <= 0:
            raise ValueError(v)
        self.v = v


def ex(a):
    """Add one.

    >>> ex(1)
    2
    """
    return a + 1


def done(a):
    """Old doc."""
    return a


def esc(a):
    """Split on \\n."""
    return a.split("\\n")


def tail(a):
    """Return a."""
    return a
'''

    def rep(t, a, b):
        assert t.count(a) == 1, a
        return t.replace(a, b)
    cur = base
    for a, b in (("        return None\n", "        return 0\n"), ("if v <= 0:", "if v < 0:"), ("return a + 1", "return a + 2"),
                 ('"""Old doc."""\n    return a\n', '"""New doc."""\n    return a or 0\n'), ('a.split("\\n")', 'a.split(",")'),
                 ('"""Return a."""\n    return a\n', '"""Return a."""\n    return a if a else None\n')):
        cur = rep(cur, a, b)
    bt = {"m.py": base}

    def sh(d, *a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=d, check=True, capture_output=True)
    with _tf.TemporaryDirectory() as d:
        sh(d, "init", "-q")
        mp = Path(d, "m.py")
        mp.write_text(cur)
        cs = doc_candidates(d, {"target_fn": "parse"}, ["m.py", "gone.py", "x.js"], {**bt, "gone.py": "", "x.js": ""})
        # keep: code unchanged; ex: an example; done: the fix rewrote its docstring; esc: an escape in a non-raw literal
        assert [(c["qual"], c["kind"], c["owner"]) for c in cs] == [("parse", "function", "parse"), ("Box.__init__", "class", "Box")], cs
        assert [c["qual"] for c in doc_candidates(d, {"target_fn": ["tail"]}, ["m.py"], bt)] == ["tail", "parse"], "the target first"
        p, bx = cs
        assert (p["width"], p["text_width"]) == (79, 69), p
        assert p["doc"] == "Parse s as a number.\n\n:param s: the text\n:returns: the number, or None when s is empty", p["doc"]
        assert p["before"].startswith("def parse(") and "return None" in p["before"] and "return 0" in p["after"], p
        assert doc_candidates(d, {}, ["m.py"], {"m.py": cur}) == [], "an unchanged file: nothing to ask"
        mp.write_text(cur + "\nHELP = parse.__doc__\n")
        assert doc_candidates(d, {}, ["m.py"], bt) == [], "a module that reads docstrings as data"
        mp.write_bytes(cur.replace("\n", "\r\n").encode())
        assert doc_candidates(d, {}, ["m.py"], bt) == [], "a CRLF file is left alone"
        mp.write_text(cur)
        Path(d, "other.py").write_text("def show(f):\n    return f.__doc__\n")
        sh(d, "add", "m.py", "other.py")
        assert [c["qual"] for c in doc_candidates(d, {"target_fn": "parse"}, ["m.py"], bt)] == ["parse", "Box.__init__"], "other.py names neither"
        Path(d, "cli.py").write_text("import m\n\nprint(m.parse.__doc__)\n")
        sh(d, "add", "cli.py")
        assert doc_readers(d, "m.py", "parse") == ["cli.py"], doc_readers(d, "m.py", "parse")
        assert doc_candidates(d, {"target_fn": "parse"}, ["m.py"], bt) == [], "cli.py prints the docstrings of m"
        sh(d, "rm", "-q", "--cached", "cli.py")
        assert doc_readers(d, "m.py", "parse") == [], "an untracked file is not a reader"
        tl = doc_candidates(d, {"target_fn": "tail"}, ["m.py"], bt)[0]
    for s in ("import click\n", "x = f.__doc__\n", "inspect.getdoc(f)\n", "from cmd import Cmd\n", "  import typer as t\n"):
        assert DOC_DATA_RE.search(s), s
    for s in ("import clickhouse\n", "import cmdline\n", "# click here\n", "docs = 1\n"):
        assert not DOC_DATA_RE.search(s), s
    # doc_apply: the correction in the old layout, or a refusal
    fix1 = p["doc"].replace("or None when", "or 0 when")
    assert doc_apply(cur, p, fix1) == (cur.replace("or None when s is empty", "or 0 when s is empty"), 1)
    for new, why in (("  ", "an empty docstring"), (p["doc"], "no change"), (fix1 + "\n\n>>> parse('')\n0", "an example, a quote run or a tab"),
                     (fix1.replace("the text", 'the """ text'), "an example, a quote run or a tab"),
                     (fix1 + ' "0"', "an example, a quote run or a tab"), (fix1 + "\tx", "an example, a quote run or a tab"),
                     (fix1.replace("the text", "the \\d text"), "a backslash in a non-raw docstring"),
                     (fix1.replace("the text", "the text " + "x" * 70), "a line over 79 characters"),
                     ("A\nB\nC\nD\nE", "it rewrites 5 of 4 lines (cap 4)")):
        assert doc_apply(cur, p, new) == (None, why), (new, doc_apply(cur, p, new))
    assert doc_apply(cur, bx, "A box of one value; the value must not be negative.") == \
        (cur.replace("the value must be positive.", "the value must not be negative."), 1), "the class docstring"
    out, n = doc_apply(cur, tl, "Return a.\n\nNone when a is empty.")
    assert n == 2 and out == cur.replace('"""Return a."""\n    return a if', '"""Return a.\n\n    None when a is empty.\n    """\n    return a if'), out

    def one(src, fixed, new):
        with _tf.TemporaryDirectory() as d2:
            sh(d2, "init", "-q")
            Path(d2, "k.py").write_text(fixed)
            cs2 = doc_candidates(d2, {}, ["k.py"], {"k.py": src})
            assert len(cs2) == 1, (src, cs2)
            return doc_apply(fixed, cs2[0], new)[0]
    src = 'def lead(a):\n    """\n    Return a.\n    """\n    return a\n'
    fx = src.replace("return a\n", "return a or 1\n")
    assert one(src, fx, "Return a, or 1 when a is falsy.") == fx.replace("Return a.", "Return a, or 1 when a is falsy."), "a leading newline"
    src = 'import re\n\n\ndef rx(a):\n    r"""Match \\d+ in a."""\n    return re.findall(r"\\d+", a)\n'
    fx = src.replace('r"\\d+"', 'r"\\d*"')
    assert one(src, fx, "Match \\d* in a.") == fx.replace("Match \\d+", "Match \\d*"), "a raw docstring keeps its backslash"
    assert one(src, fx, "Match \\d* in a.\\") is None, "a raw docstring that would end in a backslash"
    src = "class K:\n    def m(self, a):\n        '''Return a.\n\n        Never None.'''\n        return a\n"
    fx = src.replace("return a\n", "return a or 1\n")
    assert one(src, fx, "Return a.\n\nNever None; 1 when a is falsy.") == fx.replace("Never None.'''", "Never None; 1 when a is falsy.'''"), \
        "a ''' method docstring keeps its closing quotes on the text line"

    def lay(src):
        return _doc_layout(src, _doc_stmt(ast.parse(src).body[0]))
    assert lay('def f():\n\t"""Doc."""\n\treturn 1\n') is None, "a tab indent"
    assert lay('def f():\n    "Doc."\n    return 1\n') is None, "a single-quoted docstring"
    assert lay('def f():\n    """A""" """B"""\n    return 1\n') is None, "implicit concatenation"
    assert lay('def f():\n    """Doc."""\n    return 1\n')[2:] == ("", '"""', "    ", "    ", False, "")
    st = lambda src: _doc_stmt(ast.parse(src).body[0])
    assert st("def f():\n    return 1\n") is None and st('def f(): """Doc."""\n') is None
    assert st('def f():\n    """doc"""; x = 1\n') is None and st('def f():\n    x = """doc"""\n') is None
    assert st('def f():\n    """doc"""\n    x = 1\n') is not None
    assert _py_defs(ast.parse("if X:\n    def f(): pass\nelse:\n    def f(): pass\n")) == {"f": None}, "if/else variants: no single owner"
    pd = _py_defs(ast.parse("class C:\n    def m(self):\n        def inner(): pass\n"))
    assert sorted(pd) == ["C.m", "C.m.<locals>.inner"] and pd["C.m"][1].name == "C" and pd["C.m.<locals>.inner"][1] is None, pd
    assert sorted(_py_defs(ast.parse("try:\n    def t(): pass\nexcept E:\n    def u(): pass\n"))) == ["t", "u"]
    assert _changed_lines("a\nb\nc\nd", "a\nB\nc\nD") == {2, 4} and _changed_lines("a\nb\nc", "a\nc") == {1}
    assert _changed_lines("a\nb", "a\nb") == set()
    assert _compile_warnings("x = 1\n", "w.py") == [] and _compile_warnings("def (:\n", "w.py") is None
    assert len(_compile_warnings('x = "\\d"\n', "w.py")) == 1, "an invalid escape"
    for r in ("KEEP", "**KEEP**", "KEEP.", "<think>a fix?\n```text\nX\n```\n</think>\nKEEP"):
        assert doc_reply(r) == ("keep", None), r
    assert doc_reply('Fixed:\n```python\n"""Parse s.\n\n:returns: 0\n"""\n```\n') == ("fix", "Parse s.\n\n:returns: 0")
    assert doc_reply("```text\nA\n```\n```text\nB\n```") == ("bad", "more than one code block")
    assert doc_reply("```python\ndef parse(s):\n    return 0\n```") == ("bad", "a code block, not a docstring")
    assert doc_reply("```text\nParse s.\n\n:returns:\t0\n```") == ("bad", "a tab in the docstring")
    assert doc_reply("It still holds.") == doc_reply(None) == ("bad", "neither KEEP nor one code block")
    pr = doc_prompt(p, "The bug.")
    for s in ("## The problem the fix solves\n\nThe bug.", "## `parse` before the fix", "## `parse` after the fix", "at most 69 characters",
              "the single word KEEP"):
        assert s in pr, s
    assert "## The docstring of class `Box`" in doc_prompt(bx, "The bug.") and "## The docstring of class" not in pr
    tm = ("# TASK: t\n\n## Problem (as reported: this is the requirement)\n\nlabel(None) fails.\n\n## Diagnosis (a model run)\n\n"
          "### Root cause\n\nmodel words\n\n## Entry point\n\nfix it here\n")
    assert _doc_problem(tm) == "label(None) fails.", _doc_problem(tm)
    assert _doc_problem("x" * 5000) == "x" * 4000 + "\n... (truncated)"
    # doc_pass end to end: a stub model; the file is byte for byte as it was unless a correction stays green
    ok_fix = {"content": "```text\n" + fix1 + "\n```", "finish_reason": "stop", "usage": {"prompt_tokens": 900, "completion_tokens": 40}, "secs": 2}
    keep_r = {"content": "KEEP", "finish_reason": "stop", "usage": {"prompt_tokens": 800, "completion_tokens": 2}, "secs": 1}
    with _tf.TemporaryDirectory() as d:
        sh(d, "init", "-q")
        mp = Path(d, "m.py")

        def run(replies, budget=1000, check=lambda: (True, "checked")):
            mp.write_text(cur)
            log, us = [], types.SimpleNamespace(rows=[], secs=0.0)

            def call(model, local, messages, no_think, timeout):
                log.append((messages[0]["content"], timeout))
                r = replies["parse" if "`parse`" in messages[0]["content"].split("\n")[0] else "Box"]
                if isinstance(r, Exception):
                    raise r
                return r
            r = doc_pass(d, {"target_fn": "parse"}, ["m.py"], bt, "## Problem\n\nThe bug.\n", "mdl", {"api_key": "k"}, us, budget, check, call=call)
            return r, log, us
        r, log, us = run({"parse": ok_fix, "Box": keep_r})
        assert r == (True, "`parse` (m.py): model corrected it (1 line(s), in the diff); checked; `Box` (m.py): model: still true (KEEP)"), r
        assert mp.read_text() == cur.replace("or None when s is empty", "or 0 when s is empty")
        assert len(us.rows) == 2 and [t for _, t in log] == [240, 240] and "fix solves\n\nThe bug.\n\n## `parse`" in log[0][0], (us.rows, log)
        bad = lambda content: {**ok_fix, "content": content}
        for parse_r, chk, want in (
                (ok_fix, lambda: (False, "red"), "correction put back (red)"),
                (RuntimeError("boom"), None, "error (RuntimeError: boom), docstring left as it was"),
                ({"error": "HTTP 500"}, None, "model call failed (HTTP 500), docstring left as it was"),
                ({**ok_fix, "finish_reason": "length"}, None, "the reply was cut off, docstring left as it was"),
                (bad("```python\ndef parse(s):\n    return 0\n```"), None, "reply not used (a code block, not a docstring), docstring left as it was"),
                (bad("```text\n" + fix1 + "\nRun it as __main__.\n```"), None, "correction put back (no_hacks: 1 anti-gaming hit(s)"),
                (bad("```text\n" + fix1.replace("the text", "the text " + "x" * 70) + "\n```"), None,
                 "correction refused (a line over 79 characters), docstring left as it was"),
                (bad("```text\n" + p["doc"] + "\n```"), None, "model: still true (it repeated the docstring)")):
            r, log, us = run({"parse": parse_r, "Box": keep_r}, check=chk or (lambda: (True, "checked")))
            assert r[0] is True and r[1].startswith("`parse` (m.py): " + want) and r[1].endswith("`Box` (m.py): model: still true (KEEP)"), (want, r)
            assert mp.read_bytes() == cur.encode(), want
        r, log, us = run({"parse": ok_fix, "Box": keep_r}, budget=50)
        assert r == (True, "`parse` (m.py): not asked (no time left); `Box` (m.py): not asked (no time left)") and not log and not us.rows, r
        mp.write_text(base)
        assert doc_pass(d, {}, ["m.py"], bt, "", "mdl", {}, types.SimpleNamespace(rows=[], secs=0.0), 1000, lambda: (True, ""),
                        call=lambda *a: 1 / 0) is None, "nothing to ask: no call"
        Path(d, "REVIEW.md").write_text(f"| gate | | detail |\n|---|---|---|\n| docstring | ok | {r[1]} |\n")
        assert _auto_doc(d) == r[1]
        Path(d, "REVIEW.md").write_text("| style | ok | x |\n")
        assert _auto_doc(d) == "" and _auto_doc(Path(d, "gone")) == ""
    prev = os.environ.pop("MINI_DOC_PASS", None)
    try:
        assert doc_pass_on({"doc_pass": True}) and not doc_pass_on({}) and not doc_pass_on({"doc_pass": False})
        os.environ["MINI_DOC_PASS"] = "off"
        assert not doc_pass_on({"doc_pass": True})
    finally:
        os.environ.pop("MINI_DOC_PASS", None) if prev is None else os.environ.__setitem__("MINI_DOC_PASS", prev)


def _self_test_stage26():
    """Stage 26 (2026-11-07, batch 2's general causes): protocol reds (a2a), the plain-script runner (unyt, kopf, holoviews),
    duplicate method names in fn_slice/fn_scope (scrapy2) and a class named as target_fn (a2a)."""

    class Box:
        pass

    def err(f):
        """(type name, message) of the TypeError/AttributeError f raises (a coroutine f returns is run to its first await)."""
        try:
            r = f()
            if inspect.iscoroutine(r):
                r.send(None)
        except (TypeError, AttributeError) as e:
            return type(e).__name__, str(e)
        except StopIteration:
            pass
        raise AssertionError(f"{f} raised no TypeError/AttributeError")

    async def _awith(x):
        async with x:
            pass

    async def _afor(x):
        async for _ in x:
            pass

    def _with(x):
        with x:
            pass

    def _setitem(x):
        x[0] = 1

    def sp(task, cls=("Box",)):
        return {"_red_task": task, "_red_classes": list(cls)}

    good = [(lambda: _awith(Box()), "Add \\_\\_aenter\\_\\_ and \\_\\_aexit\\_\\_ to Box."),
            (lambda: _with(Box()), "Box should be usable as a context manager."),
            (lambda: _afor(Box()), "Box should be an async iterable."),
            (lambda: list(Box()), "Box should be iterable."),
            (lambda: len(Box()), "Support len() on Box."),
            (lambda: Box()[0], "Add `__getitem__` to Box."),
            (lambda: _setitem(Box()), "Add `__setitem__` to Box."),
            (lambda: Box()(), "Add `__call__` to Box."),
            (lambda: Box() + 1, "Box needs `__add__`."),
            (lambda: Box() < Box(), "Decorate Box with functools.total_ordering."),
            (lambda: -Box(), "Add `__neg__` to Box.")]
    for f, task in good:
        typ, msg = err(f)
        assert protocol_red(typ, msg, sp(task)), (typ, msg, task)
        assert red_reason_problem(typ, msg, sp(task)) is None, (typ, msg, task)
    assert protocol_red("AttributeError", "'Box' object has no attribute '__aenter__'", sp("Add __aenter__ to Box."))
    assert protocol_red("AttributeError", "__aenter__", sp("Add __aenter__ to Box."))
    assert not protocol_red("AttributeError", "'Box' object has no attribute 'close'", sp("Add close() to Box."))
    assert not protocol_red("TypeError", "'Box' object is not iterable", None)
    # python 3.14 names the class by its qualified name: 'pkg.mod.Box', 'test_x.<locals>.Box'
    q = "'pkg.mod.Box' object does not support the asynchronous context manager protocol (missed __aexit__ method)"
    assert protocol_red("TypeError", q, sp("Add __aenter__ and __aexit__ to Box.")), q
    assert protocol_red("TypeError", "'test_x.<locals>.Box' object is not iterable", sp("Box should be iterable."))
    assert protocol_red("TypeError", "unsupported operand type(s) for +: 'test_x.<locals>.Box' and 'int'", sp("Box needs `__add__`."))
    assert not protocol_red("TypeError", "'pkg.mod.Other' object is not iterable", sp("Box should be iterable."))
    t, m = err(lambda: len(Box()))
    assert not protocol_red(t, m, sp("Fix the docstring of Box.")), m
    assert not protocol_red(t, m, sp("Support len() on Box.", ("Other",))), m
    assert not protocol_red(t, m, sp("")), m
    nothing = None  # a literal None[0] is a SyntaxWarning at compile time
    t, m = err(lambda: nothing[0])
    assert not protocol_red(t, m, sp("Add `__getitem__` to Box.")), m
    assert "wrong shape" in (red_reason_problem(t, m, sp("Add `__getitem__` to Box.")) or ""), m
    t, m = err(lambda: len(5))
    assert not protocol_red(t, m, sp("Support len() on Box.")), m
    why = red_reason_problem(*err(lambda: list(Box())), sp("Fix the docstring of Box."))
    assert why and "is not an AssertionError" in why and "spec.red_ok" in why and "hasattr" in why and "\n" not in why, why
    with tempfile.TemporaryDirectory() as d:
        Path(d, "m.py").write_text("class Box:\n    pass\n")
        Path(d, "test_m.py").write_text("import m\n\n\nclass _T(m.Box):\n    pass\n\n\nclass Own(object):\n    pass\n")
        Path(d, "TASK.md").write_text("Add `__len__` to Box.\n")
        s = {"test_file": "test_m.py"}
        _remember_task(s, d, ["m.py", "test_m.py"])
        assert s["_red_classes"] == ["Box", "_T"] and "__len__" in s["_red_task"], s
        json.dumps(s)
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t_async.py").write_text("async def test_a():\n    assert 1 == 2, \"one is not two\"\n\n\ndef test_b():\n    pass\n"
                                         + "\n".join(py_harness(["test_a", "test_b"])) + "\n")
        r = subprocess.run([sys.executable, "t_async.py"], cwd=d, capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and "RESULT test_a FAIL AssertionError: one is not two" in r.stdout and "RESULT test_b PASS" in r.stdout \
            and "never awaited" not in r.stderr, (r.returncode, r.stdout[-600:], r.stderr[-600:])
    head = ("from m import double\n\n\ndef test_a():\n    assert double(2) == 4, f\"double(2) = {double(2)}\"\n\n\n"
            "def test_b():\n    assert double(5) == 10, f\"double(5) = {double(5)}\"\n")
    unyt_main = ('\n\nif __name__ == "__main__":\n    for test_fn in TESTS:\n        try:\n            test_fn()\n'
                 '            print(f"PASS: {test_fn.__name__}")\n        except Exception as e:\n'
                 '            print(f"FAIL: {test_fn.__name__}: {e}")\n')
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "m.py").write_text("def double(x):\n    return x + x + 1\n")
        spec = {"test_file": "test_m.py", "test_cmd": f"{shlex.quote(sys.executable)} test_m.py", "target_fn": "double"}
        silent = head + "\n\nTESTS = [test_a, test_b]\n" + unyt_main
        (repo / "test_m.py").write_text(silent)
        bad, ev = test_gate(repo, dict(spec), ["m.py"])
        assert bad and "exited 0 although it printed FAIL for test_a, test_b" in bad[0] and "\n" not in bad[0], (bad, ev)
        assert (repo / "test_m.py").read_text() == silent and "runner_fixed" not in ev
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        now = (repo / "test_m.py").read_text()
        assert not bad and ev.get("runner_fixed") == "replaced the `__main__` block", (bad, ev)
        assert "RESULT {_t.__name__}" in now and "TESTS = [test_a, test_b]" in now and "PASS: {test_fn" not in now, now
        (repo / "test_m.py").write_text(head)
        bad, ev = test_gate(repo, dict(spec), ["m.py"])
        assert bad and bad[0].startswith("zero tests ran") and "tempfile.TemporaryDirectory()" in bad[0] and "\n" not in bad[0], bad
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        now = (repo / "test_m.py").read_text()
        assert not bad and ev.get("runner_fixed") == "added a `__main__` block" and "TESTS = [test_a, test_b]" in now, (bad, ev, now)
        fx = head.replace("def test_a():", "def test_a(tmp_path):")
        (repo / "test_m.py").write_text(fx)
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        assert bad and bad[0].startswith("zero tests ran") and "runner_fixed" not in ev and (repo / "test_m.py").read_text() == fx, (bad, ev)
        # holoviews: TESTS and GUARDS live inside the model's block; the rewrite hoists both to module level
        keep = "\n\ndef test_keep():\n    assert isinstance(double(3), int), f\"double(3) = {double(3)!r}\"\n"
        hv_main = ('\n\nif __name__ == "__main__":\n    TESTS = [test_a, test_b, test_keep]\n    GUARDS = [test_keep]\n    failed = 0\n'
                   '    for test_fn in TESTS:\n        name = test_fn.__name__\n        try:\n            test_fn()\n'
                   '            print(f"PASS: {name}")\n        except Exception as e:\n'
                   '            print(f"FAIL: {name}: {type(e).__name__}: {e}")\n            failed += 1\n    print(f"{failed} failed")\n')
        hv = head + keep + hv_main
        out = "FAIL: test_a: AssertionError: x\nFAIL: test_b: AssertionError: y\nPASS: test_keep\n2 failed\n"
        assert _py_failed_silently("test_m.py", hv, out, spec)[0] == ["test_a", "test_b"]
        new = py_runner_rewrite(hv, "test_m.py", replace=True)
        assert new and "\nTESTS = [test_a, test_b, test_keep]" in new and "\nGUARDS = [test_keep]" in new and "PASS: {name}" not in new, new
        assert declared_tests(new, "test_m.py") == (["test_a", "test_b", "test_keep"], True), new
        assert declared_guards(new, "test_m.py") == ["test_keep"], new
        (repo / "t_hv.py").write_text(new)
        r = subprocess.run([sys.executable, "t_hv.py"], cwd=repo, capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and "RESULT test_keep PASS" in r.stdout and "RESULT test_a FAIL AssertionError: double(2) = 5" in r.stdout, \
            (r.stdout, r.stderr)
        assert py_runner_rewrite(hv.replace("    failed = 0\n", "    failed = 0\n    LIMIT = 5\n"), "test_m.py", replace=True)
        assert py_runner_rewrite(hv.replace("double(5) == 10", "double(5) == 2 * LIMIT").replace("    failed = 0\n", "    failed = 0\n    LIMIT = 5\n"),
                                 "test_m.py", replace=True) is None
        assert py_runner_rewrite(silent.replace("    for test_fn", "    setup_db()\n    for test_fn"), "test_m.py", replace=True) is None
    q = ("from typing import Protocol\n\n\nclass QueueProtocol(Protocol):\n    def pop(self):\n        ...\n\n\n"
         "class ScrapyPriorityQueue:\n    def pop(self):\n        return self.q.pop()\n\n    def peek(self):\n        return self.q[-1]\n\n\n"
         "class DownloaderAwarePriorityQueue:\n    def __init__(self):\n        self.q = []\n\n    def pop(self):\n        return self.q.pop(0)\n")
    defs = py_all_defs(q)
    assert [x[1] for x in defs] == ["QueueProtocol.pop", "ScrapyPriorityQueue.pop", "ScrapyPriorityQueue.peek",
                                    "DownloaderAwarePriorityQueue.__init__", "DownloaderAwarePriorityQueue.pop"], defs
    assert [x[4] for x in defs] == [True, False, False, False, False] and defs[4][2:4] == (21, 22), defs
    with tempfile.TemporaryDirectory() as d:
        Path(d, "q.py").write_text(q)
        Path(d, "TASK.md").write_text("DownloaderAwarePriorityQueue.pop must return the oldest request first. Fix "
                                      "DownloaderAwarePriorityQueue.\n")
        s = fn_slice(d, ["q.py"], ["pop"])
        assert "`DownloaderAwarePriorityQueue.pop`: current lines 21-22" in s and "`pop` is also defined at: QueueProtocol.pop:5-6" in s, s
        assert "`QueueProtocol.pop`: current" not in s and "`ScrapyPriorityQueue.pop`: current" not in s, s
        Path(d, "q.py").write_text(q.replace("return self.q.pop(0)", "return self.q.pop(0) if self.q else None"))
        ok, why = fn_scope_check(d, {"target_fn": ["pop"]}, ["q.py"], {"q.py": q})
        assert ok and "outside 0" in why, why
        box = "class Box:\n    def close(self):\n        return 1\n"
        Path(d, "b.py").write_text(box + "\n    async def __aenter__(self):\n        return self\n")
        ok, why = fn_scope_check(d, {"target_fn": ["Box"]}, ["b.py"], {"b.py": box})
        assert ok and "outside 0" in why, why


def _self_test_stage27():
    """Stage 27 (2026-11-07): diagnose time, the diag_check repairs the runner calls (fix_proposed, content-resolved paths), the
    plain-script runner's two new shapes (phase A's unreadable failure, the direct fallback's own test) and the crash the issue
    reports (reported_red)."""
    assert REMOTE_PYTHONS == ("3.11", "3.12", "3.13"), REMOTE_PYTHONS  # stage 27b: a 3.11 project (dmcg3) runs on the CPU runner
    assert AUTO_DIAG_TIMEOUT >= 2700 and AUTO_TIMEOUT - AUTO_DIAG_TIMEOUT - 300 >= 2400, (AUTO_TIMEOUT, AUTO_DIAG_TIMEOUT)
    assert "never with\n`<lang> <path>:<N>`: that info string says the lines are in the file now." in DIAG_CONTRACT
    src = inspect.getsource(diagnose)
    assert src.index('getattr(dc, "fix_quotes"') < src.index('getattr(dc, "fix_proposed"') < src.index("dc.fix_ranges("), "repair order"
    dc = _diag_checker()
    assert hasattr(dc, "fix_proposed") and hasattr(dc, "_resolve_by_content"), "diag_check lacks the stage 27 repairs"
    tsrc = inspect.getsource(_two_phase)
    assert tsrc.index("_fb_runner_fix(cwd, spec, tf)") < tsrc.index("tsha = sha(tp)"), "the fallback's runner repair runs first"
    assert 'fbfix = None if (after == "fix" and tsha_m == tsha0) else _fb_runner_fix(cwd, spec, tf)' in tsrc, "phase A's passed test keeps its block"
    head = ("from m import double\n\n\ndef test_a():\n    assert double(2) == 4, f\"double(2) = {double(2)}\"\n\n\n"
            "def test_b():\n    assert double(5) == 10, f\"double(5) = {double(5)}\"\n")
    up_main = ('\n\nTESTS = [test_a, test_b]\n\n\nif __name__ == "__main__":\n    import sys\n    failed = []\n    for test_fn in TESTS:\n'
               '        name = test_fn.__name__\n        try:\n            test_fn()\n            print(f"  PASS: {name}")\n'
               '        except Exception as err:\n            failed.append(name)\n            print(f"  FAIL: {name}")\n'
               '            print(f"    Error: {err}")\n    sys.exit(1 if failed else 0)\n')
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "m.py").write_text("def double(x):\n    return x + x + 1\n")
        spec = {"test_file": "test_m.py", "test_cmd": f"{shlex.quote(sys.executable)} test_m.py", "target_fn": "double"}
        up = head + up_main
        (repo / "test_m.py").write_text(up)
        bad, ev = test_gate(repo, dict(spec), ["m.py"])
        assert bad and bad[0].startswith("baseline failure is not an assertion failure") and "runner_fixed" not in ev, (bad, ev)
        assert (repo / "test_m.py").read_text() == up
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        now = (repo / "test_m.py").read_text()
        assert not bad and ev.get("runner_fixed") == "replaced the `__main__` block", (bad, ev)
        assert "RESULT {_t.__name__}" in now and "Error: {err}" not in now, now
        setup = head + "\n\ndef setup_db():\n    return None\n" + up_main.replace("    failed = []\n", "    failed = []\n    setup_db()\n")
        (repo / "test_m.py").write_text(setup)
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        assert bad and bad[0].startswith("baseline failure is not an assertion failure") and (repo / "test_m.py").read_text() == setup, (bad, ev)
        imp = "import not_a_module_xyz\n" + up
        (repo / "test_m.py").write_text(imp)
        bad, ev = test_gate(repo, dict(spec), ["m.py"], fix_runner=True)
        assert bad and "runner_fixed" not in ev and (repo / "test_m.py").read_text() == imp, (bad, ev)
        # the direct fallback's own test (_fb_runner_fix)
        (repo / "test_m.py").write_text(up)
        assert _fb_runner_fix(repo, spec, "test_m.py") == "replaced its `__main__` block"
        assert "RESULT {_t.__name__}" in (repo / "test_m.py").read_text()
        assert _fb_runner_fix(repo, spec, "test_m.py") is None
        (repo / "test_m.py").write_text(head)
        assert _fb_runner_fix(repo, spec, "test_m.py") == "added a `__main__` block"
        assert "TESTS = [test_a, test_b]" in (repo / "test_m.py").read_text()
        (repo / "test_m.py").write_text(up)
        assert _fb_runner_fix(repo, dict(spec, test_cmd="python -m pytest test_m.py"), "test_m.py") is None and (repo / "test_m.py").read_text() == up
    task = ("# TASK: x\n\n## Problem (as reported: this is the requirement)\n\nSmoothing an empty RIR crashes:\n\n```\n"
            "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n"
            "ValueError: cannot reshape array of size 0 into shape (0,1323)\n```\n\n"
            "Also `TypeError: unhashable type: 'list'` when a list is passed.\n\n```\nE   ZeroDivisionError: division by zero\n```\n\n"
            "KeyError: 'abc'\nImportError: cannot import name 'foo' from 'bar'\nRuntimeError: Event loop is closed while waiting for ...\n\n"
            "## Diagnosis (a read-only model run)\n\nOverflowError: math range error at the end of the loop\n\n## Entry point\n\n`x.py` `f`.\n")
    s = {"_red_task": task}
    reshape = "cannot reshape array of size 0 into shape (0,3969)"
    assert reported_red("ValueError", reshape, s) and red_reason_problem("ValueError", reshape, s) is None
    assert not reported_red("TypeError", reshape, s) and not reported_red("ValueError", "operands could not be broadcast together", s)
    assert not reported_red("OverflowError", "math range error at the end of the loop", s) and not reported_red("KeyError", "'abc'", s)
    assert reported_red("TypeError", "unhashable type: 'list'", s) and reported_red("ZeroDivisionError", "division by zero", s)
    assert reported_red("ImportError", "cannot import name 'foo' from 'bar'", s)
    assert "broken" in (red_reason_problem("ImportError", "cannot import name 'foo' from 'bar'", s) or "")
    assert reported_red("RuntimeError", "Event loop is closed while waiting for the reader task", s)
    assert reported_red("ValueError", "cannot reshape array of size 0 into shape (0,7)",
                        {"_red_task": "Calling f() raises\nValueError: cannot reshape array of size 0 into shape (0,1323)\n"})
    assert not reported_red("ValueError", reshape, {})


def _self_test_stage25():
    """Stage 25: fail_lines skips pytest's captured-log lines; crash_hit is positional once per-test lines parsed; fb_red_problem
    takes a plain assertion message; the fallback prompt names the RESULT line format for a plain-script run."""
    log = "ERROR    scrapy.downloadermiddlewares.retry:retry.py:118 Gave up retrying <GET https://127.0.0.1:41165/echo> (failed 3 times)\n"
    got = fail_lines(1, log + "FAILED t.py::a - AssertionError\n")
    assert got == {"FAILED t.py::a - AssertionError"}, got
    assert fail_lines(1, "ERROR t.py::b - ImportError\n") == {"ERROR t.py::b - ImportError"}
    assert fail_lines(1, "ERROR tests/t.py\n") == {"ERROR tests/t.py"} and fail_lines(1, log) == {"rc=1"}
    tb = "Traceback (most recent call last):\n  File \"h.py\", line 12, in <module>\n    import httpx\nModuleNotFoundError: No module named 'httpx'\n"
    res = "RESULT test_a FAIL AssertionError: expected None\nRESULT test_b FAIL AssertionError: expected True\n"
    per = parse_test_results(tb + res)
    assert len(per) == 2 and crash_hit(tb + res, per) is None, per
    assert crash_hit(tb + res + "Traceback (most recent call last):\nNameError: name 'x' is not defined\n", per).group(0) == "NameError: name"
    assert crash_hit(tb + res + "ERROR collecting tests/t.py\n", per).group(0) == "ERROR collecting"
    assert crash_hit(tb, []).group(0) == "ModuleNotFoundError" and crash_hit("", []) is None and crash_hit(res, per) is None
    plain = "Expected crawler.spider to be None after open_spider failure, but got <TestSpider 'x' at 0x1>"
    assert fb_red_problem(plain, plain, {}) is None and fb_red_problem("AssertionError", "expected 3", {}) is None
    assert fb_red_problem("TypeError", "unsupported operand", {}) and fb_red_problem("ModuleNotFoundError", "No module named 'x'", {})
    assert fb_red_problem("unknown", "", {}) and fb_red_problem("Expected TypeError not raised", "Expected TypeError not raised", {})
    p = fallback_prompt({"test_file": "tests/test_x.py", "test_cmd": "python3 tests/test_x.py", "fix_steps": 5}, ["m.py"], "why", "")
    assert "RESULT <name> FAIL <ExceptionType>: <message>" in p and "exits 1 when any failed" in p


def _self_test_direct_fallback():
    """Direct-fix fallback (stage 22, 2026-11-06): after phase A rejects every test, or phase B's fix fails its gates, one plain run
    writes the test and the fix together; it goes to review as `PASS needs-review soft=fallback,...` with both packets saying so,
    or ends the job as today with ` fallback=direct FAIL (<why>)` when it has no evidence; the spec field and MINI_DIRECT_FALLBACK=off
    turn it off; too little time skips it."""
    import tempfile as _tf
    m_bad = "def add(a, b):\n    return a - b\n\n\ndef other():\n    return 1\n"
    m_fix = m_bad.replace("a - b", "a + b")
    good = ("import sys\n\nfrom m import add\n\n\ndef test_add():\n    assert add(1, 2) == 3, \"add(1, 2)\"\n\n\n"
            "def test_add_neg():\n    assert add(-1, 5) == 4, \"add(-1, 5)\"\n\n\n"
            "if __name__ == \"__main__\":\n    failed = 0\n    for t in (test_add, test_add_neg):\n        try:\n            t()\n"
            "            print(f\"OK {t.__name__}\")\n        except AssertionError as e:\n            failed += 1\n"
            "            print(f\"FAIL {t.__name__}: AssertionError: {e}\")\n    print(f\"2 tests, {failed} failed\")\n"
            "    sys.exit(1 if failed else 0)\n")
    passes = good.replace("== 3", "== -1").replace("== 4", "== -6")  # green at baseline: no evidence of the bug
    task_text = "# TASK: add\n\n## Problem (as reported: this is the requirement)\n\n`add(1, 2)` returns -1; it must return the sum, 3.\n"

    def sh(d, *a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=d, check=True, capture_output=True)
    with _tf.TemporaryDirectory() as d:
        repo = Path(d, "repo")
        repo.mkdir()
        sh(repo, "init", "-q")
        Path(repo, "m.py").write_text(m_bad)
        sh(repo, "add", "m.py")
        sh(repo, "commit", "-qm", "base")
        stub = Path(d, "mini")
        stub.write_text(STUB_MINI)
        stub.chmod(0o755)
        lj = Path(d, "local.json")
        lj.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "api_key": "k-SECRET"}))
        task = Path(d, "TASK.md")
        task.write_text(task_text)
        base = {"mode": "two-phase", "baseline_from": str(repo), "task": str(task), "allowed": ["m.py"], "test_file": "test_m.py",
                "test_cmd": "python3 test_m.py", "target_fn": "add", "timeout": 900, "test_attempts": 1, "fix_attempts": 1,
                "direct_fallback": True}
        a_passes = {"match": "PHASE 1 OF 2", "A": {"test_m.py": passes}}
        a_good = {"match": "PHASE 1 OF 2", "A": {"test_m.py": good}}
        b_wrong = {"match": "PHASE 2 OF 2", "B": {"m.py": m_bad.replace("return 1", "return 2")}}
        f_good = {"match": "DIRECT-FIX FALLBACK", "B": {"m.py": m_fix, "test_m.py": good}, "say": "fixed add and wrote the test"}
        f_passes = {"match": "DIRECT-FIX FALLBACK", "B": {"m.py": m_fix, "test_m.py": passes}}
        f_nofix = {"match": "DIRECT-FIX FALLBACK", "B": {"test_m.py": good}}

        def run(name, entries, sp, envx=None):
            rd = Path(d, name)
            (rd / "work").mkdir(parents=True)
            Path(rd, "plan.json").write_text(json.dumps({"by_task": entries, "A": {}, "B": {}, "argv_dump": str(rd / "argv.txt"),
                                                         "task_dump": str(rd / "tasks.txt")}))
            Path(rd, "spec.json").write_text(json.dumps(sp))
            r = subprocess.run([sys.executable, __file__, "--cwd", str(rd / "work"), "--task-file", str(rd / "spec.json")],
                               capture_output=True, text=True,
                               env={**os.environ, "STUB_PLAN": str(rd / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(lj),
                                    **(envx or {})})
            res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
            assert len(res) == 1 and "k-SECRET" not in r.stdout, (name, r.returncode, res, r.stdout[-1800:], r.stderr[-600:])
            return r, res[0], rd

        # 1. phase A's only test is green at baseline -> rejected -> the fallback writes a red test and the fix -> a fallback PASS
        r, line, rd = run("after-test", [a_passes, f_good], base)
        assert r.returncode == 0 and line.startswith("RESULT PASS needs-review soft=fallback mode=two-phase fallback=ok "), (line, r.stdout[-1500:])
        assert " fallback=direct after=test" in line and " test_green=ok " in line and " fb_red_reason=ok " in line and " red_without_fix=ok " in line, line
        assert Path(rd, "work/m.py").read_text() == m_fix
        assert "[mini-runner] direct fallback after test (" in r.stdout and "[mini-runner] direct fallback: PASS" in r.stdout, r.stdout[-1500:]
        rv = Path(rd, "REVIEW.md").read_text()
        assert rv.startswith("# REVIEW packet: PASS needs-review soft=fallback ") and "## Direct-fix fallback (read first)" in rv, rv[:600]
        assert "NOT frozen before the fix" in rv and "| fallback | ok |" in rv and "| fb_red_reason | ok |" in rv and "REVIEW-main.md" in rv, rv[:3000]
        assert "## Baseline evidence (the fallback's test, allowed files at baseline)" in rv and "## Fallback gates" in rv and "RESULT test_add FAIL AssertionError" in rv, rv
        assert "(the gate replaced its `__main__` block with the canonical runner)" in rv, rv  # stage 27: _fb_runner_fix
        main = Path(rd, "REVIEW-main.md").read_text()
        assert main.startswith("# REVIEW packet: FAIL phase=test test-gate: ") and "## Phase A REJECTED" in main, main[:400]
        argv = Path(rd, "argv.txt").read_text().splitlines()
        assert len(argv) == 2 and argv[0].startswith("A ") and argv[1].startswith("B "), argv
        prompts = Path(rd, "tasks.txt").read_text().split("\n=====\n")
        fbp = [p for p in prompts if "DIRECT-FIX FALLBACK:" in p]
        assert len(fbp) == 1 and "PHASE 2 OF 2" not in fbp[0] and "was REJECTED (phase A rejected the test: " in fbp[0] and "### `m.py`" in fbp[0], fbp
        assert "--- TASK.md ---\n# TASK: add" in fbp[0] and "python3 test_m.py" in fbp[0], fbp[0][-800:]
        # the compact packet: the section first, the test marked as not frozen, review ask 0, the stopped path's packet listed
        auto_review(rd, str(rd / "work"), "fb", task_text, line[len("RESULT "):].replace("mode=two-phase", "mode=auto", 1),
                    target=("m.py", ["add"]), fspec={**base, "allowed": ["m.py"]}, base_text=m_bad)
        ar = Path(rd, "REVIEW-AUTO.md").read_text()
        assert ar.index("## Direct-fix fallback (read first)") < ar.index("Diagnosed target:") and "0. FALLBACK CANDIDATE" in ar, ar[:1500]
        assert "NOT frozen, never passed phase A's gate" in ar and "REVIEW-main.md (the stopped structured path's packet)" in ar, ar
        assert ar.index("0. FALLBACK CANDIDATE") < ar.index("1. Does the test assert"), ar

        # 2. phase A froze a good test, phase B's fix failed test_green -> the fallback (test kept) -> a fallback PASS after fix
        r, line, rd = run("after-fix", [a_good, b_wrong, f_good], base)
        assert r.returncode == 0 and " fallback=direct after=fix" in line and line.startswith("RESULT PASS needs-review soft=fallback "), (line, r.stdout[-1500:])
        rv, main = Path(rd, "REVIEW.md").read_text(), Path(rd, "REVIEW-main.md").read_text()
        assert "the stopped path's own, unchanged: it passed phase A's gate" in rv and "(the test is the stopped path's, unchanged)" in rv, rv[:3000]
        assert "the gate replaced its" not in rv and "FAIL test_add: AssertionError" in rv, rv  # stage 27: the test phase A passed keeps its block
        assert main.startswith("# REVIEW packet: FAIL ") and "| test_green | FAIL |" in main, main[:600]
        fbp = [p for p in Path(rd, "tasks.txt").read_text().split("\n=====\n") if "DIRECT-FIX FALLBACK:" in p]
        assert len(fbp) == 1 and "Its fix (reverted; the diff below) failed these gates: test_green:" in fbp[0] and "passed the test gate" in fbp[0], fbp
        assert len(Path(rd, "argv.txt").read_text().splitlines()) == 3

        # 2b. phase B edited the frozen test (TEST-SUSPECT) with a wrong fix: the fallback starts from the FROZEN test, not the edit
        b_edit = {"match": "PHASE 2 OF 2", "B": {"m.py": m_bad.replace("return 1", "return 2"), "test_m.py": passes}}
        r, line, rd = run("after-fix-edited", [a_good, b_edit, f_good], base)
        assert r.returncode == 0 and " fallback=direct after=fix" in line, (line, r.stdout[-1500:])
        assert "| test_unchanged | FAIL |" in Path(rd, "REVIEW-main.md").read_text()
        fbp = [p for p in Path(rd, "tasks.txt").read_text().split("\n=====\n") if "DIRECT-FIX FALLBACK:" in p]
        assert len(fbp) == 1 and "add(1, 2) == 3" in fbp[0] and "== -1" not in fbp[0] and "test_unchanged:" in fbp[0], fbp[0][-1500:]

        # 3. the fallback's test is green at baseline -> no evidence -> today's FAIL line plus the fallback's own verdict, class TEST
        r, line, rd = run("fb-green-at-base", [a_passes, f_passes], base)
        assert r.returncode == 1 and line.startswith("RESULT FAIL mode=two-phase phase=test test-gate: ") and line.endswith(" class=TEST"), line
        assert " fallback=direct FAIL (" in line and "fb_red_reason" in line and "red_without_fix" in line, line
        assert "[mini-runner] direct fallback: FAIL (failing: " in r.stdout, r.stdout[-1200:]
        rv = Path(rd, "REVIEW.md").read_text()
        assert rv.startswith("# REVIEW packet: FAIL fallback=ok ") and "| fb_red_reason | FAIL | the test passes at baseline" in rv, rv[:2500]

        # 4. the fallback wrote a test but no fix -> no candidate
        r, line, rd = run("fb-no-fix", [a_passes, f_nofix], base)
        assert r.returncode == 1 and line.endswith(" fallback=direct FAIL (no fix written) class=TEST"), line
        assert Path(rd, "REVIEW-main.md").is_file() and "produced nothing to check: no fix written" in Path(rd, "REVIEW.md").read_text()

        # 5./6. off by env, off by spec: today's line exactly, one model run, no fallback output
        for name, sp, envx in (("env-off", base, {"MINI_DIRECT_FALLBACK": "off"}), ("spec-off", {**base, "direct_fallback": False}, None)):
            r, line, rd = run(name, [a_passes, f_good], sp, envx)
            assert r.returncode == 1 and line == "RESULT FAIL mode=two-phase phase=test test-gate: test passes at baseline (no evidence of the bug) class=TEST", (name, line)
            assert "direct fallback" not in r.stdout and len(Path(rd, "argv.txt").read_text().splitlines()) == 1, (name, r.stdout[-800:])
            assert not Path(rd, "REVIEW-main.md").exists() and "Direct-fix fallback" not in Path(rd, "REVIEW.md").read_text(), name

        # 7. too little stage time (420s total, 240s reserve): not run, today's line
        r, line, rd = run("no-time", [a_passes, f_good], {**base, "timeout": 420})
        assert r.returncode == 1 and "fallback" not in line and "[mini-runner] direct fallback not run: only " in r.stdout, (line, r.stdout[-800:])
        assert len(Path(rd, "argv.txt").read_text().splitlines()) == 1

    # the switch, as doc_pass_on's
    prev = os.environ.pop("MINI_DIRECT_FALLBACK", None)
    try:
        assert fallback_on({"direct_fallback": True}) and not fallback_on({}) and not fallback_on({"direct_fallback": False})
        os.environ["MINI_DIRECT_FALLBACK"] = "off"
        assert not fallback_on({"direct_fallback": True})
    finally:
        os.environ.pop("MINI_DIRECT_FALLBACK", None) if prev is None else os.environ.__setitem__("MINI_DIRECT_FALLBACK", prev)
    assert "direct_fallback" in AUTO_PASS_KEYS and _auto_fallback(Path("/nonexistent-dir")) == ""


def self_test():
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        Path(_d, "a.js").write_text("function inPage() { return 1; }\nfunction caller(page) { return page.evaluate(inPage); }\nfunction plain() { return caller(); }\nplain();\n")
        assert in_browser_only(_d, ["a.js"], ["inPage", "caller", "plain"]) == ["inPage"], in_browser_only(_d, ["a.js"], ["inPage", "caller", "plain"])
    assert scope_violations(["a.py", "b.py"], ["a.py"]) == ["b.py"]
    assert untracked_violations(["?? x.py", "?? .venv", "?? TASK.md"]) == ["x.py"]
    assert untracked_violations(["?? t.test.js", "?? y.js"], extra_ok=("t.test.js",)) == ["y.js"]
    # update_tests: scope/untracked/guard widen by exactly the listed tests; case-count gate; precheck rejections
    es = edit_scope({"update_tests": ["t/test-a.js"]}, ["src/a.js"])
    assert es == ["src/a.js", "t/test-a.js"] and edit_scope({}, ["src/a.js"]) == ["src/a.js"]
    assert scope_violations(["src/a.js", "t/test-a.js", "t/test-b.js"], es) == ["t/test-b.js"]
    assert untracked_violations(["?? t/test-a.js", "?? t/test-b.js"], extra_ok=("t.js", *es)) == ["t/test-b.js"]
    assert guard_violations("/nonexistent", {"_guard": {"t/test-a.js": "x"}}, "t.js", ["src/a.js"]) == ["t/test-a.js"]
    assert guard_violations("/nonexistent", {"_guard": {"t/test-a.js": "x"}}, "t.js", es) == []
    two_js = "test('a', () => {});\ntest('b', () => {});\n"
    assert case_count(two_js) == 2 and case_count("  it('a', () => {});\n") == 1 and case_count("def test_a():\n    pass\nasync def test_b():\n    pass\n") == 2
    assert case_count(two_js.replace("test('b'", "test.skip('b'")) == 1 and case_count("// test('x')\nconst latest = 1;\n") == 0
    with tempfile.TemporaryDirectory() as _u:
        Path(_u, "t").mkdir()
        up = {"update_tests": ["t/test-a.js"], "_upd_base": {"t/test-a.js": two_js}}
        Path(_u, "t/test-a.js").write_text(two_js.replace("() => {}", "() => { assert.ok(1) }"))
        assert update_tests_check(_u, up)[0]
        Path(_u, "t/test-a.js").write_text("test('a', () => {});\n")
        ok, why = update_tests_check(_u, up)
        assert not ok and "1 test case(s), baseline 2" in why, why
        Path(_u, "t/test-a.js").unlink()
        assert not update_tests_check(_u, up)[0]
        repo = Path(_u, "repo")
        for f, t in (("src/a.js", "x"), ("t/test-a.js", two_js), ("t/test-frozen.js", "x"), ("t/test-new.js", "x"), ("t/helper.js", "x")):
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text(t)
        for c in (["init", "-q"], ["add", "src", "t/test-a.js", "t/test-frozen.js", "t/helper.js"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        pb = {"baseline_from": str(repo), "allowed": ["src/a.js"], "test_file": "t/test-frozen.js", "test_cmd": "true",
              "suite_cmd": "true test-a.js", "update_tests": ["t/test-a.js"]}
        assert not [e for e in precheck(pb)[0] if "update_tests" in e], precheck(pb)[0]
        for bad, frag in (({"update_tests": ["t/test-missing.js"]}, "does not exist"), ({"update_tests": ["t/test-new.js"], "suite_cmd": "true test-new.js"}, "not tracked"),
                          ({"update_tests": ["t/helper.js"], "suite_cmd": "true helper.js"}, "not a test file"),
                          ({"update_tests": ["t/test-frozen.js"], "suite_cmd": "true test-frozen.js"}, "frozen test_file"),
                          ({"update_tests": ["t/test-a.js"], "allowed": ["src/a.js", "t/test-a.js"]}, "also in allowed"),
                          ({"suite_cmd": "true other.js"}, "must appear in suite_cmd"), ({"suite_cmd": None}, "must appear in suite_cmd"),
                          ({"update_tests": "t/test-a.js"}, "must be a list")):
            assert any(frag in e for e in precheck({**pb, **bad})[0]), (bad, precheck({**pb, **bad})[0])
        strict = {**pb, "suite_cmd": "true other.js", "update_tests_strict": True}  # auto's: update_flip runs each edited test itself
        assert not [e for e in precheck(strict)[0] if "update_tests" in e], precheck(strict)[0]
        assert any("not a test file" in e for e in precheck({**strict, "update_tests": ["t/helper.js"]})[0])
    pbp = phase_b_prompt({"test_file": "t.js", "test_cmd": "true", "update_tests": ["t/test-a.js"]}, ["src/a.js"], "x")
    assert "Keep every test case" in pbp and "t/test-a.js" in pbp and "update_tests" not in phase_b_prompt({"test_file": "t.js", "test_cmd": "true"}, ["src/a.js"], "x")
    assert ss_target("/nonexistent", {"update_tests": ["t/test-a.js"], "target_fn": "f"}, ["src/a.js"]) == (None, "single_shot cannot edit the update_tests files")
    sst = ss_target("/nonexistent", {"update_tests": ["t/test-a.js"], "update_tests_strict": True, "target_fn": "f"}, ["src/a.js"])
    assert sst[0] is None and "defined in 0 allowed files" in sst[1], sst  # strict: past the update_tests refusal
    argv = build_argv("m", "task", "/t.json")
    assert "environment.cwd=" in " ".join(build_argv("m", "t", "/t.json", "/tmp"))
    assert "agent.step_limit=7" in build_argv("m", "t", "/t.json", "/tmp", 7)
    assert "agent.wall_time_limit_seconds=600" in build_argv("m", "t", "/t.json", "/tmp", 7, 600) and "wall_time" not in " ".join(argv)
    assert argv[0] == MINI and "-c" in argv and argv[-1] == "task" and "--exit-immediately" in argv
    assert str(DIAG_OVERLAY) not in argv and build_argv("m", "t", "/t.json", "/tmp", 7, overlay="/nonexistent/x.yaml") == build_argv("m", "t", "/t.json", "/tmp", 7)
    if DIAG_OVERLAY.is_file():  # merged right after the fix overlay, so its instance template wins (later -c overrides)
        dv = build_argv("m", "t", "/t.json", "/tmp", 7, overlay=DIAG_OVERLAY)
        assert dv.index(str(DIAG_OVERLAY)) == dv.index(str(OVERLAY)) + 2 and dv[dv.index(str(DIAG_OVERLAY)) - 1] == "-c", dv
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "src").mkdir()
        (Path(d) / "src/a.py").write_text("")
        (Path(d) / "sched.py").write_text("")
        t = ("`src/a.py` exists and is used.\nThe test imports `from ..sched import x`.\n"
             "`src/webui/app.py` exists at baseline.\nThe fixture imports `from ..scheduled_runner import y`.\n"
             "`src/new.py` does not exist; this change adds it.\n")
        assert preflight_missing(d, t) == ["src/webui/app.py", "scheduled_runner"], preflight_missing(d, t)
        # a relative module specifier resolves anywhere in the tree; a missing one is still reported
        t2 = "The suite imports `./a.py` and `../src/a.py`.\nThe test imports `./gone.ts`.\n"
        assert preflight_missing(d, t2) == ["./gone.ts"], preflight_missing(d, t2)
        t3 = "The test imports `from .. import sched` and `from . import a`.\nIt imports from ..gone. import z.\n"
        assert preflight_missing(d, t3) == ["gone"], preflight_missing(d, t3)  # no crash on a module-less relative import
    rows = [(100, 50, 20), (104, 60, 80)]
    W = "tok/s wall incl. prefill+tools"
    assert tok_report(rows, 98, 0) == [f"[mini-runner] step 1: 50 in / 20 out (10.0 {W}, 2s since prev reply)", f"[mini-runner] step 2: 60 in / 80 out (20.0 {W}, 4s since prev reply)"]
    assert tok_report(rows, 98, 1) == [f"[mini-runner] step 2: 60 in / 80 out (20.0 {W}, 4s since prev reply)"]
    assert tok_report(rows, 98, 1, base=5) == [f"[mini-runner] step 7: 60 in / 80 out (20.0 {W}, 4s since prev reply)"]
    assert re.search(r"\(([\d.]+) tok/s\b", tok_report(rows, 98, 1)[0]).group(1) == "20.0"  # same pattern as ollama-queue-api._TOKS_RE (test-queue-api-usage-regex.py runs the real one)
    _u = Usage(); _u.rows, _u.secs = [(1, 10, 20)], 4
    assert re.search(r"USAGE SUMMARY: prompt_tokens=(\S+) completion_tokens=(\S+) tok_s=(\S+) decode_tok_s=(\S+)(?: tok_s_basis=\S+)? calls=(\S+)", _u.summary())  # ditto _USAGE_RE
    # static test checks
    assert test_static(GOOD_T, ["calc.js"]) == []
    assert any("vacuous" in b for b in test_static(GOOD_T + "test('x', () => assert.ok(true));\n", ["calc.js"]))
    assert any("vacuous" in b for b in test_static("from calc import add\ndef test_a():\n    assert len(add(1, 2)) >= 0\n", ["calc.py"]))
    assert any("skipped" in b for b in test_static(GOOD_T + "test.skip('y', () => {});\n", ["calc.js"]))
    assert any("mocks the code" in b for b in test_static(GOOD_T + "mock.module('./calc', {});\n", ["calc.js"]))
    # regex path says "overwrites an export ..."; the acorn path (node_modules/acorn above the cwd) says "mocks the code under test: overwrites calc.add"
    assert any("overwrites" in b for b in test_static(GOOD_T.replace("const { add }", "const calc") + "calc.add = (a, b) => a + b;\n", ["calc.js"]))
    assert any("never imports" in b for b in test_static("const assert = require('assert');\nassert.equal(1, 2); assert.equal(2, 3);\n", ["calc.js"]))
    # dynamic import()/require() whose specifier is wrapped onto the next line still counts (regex fallback; no acorn)
    _ml = "const assert = require('assert');\nconst m = await import(\n  '../src/calc.js'\n);\nassert.equal(m.add(1, 2), 3); assert.equal(m.add(2, 2), 4);\n"
    assert not any("never imports" in b for b in test_static_regex(_ml, ["calc.js"])), test_static_regex(_ml, ["calc.js"])
    assert not any("never imports" in b for b in test_static_regex(_ml.replace("await import(", "require("), ["calc.js"]))
    assert any("never imports" in b for b in test_static_regex(_ml.replace("calc.js", "other.js"), ["calc.js"]))
    assert any("never imports" in b for b in test_static_regex("const assert = require('assert');\nconst x = foo(\n  'calc'\n);\nassert.equal(x, 1); assert.equal(x, 2);\n", ["calc.js"]))
    assert any("fewer than" in b for b in test_static("const { add } = require('./calc');\nassert.equal(add(1, 2), 3);\n", ["calc.js"]))
    _self_test_static()
    lits = parse_must_contain("## Must contain\n\n- `sumFix`\n- in calc.js: `add`\n(note `x`)\n\n## Scope\n- `nope`\n")
    assert lits == [(None, "sumFix"), ("calc.js", "add")], lits
    assert parse_must_keep("x\nMust keep: `amps>0` and `b`\n- Must keep: in calc.js: `c`\n") == [(None, "amps>0"), (None, "b"), ("calc.js", "c")]
    _self_test_quality_pure()
    assert test_count("ok 1\n# tests 3\n") == 3 and test_count("2 failed, 1 passed in 0.1s") == 3 and test_count("x") is None
    assert fail_lines(0, "x") == set() and fail_lines(1, "not ok 3 - foo (2ms)\n") == {"foo"}
    with tempfile.TemporaryDirectory() as d:  # prepare() of a copied worktree must not touch the source's index
        repo = _toy(d)
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(Path(d, "wt"))], cwd=repo, check=True, capture_output=True)
        Path(d, "wt/calc.js").write_text(CALC_FIXED)
        subprocess.run(["git", "add", "calc.js"], cwd=Path(d, "wt"), check=True)
        Path(d, "w").mkdir()
        prepare(Path(d, "w"), {"baseline_from": str(Path(d, "wt"))})
        assert git(Path(d, "wt"), "diff", "--cached", "--name-only").split() == ["calc.js"]
        assert Path(d, "w/calc.js").read_text() == CALC and Path(d, "w/.git").is_dir()
        Path(d, "w/calc.js").write_text(CALC_FIXED)
        assert git(Path(d, "w"), "diff", "--name-only").split() == ["calc.js"]
        assert keep_check(Path(d, "w"), [(None, "sumFix"), (None, "other")], ["calc.js"], {"calc.js": CALC}) == []  # kept/added literals pass
        assert keep_check(Path(d, "w"), [(None, "return a")], ["calc.js"], {"calc.js": CALC}) == []  # still on an unchanged line
        Path(d, "w/calc.js").write_text(CALC_FIXED.replace("function other() { return 1; }\n", ""))
        assert keep_check(Path(d, "w"), [(None, "function other")], ["calc.js"], {"calc.js": CALC}) == ["function other"]  # removed literal fails
        Path(d, "w/calc.js").write_text(CALC_FIXED)
        Path(d, "w/x.pyc").write_text("a")
        assert not is_bytecode("calc.js") and is_bytecode("src/__pycache__/a.cpython-3.pyc") and is_bytecode("__pycache__/a.x")
    if shutil.which("node"):
        _self_test_gates()
        _self_test_quality_node()
        _self_test_probe()
        _self_test_obs_cap()
        _self_test_e2e()
        _self_test_single_shot()
        _self_test_mutation()
    if subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode == 0:
        _self_test_py_reach()
        _self_test_collect()
        _self_test_update_flip()
    else:
        print("[self-test] collect: pytest not importable; collect_check and update_flip cases skipped", flush=True)
    _self_test_bg_baseline()
    _self_test_suite_pure()
    _self_test_collect_static()
    _ff_prev = os.environ.get("MINI_FAILFAST_STEPS")
    os.environ["MINI_FAILFAST_STEPS"] = "1"  # the Phase-B step watcher is opt-in; its self-tests run with it on
    try:
        _self_test_ff_stage()
    finally:
        if _ff_prev is None:
            os.environ.pop("MINI_FAILFAST_STEPS", None)
        else:
            os.environ["MINI_FAILFAST_STEPS"] = _ff_prev
    _self_test_review2()
    _self_test_precheck()
    _self_test_skeleton()
    _self_test_review_fixes()
    _self_test_ctx()
    os.environ["MINI_FAILFAST_STEPS"] = "1"
    try:
        _self_test_failfast()
    finally:
        os.environ.pop("MINI_FAILFAST_STEPS", None) if _ff_prev is None else os.environ.__setitem__("MINI_FAILFAST_STEPS", _ff_prev)
    _self_test_slice()
    _self_test_jsend()
    _self_test_exit_status()
    _self_test_nothink()
    _self_test_gates_v2()
    _self_test_ground()
    _self_test_user_view()
    _self_test_style()
    _self_test_doc_pass()
    _self_test_direct_fallback()
    _self_test_stage25()
    _self_test_stage26()
    _self_test_stage27()
    print("self-test ok")
    return RC_OK


_SCOPE_BASE = """const { acquireLock, patchCommand, logError } = require('./lib');
const { parse } = require('./parse');

function parsePayload(x) {
  return parse(x);
}

function patchLocal(c, r) {
  return patchCommand(c, r);
}

function unrelated(c) {
  return c.id;
}

async function handleCommand(cmd) {
  const p = parsePayload(cmd.payload);
  try {
    const r = await acquireLock(p);
    return patchLocal(cmd, r);
  } catch (e) {
    logError(e);
    throw e;
  }
}

module.exports = { handleCommand, unrelated };
"""


def _scope_variants():
    b = _SCOPE_BASE
    a = (b.replace("{ acquireLock, patchCommand, logError }", "{ acquireLock, patchCommand, logApiError }")
          .replace("logError(e);", "logApiError(e);").replace("return patchCommand(c, r);", "return patchCommand(c, r, true);"))
    ok = (b.replace("{ acquireLock, patchCommand, logError }", "{ acquireLock, patchCommand, logError, redact }")
           .replace("    logError(e);\n    throw e;", "    logError(describeErr(e));\n    console.error('lock failed');\n    throw new Error('lock: ' + describeErr(e));")
           .replace("async function handleCommand", "function describeErr(e) {\n  return redact(String(e && e.message));\n}\n\nasync function handleCommand"))
    return a, ok, b.replace("return c.id;", "return c.id + 1;")


def self_test_scope():
    """Fixture checks of fn_scope_check only (no model, no network): (a) import renames + an edit in a sibling function -> outside;
    (b) catch-block edits + a new helper the target calls (+ an added import name it uses) -> inside; (c) the same verdicts with a
    fresh graph, a stale graph and no graph; modes warn/enforce/off; scope_slack; pure deletions; churn note."""
    import tempfile as _tf
    ba, bb, bc = _scope_variants()
    spec = {"target_fn": "handleCommand", "fn_scope": "enforce"}

    def check(d, text, sp=None, base=_SCOPE_BASE):
        Path(d, "poll.js").write_text(text)
        return fn_scope_check(d, {**spec, **(sp or {})}, ["poll.js"], {"poll.js": base})
    with _tf.TemporaryDirectory() as d:
        ok, det = check(d, ba)
        assert not ok and "`module imports`" in det and "dropped/renamed ['logError']" in det, det
        warn_ok, warn_det = check(d, ba, {"fn_scope": "warn"})
        assert warn_ok and warn_det.startswith("WARN ") and "`module imports`" in warn_det, warn_det
        assert check(d, ba, {"fn_scope": "off"}) == (True, "off")
        ok, det = check(d, bb)
        assert ok and "helper" in det and "describeErr" in det and "outside 0" in det, det
        ok, det = check(d, bc)  # an edit in a function the target does not touch
        assert not ok and "`unrelated` lines" in det, det
        assert check(d, _SCOPE_BASE) [0] and "target 0" in check(d, _SCOPE_BASE)[1]
        # neighbour edits: within slack pass, beyond slack fail
        nb = _SCOPE_BASE.replace("return patchCommand(c, r);", "const q = r;\n  return patchCommand(c, q);")
        ok, det = check(d, nb)
        assert ok and "neighbor 2" in det, det
        ok, det = check(d, nb, {"scope_slack": 1})
        assert not ok and "`patchLocal`" in det and "scope_slack 1" in det, det
        # a pure deletion in a sibling function (invisible to changed_lines) is outside
        ok, det = check(d, _SCOPE_BASE.replace("function unrelated(c) {\n  return c.id;\n}\n\n", ""))
        assert not ok and "unrelated (deleted lines" in det, det
        # a pure deletion inside the target is fine; heavy edits inside the target add a churn note
        ok, det = check(d, _SCOPE_BASE.replace("    logError(e);\n", ""))
        assert ok and "target 1" in det, det
        heavy = (_SCOPE_BASE.replace("const p = parsePayload(cmd.payload);", "const p = parsePayload(cmd.payload || {});")
                 .replace("const r = await acquireLock(p);\n    return patchLocal(cmd, r);", "const r2 = await acquireLock(p);\n    return patchLocal(cmd, r2);")
                 .replace("catch (e)", "catch (err)").replace("logError(e)", "logError(err)").replace("throw e;", "throw err;"))
        ok, det = check(d, heavy)
        assert ok and "NOTE churn" in det, det
        # an added import name the target does not use is outside; a changed module path is outside
        ok, det = check(d, _SCOPE_BASE.replace("{ parse }", "{ parse, extra }"))
        assert not ok and "added but unused" in det, det
        ok, det = check(d, _SCOPE_BASE.replace("'./parse'", "'./parse2'"))
        assert not ok and "`module imports`" in det, det
        # the bound name of a destructuring rename is what must be used; words in a trailing comment are not names
        used_alias = _SCOPE_BASE.replace("{ parse }", "{ parse, extra: ex }").replace("parsePayload(cmd.payload)", "ex(parsePayload(cmd.payload))")
        ok, det = check(d, used_alias)
        assert ok and "import 1" in det, det
        ok, det = check(d, used_alias.replace("ex(parsePayload", "parsePayload").replace("payload));", "payload);"))
        assert not ok and "added but unused by target/helper ['ex']" in det, det
        ok, det = check(d, _SCOPE_BASE.replace("require('./parse');", "require('./parse'); // eslint-disable-line no-undef"))
        assert ok and "import 1" in det, det
        # python: `import M as A` binds A, `from M import n` binds n (M is a module path), a comment binds nothing; a renamed alias
        # and a changed module path stay outside; an import a spec.new_names constant uses counts as used
        pyb = "import os\nimport json as J  # noqa: E402\nfrom lib import load\n\n\ndef target(h):\n    return load(h)\n\n\ndef other():\n    return 1\n"

        def pcheck(text, sp=None):
            Path(d, "m.py").write_text(text)
            return fn_scope_check(d, {"target_fn": ["target"], **(sp or {})}, ["m.py"], {"m.py": pyb})
        ok, det = pcheck(pyb.replace("from lib import load\n", "from lib import load\nimport bloom_settings as BS  # noqa: E402\n"
                                     "from helpers.fmt import money\n").replace("return load(h)", "return money(BS.ids(load(h)))"))
        assert ok and "import 2" in det, det
        ok, det = pcheck(pyb.replace("from lib import load\n", "from lib import load\nfrom helpers.fmt import money, cents as c\n")
                         .replace("return load(h)", "return money(load(h))"))
        assert not ok and "added but unused by target/helper ['c']" in det, det
        ok, det = pcheck(pyb.replace("json as J", "json as JS"))
        assert not ok and "dropped/renamed ['J']" in det, det
        ok, det = pcheck(pyb.replace("from lib import", "from lib2 import"))
        assert not ok and "dropped/renamed ['lib']" in det, det
        ok, det = pcheck(pyb.replace("import os\n", "import os\nimport os.path\n").replace("return load(h)", "return load(os.path.basename(h))"))
        assert ok and "import 1" in det, det
        with_re = pyb.replace("from lib import load\n", "from lib import load\nimport re\n\nPATH_RE = re.compile(r'^/x/(\\d+)$')\n").replace(
            "return load(h)", "return load(PATH_RE.match(h))")
        ok, det = pcheck(with_re, {"new_names": ["PATH_RE"]})
        assert ok and "new_names 1" in det and "import 1" in det, det
        ok, det = pcheck(with_re)
        assert not ok and "module level" in det, det
        # python docstrings (2026-11-03 bench-libseed: the module docstring went stale because fn_scope refused a docstring edit
        # outside the target): a module/class/function docstring line is in scope; code next to it is not
        dpy = '"""Keep labels.\n\nOther labels are kept.\n"""\n' + pyb.replace("def other():\n", 'def other():\n    """One."""\n')
        def dcheck(text, base=dpy):
            Path(d, "m.py").write_text(text)
            return fn_scope_check(d, {"target_fn": ["target"], "fn_scope": "enforce"}, ["m.py"], {"m.py": base})
        ok, det = dcheck(dpy.replace("Other labels are kept.", "Other labels are kept; x is new.").replace('"""One."""', '"""Uno."""')
                         .replace("return load(h)", "return load(h or 'x')"))
        assert ok and "outside 0" in det, det
        ok, det = pcheck(dpy)  # docstrings added to the module and to another function: still in scope
        assert ok and "outside 0" in det, det
        ok, det = dcheck(dpy.replace("Other labels are kept.", "Other labels are kept; x is new.").replace("return 1", "return 2"))
        assert not ok and "`other`" in det, det
        ok, det = dcheck(dpy.replace('    """One."""\n', '    """One."""; import sys\n'))  # code on a docstring's line is code
        assert not ok and "`other`" in det, det
        assert _py_doc_lines(dpy) == frozenset({1, 2, 3, 4, 15}), sorted(_py_doc_lines(dpy))
        assert _import_facts(["from __future__ import annotations"], {1}, True) == (set(), {"__future__"})
        assert _import_facts(["import a.b.c, d as e  # x"], {1}, True) == ({"a", "e"}, {"a.b.c", "d"})
        assert _import_facts(["import x, { a as b } from 'm'; /* c */"], {1}) == ({"x", "b"}, {"m"})
        assert _import_facts(["import * as ns from 'm'"], {1}) == ({"ns"}, {"m"})
        Path(d, "m.py").unlink()
        # (c) a graph in the repo gives the same verdicts; stale (baseline differs from the graph's commit) falls back to text
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        Path(d, "poll.js").write_text(_SCOPE_BASE)
        for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", *c], cwd=d, check=True, capture_output=True, env=env)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
        nd = lambda i, lab, loc, call=True: {"id": i, "label": lab, "source_file": "poll.js", "source_location": loc, "_callable": call, "community": 1}
        nodes = [nd("poll", "poll.js", "L1", False), nd("poll_parsepayload", "parsePayload()", "L4"), nd("poll_patchlocal", "patchLocal()", "L8"),
                 nd("poll_unrelated", "unrelated()", "L12"), nd("poll_handlecommand", "handleCommand()", "L16")]
        links = [{"source": "poll_handlecommand", "target": "poll_parsepayload", "relation": "calls"},
                 {"source": "poll_handlecommand", "target": "poll_patchlocal", "relation": "calls"}]
        gd = Path(d, "graphify-out")
        gd.mkdir()
        (gd / "graph.json").write_text(json.dumps(dict(directed=False, nodes=nodes, links=links, built_at_commit=sha)))
        (gd / "manifest.json").write_text(json.dumps({"poll.js": {}}))
        verdicts = {}
        for label, sp in (("graph", {}), ("no-graph", {"graph": "/nonexistent/graph.json"})):
            verdicts[label] = [check(d, t, sp) for t in (ba, bb, bc, nb)]
            assert [v[0] for v in verdicts[label]] == [False, True, False, True], verdicts[label]
        assert "graph: fresh@" + sha[:7] in verdicts["graph"][1][1] and "graph: absent" in verdicts["no-graph"][1][1], verdicts
        assert [v[0] for v in verdicts["graph"]] == [v[0] for v in verdicts["no-graph"]]
        # stale: the baseline text the gate holds differs from the graph's commit -> text fallback, same verdicts
        sb = _SCOPE_BASE + "// edited after the graph was built\n"
        ok, det = check(d, sb.replace("return c.id;", "return c.id + 1;"), base=sb)
        assert not ok and "graph: stale" in det and "`unrelated`" in det, det
    print("self-test-scope ok")
    replay_old_scope_runs()
    return RC_OK


def replay_old_scope_runs():
    """Best effort: replay fn_scope_check on the old lock-error-fails-cmd worktrees/diffs when they still exist (never fails)."""
    root = Path(os.path.expanduser("~/.ollama-dispatch/mini-runs/lock-error-fails-cmd"))
    cands = sorted(glob.glob(str(root.parent / "rt-lock-error-fails-cmd-*"))) + sorted(glob.glob(str(root / "work")))
    try:
        spec = json.loads((root / "spec.json").read_text())
    except (OSError, ValueError):
        print("[self-test-scope] replay: spec.json gone, skipped")
        return
    spec = {**spec, "fn_scope": "warn"}
    done = False
    for wtd in cands:
        for f in as_list(spec.get("allowed")):
            if not Path(wtd, f).is_file():
                continue
            base = git(wtd, "show", f"HEAD:{f}")
            if not base:
                continue
            ok, det = fn_scope_check(wtd, spec, [f], {f: base})
            print(f"[self-test-scope] replay {wtd}: ok={ok} {det[:400]}")
            done = True
    if not done:
        print("[self-test-scope] replay: no old lock-error-fails-cmd worktree with a diff found, skipped")


def _self_test_nothink():
    """no_think: default off, per-phase resolution, argv carries the override only when on, and (mini venv present) the
    override really lands in the merged model_kwargs without touching the other kwargs."""
    assert not no_think_for({}, "A") and no_think_for({"no_think": True}, "B") and not no_think_for({"no_think": False}, "A")
    d = {"no_think": {"B": True, "single": True}}
    assert no_think_for(d, "B") and no_think_for(d, "single") and not no_think_for(d, "A")
    assert NO_THINK_CFG not in build_argv("m", "t", "/t.json", "/tmp", 5, 60)
    on = build_argv("m", "t", "/t.json", "/tmp", 5, 60, no_think=True)
    assert on[on.index(NO_THINK_CFG) - 1] == "-c" and on.index(NO_THINK_CFG) < on.index("--exit-immediately")
    py = Path(MINI).parent / "python"
    if py.exists():
        code = ("from minisweagent.config import get_config_from_spec as g\nfrom minisweagent.utils.serialize import recursive_merge as m\n"
                "import json,sys\nprint('JSON'+json.dumps(m(g('mini.yaml'), g(sys.argv[1]), g(sys.argv[2]))['model']['model_kwargs']))")
        out = subprocess.run([str(py), "-c", code, str(OVERLAY), NO_THINK_CFG], capture_output=True, text=True).stdout
        kw = json.loads(out.split("JSON", 1)[1])
        ct = kw["extra_body"]["chat_template_kwargs"]
        assert ct["enable_thinking"] is False and ct["preserve_thinking"] is True and kw["presence_penalty"] == 1.0 and kw["max_tokens"] == 12288, kw


def _synth_traj(path, steps):
    """steps = [(command, output)] -> a trajectory file in mini's message format."""
    msgs = []
    for i, (c, o) in enumerate(steps):
        tid = f"call_{i}"
        msgs.append({"role": "assistant", "content": "x", "extra": {"actions": [{"command": c, "tool_call_id": tid}]}})
        msgs.append({"role": "tool", "tool_call_id": tid, "content": o})
    Path(path).write_text(json.dumps({"messages": msgs}))


def _self_test_failfast():
    """Fail-fast rules on synthetic trajectories, then a stub run that is killed by the watcher."""
    spec = {"test_file": "t.test.js", "test_cmd": "node --test t.test.js", "allowed": ["calc.js"]}
    var = lambda n, k=0: [(f"cmd{i + k}", f"out{i + k}") for i in range(n)]
    with tempfile.TemporaryDirectory() as d:
        tj = Path(d, "t.json")
        _synth_traj(tj, var(3))
        assert traj_steps(tj) == var(3) and traj_steps(Path(d, "none.json")) == []
        # phase A: test file absent / ok; only from step 30 (no 'must have run' rule: accepted runs ran the test as late as step 18)
        assert failfast_reason(var(29), "A", spec, d) is None
        assert "no test written" in failfast_reason(var(30), "A", spec, d)
        Path(d, "t.test.js").write_text("// mine\n")
        assert failfast_reason(var(30), "A", spec, d) is None  # written, never run: not a fail-fast reason
        assert "no test written" in failfast_reason(var(30), "A", spec, d, {"t.test.js": "// mine\n"})  # unedited skeleton counts as not written
        assert failfast_reason(var(30), "A", spec, d, {"t.test.js": "// skel\n"}) is None
        # ran_test_cmd: lenient normalisation (cd prefix, python/python3, -m, flag order, basename + runner word); never a false 'not run'
        yes = [("cd . && node  --test t.test.js 2>&1 | tail", ""), ("cd sidecar && node --test-timeout=5 --test t.test.js", ""), ("node t.test.js", "")]
        assert all(ran_test_cmd([x], spec) for x in yes) and not ran_test_cmd(var(5) + [("cat t.test.js", "")], spec)
        ps = {"test_file": "tests/test_a.py", "test_cmd": "python3 -m pytest -x tests/test_a.py"}
        assert all(ran_test_cmd([(c, "")], ps) for c in ("python -m pytest tests/test_a.py -x", "cd /w && python -m pytest -q tests/test_a.py", "pytest tests/test_a.py", "python3 -m pytest -x tests/test_a.py"))
        assert not ran_test_cmd([("grep -n def tests/test_a.py", ""), ("ls", "")], ps)
        # replay every past mini-run trajectory (accepted or not): no prefix of any may trip the command-based rules
        runs = Path.home() / ".ollama-dispatch/mini-runs"
        for tj_ in sorted(runs.glob("*/*.traj.json")) if runs.is_dir() else []:
            m_ = re.search(r"\.(?:s\d+)?([AB])\d+\.traj", tj_.name)
            if not m_:
                continue
            st_ = traj_steps(tj_)
            for n_ in range(1, len(st_) + 1):
                assert failfast_reason(st_[:n_], m_.group(1), spec, d) is None, (str(tj_), n_)
        # phase B: an allowed file must differ from baseline by step 15
        Path(d, "calc.js").write_text("a\n")
        assert failfast_reason(var(14), "B", spec, d, {"calc.js": "a\n"}) is None
        assert "killed by fail-fast at step 15 (no allowed-file edit)" in failfast_reason(var(15), "B", spec, d, {"calc.js": "a\n"})
        assert failfast_reason(var(15), "B", spec, d, {"calc.js": "b\n"}) is None
        assert failfast_reason(var(15), "B", spec, d, {"missing.js": None}) is not None  # still absent = unchanged
        # either phase: 3 identical commands with identical output in a row (whitespace-insensitive command), otherwise fine
        same = [("nl -ba  poll.js | sed -n 217,280p", "L217")] * 3
        for ph in "AB":
            assert "3 times in a row" in failfast_reason(var(2) + same, ph, spec, d, {"calc.js": "b\n"} if ph == "B" else None)
            assert failfast_reason(var(2) + same[:2], ph, spec, d, {"calc.js": "b\n"} if ph == "B" else None) is None
            assert failfast_reason(var(2) + same[:2] + [("nl -ba poll.js | sed -n 217,280p", "CHANGED")], ph, spec, d, {"calc.js": "b\n"} if ph == "B" else None) is None
        # a stub run whose trajectory is stuck is killed by the watcher well before its 60 s sleep, and says why
        global MINI, POLL_SECS
        old = MINI, POLL_SECS
        stub = Path(d, "stub-mini")
        stub.write_text(STUB_MINI)
        stub.chmod(0o755)
        MINI, POLL_SECS = str(stub), 0.2
        try:
            _synth_traj(Path(d, "canned.json"), same)
            plan = Path(d, "plan.json")
            plan.write_text(json.dumps({"A": {}, "B": {}, "sleep": 60, "raw_traj": json.load(open(Path(d, "canned.json")))}))
            u = Usage()
            t0 = time.time()
            work = Path(d, "work")
            work.mkdir()
            log, fired = run_mini("m", "task", Path(d, "run.traj.json"), work, {**os.environ, "STUB_PLAN": str(plan)}, "", 120, u, tag="ff",
                                  watch=make_watch("A", spec, work))
            assert time.time() - t0 < 30 and "3 times in a row" in (u.trip or ""), (u.trip, time.time() - t0)
            plan.write_text(json.dumps({"A": {}, "B": {}, "sleep": 0}))
            run_mini("m", "task", Path(d, "run.traj.json"), work, {**os.environ, "STUB_PLAN": str(plan)}, "", 120, u, tag="ff", watch=make_watch("A", spec, work))
            assert u.trip is None  # reset per run
        finally:
            MINI, POLL_SECS = old
        # phase prompts carry the reason via `rejected`
        assert "3 times in a row" in phase_a_prompt({**spec, "min_tests": 2}, ["calc.js"], ["fail-fast: the same command ran 3 times in a row"])


def _self_test_ff_stage():
    """Phase B fail-fast: size-scaled step threshold, staged writes count as progress (replays of the three s2B1/s3B1/s3B2 kills), and
    stray-file cleanup between attempts."""
    AL = ["sidecar/src/x.js"]
    assert ff_fix_step({"a": "l\n" * 100}) == 15 and ff_fix_step({"a": "l\n" * 201}) == 25 and ff_fix_step({"a": "l\n" * 401}) == 35 and ff_fix_step({}) == 15
    # staged_write: in-place edits, staging toward the allowed file, long heredocs; not pure reads
    yes = ["sed -i '' 's/a/b/' sidecar/src/x.js", "cp sidecar/src/x.js sidecar/src/x.js.bak", "head -n 93 sidecar/src/x.js > /tmp/part1.js",
           "cat > /tmp/new.txt << 'E'\n" + "line => {\n" * 9 + "E", "python3 - <<'E'\nopen('sidecar/src/x.js','w').write('')\nE", "cat x | tee x.js"]
    no = ["nl -ba sidecar/src/x.js | sed -n '1,60p'", "node --test t.js 2>&1 | tail -5", "grep -n a sidecar/src/x.js > /dev/null", "cat sidecar/src/x.js 2>/dev/null; wc -l sidecar/src/x.js",
          "cat > /tmp/s.txt << 'E'\nshort\nE", "ls 2>&1"]
    assert all(staged_write(c, AL) for c in yes), [c for c in yes if not staged_write(c, AL)]
    assert not any(staged_write(c, AL) for c in no), [c for c in no if staged_write(c, AL)]
    with tempfile.TemporaryDirectory() as d:
        Path(d, "sidecar/src").mkdir(parents=True)
        big = "x\n" * 356
        Path(d, AL[0]).write_text(big)
        base = {AL[0]: big}
        reads = [(f"nl -ba sidecar/src/x.js | sed -n '{i},{i + 9}p'", f"o{i}") for i in range(40)]
        assert failfast_reason(reads[:24], "B", {}, d, base) is None  # 356 lines: not before step 25
        assert "killed by fail-fast at step 25" in failfast_reason(reads[:25], "B", {}, d, base)
        small = {AL[0]: "x\n" * 50}
        Path(d, AL[0]).write_text("x\n" * 50)
        assert "killed by fail-fast at step 15" in failfast_reason(reads[:15], "B", {}, d, small)
        Path(d, AL[0]).write_text(big)
        staged = reads[:12] + [("cp sidecar/src/x.js sidecar/src/x.js.bak", "")] + reads[13:34]
        assert failfast_reason(staged[:34], "B", {}, d, base) is None  # staged, within threshold + grace
        assert "never copied" in failfast_reason(reads[:12] + [("cp sidecar/src/x.js sidecar/src/x.js.bak", "")] + reads[13:35], "B", {}, d, base)
        Path(d, AL[0]).write_text(big + "y\n")
        assert failfast_reason(reads[:40], "B", {}, d, base) is None  # real change = progress
        Path(d, AL[0]).write_text(big)
        assert "3 times in a row" in failfast_reason([("ls", "o")] * 3, "B", {}, d, base)  # identical-command rule kept
        # replay the three real failures (s2B1, s3B1, s3B2: killed at step 15 before) and every other trajectory, with the file never changing
        runs = Path.home() / ".ollama-dispatch/mini-runs"
        for name in ("s2B1", "s3B1", "s3B2"):
            tj = runs / f"resell-egift-s1/work8.{name}.traj.json"
            if not tj.is_file():
                print(f"[mini-runner] self-test: {tj} absent, replay skipped", flush=True)
                continue
            st = traj_steps(tj)
            assert len(st) == 15 and any(staged_write(c, AL) for c, _ in st), name
            for n in range(1, len(st) + 1):
                assert failfast_reason(st[:n], "B", {}, d, base) is None, (name, n)
        for tj in sorted(runs.glob("*/*B[0-9].traj.json")) if runs.is_dir() else []:
            st = traj_steps(tj)
            for n in range(1, min(len(st), 24) + 1):  # <25 steps never trips on a 356-line file (except the identical-command rule, absent in real runs)
                r = failfast_reason(st[:n], "B", {}, d, base)
                assert r is None or "3 times" in r, (tj.name, n, r)
    # clean_new_untracked: only files new since the attempt start, never allowed/test/ignored/pre-existing
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        for c in (["init", "-q"],):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        (repo / "a.js").write_text("1\n")
        subprocess.run(["git", "add", "a.js"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"], cwd=repo, check=True, capture_output=True)
        (repo / "old.txt").write_text("keep")  # pre-existing untracked
        before = untracked_files(repo)
        (repo / "a.js.bak").write_text("x")
        (repo / "t.test.js").write_text("t")
        (repo / "newallowed.js").write_text("n")
        (repo / "sub").mkdir()
        (repo / "sub/s.bak").write_text("x")
        (repo / "node_modules").mkdir()
        (repo / "node_modules/m.js").write_text("m")
        gone = clean_new_untracked(repo, before, keep={"t.test.js", "newallowed.js"})
        assert gone == ["a.js.bak", "sub/s.bak"], gone
        assert (repo / "old.txt").exists() and (repo / "t.test.js").exists() and (repo / "newallowed.js").exists() and (repo / "node_modules/m.js").exists()
    # prompt: N from the same function, retry text names the cleanup
    with tempfile.TemporaryDirectory() as d:
        Path(d, "f.js").write_text("x\n" * 300)
        sp = {"test_file": "t.js", "test_cmd": "node t.js"}
        assert "unchanged at step 25" in phase_b_prompt(sp, ["f.js"], "T", cwd=d) and "unchanged at step 15" in phase_b_prompt(sp, ["f.js"], "T")
        pr = phase_b_prompt(sp, ["f.js"], "T", ["red"], cwd=d, cleaned=["f.js.bak"])
        assert "deleted these stray files" in pr and "f.js.bak" in pr and "unchanged at step 25" in pr and "in place" in pr
        assert "stray files" not in phase_b_prompt(sp, ["f.js"], "T", ["red"], cwd=d)
    assert "at most 60 lines" in READ_HINT
    with tempfile.TemporaryDirectory() as _gd:
        _env = os.environ.get("MINI_GRAPH_LOOKUP")
        try:
            os.environ.pop("MINI_GRAPH_LOOKUP", None)
            _gj = Path(_gd, "graph.json")
            assert graph_lookup_note(_gd, {}) == "" and graph_lookup_note(_gd, {"graph": str(_gj)}) == ""  # no graph: no prompt lines
            _gj.write_text("{}")
            _n = graph_lookup_note(_gd, {"graph": str(_gj)})
            assert "mini-graph" in _n and str(_gj) in _n and _n.count("\n") <= 3 and str(Path(os.path.realpath(__file__)).parent) in _n
            os.environ["MINI_GRAPH_LOOKUP"] = "off"
            assert graph_lookup_note(_gd, {"graph": str(_gj)}) == ""
        finally:
            os.environ.pop("MINI_GRAPH_LOOKUP", None)
            if _env is not None:
                os.environ["MINI_GRAPH_LOOKUP"] = _env


def _self_test_review2():
    """Splice indentation/strings/decorators, toplevel-extra, single-shot never deletes the frozen test and keeps RED gates, parse_check timeout,
    precheck test_file-in-allowed and bool-only no_think, mutation gate env handling."""
    # 1. splice: verbatim python method with a column-0 string; dedented reply; decorators; extra top-level statements
    py = 'class A:\n    def m(self):\n        s = """\nhello\n"""\n        return s + "x"\n    def n(self):\n        return 2\n'
    a, b = functions("a.py", py)["m"]
    for reply in ('    def m(self):\n        s = """\nhello\n"""\n        return s + "y"', 'def m(self):\n    s = """\nhello\n"""\n    return s + "y"'):
        out = ss_splice(py, a, b, reply, "a.py")
        assert out == py.replace('"x"', '"y"'), out  # same bytes either way: string body untouched, one uniform shift
        compile(out, "a", "exec")
    tq = "def f():\n    return 1\n"
    out = ss_splice(tq, 1, 2, 'def f():\n    return """a\n  b\n"""', "f.py")
    assert out == 'def f():\n    return """a\n  b\n"""\n', repr(out)  # col-0 reply, col-0 original: verbatim
    assert ss_splice("class C:\n    def f(self):\n        return 1\n", 2, 3, "        def f(self):\n            return 2", "c.py") == "class C:\n    def f(self):\n        return 2\n"  # over-indented reply
    if shutil.which("node"):
        js = "class V {\n  render() {\n    return `\n<b>x</b>\n`;\n  }\n}\nmodule.exports = { V };\n"
        fa, fb = functions("v.js", js)["render"]
        for rep_ in ("  render() {\n    return `\n<b>y</b>\n`;\n  }", "render() {\n  return `\n<b>y</b>\n`;\n}"):
            out = ss_splice(js, fa, fb, rep_, "v.js")
            assert out == js.replace("<b>x</b>", "<b>y</b>"), out
            with tempfile.TemporaryDirectory() as d:
                Path(d, "v.js").write_text(out)
                assert parse_check(d, "v.js") is None
    py3 = "import functools\nclass C:\n    @property\n    def v(self):\n        return 1\n    @functools.cache\n    def w(self):\n        return 3\n"
    a3, b3 = functions("c.py", py3)["v"]
    assert (a3, b3) == (3, 5) and functions("c.py", py3)["w"][0] == 6, functions("c.py", py3)  # span starts at the decorator
    for reply in ("@property\ndef v(self):\n    return 2", "    @property\n    def v(self):\n        return 2", "def v(self):\n    return 2"):  # decorator kept, never duplicated
        out = ss_splice(py3, a3, b3, reply, "c.py")
        assert out.count("@property") == 1 and "return 2" in out and "return 3" in out, out
        ns = {}
        exec(out, ns)
        assert ns["C"]().v == 2
    assert "@property" in ss_prompt({"test_file": "t.py", "test_cmd": "x"}, ("c.py", "v", 3, 5, ""), _tmp_with("c.py", py3), "T", "tt")
    # extra top-level statements are flagged
    ex = ss_toplevel_extra("def f(x):\n    return x + 1\nimport builtins\nbuiltins.len = lambda *a: 0\nOTHER = 5", "t.py", "f")
    assert len(ex) == 3 and "Import" in ex[0] and "OTHER" in ex[2], ex
    assert ss_toplevel_extra("    def f(self):\n        return 1", "t.py", "f") == [] and ss_toplevel_extra("@dec\ndef f():\n    '''doc'''\n    return 1", "t.py", "f") == []
    assert ss_toplevel_extra("// c\nfunction add(a,b){ return a+b }\nmodule.exports.sub = (a,b)=>a-b;", "x.js", "add")[0].startswith("<statement: module.exports.sub")
    assert ss_toplevel_extra("/** doc\n * x\n */\nfunction add(a,b){\n  return h(a,b);\n}", "x.js", "add") == []
    assert ss_toplevel_extra("  render() {\n    return 1;\n  }", "x.js", "render") == []
    # 3. single-shot never deletes the frozen test, even if (mis)listed in allowed; RED gates survive a non-green end; precheck rejects tf in allowed
    if shutil.which("node"):
        class U:
            rows, secs = [], 0
        with tempfile.TemporaryDirectory() as d:
            Path(d, "calc.js").write_text("function add(a, b) { return a - b; }\nmodule.exports = { add };\n")
            Path(d, "calc.test.js").write_text("const {add}=require('./calc');require('node:test')('t',()=>require('assert').equal(add(1,2),3));\n")
            base = {"calc.js": Path(d, "calc.js").read_text(), "calc.test.js": None}
            sp = {"target_fn": "add", "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js", "single_shot_retries": 0}
            res = single_shot_fix(d, sp, ["calc.js", "calc.test.js"], base, "sha", 1, set(), set(), (), "TASK", "t", {}, U(), "m", {}, time.time() + 600,
                                  call=lambda *a: {"error": "x"})
            assert res["fallback"] and Path(d, "calc.test.js").exists() and Path(d, "calc.js").read_text() == base["calc.js"]
            fix = {"content": "```js\nfunction add(a, b) { return a + b; }\n```", "finish_reason": "stop"}
            res = single_shot_fix(d, {**sp, "suite_cmd": "exit 1"}, ["calc.js"], {"calc.js": base["calc.js"]}, "sha", 1, set(), set(), (), "TASK", "t", {}, U(), "m", {},
                                  time.time() + 600, call=lambda *a: fix)
            assert not res["green"] and res["gates"] and any(not ok for ok, _ in res["gates"].values()), res  # RED gates kept for the caller
            assert Path(d, "calc.js").read_text() == base["calc.js"] and Path(d, "calc.test.js").exists()
    err, _ = precheck({"test_file": "t.js", "test_cmd": "true", "allowed": ["a.js", "t.js"]})
    assert any("listed in allowed" in e for e in err), err
    assert not any("listed in allowed" in e for e in precheck({"test_file": "t.js", "test_cmd": "true", "allowed": ["a.js"]})[0])
    assert any("no_think" in e for e in precheck({"test_file": "t.js", "test_cmd": "true", "allowed": ["a.js"], "no_think": {"A": "false"}})[0])
    assert not any("no_think" in e for e in precheck({"test_file": "t.js", "test_cmd": "true", "allowed": ["a.js"], "no_think": {"A": False, "single": True}})[0])
    # parse_check: a timeout rejects instead of raising
    if shutil.which("node"):
        real = subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 60)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.js").write_text("1\n")
            subprocess.run = boom
            try:
                assert "timed out" in parse_check(d, "x.js")
            finally:
                subprocess.run = real
    # 5. mutation gate: spec env, PYTHONDONTWRITEBYTECODE, no OPENAI_API_KEY
    with tempfile.TemporaryDirectory() as d:
        Path(d, "calc.js").write_text("function add(a, b) { return a - b; }\n")
        os.environ["OPENAI_API_KEY"] = "zz"
        try:
            ok, det, sc = run_mutation_gate(d, {"target_fn": "add", "test_file": "c.test.js", "test_cmd": "true", "env": {"FOO": 3},
                                                "mutation_gate": {"cmd": "echo MUTATION killed=$FOO survived=$PYTHONDONTWRITEBYTECODE${{OPENAI_API_KEY:-0}}"}}, ["calc.js"], 300)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        assert sc == 3 / 13, (det, sc)  # killed=3 survived=10: spec env passed, bytecode off, key stripped


def _tmp_with(name, text):
    d = tempfile.mkdtemp(prefix="mini-st-")
    Path(d, name).write_text(text)
    return d


def _self_test_slice():
    """fn_slice on JS and Python toys: the slice is exactly functions()' span, line numbers match the real file, caps and retry recompute."""
    js = ("'use strict';\nconst fs = require('fs');\nimport { a,\n  b } from './x.js';\n\nfunction other(x) {\n  return x;\n}\n\n"
          "async function target(p) {\n  const s = `}{`;\n  // }\n  if (!p) {\n    return 1;\n  }\n  return other(s);\n}\n\nconst last = () => 2;\n")
    py = "import os\nfrom a import b\n\ndef other(x):\n    return x\n\n\ndef target(p):\n    if not p:\n        return 1\n    return other(p)\n"
    big = "const z = require('z');\nfunction target(p) {\n" + "".join(f"  step{i}();\n" for i in range(300)) + "  ANCHOR_HERE();\n" + "".join(f"  more{i}();\n" for i in range(200)) + "}\n"
    parse = lambda txt: [(int(m.group(1)), m.group(2)) for m in re.finditer(r"^ *(\d+)\t(.*)$", txt, re.M)]
    with tempfile.TemporaryDirectory() as d:
        for name, src in (("a.js", js), ("a.py", py), ("big.js", big)):
            Path(d, name).write_text(src)
        for name, src in (("a.js", js), ("a.py", py)):
            sl = fn_slice(d, [name], "target")
            real = src.splitlines()
            nums = parse(sl)
            assert nums and all(real[n - 1] == t for n, t in nums), (name, sl)  # line numbers match the real file
            a, b = functions(name, src)["target"]
            body = [n for n, _ in nums if a <= n <= b]
            assert body == list(range(a, b + 1)), (name, body, a, b)  # the target function in full, equal to functions()' span
            assert f"current lines {a}-{b} shown; edit the real file `{name}`; after an edit view only the changed range." in sl
            assert "other:" in sl.split("Other functions")[1] and "target:" not in sl.split("Other functions")[1], sl
            assert ("require('fs')" in sl and "from './x.js'" in sl and "'use strict'" in sl) if name == "a.js" else ("import os" in sl and "from a import b" in sl)
            assert "return x;" not in sl.split("Other functions")[0].split("current lines")[1] if name == "a.js" else True  # other fn bodies are not shown
        assert fn_slice(d, ["a.js"], "nope") == "" and fn_slice(d, ["a.js"], ["other", "target"]).count("current lines") == 2
        a, b = functions("big.js", big)["target"]
        n0 = [n for n, _ in parse(fn_slice(d, ["big.js"], "target"))]
        assert [n for n in n0 if a <= n <= b] == list(range(a, a + 150)), "capped at 150 lines from the start"
        sl = fn_slice(d, ["big.js"], "target", ["ANCHOR_HERE"])
        al = big.splitlines().index("  ANCHOR_HERE();") + 1
        n1 = [n for n in (n for n, _ in parse(sl)) if a <= n <= b]
        assert al in n1 and len(n1) <= 121 and n1 == list(range(n1[0], n1[-1] + 1)) and f"function spans {a}-{b}" in sl, (al, n1[:2], n1[-2:])
        before = fn_slice(d, ["a.js"], "target")
        Path(d, "a.js").write_text("// new first line\n" + js)  # a retry sees the file as the last attempt left it
        after = fn_slice(d, ["a.js"], "target")
        assert before != after and "// new first line" not in after and f"{functions('a.js', js)['target'][0] + 1}\t" in after
        sp = {"test_file": "t.js", "test_cmd": "node --test t.js", "target_fn": "target", "reach_anchor": "ANCHOR_HERE"}
        assert "CURRENT CODE (slice)" in phase_a_prompt(sp, ["big.js"], cwd=d) and "CURRENT CODE (slice)" in phase_b_prompt(sp, ["big.js"], "T", cwd=d)
        assert "CURRENT CODE" not in phase_a_prompt(sp, ["big.js"]) and "CURRENT CODE" not in phase_b_prompt({**sp, "target_fn": None}, ["big.js"], "T", cwd=d)


def _self_test_exit_status():
    with tempfile.TemporaryDirectory() as d:
        for ex in ("TimeExceeded", "LimitsExceeded", "Submitted"):
            Path(d, ex).write_text(json.dumps({"messages": [{"role": "assistant"}, {"role": "exit", "extra": {"exit_status": ex}}]}))
            assert exit_status(Path(d, ex)) == ex
        assert exit_status(Path(d, "none")) is None and set(LIMIT_WHY) == {"TimeExceeded", "LimitsExceeded"}


def _self_test_jsend():
    """functions()/_js_end on the JS shapes that used to break: destructured/default params, regex literals with quotes, expression-body
    arrows, multi-line imports, and the unknowable-end fallback in fn_slice."""
    def end(src, name):
        return functions("a.js", src)[name]
    e = end("function a({ x, y } = {}, z = { k: 1 }) {\n  return x;\n}\nfunction b() {\n  return 1;\n}\n", "a")
    assert e == (1, 3), e
    e = end("const c = ({ p, q }) => {\n  return p;\n};\nfunction d() {\n  return 1;\n}\n", "c")
    assert e == (1, 3), e
    e = end("function r(s) {\n  const m = s.match(/['\"]+/g);\n  return /}\"/.test(s) ? m : null;\n}\nfunction t() {\n  return 1;\n}\n", "r")
    assert e == (1, 4), e
    e = end("const f = (a) => a + 1;\nfunction g() {\n  return 2;\n}\n", "f")
    assert e == (1, 1), e
    e = end("const h = x => ({ v: x,\n  w: 2 });\nfunction g() {\n  return 2;\n}\n", "h")
    assert e == (1, 2), e
    e = end("const h = x =>\n  x * 2;\nconst o = { a: 1 };\nfunction g() {\n  return 2;\n}\n", "h")
    assert e == (1, 2), e
    e = end("function g({ a = '}' }) {\n  return `}`; // }\n}\n", "g")
    assert e == (1, 3), e
    # multi-line imports are included through the closing line
    src = "import {\n  a,\n  b,\n} from './x.js';\nimport d,\n  { e } from 'y';\nconst { z } = require(\n  'z'\n);\nfunction target() {\n  return 1;\n}\n"
    il = _import_lines(src.splitlines(), 9)
    assert [n for n, _ in il] == [1, 2, 3, 4, 5, 6, 7, 8, 9], il
    # unknowable end (unbalanced): fn_slice shows 60 lines from the start and says so
    with tempfile.TemporaryDirectory() as d:
        bad = "function target(p) {\n" + "".join(f"  s{i}();\n" for i in range(100)) + "  if (x) {\n"
        Path(d, "bad.js").write_text(bad)
        assert functions("bad.js", bad)["target"] == (1, None)
        sl = fn_slice(d, ["bad.js"], "target")
        assert "could not find where the function ends; showing 60 lines" in sl and "   60\t" in sl and "   61\t" not in sl, sl[:400]


def _self_test_ctx():
    """mini_ctx (overlay model_class) in the mini venv: its own self-test, the overlay wiring, and the LAUNCH sys.path entry."""
    assert "@MINI_EXT@" not in " ".join(launch_argv(["/nonexistent"])) and str(MINI_EXT) in " ".join(launch_argv(["/nonexistent"]))
    assert "PYTHONPATH" not in LAUNCH
    ov = OVERLAY.read_text()
    assert re.search(r"^  model_class: mini_ctx\.CtxLitellmModel$", ov, re.M) and re.search(r"^  strip_old_thinking: false$", ov, re.M)
    py = Path(MINI).resolve().parent / "python"
    if not py.exists():
        print("[self-test] mini venv absent: mini_ctx skipped")
        return
    r = subprocess.run([str(py), "-c", "import sys; sys.path.insert(0, %r); import mini_ctx, yaml; from minisweagent.models import get_model_class; "
                        "assert get_model_class('x', yaml.safe_load(open(%r))['model']['model_class']) is mini_ctx.CtxLitellmModel; mini_ctx.self_test()"
                        % (str(MINI_EXT), str(OVERLAY))], capture_output=True, text=True, env={**os.environ, "MSWEA_SILENT_STARTUP": "1"})
    assert r.returncode == 0 and "mini_ctx self-test ok" in r.stdout, r.stdout[-500:] + r.stderr[-1500:]


def _self_test_review_fixes():
    """Shell-syntax test_cmds, chain file guard, whole-chain precheck in run_steps, per-step task, lazy skeleton imports."""
    import contextlib, io, types
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d, "repo")
        (repo / "src").mkdir(parents=True)
        (repo / "src/text.py").write_text("def trunc(s):\n    return s\n")
        for c in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        base = {"allowed": ["src/text.py"], "baseline_from": str(repo), "test_file": "src/test_text.py", "target_fn": "trunc"}
        # 1. shell syntax is not a command; unresolvable heads are WARN, never FAIL
        for tc in ["(cd src && true)", "for f in a b; do true; done", "if true; then true; fi", "! grep -q zzz src/text.py",
                   "$PYTHON -m pytest -q", 'cd "$(git rev-parse --show-toplevel)" && true', "{ true; }", "while false; do true; done"]:
            err, warn = precheck({**base, "test_cmd": tc})
            assert err == [], (tc, err)
        err, warn = precheck({**base, "test_cmd": "$PYTHON -m pytest -q"})
        assert any("cannot be resolved" in w for w in warn), warn
        err, _ = precheck({**base, "test_cmd": "(cd src && nosuchbin-xyz --t)"})
        assert any("nosuchbin-xyz" in e for e in err), err  # real missing binaries inside a subshell still fail
        err, _ = precheck({**base, "test_cmd": "for f in a; do nosuchbin-xyz $f; done"})
        assert any("nosuchbin-xyz" in e for e in err), err
        # 2. earlier steps' untracked files are guarded
        w = Path(d, "w")
        (w / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=w, check=True)
        (w / "src/test_a.py").write_text("one")
        (w / "src/new_a.py").write_text("x")
        g = guard_hashes(w, "src/test_a.py", ["src/new_a.py", "src/text.py"])
        assert set(g) == {"src/test_a.py", "src/new_a.py"}, g
        assert guard_violations(w, {"_guard": g}, "src/test_b.py", ["src/text.py"]) == []
        (w / "src/test_a.py").write_text("two")
        assert guard_violations(w, {"_guard": g}, "src/test_b.py", ["src/text.py"]) == ["src/test_a.py"]
        assert guard_violations(w, {"_guard": g}, "src/test_b.py", ["src/test_a.py"]) == []  # exempt when this step owns it
        (w / "src/new_a.py").unlink()
        assert "src/new_a.py" in guard_violations(w, {"_guard": g}, "src/test_b.py", ["src/text.py"])
        # 4. a step without its own task is not hidden by a top-level task
        ok_step = {"allowed": ["src/text.py"], "test_file": "src/test_a.py", "test_cmd": "true", "task": "do a thing"}
        raw = [ok_step, {**ok_step, "test_file": "src/test_b.py", "task": None}]
        raw[1].pop("task")
        spec = {**base, "task": "shared task text", "test_cmd": "true", "steps": raw}
        bad = validate_steps(merged_steps(spec), spec["steps"])
        assert any("step-2" in b and "own 'task'" in b for b in bad), bad
        assert validate_steps(merged_steps({**spec, "steps": [ok_step, {**ok_step, "test_file": "src/test_b.py"}]}), [ok_step, {**ok_step, "test_file": "src/test_b.py"}]) == []
        err, _ = precheck_all(spec)
        assert any("step-2" in e and "own 'task'" in e for e in err), err
        # 3. run_steps runs the whole-chain precheck before any step: step 2's test_file dir is missing -> fails with no step started
        chain = {**base, "test_cmd": "true", "steps": [ok_step, {**ok_step, "test_file": "nodir/test_b.py", "task": "second"}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_steps(types.SimpleNamespace(cwd=str(Path(d, "wk")), try_test=None), chain, {}, check_only=True)
        out = buf.getvalue()
        assert rc == RC_FAIL and "RESULT FAIL bad-spec" in out and "=== step" not in out, out
    # lazy skeleton imports: nothing from the allowed files is imported at module top level
    with tempfile.TemporaryDirectory() as d:
        txt = skeleton(d, {"test_file": "test_m.py"}, ["pkg_missing/m.py"])
        assert not re.search(r"^import pkg_missing", txt, re.M) and "    import pkg_missing.m" in txt, txt
        Path(d, "t.py").write_text(txt)
        r = subprocess.run([sys.executable, str(Path(d, "t.py"))], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr  # importing the skeleton works without the target's deps
        js = skeleton(d, {"test_file": "a.test.js"}, ["a.js"])
        assert not re.search(r"^(const \w+ = require\('\./a|import )", js.split("const load0")[0] + "\n", re.M) and "const load0 = () => require('./a.js');" in js, js


def _self_test_collect_static():
    """collect_check's model-free parts: which suite commands it runs, which test texts trigger it."""
    for c in (".venv/bin/python -m pytest -q -p no:cacheprovider", "cd test && ../.venv/bin/python -m pytest -q", "pytest -x tests",
              "python3.12 -m pytest tests/unit -k 'not slow'", "py.test"):
        assert PYTEST_SUITE_RE.match(c), c
    for c in ("tox -e py", "make test", "pytest | tee log", "python -m pytest; echo done", "pytest $(cat files)", "npm test"):
        assert not PYTEST_SUITE_RE.match(c), c
    calls = []
    real = globals()["suite_collect_errors"]
    globals()["suite_collect_errors"] = lambda cwd, spec: calls.append(Path(cwd, "tests/t.py").exists()) or {}
    try:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "tests").mkdir()
            Path(d, "tests/t.py").write_bytes(b"x = 1\r\n")
            assert collect_check(d, {}, "tests/t.py", "import sys\nsys.path.insert(0, 'x')\n") is None and not calls  # no suite_cmd
            sp = {"suite_cmd": "pytest -q"}
            root_only = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent / '..'))\n"
            assert collect_check(d, sp, "tests/t.py", root_only) is None and not calls  # the skeleton's repo-root line alone: no collect
            assert collect_check(d, sp, "tests/t.js", "sys.path") is None and not calls  # not python
            assert collect_check(d, sp, "tests/t.py", "import os\nos.chdir('/tmp')\n") is None and calls == [False, True]  # base without it
            assert Path(d, "tests/t.py").read_bytes() == b"x = 1\r\n"  # restored byte for byte
            assert collect_check(d, sp, "tests/t.py", "import sys\nsys.modules['a'] = 1\n") is None and len(calls) == 3  # base cached
    finally:
        globals()["suite_collect_errors"] = real
    assert collect_cmd({"suite_cmd": "pytest -q -x tests --maxfail=2"}) == "pytest -q tests --collect-only"
    assert collect_cmd({"suite_cmd": "pytest -k 'a -x' tests"}) == "pytest -k 'a -x' tests --collect-only"
    assert collect_cmd({"suite_cmd": "make test"}) is None and collect_cmd({}) is None
    out = ("tests/test_new.py::test_new\n\n" + "=" * 20 + " ERRORS " + "=" * 20 + "\n"
           "_____ ERROR collecting tests/test_other.py _____\n"
           "ImportError while importing test module '/w/tests/test_other.py'.\nTraceback:\ntests/test_other.py:1: in <module>\n"
           "    from colorsys import rgb_to_hsv\nE   ImportError: cannot import name 'rgb_to_hsv' from 'colorsys' (/w/pkg/sub/colorsys.py)\n"
           "_ ERROR collecting tests/test_b.py _\nE   SyntaxError: first\nE   SyntaxError: bad\n"
           + "=" * 10 + " short test summary info " + "=" * 10 + "\nERROR tests/test_other.py\nERROR tests/test_b.py\n"
           "ERROR tests/test_c.py - ValueError: boom\n!!!! Interrupted: 3 errors during collection !!!!\n")
    r = collect_reasons(out, "/w")
    assert r == {"tests/test_other.py": "ImportError: cannot import name 'rgb_to_hsv' from 'colorsys' (pkg/sub/colorsys.py)",
                 "tests/test_b.py": "SyntaxError: bad", "tests/test_c.py": "ValueError: boom"}, r
    assert collect_reasons("1 test collected in 0.01s\n") == {}


def _self_test_collect():
    """collect_check end to end with pytest: a module-level sys.path insert that shadows a stdlib module for the files collected
    after the test is rejected (conan1 r2), so is a test pytest cannot import; an import-state change that breaks nothing passes."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "pkg/sub").mkdir(parents=True)
        (root / "pkg/__init__.py").write_text("")
        (root / "pkg/sub/__init__.py").write_text("")
        (root / "pkg/sub/colorsys.py").write_text("SHADOW = 1\n")
        (root / "pkg/sub/m.py").write_text("def f():\n    return 1\n")
        (root / "tests").mkdir()
        (root / "tests/test_other.py").write_text("from colorsys import rgb_to_hsv\n\n\ndef test_other():\n    assert rgb_to_hsv(0, 0, 0)[2] == 0\n")
        tf = "tests/test_new.py"
        spec = {"suite_cmd": f"{shlex.quote(sys.executable)} -m pytest -q -x -p no:cacheprovider tests", "test_file": tf, "cmd_timeout": 120}
        bad = ("import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parent / '../pkg/sub'))\n\n\n"
               "def test_new():\n    import m\n    assert m.f() == 1\n")
        (root / tf).write_text(bad)
        p = collect_check(root, spec, tf, bad)
        assert p and "tests/test_other.py" in p and "1 other test file" in p and "ImportError" in p, p
        assert "pkg/sub/colorsys.py" in p and d not in p and " -x " not in p, p  # the shadow, relative; the command really run
        assert spec["_collect_base"] == {} and (root / tf).read_text() == bad, spec["_collect_base"]  # the file is back
        ok = ("import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n\n\n"
              "def test_new():\n    import pkg.sub.m\n    assert pkg.sub.m.f() == 1\n")
        (root / tf).write_text(ok)
        assert collect_check(root, spec, tf, ok) is None
        own = "import sys\nsys.modules.pop('nothing', None)\nimport no_such_module_xyz  # noqa\n\n\ndef test_new():\n    assert 1 == 2\n"
        (root / tf).write_text(own)
        p = collect_check(root, spec, tf, own)
        assert p and p.startswith(f"pytest cannot import `{tf}`") and "no_such_module_xyz" in p, p


def _self_test_skeleton():
    """Phase A skeleton per language: right import path/extension, never accepted unedited, filled version passes static gates."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "lib").mkdir()
        (root / "pkg/sub").mkdir(parents=True)
        cases = {  # name: (spec, expected substrings, forbidden substrings)
            "node-js": ({"test_file": "calc.test.js", "test_cmd": "node --test calc.test.js", "target_fn": "add"}, ["calc.js"],
                        ["const { test } = require('node:test');", "const assert = require('assert');", "require('./calc.js')"], ["import "]),
            "node-ts": ({"test_file": "lib/circuit.test.ts", "test_cmd": "node --test lib/circuit.test.ts", "target_fn": ["circuitStatus"]}, ["lib/circuit.ts"],
                        ["require('./circuit.ts')", "const { test } = require('node:test');"], ["circuit.js'"]),
            "node-up": ({"test_file": "sidecar/t/x.test.js", "test_cmd": "node --test"}, ["sidecar/src/a.js"], ["require('../src/a.js')"], []),
            "python": ({"test_file": "pkg/sub/test_m.py", "test_cmd": "python3 -m pytest -q test_m.py", "target_fn": "f"}, ["pkg/sub/m.py", "pkg/other.py"],
                        ["import m\n", "sys.path.insert(0, str(Path(__file__).resolve().parent / '..'))", "import other\n", "def test_todo_case_2"], ["calc"]),
            "python-pkg": ({"test_file": "test_m.py", "test_cmd": "python3 -m pytest -q test_m.py"}, ["pkg/sub/m.py"], ["import pkg.sub.m\n"], []),
            # conan1 r2: a file in a regular package outside the test dir imports by its package path from the import root,
            # never with its own directory (../conan/internal/model, whose profile.py shadowed the stdlib) on sys.path
            "python-deep": ({"test_file": "test/test_c.py", "test_cmd": "cd test && python3 test_c.py"}, ["conan/internal/model/cpp_info.py"],
                            ["sys.path.insert(0, str(Path(__file__).resolve().parent / '..'))", "import conan.internal.model.cpp_info\n",
                             "_load_conan_internal_model_cpp_info()"], ["conan/internal/model'", "import cpp_info"]),
            "python-src": ({"test_file": "tests/test_m.py", "test_cmd": "python3 -m pytest -q tests/test_m.py"}, ["src/spkg/m.py"],
                           ["sys.path.insert(0, str(Path(__file__).resolve().parent / '../src'))", "import spkg.m\n"], ["src/spkg'", "import m\n"]),
        }
        for pk in ("conan", "conan/internal", "conan/internal/model", "src/spkg"):
            (root / pk).mkdir(parents=True, exist_ok=True)
            (root / pk / "__init__.py").write_text("")
        assert _py_import_root(root, "conan/internal/model/cpp_info.py") == (".", "conan.internal.model.cpp_info")
        assert _py_import_root(root, "conan/__init__.py") == (".", "conan")
        assert _py_import_root(root, "src/spkg/m.py") == ("src", "spkg.m")
        assert _py_import_root(root, "lib/x.py") == ("lib", "x") and _py_import_root(root, "top.py") == (".", "top")
        for name, (sp, allowed, want, nope) in cases.items():
            txt = skeleton(root, sp, allowed)
            assert txt and all(w in txt for w in want) and not any(w in txt for w in nope), (name, txt)
            assert SKELETON_MARK in txt and txt.count(SKELETON_MARK) >= 2, name
            (root / sp["test_file"]).parent.mkdir(parents=True, exist_ok=True)
            (root / sp["test_file"]).write_text(txt)
            bad, _ = test_gate(root, {**sp, "allowed": allowed}, allowed)  # unedited skeleton: rejected before anything runs
            assert bad and "skeleton placeholder" in bad[0], (name, bad)
            # the static gates alone also reject it (no assertions), marker or not
            assert test_static(txt.replace(SKELETON_MARK, "NOTE"), allowed, 2, sp["test_file"], root), name
        # ESM package: import syntax, namespace import
        (root / "esm").mkdir()
        (root / "esm/package.json").write_text('{"type": "module"}')
        txt = skeleton(root, {"test_file": "esm/a.test.js"}, ["esm/a.js"])
        assert "import { test } from 'node:test';" in txt and "const load0 = () => import('./a.js');" in txt and "async () =>" in txt, txt
        # no "type": "module", but the tests next to it import node:test: the template follows them (resell-tracker); a CJS
        # neighbour or a dir whose files mix both styles leaves it CommonJS
        (root / "tsapp/lib").mkdir(parents=True)
        (root / "tsapp/package.json").write_text('{"name": "tsapp"}')
        (root / "tsapp/lib/old.test.ts").write_text("import { test } from 'node:test';\nimport { f } from './old.ts';\n")
        txt = skeleton(root, {"test_file": "tsapp/lib/new.test.ts"}, ["tsapp/lib/new.ts"])
        assert "import { test } from 'node:test';" in txt and "const load0 = () => import('./new.ts');" in txt, txt
        (root / "tsapp/lib/cjs.test.ts").write_text("const { test } = require('node:test');\n")
        (root / "tsapp/lib/cjs2.test.ts").write_text("const assert = require('assert');\nmodule.exports = {};\n")
        assert "require('node:test')" in skeleton(root, {"test_file": "tsapp/lib/new.test.ts"}, ["tsapp/lib/new.ts"])
        assert skeleton(root, {"test_file": "x.rb"}, ["a.rb"]) is None
        # a filled skeleton passes the static gate
        filled = skeleton(root, cases["node-js"][0], ["calc.js"]).split("test('TODO")[0]
        filled += "test('a', () => { assert.strictEqual(load0().add(1, 2), 3); });\ntest('b', () => { assert.strictEqual(load0().add(2, 2), 4); });\n"
        assert test_static(filled, ["calc.js"], 2, "calc.test.js", root) == [], test_static(filled, ["calc.js"], 2, "calc.test.js", root)
        fpy = skeleton(root, cases["python"][0], ["pkg/sub/m.py"]).split("\n\ndef test_todo")[0]
        fpy += "\n\ndef test_a():\n    assert _load_m().f(1) == 2\n\ndef test_b():\n    assert _load_m().f(2) == 3\n"
        assert test_static(fpy, ["pkg/sub/m.py"], 2, "pkg/sub/test_m.py", root) == [], test_static(fpy, ["pkg/sub/m.py"], 2, "pkg/sub/test_m.py", root)
        # a module file whose name is not an identifier (bench-dupes5 arr-webhook.py): the skeleton parses, its loader really
        # loads the file, and a test filled in through that loader imports the code under test as far as the gate can tell
        (root / "arr-hook.py").write_text("def f(x):\n    return x + 1\n")
        sp = {"test_file": "test_arr_hook.py", "test_cmd": "python3 -m pytest -q test_arr_hook.py", "target_fn": "f"}
        hy = skeleton(root, sp, ["arr-hook.py"])
        ast.parse(hy)
        assert "def _load_arr_hook():" in hy and "importlib.import_module('arr-hook')" in hy and "_load_arr_hook()" in hy.split("def test_todo_case_1")[1], hy
        hy = hy.split("\n\ndef test_todo")[0] + "\n\ndef test_a():\n    assert _load_arr_hook().f(1) == 2\n\ndef test_b():\n    assert _load_arr_hook().f(2) == 3\n"
        assert test_static(hy, ["arr-hook.py"], 2, sp["test_file"], root) == [], test_static(hy, ["arr-hook.py"], 2, sp["test_file"], root)
        (root / sp["test_file"]).write_text(hy + "\ntest_a()\ntest_b()\nprint('HYPHEN-OK')\n")
        r = subprocess.run([sys.executable, sp["test_file"]], cwd=root, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "HYPHEN-OK" in r.stdout, (r.stdout, r.stderr)
        ft = frozen_template(root, sp, ["arr-hook.py"])
        ast.parse(ft)
        assert "_load_arr_hook().f(...)" in ft and "importlib.import_module('arr-hook')" in ft, ft
    assert "SKELETON" in phase_a_prompt({"test_file": "t.py", "test_cmd": "pytest"}, ["a.py"], skel=True)
    assert "SKELETON" not in phase_a_prompt({"test_file": "t.py", "test_cmd": "pytest"}, ["a.py"])
    pa = phase_a_prompt({"test_file": "t.py", "test_cmd": "pytest"}, ["a.py"])  # phase A states its deadline (FF_TEST_STEP) like phase B
    assert f"Budget: {FF_TEST_STEP} commands" in pa and f"by command {FF_TEST_STEP // 3} " in pa and "HOW to fix" in pa, pa
    assert "Test only the cases TASK.md describes" in pa and "never assert a new result for one" in pa, pa  # moto r2: no unasked cases
    assert ("at least one test feeds that input (or a smaller one with the same bug) through the public function" in pa
            and "may come in addition, never instead" in pa), pa  # isort r3: the user's view, not an internal dict
    assert "Budget: 20 commands" in phase_a_prompt({"test_file": "t.py", "test_cmd": "pytest", "test_steps": 20}, ["a.py"])
    assert f"Budget: {FF_TEST_STEP} commands" in phase_a_prompt({"test_file": "t.py", "test_cmd": "pytest", "test_steps": 60}, ["a.py"])


def _self_test_precheck():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d, "repo")
        (repo / "src").mkdir(parents=True)
        (repo / "src/text.py").write_text("def trunc(s):\n    return s\n")
        (repo / "TASK.md").write_text("# T\n`src/nothere.py` exists and is used.\n")
        for c in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        first = git(repo, "rev-parse", "HEAD").strip()
        (repo / "x").write_text("1")
        for c in (["add", "x"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD").strip()], cwd=repo, check=True)
        subprocess.run(["git", "reset", "-q", "--hard", first], cwd=repo, check=True)
        py = f"cd src && {sys.executable} -m pytest -q test_text.py"
        good = {"allowed": ["src/text.py"], "baseline_from": str(repo), "test_file": "src/test_text.py", "test_cmd": py, "target_fn": "trunc"}
        err, warn = precheck(good)
        has_pytest = subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode == 0
        assert (err == [] if has_pytest else all("pytest" in e for e in err)), err
        assert any("behind origin/main" in w for w in warn), warn  # warn only
        bad = [({"test_cmd": "cd src && nosuchbin-xyz --test t.js"}, "nosuchbin-xyz"),
               ({"test_cmd": "cd nodir && python3 -m pytest"}, "nodir"),
               ({"target_fn": ["trunc", "missing_fn"]}, "missing_fn"),
               ({"test_file": "nodir/test_text.py"}, "test_file directory"),
               ({"baseline_from": str(d)}, "not a git worktree"),
               ({"mutation_gate": {"fail_below": 2}}, "0..1"), ({"mutation_gate": {"tool": "pit"}}, "mutation_gate.tool"),
               ({"mutation_gate": {"bogus": 1}}, "unknown keys"), ({"mutation_gate": "yes"}, "mutation_gate must be"),
               ({"test_cmd": f"{sys.executable} -m no_such_module_xyz"}, "no_such_module_xyz")]
        for patch, want in bad:
            err, _ = precheck({**good, **patch})
            assert any(want in e for e in err), (patch, err)
        prep = Path(d, "w")
        prep.mkdir()
        prepare(prep, good)
        err, _ = precheck(good, prep)
        assert any("preflight" in e and "src/nothere.py" in e for e in err), err
        # minimal queue PATH: a runtime missing there is reported, not discovered mid-run
        err, _ = precheck({**good, "env": {"PATH": "/nonexistent"}, "test_cmd": "cd src && node --test t.js"})
        assert any("`node` not found" in e for e in err), err
        # wrapper: exits non-zero with the reasons, no queue touched
        sj = Path(d, "spec.json")
        sj.write_text(json.dumps({**good, "target_fn": "missing_fn"}))
        w = Path(__file__).resolve().parent / "mini-precheck"
        if w.exists():
            r = subprocess.run([sys.executable, str(w), str(sj)], capture_output=True, text=True,
                               env={**os.environ, "MINI_RUNNER": str(Path(__file__).resolve())})
            assert r.returncode != 0 and "missing_fn" in r.stdout + r.stderr, (r.returncode, r.stdout, r.stderr)


EARLY_JS = ("function scale(x) {\n  if (x === null) return 0;\n  const a = x * 2;\n  const b = a + 1;\n  const c = b - 1;\n"
            "  const d = c + 0;\n  const e = d * 1;\n  const f = e - 0;\n  return f;\n}\nmodule.exports = { scale };\n")
EARLY_JS_T_GUARD = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { scale } = require('./calc');\n"
                    "test('null', () => { assert.strictEqual(scale(null), 1); });\ntest('null2', () => { assert.strictEqual(scale(null), 1); });\n")
EARLY_JS_T_FULL = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { scale } = require('./calc');\n"
                   "test('two', () => { assert.strictEqual(scale(2), 5); });\ntest('three', () => { assert.strictEqual(scale(3), 7); });\n")
EARLY_PY = ("def scale(x):\n    if x is None:\n        return 0\n    a = x * 2\n    b = a + 1\n    c = b - 1\n    d = c + 0\n"
            "    e = d * 1\n    f = e - 0\n    return f\n")
# loguru r3 shape: the bug path (a KeyError from format_map, line 7, into the generic except, line 24) is one of five alternative
# branches, so a test of it runs 6 of the 21 body lines; line 3 is an early return the diagnosis does not cite
EMIT_PY = ("def emit(rec, fmt, kind):\n    if rec is None:\n        return 'none'\n    out = []\n    try:\n        if kind == 1:\n"
           "            s = fmt.format_map(rec)\n            out.append(s)\n        elif kind == 2:\n            s = fmt.upper().format_map(rec)\n"
           "            out.append(s)\n        elif kind == 3:\n            s = fmt.lower().format_map(rec)\n            out.append(s)\n"
           "        elif kind == 4:\n            s = fmt.strip().format_map(rec)\n            out.append(s)\n        else:\n"
           "            s = fmt.title().format_map(\n                rec)\n            out.append(s)\n        out.append('ok')\n"
           "        return ' '.join(out)\n    except Exception:\n        return 'error'\n")
JS_HEAD = "const test = require('node:test');\nconst { add } = require('./calc');\n"
PY_HEAD = "import calc\nfrom calc import add\n\n"
EVASIONS = {  # (language, body appended to a head that has no other asserts, expected problem)
    "js aliased ok(true)": ("node", "const a = require('node:assert');\ntest('x', () => { a.ok(true); a.strictEqual(add(1, 2), 3); a.strictEqual(add(2, 2), 4); });\n", "vacuous"),
    "js destructured ok(true)": ("node", "const { ok, equal } = require('assert');\ntest('x', () => { ok(true); equal(add(1, 2), 3); equal(add(1, 1), 2); });\n", "vacuous"),
    "js strict alias self-compare": ("node", "const { strict: s } = require('assert');\ntest('x', () => { const r = add(1, 2); s.equal(r, r); s.equal(add(1, 1), 2); s.equal(add(0, 1), 1); });\n", "vacuous"),
    "js upper bound allows zero": ("node", "const assert = require('assert');\ntest('x', () => { const r = [add(1, 2)]; assert.ok(r.length <= 5); assert.equal(add(1, 2), 3); assert.equal(add(1, 1), 2); });\n", "vacuous"),
    "js mock.module": ("node", "const assert = require('assert');\nconst { mock } = require('node:test');\nmock.module('./calc', { namedExports: { add: () => 3 } });\ntest('x', () => { assert.equal(add(1, 2), 3); assert.equal(add(1, 1), 2); });\n", "mocks"),
    "js jest.fn on export": ("node", "const assert = require('assert');\nconst calc = require('./calc');\ncalc.add = jest.fn(() => 3);\ntest('x', () => { assert.equal(add(1, 2), 3); assert.equal(add(1, 1), 2); });\n", "mocks|overwrites"),
    "js sinon.stub": ("node", "const assert = require('assert');\nconst sinon = require('sinon');\nconst calc = require('./calc');\nsinon.stub(calc, 'add').returns(3);\ntest('x', () => { assert.equal(calc.add(1, 2), 3); assert.equal(calc.add(1, 1), 2); });\n", "mocks"),
    "py TestCase assertTrue(True)": ("py", "import unittest as ut\nclass T(ut.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n        self.assertEqual(add(1, 2), 3)\n        self.assertEqual(add(1, 1), 2)\n", "vacuous"),
    "py truthy tuple": ("py", "def test_a():\n    assert (1,)\n    assert add(1, 2) == 3\n    assert add(1, 1) == 2\n", "vacuous"),
    "py upper bound allows zero": ("py", "def test_a():\n    r = [add(1, 2)]\n    assert len(r) <= 3\n    assert add(1, 2) == 3\n    assert add(1, 1) == 2\n", "vacuous"),
    "py monkeypatch": ("py", "def test_a(monkeypatch):\n    monkeypatch.setattr(calc, 'add', lambda a, b: 3)\n    assert calc.add(1, 2) == 3\n    assert calc.add(1, 1) == 3\n", "mocks"),
    "py mock alias decorator": ("py", "from unittest import mock as m\n@m.patch('calc.add', return_value=3)\ndef test_a(p):\n    assert calc.add(1, 2) == 3\n    assert calc.add(1, 1) == 3\n", "mocks"),
    "py unittest.mock module alias": ("py", "import unittest.mock as um\ndef test_a():\n    with um.patch.object(calc, 'add', return_value=3):\n        assert calc.add(1, 2) == 3\n        assert calc.add(1, 1) == 3\n", "mocks"),
    "py attribute overwrite": ("py", "calc.add = lambda a, b: a + b\ndef test_a():\n    assert calc.add(1, 2) == 3\n    assert calc.add(1, 1) == 2\n", "mocks"),
    "py sys.modules assign": ("py", "import sys, types\nsys.modules['calc'] = types.SimpleNamespace(add=lambda a, b: 3)\ndef test_a():\n    assert add(1, 2) == 3\n    assert add(1, 1) == 3\n", "mocks"),
    "py sys.modules setdefault": ("py", "import sys, types\nsys.modules.setdefault('calc', types.SimpleNamespace(add=lambda a, b: 3))\ndef test_a():\n    assert add(1, 2) == 3\n    assert add(1, 1) == 3\n", "mocks"),
    "py sys.modules update": ("py", "import sys, types\nsys.modules.update({'calc': types.SimpleNamespace(add=lambda a, b: 3)})\ndef test_a():\n    assert add(1, 2) == 3\n    assert add(1, 1) == 3\n", "mocks"),
    "py patch.dict sys.modules": ("py", "import sys, types\nfrom unittest import mock\n@mock.patch.dict(sys.modules, {'calc': types.SimpleNamespace(add=lambda a, b: 3)})\ndef test_a():\n    assert add(1, 2) == 3\n    assert add(1, 1) == 3\n", "mocks"),
    "py skip": ("py", "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert add(1, 2) == 3\n    assert add(1, 1) == 2\n", "skipped"),
}


def _acorn_dir():
    if os.environ.get("MINI_ACORN"):
        return os.environ["MINI_ACORN"]
    hits = sorted(glob.glob(os.path.expanduser("~/.ollama-dispatch/node_modules-cache/*/node_modules/acorn")))
    return str(Path(hits[0]).parent) if hits else None


def _self_test_static():
    good_py = PY_HEAD + "def test_a():\n    assert add(1, 2) == 3\n    assert add(-1, 1) == 0\n"
    assert test_static(good_py, ["calc.py"], test_path="test_calc.py") == [], test_static(good_py, ["calc.py"], test_path="test_calc.py")
    fresh = "import sys\nif 'calc' in sys.modules:\n    del sys.modules['calc']\n" + good_py  # fresh import of the real module
    assert test_static(fresh, ["calc.py"], test_path="test_calc.py") == [], test_static(fresh, ["calc.py"], test_path="test_calc.py")
    _self_test_deps(good_py)
    _self_test_reexports()
    _self_test_precedent(good_py)
    _self_test_zero_tests()
    _self_test_draft_feedback()
    acorn = _acorn_dir()
    modes = ["regex"] + (["ast"] if acorn and shutil.which("node") else [])
    if "ast" in modes:
        os.environ["MINI_ACORN"] = acorn
        good_js = JS_HEAD + "const assert = require('assert');\ntest('x', () => { assert.equal(add(1, 2), 3); assert.equal(add(1, 1), 2); });\n"
        assert test_static(good_js, ["calc.js"], test_path="calc.test.js") == [], test_static(good_js, ["calc.js"], test_path="calc.test.js")
        assert js_static(good_js, ["calc"], ".", False)["parser"] == "acorn"
    else:
        print("[self-test] acorn not found: JS AST evasion cases skipped (regex fallback still tested)")
    for name, (lang, body, want) in EVASIONS.items():
        src = (JS_HEAD if lang == "node" else PY_HEAD) + body
        tgt = ["calc.js" if lang == "node" else "calc.py"]
        tp = "calc.test.js" if lang == "node" else "test_calc.py"
        for mode in (["ast"] if lang == "py" else modes):
            bad = test_static(src, tgt, test_path=tp) if mode == "ast" else test_static_regex(src, tgt)
            assert any(w in b for b in bad for w in want.split("|")), (name, mode, bad)


def _self_test_reexports():
    """bench optuna: a test that imports the target's class from the package that re-exports it (directly, through `*`, through
    a parent package, or as an attribute of the imported package) imports the code under test; a name the package takes from
    another module, a private name a `*` skips, or a name the target only imports, does not."""
    never = lambda bad: any("never imports" in b for b in bad)
    with tempfile.TemporaryDirectory() as d:
        files = {"pkg/__init__.py": "from pkg.sub import Sampler as PublicSampler\nfrom . import sub\n",
                 "pkg/sub/__init__.py": "try:\n    from ._impl import Sampler, helper\nexcept ImportError:\n    pass\nfrom .other import Other\n"
                                        "from ._impl import Base\n",
                 "pkg/sub/_impl.py": "from pkg.sub._base import Base\n\nclass Sampler:\n    def run(self, x):\n        return x + 1\n\n"
                                     "def helper():\n    return 1\n\ndef _private():\n    return 2\n",
                 "pkg/sub/other.py": "class Other:\n    pass\n",
                 "pkg/sub/_base.py": "class Base:\n    pass\n",
                 "star/__init__.py": "from ._impl import *\n",
                 "star/_impl.py": "def run(x):\n    return x + 1\n\ndef _private():\n    return 2\n",
                 "src/lay/__init__.py": "from .core import go\n",
                 "src/lay/core.py": "def go(x):\n    return x\n"}
        for p, body in files.items():
            Path(d, p).parent.mkdir(parents=True, exist_ok=True)
            Path(d, p).write_text(body)
        ex = _py_reexports(d, ["pkg/sub/_impl.py"])
        for want in ("pkg.sub.Sampler", "pkg.sub.helper", "pkg.PublicSampler", "pkg.sub._impl.Sampler"):
            assert want in ex, (want, ex)
        assert not {"pkg.sub.Other", "pkg.sub.Base", "pkg.Sampler"} & ex, ex
        body = "\n\ndef test_a():\n    assert {0}(1) == 2\n    assert {0}(2) == 3\n"
        imp = lambda head, call, tgt="pkg/sub/_impl.py", cwd=d: never(test_static(head + body.format(call), [tgt], test_path="tests/test_x.py", cwd=cwd))
        assert not imp("from pkg.sub import Sampler", "Sampler().run")
        assert not imp("from pkg import PublicSampler as S", "S().run")
        assert not imp("import pkg", "pkg.sub.Sampler().run")
        assert not imp("import pkg.sub as ps", "ps.Sampler().run")
        assert imp("from pkg.sub import Other", "Other().run")
        assert imp("from pkg.sub import Base", "Base().run")
        assert imp("import pkg", "pkg.sub.Other().run")
        assert imp("from pkg.sub import Sampler", "Sampler().run", cwd=None)  # no source to read: the stem rule alone, as before
        assert not imp("from star import run", "run", "star/_impl.py")
        assert imp("from star import _private", "_private", "star/_impl.py")
        assert not imp("from lay import go", "go", "src/lay/core.py")


def _self_test_deps(good_py):
    """A test may fake what the target imports (requests) and its settings (URL); replacing a def of the target stays mocking, and
    every offender is named."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "calc.py").write_text("import os\nimport requests\nfrom pathlib import Path\nfrom time import sleep as nap\ntry:\n    import yaml\nexcept ImportError:\n"
                                      "    yaml = None\nURL = os.environ.get('CALC_URL', 'http://x')\nHOME_DIR = os.path.expanduser('~/.calc')\n"
                                      "LIMIT = int(os.getenv('LIMIT', '3'))\nCACHE = {}\nLABELS = {'a': 'b'}\nsession = requests.Session()\n"
                                      "STATE = Path(HOME_DIR) / 'state.json'\nHERE = Path(__file__).parent\nKEY = os.environ['CALC_KEY'].strip()\n\n"
                                      "def add(a, b):\n    return a + b\n\ndef fetch():\n    return requests.get(URL)\n")
        deps = _py_dep_names(d, ["calc.py"], ["calc"])
        assert deps == {"os", "requests", "Path", "nap", "yaml", "URL", "HOME_DIR", "LIMIT", "STATE", "HERE", "KEY"}, deps
        ok = good_py + ("def test_b(monkeypatch):\n    calc.requests = object()\n    calc.URL = 'http://fake'\n    calc.nap = lambda s: None\n"
                        "    monkeypatch.setattr(calc, 'requests', object())\n    monkeypatch.setattr('calc.requests.get', lambda u: 1)\n"
                        "    assert add(2, 2) == 4\n    assert add(0, 0) == 0\n")
        assert test_static(ok, ["calc.py"], test_path="test_calc.py", cwd=d) == [], test_static(ok, ["calc.py"], test_path="test_calc.py", cwd=d)
        for body in ("calc.add = lambda a, b: 3\n", "calc.session = object()\n", "calc.CACHE = {}\n", "calc.LABELS = {}\n", "del calc.requests\n",
                     "import unittest.mock as um\num.patch('calc.fetch').start()\n", "import unittest.mock as um\num.patch.object(calc, 'add').start()\n"):
            bad = test_static(good_py + body, ["calc.py"], test_path="test_calc.py", cwd=d)
            assert any("mocks the code under test" in b for b in bad), (body, bad)
        many = good_py + "calc.add = 1\ncalc.fetch = 2\ncalc.session = 3\ncalc.requests = 4\n"
        bad = [b for b in test_static(many, ["calc.py"], test_path="test_calc.py", cwd=d) if "mocks" in b]
        assert bad and all(x in bad[0] for x in ("calc.add", "calc.fetch", "calc.session")) and "calc.requests" not in bad[0], bad
        # without the target's source (no cwd) nothing is exempt: same verdicts as before
        assert any("mocks" in b for b in test_static(good_py + "calc.requests = 4\n", ["calc.py"], test_path="test_calc.py"))


def _self_test_precedent(good_py):
    """bench-dupes4: a def of the target that the repo's committed tests replace may be faked the same way; the functions under
    test, defs no committed test replaces, and precedent that exists only in the work tree stay mocking."""
    src = "import requests\n\ndef helper():\n    return 1\n\ndef other():\n    return 2\n\ndef add(a, b):\n    return helper() + a + b - 1\n"
    old = ("import calc\nimport unittest.mock as um\n\ndef _stub(calc):\n    calc.helper = lambda: 1\n\n"
           "def test_old(monkeypatch):\n    _stub(calc)\n    monkeypatch.setattr(calc, 'other', lambda: 0)\n"
           "    um.patch('calc.add').start()\n    assert calc.add(1, 2) == 3\n")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "calc.py").write_text(src)
        Path(d, "tests").mkdir()
        Path(d, "tests", "helpers.py").write_text(old)
        for c in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
            subprocess.run(["git", *c], cwd=d, check=True, capture_output=True)
        Path(d, "test_late.py").write_text("import calc\ncalc.unrelated = 1\n")  # uncommitted: no precedent
        assert _py_precedent_fakes(d, ["calc"]) == {"helper", "other", "add"}, _py_precedent_fakes(d, ["calc"])
        assert _py_precedent_fakes(Path(d, "tests"), ["nope"]) == set()
        ok = good_py + "def test_b(monkeypatch):\n    calc.helper = lambda: 1\n    monkeypatch.setattr(calc, 'other', lambda: 0)\n" \
                       "    assert add(2, 2) == 4\n    assert add(0, 0) == 0\n"
        got = test_static(ok, ["calc.py"], test_path="test_calc.py", cwd=d, fns=["add"])
        assert got == [], got
        for body in ("calc.add = lambda a, b: 3\n", "calc.unrelated = 1\n", "import unittest.mock as um\num.patch('calc.add').start()\n"):
            bad = test_static(good_py + body, ["calc.py"], test_path="test_calc.py", cwd=d, fns=["add"])
            assert any("mocks the code under test" in b and "repo's own tests already fake (helper, other)" in b for b in bad), (body, bad)
        # no repo (or no cwd): nothing is exempt, as before
        assert any("mocks" in b for b in test_static(ok, ["calc.py"], test_path="test_calc.py", fns=["add"]))


def _self_test_zero_tests():
    plain ="import calc\n\ndef test_a(tmp_path):\n    assert calc.add(1, 2) == 3\n"
    assert _zero_tests_ran("test_calc.py", plain, "", [], "python3 test_calc.py")
    assert not _zero_tests_ran("test_calc.py", plain, "", [], "python3 -m pytest -q test_calc.py")
    assert not _zero_tests_ran("test_calc.py", plain + "\nif __name__ == '__main__':\n    test_a(None)\n", "", [], "python3 test_calc.py")
    assert not _zero_tests_ran("test_calc.py", plain + "\nfor t in [test_a]:\n    t(None)\n", "", [], "python3 test_calc.py")
    assert not _zero_tests_ran("test_calc.py", plain + "\ntest_a(None)\n", "", [], "python3 test_calc.py")
    assert not _zero_tests_ran("test_calc.py", plain, "2 passed", [], "python3 test_calc.py")
    assert not _zero_tests_ran("calc.test.js", "test('a', () => {})", "", [], "node --test")


def _self_test_draft_feedback():
    hdr = "  s1 V1 precheck: ok ok\n  s1 V2 static: ok clean\n  s1 V3 red/right reason: ok ACCEPTED\n"
    ref_red = (hdr + "  s1 V4 ref green: FAIL test_green: FAIL rc=1 tests=5\n  s1 V5 hunks covered: ok 3/3\n  s1 V6 sizing: ok x\n"
               "  s1 V7 reverse order: FAIL rc=1 \n  mutation: FAIL line-scoped unscored\n--- draft vs ref: test output (tail, rc=1) ---\n"
               "  FAIL: test_x: m must define purge\n--- end ---\nmini-testdraft: REJECT (t.py vs ref ref)\n")
    items, retry = draft_feedback(ref_red)
    assert retry and "reference fix applied the test still FAILS" in items[0] and items[-1].endswith("m must define purge"), items
    assert not any("reverse order" in i for i in items), items  # V7 is the same failure: not a second instruction
    weak = (hdr + "  s1 V4 ref green: ok all fix gates green\n  s1 V5 hunks covered: FAIL 1/2 ref hunks make the test red; NOT caught: "
            "a.py hunk 2\n  s1 V6 sizing: ok x\n  s1 V7 reverse order: ok rc=0\n  mutation: FAIL line-scoped on a.py: score=0.5 (2/4 "
            "killed) < 0.7; survivors: a.py:3 cmp '<'->'<='\nmini-testdraft: REJECT (t.py vs ref ref)\n")
    items, retry = draft_feedback(weak)
    assert retry and any("NOT caught: a.py hunk 2" in i for i in items) and any("survivors: a.py:3" in i for i in items), items
    items, retry = draft_feedback(hdr.replace("V2 static: ok clean", "V2 static: ok clean") + "  s1 V4 ref green: ok g\n  s1 V5 hunks covered: ok\n"
                                  "  s1 V6 sizing: ok\n  s1 V7 reverse order: ok\n  mutation: FAIL line-scoped unscored: no changed .py file to mutate\n")
    assert not retry and items, (items, retry)  # a tool problem: retrying cannot help
    assert draft_feedback("REFUSED: x matches a must-not-offload area")[1] is False
    skipped = hdr.replace("V3 red/right reason: ok ACCEPTED", "V3 red/right reason: FAIL wrong reason") + "  s1 V4 ref green: FAIL skipped: V3 failed\n"
    items, retry = draft_feedback(skipped)
    assert retry and any("wrong reason" in i for i in items) and not any("still FAILS" in i for i in items), items
    ok4 = hdr + "  s1 V4 ref green: ok all fix gates green\n  s1 V5 hunks covered: ok 1/1\n"
    tail = "  s1 V7 reverse order: ok rc=0 \n  mutation: ok line-scoped on a.py: score=0.98 (56/57 killed) >= 0.7\nmini-testdraft: REJECT (t.py)\n"
    items, retry = draft_feedback(ok4 + "  s1 V6 sizing: FAIL 12 tests (need 2..10); 27 changed lines, 12 tests; formula fix_steps=74\n" + tail)
    assert retry and len(items) == 1 and "merge related tests" in items[0] and "keep EVERY assertion" in items[0], items
    items, retry = draft_feedback(ok4 + "  s1 V6 sizing: FAIL 1 tests (need 2..10); 3 changed lines, 1 tests\n" + tail)
    assert retry and "at least 2" in items[0], items
    items, retry = draft_feedback(ok4 + "  s1 V6 sizing: FAIL 45 changed ref lines > 40; 45 changed lines, 4 tests\n" + tail)
    assert not retry and "not something the test can change" in items[0], items  # the ref's size: a retry cannot fix it
    items, retry = draft_feedback(ok4 + "  s1 V6 sizing: FAIL 45 changed ref lines > 40; 12 tests (need 2..10); 45 changed lines, 12 tests\n" + tail)
    assert not retry and len(items) == 1 and "not something the test can change" in items[0], items  # merging tests cannot pass V6 here


def _toy(d):
    repo = Path(d) / "repo"
    repo.mkdir()
    (repo / "calc.js").write_text(CALC)
    (repo / "TASK.md").write_text("# TASK\n`add(a, b)` must return the sum.\n\n## Must contain\n\n- `sumFix`\n")
    (repo / ".harness.json").write_text("{}")  # untracked baseline helper: kept, never a stray
    for c in (["init", "-q"], ["add", "calc.js"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    return repo


def _self_test_obs_cap():
    """The overlay's observation_template: short output verbatim, big TAP output reduced to failures + totals, other big output head+tail."""
    try:
        import jinja2
        import yaml
    except ImportError as e:  # the overlay's observation_template is part of the runner contract: never skip silently
        raise AssertionError(f"observation-cap self-test needs jinja2 and yaml (mini-swe-agent's venv has them): {e}")
    tpl = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(yaml.safe_load(OVERLAY.read_text())["model"]["observation_template"])

    def obs(text, rc=1):
        o = types.SimpleNamespace(output=text, returncode=rc, exception_info="")
        return json.loads(tpl.render(output=o)), len(tpl.render(output=o))

    assert obs("short")[0] == {"returncode": 1, "output": "short"}
    tap = ("TAP version 13\n" + "".join(f"# Subtest: t{i}\nok {i} - t{i}\n  ---\n  duration_ms: 0.5\n  ...\n" for i in range(200))
           + "not ok 201 - bad\n  ---\n  error: 'strictly equal'\n  expected: 3\n  actual: 4\n  code: 'ERR_ASSERTION'\n  ...\n# tests 201\n# pass 200\n# fail 1\n")
    j, n = obs(tap)
    assert n < 1200 and "not ok 201 - bad" in j["output_summary"] and "actual: 4" in j["output_summary"] and "# fail 1" in j["output_summary"] and "ok 5 - t5" not in j["output_summary"], j
    j, n = obs("line\n" * 4000)
    assert "output_head" in j and n < 5000, n
    # 3000-4500 chars: whole (no head/tail overlap, no negative elided_chars); a 100-line `nl -ba | sed` read fits
    for size in (2999, 3000, 3200, 3499, 3500, 3600, 4400, 4499):
        t = "".join(f"{i:6d}\tline {i}\n" for i in range(1000))[:size]
        j, n = obs(t)
        assert j.get("output") == t, (size, list(j))
    nl100 = "".join(f"{i:6d}\t    const v{i} = call({i});\n" for i in range(100, 200))
    assert 3000 < len(nl100) < 4500 and obs(nl100)[0].get("output") == nl100
    for size in (4500, 4501, 6000, 20000):
        t = "".join(f"{i:6d}\tline {i}\n" for i in range(5000))[:size]
        j, n = obs(t)
        assert j["output_head"] + j["output_tail"] in (t[:1200] + t[-2300:]) and j["elided_chars"] == size - 3500 > 0, (size, j["elided_chars"])
        assert t.startswith(j["output_head"]) and t.endswith(j["output_tail"]) and len(t) - len(j["output_head"]) - len(j["output_tail"]) == j["elided_chars"]
    # totals survive the 40-line cut, even with many failures
    many = "TAP version 13\n" + "".join(f"not ok {i} - f{i}\n  ---\n  error: 'e{i}'\n  expected: {i}\n  actual: 0\n  ...\n" for i in range(60)) + "# tests 60\n# pass 0\n# fail 60\n"
    j, n = obs(many)
    assert "# fail 60" in j["output_summary"] and "# tests 60" in j["output_summary"] and "not ok 59" not in j["output_summary"], j
    # real node --test output, both reporters: deepStrictEqual values, error name/location, a crash line, totals
    node = shutil.which("node")
    if node:
        with tempfile.TemporaryDirectory() as d:
            body = "".join(f"test('pass {i}', () => {{ assert.ok(true); }});\n" for i in range(120))
            Path(d, "x.test.js").write_text(
                "const { test } = require('node:test');\nconst assert = require('node:assert');\n" + body
                + "test('deep', () => { assert.deepStrictEqual({a: 1, b: [1, 2]}, {a: 1, b: [1, 3]}); });\n"
                + "test('crash', () => { const o = undefined; return o.field.sub; });\n"
                + "test('neq', () => { assert.strictEqual('hello world', 'hello there'); });\n")
            for rep in ("tap", "spec"):
                r = subprocess.run([node, "--test", f"--test-reporter={rep}", "x.test.js"], cwd=d, capture_output=True, text=True)
                raw = r.stdout + r.stderr
                j, n = obs(raw)
                sm = j.get("output_summary", "")
                assert len(raw) >= 3000 and sm, (rep, len(raw), list(j))
                assert "Cannot read properties of undefined" in sm and "TypeError" in sm and "hello there" in sm and "x.test.js:12" in sm, (rep, sm)
                assert "1, 3" in sm or "1: 3" in sm, (rep, sm)  # the deepStrictEqual expected value, inline (spec) or indented block (tap)
                assert "fail 3" in sm and "tests 123" in sm and "pass 5" not in sm, (rep, sm)
                if rep == "tap":
                    assert "name: 'AssertionError'" in sm and "location:" in sm, sm


def _self_test_probe():
    """--probe fixtures: healthy CJS/ESM -> ok; egift-s1 shape (module has no functions) and egift-s2 shape (ESM syntax in a
    CommonJS file) fail with the right reason; a seeded stub (throws, stub_error) still counts as reached and red for test_gate."""
    def mk(d, files):
        for rel, txt in files.items():
            Path(d, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(d, rel).write_text(txt)
        return Path(d)
    spec = {"test_file": "test/p.test.js", "test_cmd": "node --test test/p.test.js", "target_fn": "parse"}
    with tempfile.TemporaryDirectory() as d:
        ok = mk(Path(d, "ok"), {"package.json": '{"name":"x"}', "src/m.js": "function parse(t) {\n  if (!t) return [];\n  const out = t.split(',');\n  return out;\n}\nmodule.exports = { parse };\n"})
        bad, info = probe_check(ok, {**spec, "reach_anchor": "const out = t.split"}, ["src/m.js"])
        assert not bad and info["reach"] == ["parse"] and info["anchors"]["const out = t.split"]["guards_before"] == 1, (bad, info)
        esm = mk(Path(d, "esm"), {"package.json": '{"name":"x","type":"module"}', "src/m.js": "export function parse(t) { return t; }\n"})
        bad, info = probe_check(esm, spec, ["src/m.js"])
        assert not bad and info["reach"] == ["parse"], (bad, info)
        s1 = mk(Path(d, "s1"), {"package.json": '{"name":"x","type":"module"}', "src/m.js": "// Stub\nexport {};\n"})
        bad, _ = probe_check(s1, spec, ["src/m.js"])
        assert bad and "`parse` never ran" in bad[0], bad
        s2 = mk(Path(d, "s2"), {"package.json": '{"name":"x"}', "src/m.cjs": "export function parse(t) { return t; }\n"})
        bad, _ = probe_check(s2, spec, ["src/m.cjs"])
        assert bad and "cannot be loaded" in bad[0] and "SyntaxError" in bad[0], bad
        stub = mk(Path(d, "stub"), {"package.json": '{"name":"x"}', "src/m.js": "function parse(t) {\n  throw new Error('not implemented: parse');\n}\nmodule.exports = { parse };\n"})
        bad, info = probe_check(stub, {**spec, "reach_anchor": "not implemented: parse'"}, ["src/m.js"])
        assert not bad and info["reach"] == ["parse"], (bad, info)
        # exported-name variants: internal helper / default-class method / module.exports = fn are WARN or ok, never "never ran"
        hp = mk(Path(d, "helper"), {"package.json": '{"name":"x"}', "src/m.js": "function normalize(t) {\n  return String(t).trim();\n}\nfunction parse(t) {\n  return t.split(',');\n}\nmodule.exports = { parse };\n"})
        bad, info = probe_check(hp, {**spec, "target_fn": "normalize"}, ["src/m.js"])
        assert not bad and info["warn"] and "unable to call" in info["warn"][0], (bad, info)
        bad, _ = probe_check(hp, {**spec, "target_fn": "nosuchfn"}, ["src/m.js"])
        assert bad and "never ran" in bad[0], bad
        cl = mk(Path(d, "cls"), {"package.json": '{"name":"x","type":"module"}', "src/m.js": "export default class P {\n  parse(t) {\n    if (!t) return [];\n    return t.split(',');\n  }\n}\n"})
        bad, info = probe_check(cl, spec, ["src/m.js"])
        assert not bad and "parse" in info["reach"], (bad, info)
        cf = mk(Path(d, "cjsfn"), {"package.json": '{"name":"x"}', "src/m.js": "function parse(t) {\n  if (!t) return [];\n  return t.split(',');\n}\nmodule.exports = parse;\n"})
        bad, info = probe_check(cf, spec, ["src/m.js"])
        assert not bad and "parse" in info["reach"], (bad, info)
        dom = mk(Path(d, "dom"), {"package.json": '{"name":"x"}', "src/m.js": "function parse() {\n  return document.querySelector('.p').textContent;\n}\nmodule.exports = { parse };\n"})
        bad, info = probe_check(dom, spec, ["src/m.js"])
        assert bad and "browser-only (not-local)" in bad[0] and info["browser_only"] == ["parse"], (bad, info)
        pyspec = {"test_file": "tests/test_p.py", "test_cmd": "python3 -m pytest -q tests/test_p.py", "target_fn": "parse"}
        if shutil.which("python3") and subprocess.run(["python3", "-c", "import pytest"], capture_output=True).returncode == 0:
            pm = mk(Path(d, "pym"), {"src/__init__.py": "", "src/m.py": "class P:\n    def parse(self, t):\n        if not t:\n            return []\n        return t.split(',')\n"})
            bad, info = probe_check(pm, pyspec, ["src/m.py"])
            assert not bad and "parse" in info["reach"], (bad, info)
            pd = mk(Path(d, "pyd"), {"src/__init__.py": "", "src/m.py": "def logged(fn):\n    def wrapper(*a, **k):\n        return fn(*a, **k)\n    return wrapper\n\n\n@logged\ndef parse(t):\n    return t.split(',')\n"})
            bad, info = probe_check(pd, pyspec, ["src/m.py"])
            assert not bad and "parse" in info["reach"], (bad, info)
        # test_gate: a test that only fails on the stub's own throw is a wrong-reason failure unless stub_error names it
        (stub / "test").mkdir(exist_ok=True)
        (stub / "test/p.test.js").write_text("const { test } = require('node:test');\nconst { parse } = require('../src/m.js');\n"
                                             "const assert = require('node:assert');\n"
                                             "test('a', () => { assert.strictEqual(parse('a').length, 1); });\ntest('b', () => { assert.deepStrictEqual(parse('b'), ['b']); });\n")
        sp = {**spec, "reach_anchor": "not implemented: parse'"}
        bad, _ = test_gate(stub, sp, ["src/m.js"])  # per test since node's spec reporter parses: "<name> red for the wrong reason"
        assert any("wrong-reason" in b or "red for the wrong reason" in b for b in bad), bad
        bad, ev = test_gate(stub, {**sp, "stub_error": "not implemented: parse"}, ["src/m.js"])
        assert not any("wrong-reason" in b or "red for the wrong reason" in b for b in bad), (bad, ev.get("baseline_out", "")[-300:])


def _self_test_gates():
    with tempfile.TemporaryDirectory() as d:
        repo = _toy(d)
        spec = {"test_file": "calc.test.js", "test_cmd": "node --test calc.test.js", "target_fn": "add"}
        cases = {
            "good": (GOOD_T, None),
            "passes at baseline": (GOOD_T.replace("3);", "-1);").replace("4);", "-6);"), "passes at baseline"),
            "never reaches target": ("const test = require('node:test');\nconst assert = require('node:assert');\nrequire('./calc');\n"
                                     "const add = (a, b) => a - b;\ntest('a', () => { assert.strictEqual(add(1, 2), 3); });\n"
                                     "test('b', () => { assert.strictEqual(add(2, 2), 4); });\n", "never reaches `add`"),
            "vacuous": (GOOD_T + "test('v', () => { assert.ok([].length >= 0); });\n", "vacuous"),
            "crash": (GOOD_T + "test('z', () => { add(; });\n", "crashes"),
            "wrong symptom": (GOOD_T, "symptom"),
        }
        for name, (src, want) in cases.items():
            (repo / "calc.test.js").write_text(src)
            sp = {**spec, "symptom": "NEVER-SEEN"} if name == "wrong symptom" else spec
            bad, ev = test_gate(repo, sp, ["calc.js"])
            assert (not bad) if want is None else any(want in b for b in bad), (name, bad, ev.get("baseline_out", "")[-400:])
        assert ev is not None
        (repo / "calc.test.js").write_text(GOOD_T)
        bad, ev = test_gate(repo, spec, ["calc.js"])
        assert ev["reach"] == ["add"] and ev["negative_control_rc"] != 0, ev
        bad, _ = test_gate(repo, {**spec, "target_fn": ["add", "other"]}, ["calc.js"])
        assert any("never reaches `other`" in x for x in bad), bad
        # a run still going at test_run_cap is a hang (scrapy r2 5f2fbdc72e8e); a run the job's deadline cut short is not
        assert test_run_cap({}) == TEST_RUN_S == 150 and test_run_cap({"cmd_timeout": 60}) == 60, test_run_cap({"cmd_timeout": 60})
        assert test_run_cap({"test_cmd_timeout": 30, "cmd_timeout": 60}) == 30
        t0 = time.time()
        bad, ev = test_gate(repo, {**spec, "test_cmd": "sleep 5", "test_cmd_timeout": 1}, ["calc.js"])
        assert bad == [hang_problem(1)] and ev["baseline_rc"] == 124 and time.time() - t0 < 4, (bad, ev.get("baseline_out"))
        keep, _DEADLINE[0] = _DEADLINE[0], time.time() + 1
        try:
            bad, ev = test_gate(repo, {**spec, "test_cmd": "sleep 5"}, ["calc.js"])
        finally:
            _DEADLINE[0] = keep
        assert bad and ev["baseline_rc"] == 124 and not any("hangs" in b for b in bad), (bad, ev.get("baseline_out"))
        # early return: the test calls the target but only runs its guard line
        (repo / "calc.js").write_text(EARLY_JS)
        (repo / "calc.test.js").write_text(EARLY_JS_T_GUARD)
        bad, ev = test_gate(repo, {**spec, "target_fn": "scale"}, ["calc.js"])
        assert any("body lines" in b for b in bad) and ev["fn_cov"]["scale"] < 0.3, (bad, ev.get("fn_cov"))
        (repo / "calc.test.js").write_text(EARLY_JS_T_FULL)
        bad, ev = test_gate(repo, {**spec, "target_fn": "scale"}, ["calc.js"])
        assert not bad and ev["fn_cov"]["scale"] > 0.8, (bad, ev.get("fn_cov"), ev.get("baseline_out", "")[-300:])
        # reach_anchor: gate on the TASK line executing, not on whole-function coverage
        asp = {**spec, "target_fn": "scale", "reach_anchor": "const a = x * 2"}
        (repo / "calc.test.js").write_text(EARLY_JS_T_GUARD)
        bad, ev = test_gate(repo, asp, ["calc.js"])
        assert any("line 3" in b and "line 2" not in b and "ran before it: 2 " in b for b in bad), bad
        (repo / "calc.test.js").write_text(EARLY_JS_T_FULL)
        bad, ev = test_gate(repo, asp, ["calc.js"])
        assert not bad, (bad, ev.get("baseline_out", "")[-300:])
        (repo / "calc.test.js").write_text(EARLY_JS_T_GUARD)
        bad, ev = test_gate(repo, {**asp, "reach_anchor": "if (x === null) return 0"}, ["calc.js"])
        assert not bad and ev["fn_cov"]["scale"] < 0.3, (bad, ev.get("fn_cov"))
        errs, _ = precheck({**asp, "baseline_from": str(repo), "allowed": ["calc.js"], "reach_anchor": "nope"})
        assert any('reach_anchor "nope"' in e for e in errs), errs


def _self_test_quality_pure():
    """must_not_contain parsing/matching, the whitespace-unit walk and the needs-review verdict of the new soft gates (no node)."""
    task = ("## Must contain\n\n- `sumFix`\n\n## Must not contain\n\n- `first row wins`\n- in calc.js: `old text`\n(note `x`)\n\n"
            "## Scope\n- `nope`\n")
    assert parse_must_contain(task) == [(None, "sumFix")], parse_must_contain(task)
    assert parse_must_not_contain(task) == [(None, "first row wins"), ("calc.js", "old text")], parse_must_not_contain(task)
    assert parse_must_not_contain("## Must contain\n- `a`\n") == []
    assert _prose("first row wins") in _prose("/**\n * When two rows tie the first\n * row wins.\n */") and _prose("# a\n#  b") == "a b"
    with tempfile.TemporaryDirectory() as d:
        base = "/**\n * Ties: the first\n * row wins.\n */\nfunction f() {}\n"
        Path(d, "m.ts").write_text(base)
        lits = [(None, "the first row wins"), (None, "never there"), ("other.ts", "the first row wins")]
        assert stale_check(d, lits, ["m.ts"], {"m.ts": base}) == (["the first row wins"], ["never there", "the first row wins"])
        Path(d, "m.ts").write_text(base.replace("the first\n * row wins", "the newest row\n * wins"))
        assert stale_check(d, lits[:1], ["m.ts"], {"m.ts": base}) == ([], [])
        ok, det = stale_gate(d, ["m.ts"], {"m.ts": base}, "## Must not contain\n- `the first row wins`\n")
        assert ok and det == "1 stale phrase(s) gone", det
        ok, det = stale_gate(d, ["m.ts"], {"m.ts": base}, "## Must not contain\n- `typo here`\n")
        assert not ok and "fix the spec" in det, det
        assert stale_gate(d, ["m.ts"], {"m.ts": base}, "no section") is None
    L = lambda s: s.splitlines(True)
    # the bfmr-split-tiebreak shape: a re-spaced comment next to a real edit is restored; the edit is not
    assert ws_units("a.ts", L("// column. A bad\nconst x = 1;\n"), L("// column.  A bad\nconst x = 2;\n")) == [(0, "// column. A bad\n", "inner")]
    assert ws_units("a.ts", L("let x = 1;  \n"), L("let x = 1;\n")) == [(0, "let x = 1;  \n", "inner")]  # trailing
    assert ws_units("a.ts", L('const s = "a b";\n'), L('const s = "a  b";\n')) == []  # inside a string literal: data
    assert ws_units("a.ts", L("let x = 1;\n"), L("letx = 1;\n")) == [] and _ws_kind("let x\n", "letx\n") is None
    assert ws_units("a.py", L("def f():\n    return 1\n"), L("def f():\n        return 1\n")) == []  # python indentation is syntax
    assert ws_units("a.py", L("x  = 1\n"), L("x = 1\n")) == [(0, "x  = 1\n", "inner")]
    iso = (L("function f() {\n  return 1;\n}\nconst y = 2;\n"), L("function f() {\n    return 1;\n}\nconst y = 3;\n"))
    assert ws_units("a.ts", *iso) == [(1, "  return 1;\n", "lead")], ws_units("a.ts", *iso)  # an isolated re-indent
    assert ws_units("a.ts", L("foo();\n\nbar();\n"), L("if (x) {\n\n  bar();\n}\n")) == []  # re-indent into a new block: layout
    assert ws_units("README.md", L("a  b\n"), L("a b\n")) == [] and ws_units("x.yml", L("a:  1\n"), L("a: 1\n")) == []
    assert ws_units("a.ts", L("a\n"), L("a\n")) == []
    # TASK.md about formatting the code skips ws_cleanup; whitespace as data does not (bfmr-split-tiebreak's TASK.md, 2026-11-03)
    for t in ("Fix the indentation of calc.js", "re-indent the function body", "Reformat lib/foo.ts with prettier",
              "Remove trailing whitespace from the file", "Convert tabs to spaces", "a whitespace-only change", "Use 2-space indentation",
              "Align the comments in the table", "Clean up the whitespace in foo.ts", "Indent the switch cases"):
        assert _WS_TASK_RE.search(t), t
    for t in ("Normalize a tracking number as `String(v ?? '')` with every whitespace character removed (`/\\s+/g`) and upper-cased.",
              "tracking numbers compare with all whitespace removed and case ignored, on both sides", "the key space is 12 hex digits",
              "the name is trimmed and internal spaces collapsed to one", "open a new browser tab", "trailing zeros are dropped",
              "a trailing slash is ignored", "the result is aligned with the API response", "date formatting stays as today",
              "an empty/whitespace-only line becomes a blank row", "Compare reserveId exactly: trim whitespace only, do NOT lowercase"):
        assert not _WS_TASK_RE.search(t), t
    # new TypeScript casts: only on changed code lines, outside strings/comments; never `as const`, double casts, empty literals, aliases
    nc = lambda a, b, f="m.ts": [(j, c) for j, _s, _e, c in _new_casts(f, L(a), L(b))]
    bts = "const x = 1;\nfunction g(l: Row) {\n  return l.id;\n}\n"
    assert nc(bts, bts.replace("l.id", "(l as Row).tracking_number")) == [(2, "as Row")]
    assert nc("const a = b as Foo;\n", "const a = b as Foo;\nconst c = 1;\n") == []
    assert nc("x\n", "const k = ['a'] as const;\nconst y = z as unknown as Foo;\nconst e = [] as Foo[];\n") == []
    assert nc("x\n", "import { a as b } from './m';\nexport { c as d };\nimport * as ns from 'n';\nimport type { E as F } from 'e';\n") == []
    assert nc("x\n", "const s = 'a as Foo'; // b as Bar\n") == [] and nc("x\n", "const y = z as Foo;\n", "m.js") == []
    assert nc("x\n", "const y = (z as Map<string, number>).get('k') as Foo[];\n") == [(0, "as Map<string, number>"), (0, "as Foo[]")]
    with tempfile.TemporaryDirectory() as d:  # cast_check with a stand-in type check: it needs `as Needed`, never `as Row`
        fin = bts.replace("l.id", "(l as Row).id + (l as Needed).n")
        Path(d, "m.ts").write_text(fin)
        old = 'echo "other.ts(1,1): error TS1005: old"; '
        tc = 'grep -q "as Needed" m.ts || { echo "m.ts(3,1): error TS2339: nope"; exit 2; }'
        sp = {"test_file": "t", "test_cmd": "true", "typecheck_cmd": tc}
        for s in (sp, {**sp, "typecheck_cmd": old + tc + "; exit 2"}):  # a type check red at baseline compares the same way
            ok, det = cast_check(d, s, ["m.ts"], {"m.ts": bts}, 60)
            assert not ok and det.startswith("1 of 2 new cast(s) the type check passes without") and "m.ts:3 `as Row`" in det, det
            assert "as Needed" not in det and Path(d, "m.ts").read_text() == fin
        assert cast_check(d, {**sp, "typecheck_cmd": "exit 3"}, ["m.ts"], {"m.ts": bts}, 60)[1].startswith("skipped: the type check")
        assert cast_check(d, {**sp, "typecheck_cmd": ""}, ["m.ts"], {"m.ts": bts}, 60) is None
        assert cast_check(d, sp, ["m.ts"], {"m.ts": bts}, 0) == (True, "skipped: no time left to check 2 new cast(s)")
        Path(d, "m.ts").write_text(bts.replace("l.id", "(l as Needed).n"))
        assert cast_check(d, sp, ["m.ts"], {"m.ts": bts}, 60) == (True, "each of 1 new cast(s) is needed by the type check")
    assert merge_soft(None, None) is None and merge_soft((True, "a"), None) == (True, "a")
    assert merge_soft((True, "a"), (False, "b")) == (False, "b; a")
    # the new soft gates make a needs-review pass, never a FAIL, while test_green/test_unchanged hold; a chain stays strict
    g = {"test_unchanged": (True, ""), "test_green": (True, ""), "must_not_contain": (False, "x"), "fix_hunks": (False, "y"), "ws_only": (True, "z")}
    assert gate_verdict(g) == ("NEEDS-REVIEW", ["fix_hunks", "must_not_contain"])
    rc, line = final_result(g, "s", "")
    assert rc == RC_OK and line.startswith("PASS needs-review soft=fix_hunks,must_not_contain "), line
    assert final_result(g, "s", "", chain=True)[0] == RC_FAIL
    assert gate_verdict({**g, "test_green": (False, "")})[0] == "FAIL"
    assert review_result(False, "s", None, ["fix_hunks"]) == "PASS needs-review soft=fix_hunks s" and review_result(False, "s", None) == "FAIL s"
    prev = os.environ.get("MINI_FIX_HUNKS")
    os.environ["MINI_FIX_HUNKS"] = "off"
    try:
        assert not fix_hunks_on({}) and not fix_hunks_on({"fix_hunks": True})
    finally:
        os.environ.pop("MINI_FIX_HUNKS") if prev is None else os.environ.__setitem__("MINI_FIX_HUNKS", prev)
    if prev is None:
        assert fix_hunks_on({}) and not fix_hunks_on({"fix_hunks": "off"}) and not fix_hunks_on({"fix_hunks": False})
    pb = phase_b_prompt({"test_file": "t.js", "test_cmd": "true"}, ["calc.js"], "x")
    assert "Must not contain" not in pb


def _self_test_quality_node():
    """ws_cleanup and fix_hunk_check on the toy repo with real `node --test` runs."""
    spec = {"test_file": "calc.test.js", "test_cmd": "node --test calc.test.js"}
    with tempfile.TemporaryDirectory() as d:
        repo = _toy(d)
        (repo / "calc.test.js").write_text(GOOD_T)
        noisy = CALC_FIXED.replace("function other() {", "function other()  {")
        (repo / "calc.js").write_text(noisy)
        r = ws_cleanup(repo, spec, ["calc.js"], {"calc.js": CALC}, 2, "")
        assert r and r[0] == (True, "restored the original whitespace on 1 line(s) the fix changed only in spacing: calc.js:2"), r
        assert test_count(r[1]) == 2 and (repo / "calc.js").read_text() == CALC_FIXED, r
        (repo / "calc.js").write_text(noisy)
        assert ws_cleanup(repo, spec, ["calc.js"], {"calc.js": CALC}, 2, "Fix the indentation of calc.js") is None  # the task is about whitespace
        assert ws_cleanup(repo, {**spec, "ws_clean": False}, ["calc.js"], {"calc.js": CALC}, 2, "") is None
        assert (repo / "calc.js").read_text() == noisy
        (repo / "TASK.md").write_text("# TASK\nx\n\n## Must not contain\n\n- `a - b`\n")
        assert "must no longer contain any literal in TASK.md's `## Must not contain` list" in phase_b_prompt(spec, ["calc.js"], "x", cwd=repo)
        # whitespace the test can see (Function.prototype.toString) is kept: restoring it all, then that one line, turns the test red
        (repo / "calc.test.js").write_text(GOOD_T + "test('src', () => { assert.ok(require('./calc').other.toString().includes('()  {')); });\n")
        r = ws_cleanup(repo, spec, ["calc.js"], {"calc.js": CALC}, 3, "")
        assert r and r[0][0] and r[0][1].startswith("kept 1 whitespace-only") and r[1] is None, r
        assert (repo / "calc.js").read_text() == noisy
        # fix_hunks: a second code hunk the test never needs is flagged; the files are put back afterwards
        (repo / "calc.test.js").write_text(GOOD_T)
        base = "function add(a, b) { return a - b; }\nconst K = 1;\nfunction other() { return 1; }\nmodule.exports = { add, other, K };\n"
        two = base.replace("a - b", "a + b").replace("return 1;", "return Number(1);") + "// sumFix\n"
        (repo / "calc.js").write_text(two)
        ok, det = fix_hunk_check(repo, spec, ["calc.js"], {"calc.js": base}, set(), 300)
        assert not ok and det.startswith("1 of 2 code hunk(s)") and "calc.js:3-3 `function other() { return Number(1); }`" in det, det
        assert (repo / "calc.js").read_text() == two
        ok, det = fix_hunk_check(repo, {**spec, "typecheck_cmd": "grep -q Number calc.js"}, ["calc.js"], {"calc.js": base}, set(), 300)
        assert ok and "1 more caught by the type check" in det, det  # a hunk only the type check needs is not flagged
        (repo / "calc.js").write_text(base.replace("a - b", "a + b") + "// sumFix\n")  # one code hunk (+ a comment-only one)
        assert fix_hunk_check(repo, spec, ["calc.js"], {"calc.js": base}, set(), 300) is None  # red_without_fix already proves it
        (repo / "calc.js").write_text(two)
        ok, det = fix_hunk_check(repo, spec, ["calc.js"], {"calc.js": base}, set(), 0)
        assert ok and det == "skipped: no time left to revert 2 code hunk(s)" and (repo / "calc.js").read_text() == two, det
        (repo / "calc.js").write_bytes(two.replace("\n", "\r\n").encode())  # CRLF: read_text/write_text would rewrite every ending
        assert fix_hunk_check(repo, spec, ["calc.js"], {"calc.js": base}, set(), 300) is None
        assert ws_cleanup(repo, spec, ["calc.js"], {"calc.js": base}, 2, "") is None


def _self_test_py_reach():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        spec = {"test_file": "test_calc.py", "test_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider test_calc.py", "target_fn": "add"}
        (repo / "test_calc.py").write_text("from calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n\ndef test_b():\n    assert add(2, 2) == 4\n")
        bad, ev = test_gate(repo, spec, ["calc.py"])
        assert not bad and ev["reach"] == ["add"], (bad, ev)
        # stage 19: with no other sitecustomize to chain (the remote venv), the shim dropped itself from sys.modules and site
        # printed "Error in sitecustomize ... KeyError: 'sitecustomize'" into every traced run; a second one still chains
        rc, out, reach = run_cmd(spec["test_cmd"], str(repo), {}, probe_targets=["calc.py"], full_reach=True)
        assert "Error in sitecustomize" not in out and reach and "add" in reach["fns"], (rc, out[:400], reach)
        with tempfile.TemporaryDirectory() as ch:
            # stage 23: the fixture chains onward the way the shim does. Homebrew's python reaches its site-packages only through
            # its own sitecustomize (lib/python3.X/sitecustomize.py rewrites the Cellar path); a sitecustomize on PYTHONPATH
            # shadows it, shim or no shim, so a print-only fixture lost pytest there: "No module named pytest", C0 red on the Mac
            Path(ch, "sitecustomize.py").write_text(
                "print('chained-sitecustomize', flush=True)\nimport importlib, os, sys\n"
                "_d = os.path.dirname(os.path.abspath(__file__))\n"
                "sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != _d]; _me = sys.modules.pop('sitecustomize', None)\n"
                "try: importlib.import_module('sitecustomize')\nexcept ImportError: pass\n"
                "if _me is not None: sys.modules.setdefault('sitecustomize', _me)\n")
            rc, out, reach = run_cmd(spec["test_cmd"], str(repo), {"env": {"PYTHONPATH": ch}}, probe_targets=["calc.py"],
                                     full_reach=True)
            assert "chained-sitecustomize" in out and "Error in sitecustomize" not in out and reach and "add" in reach["fns"], \
                (rc, out[:400], reach)
        (repo / "test_calc.py").write_text("import calc\n\ndef add(a, b):\n    return a - b\n\ndef test_a():\n    assert add(1, 2) == 3\n\ndef test_b():\n    assert add(2, 2) == 4\n")
        bad, ev = test_gate(repo, spec, ["calc.py"])
        assert any("never reaches" in b for b in bad), (bad, ev)
        (repo / "calc.py").write_text(EARLY_PY)
        sp = {**spec, "target_fn": "scale"}
        (repo / "test_calc.py").write_text("from calc import scale\n\ndef test_a():\n    assert scale(None) == 1\n\ndef test_b():\n    assert scale(None) == 1\n")
        bad, ev = test_gate(repo, sp, ["calc.py"])
        assert any("body lines" in b for b in bad), (bad, ev.get("fn_cov"))
        (repo / "test_calc.py").write_text("from calc import scale\n\ndef test_a():\n    assert scale(2) == 5\n\ndef test_b():\n    assert scale(3) == 7\n")
        bad, ev = test_gate(repo, sp, ["calc.py"])
        assert not bad and ev["fn_cov"]["scale"] > 0.8, (bad, ev.get("fn_cov"))
        asp = {**sp, "reach_anchor": "a = x * 2"}
        bad, ev = test_gate(repo, asp, ["calc.py"])
        assert not bad, bad
        # cfgkeys3 2d079d373db0: the test stops at the guard that reads the Constant the fix edits; that stop is the bug
        (repo / "cfg.py").write_text('KNOWN = {"a": 1}\n\n\ndef put(store, key, value):\n    if key not in KNOWN:\n'
                                     '        raise ValueError("unknown " + key)\n    if not isinstance(value, int):\n'
                                     '        raise ValueError("int only")\n    store[key] = value\n    store["n"] = store.get("n", 0) + 1\n'
                                     '    return store\n')
        (repo / "test_cfg.py").write_text("from cfg import put\n\n\ndef _try(k, v):\n    s, err = {}, None\n    try:\n        put(s, k, v)\n"
                                          "    except ValueError as e:\n        err = str(e)\n    return s, err\n\n\n"
                                          "def test_b():\n    s, err = _try('b', 2)\n    assert err is None and s.get('b') == 2, err\n\n\n"
                                          "def test_c():\n    s, err = _try('c', 3)\n    assert err is None and s.get('c') == 3, err\n")
        csp = {**spec, "test_file": "test_cfg.py", "test_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider test_cfg.py", "target_fn": "put"}
        bad, ev = test_gate(repo, csp, ["cfg.py"])
        assert any("body lines" in b for b in bad) and ev["fn_cov"]["put"] < 0.3 and "cov_waived" not in ev, (bad, ev.get("fn_cov"))
        bad, ev = test_gate(repo, {**csp, "new_names": ["OTHER"]}, ["cfg.py"])
        assert any("body lines" in b for b in bad), bad
        bad, ev = test_gate(repo, {**csp, "new_names": ["KNOWN"]}, ["cfg.py"])
        assert not bad and list(ev["cov_waived"]) == ["put"] and ev["cov_waived"]["put"].startswith("cfg.py:5 (it stops at the guard"), \
            (bad, ev.get("cov_waived"), ev.get("baseline_out", "")[-400:])
        src = (repo / "cfg.py").read_text()
        assert const_guard_stop(src, "put", {5, 6}, ["KNOWN"]) == 5 and const_guard_stop(src, "put", {5, 7, 8}, ["KNOWN"]) is None
        assert const_guard_stop(src, "put", {5, 6}, []) is None and const_guard_stop(src, "nope", {5, 6}, ["KNOWN"]) is None
        # loguru r3 bf6183725d19: the test runs a line the diagnosis names as the fault (spec.diag_lines) on one of several
        # alternative branches: that meets the floor; a test that stops at the uncited early return still does not
        (repo / "h.py").write_text(EMIT_PY)
        (repo / "test_h.py").write_text("from h import emit\n\n\ndef test_a():\n    assert emit({'a': 1}, '{b}', 1) == 'missing key: b'\n\n\n"
                                        "def test_b():\n    assert emit({'a': 1}, '{c}', 1) == 'missing key: c'\n")
        hsp = {**spec, "test_file": "test_h.py", "test_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider test_h.py", "target_fn": "emit"}
        bad, ev = test_gate(repo, hsp, ["h.py"])
        assert any("body lines" in b for b in bad) and ev["fn_cov"]["emit"] < 0.3 and "cov_waived" not in ev, (bad, ev.get("fn_cov"))
        dsp = {**hsp, "diag_lines": [7, 10, 13, 16, 19, 24]}
        bad, ev = test_gate(repo, dsp, ["h.py"])
        assert not bad and ev["cov_waived"] == {"emit": "h.py:7 (it runs a line the diagnosis names as the fault)"}, \
            (bad, ev.get("cov_waived"), ev.get("fn_cov"), ev.get("baseline_out", "")[-400:])
        errs, _ = precheck({**dsp, "baseline_from": str(repo), "allowed": ["h.py"]})
        assert not any("diag_lines" in e for e in errs), errs
        errs, _ = precheck({**dsp, "baseline_from": str(repo), "allowed": ["h.py"], "diag_lines": ["7"]})
        assert any("diag_lines must be" in e for e in errs), errs
        (repo / "test_h.py").write_text("from h import emit\n\n\ndef test_a():\n    assert emit(None, '{b}', 1) == 'missing key: b'\n\n\n"
                                        "def test_b():\n    assert emit(None, '{c}', 1) == 'missing key: c'\n")
        bad, ev = test_gate(repo, dsp, ["h.py"])
        assert any("body lines" in b and "7, 10, 13, 16, 19, 24 of h.py" in b for b in bad) and "cov_waived" not in ev, (bad, ev.get("cov_waived"))
        src = (repo / "h.py").read_text()
        assert diag_line_hit(src, "emit", {2, 4, 5, 6, 7, 24, 25}, [24]) == 24
        assert diag_line_hit(src, "emit", {2, 4, 5, 6, 7, 25}, [24]) == 24  # the except line itself unrecorded: its handler body ran
        assert diag_line_hit(src, "emit", {2, 4, 5, 6, 9, 12, 15, 19}, [20]) == 20  # a continuation line of a statement that ran
        assert diag_line_hit(src, "emit", {2, 4, 5, 6, 9, 12, 15}, [18]) is None  # `else:`: only the enclosing `elif` header ran
        assert diag_line_hit(src, "emit", {1, 2, 3}, [1]) is None and diag_line_hit(src, "nope", {7}, [7]) is None  # def line; no such fn
        assert _line_list([3, 4]) and not _line_list([True]) and not _line_list([0]) and not _line_list(list(range(1, 42)))
        (repo / "test_calc.py").write_text("from calc import scale\n\ndef test_a():\n    assert scale(None) == 1\n\ndef test_b():\n    assert scale(None) == 1\n")
        bad, ev = test_gate(repo, asp, ["calc.py"])
        assert any("line 4" in b and "line 3" not in b and "ran before it: 3 " in b for b in bad), bad
        bad, ev = test_gate(repo, {**asp, "reach_anchor": "return 0"}, ["calc.py"])
        assert not bad, bad
    _self_test_changed_lines_chained()


def _self_test_changed_lines_chained():
    """Chained steps: step 1's edit is uncommitted and lives in base_text; step 2 must only own its own lines."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        Path(d, "m.py").write_text("a = 1\nb = 2\nc = 3\n")
        subprocess.run(["git", "-C", d, "add", "m.py"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], check=True)
        step1 = "a = 10\nb = 2\nc = 3\nstep1_new = 1\n"  # uncommitted step-1 edits
        Path(d, "m.py").write_text("a = 10\nb = 20\nc = 3\nstep1_new = 1\nstep2_new = 2\n")
        got = changed_lines(d, "m.py", step1)
        assert got == {2, 5}, got  # git diff vs HEAD would also blame lines 1 and 4
        assert changed_lines(d, "m.py", None) == {1, 2, 3, 4, 5}


CALC_STEP2 = CALC_FIXED.replace("function other() { return 1; }", "function other() { return 2; } // otherFix")
T2 = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { other } = require('./calc');\n"
      "test('o1', () => { assert.strictEqual(other(), 2); });\ntest('o2', () => { assert.deepStrictEqual([other()], [2]); });\n")


def _self_test_steps(d, repo, stub):
    """`steps` chain end to end with the stub model: order, state carried over, fail-stop, combined REVIEW, check-only."""
    s1 = {"task": "GOAL-ONE: add must return the sum.\n\n## Must contain\n\n- `sumFix`\n", "test_file": "calc.test.js",
          "test_cmd": "node --test calc.test.js", "allowed": ["calc.js"], "target_fn": "add"}
    s2 = {"task": "GOAL-TWO: other must return 2.\n\n## Must contain\n\n- `otherFix`\n", "test_file": "calc2.test.js",
          "test_cmd": "node --test calc2.test.js", "allowed": ["calc.js"], "target_fn": "other"}
    spec = {"baseline_from": str(repo), "timeout": 600, "fix_attempts": 1, "steps": [s1, s2]}
    p1 = {"match": "GOAL-ONE", "A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED}}
    p2 = {"match": "GOAL-TWO", "A": {"calc2.test.js": T2}, "B": {"calc.js": CALC_STEP2}}
    runs = {  # name: (plan entries, spec, rc, RESULT substring, steps with a REVIEW section)
        "chain pass": ([p2, p1], spec, 0, "RESULT PASS mode=two-phase steps=2 step-1=ok step-2=ok"),
        "chain step2 fails": ([{**p2, "B": {"calc.js": CALC_FIXED}}, p1], spec, 1, "RESULT FAIL step-2 test_unchanged=ok test_green=FAIL"),
        "chain step1 fails": ([p2, {**p1, "B": {"calc.js": CALC}}], spec, 1, "RESULT FAIL step-1 test_unchanged=ok test_green=FAIL"),
        "chain bad step2 target": ([p2, p1], {**spec, "steps": [s1, {**s2, "target_fn": "nope"}]}, 1, "RESULT FAIL step-2 bad-spec"),
    }
    for name, (entries, sp, want_rc, want) in runs.items():
        run = Path(d) / name.replace(" ", "-")
        (run / "work").mkdir(parents=True)
        Path(run, "plan.json").write_text(json.dumps({"by_task": entries, "A": {}, "B": {}, "argv_dump": str(run / "argv.txt")}))
        Path(run, "spec.json").write_text(json.dumps(sp))
        r = subprocess.run([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                           capture_output=True, text=True, env={**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
        res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        assert r.returncode == want_rc and len(res) == 1 and want in res[0], (name, r.returncode, res, r.stdout[-1800:], r.stderr[-600:])
        assert "k-SECRET" not in r.stdout and r.stdout.count("[worker] USAGE SUMMARY") == 1, (name, r.stdout[-600:])
        rv = (run / "REVIEW.md").read_text()
        assert "## Step 1:" in rv and "## Step 2:" in rv and not list(run.glob("REVIEW.step*.md")), (name, rv[:500])
        work = run / "work"
        if name == "chain pass":
            av = (run / "argv.txt").read_text().splitlines()  # default caps: test_wall 600 / fix_wall 900, step caps unchanged
            assert [x.split()[0] for x in av] == ["A", "B", "A", "B"] and all(("wall_time_limit_seconds=600" in x) == x.startswith("A") and
                   ("wall_time_limit_seconds=900" in x) == x.startswith("B") for x in av), av
            assert "otherFix" in (work / "calc.js").read_text() and "sumFix" in (work / "calc.js").read_text()
            assert (work / "calc.test.js").exists() and (work / "calc2.test.js").exists()  # earlier step's test stays
            tm = (work / "TASK.md").read_text()
            assert "GOAL-TWO" in tm and "Already done in earlier steps" in tm and "Must contain\n\n- `sumFix`" not in tm, tm
            assert "prior_tests" in rv and "Step 1 (GOAL-ONE" in tm and "| 2 | `calc2.test.js` | PASS |" in rv, rv[:900]
            assert "[mini-runner] context s1A1" in r.stdout and "[mini-runner] context s2B1" in r.stdout
            sr = run / STEP_REFS  # each passing step's files, for a mini-finish replay
            assert ref_manifest(sr / "s1") == {"files": ["calc.js", "calc.test.js"], "deleted": []}, ref_manifest(sr / "s1")
            assert "sumFix" in (sr / "s1" / "calc.js").read_text() and "otherFix" not in (sr / "s1" / "calc.js").read_text()
            assert "otherFix" in (sr / "s2" / "calc.js").read_text() and (sr / "s2" / "calc2.test.js").is_file()
        if name == "chain step2 fails":
            assert (run / STEP_REFS / "s1" / "calc.js").is_file() and not (run / STEP_REFS / "s2").exists()
            assert "sumFix" in (work / "calc.js").read_text() and (work / "calc.test.js").exists()  # step 1 stays in the work dir
            assert "| 2 | `calc2.test.js` | FAIL" in rv, rv[:600]
        if name == "chain step1 fails":
            assert "| 2 | `calc2.test.js` | not run |" in rv, rv[:600]
            assert "context s2" not in r.stdout
    # model-free: --check-only validates every step; a bad later step names itself
    for name, sp, rc, want in (("ok", spec, 0, "check-only: 2 steps ok"),
                               ("bad", {**spec, "steps": [s1, {**s2, "test_file": "nodir/t.js"}]}, 1, "RESULT FAIL bad-spec step-2")):  # whole-chain precheck: fails before step 1 runs
        run = Path(d) / f"chk-{name}"
        (run / "work").mkdir(parents=True)
        Path(run, "spec.json").write_text(json.dumps(sp))
        r = subprocess.run([sys.executable, __file__, "--check-only", "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                           capture_output=True, text=True, env={**os.environ, "MINI_BIN": str(stub)})
        assert r.returncode == rc and want in r.stdout, (name, r.returncode, r.stdout[-1200:], r.stderr[-400:])
        if name == "bad":
            assert "=== step 1" not in r.stdout, r.stdout[-600:]
        if name == "ok":
            assert r.stdout.count("check-only: phase A prompt follows") == 2 and "GOAL-TWO" not in r.stdout.split("step 1/2")[0]
    err, warn = precheck_all(spec)
    assert err == [], err
    err, _ = precheck_all({**spec, "steps": [s1, {**s2, "test_file": "calc.test.js"}]})
    assert any("already used by step-1" in e for e in err), err
    err, _ = precheck_all({**spec, "steps": [s1, {**s2, "test_cmd": "nosuchbin-xyz t.js"}]})
    assert any(e.startswith("step-2:") and "nosuchbin-xyz" in e for e in err), err
    err, warn = precheck_all({**spec, "steps": [s1, {**s2, "target_fn": "createdByStep1"}]})
    assert err == [] and any("createdByStep1" in w for w in warn), (err, warn)  # later step: warn only before the chain ran
    assert merged_steps({"baseline_from": "x", "timeout": 5, "steps": [{"allowed": ["a"], "timeout": 9}]}) == [
        {"baseline_from": "x", "timeout": 9, "allowed": ["a"], "mode": "two-phase"}]


def _self_test_replay(d, repo, stub, base):
    """mini-finish support: a continuation replays steps 1..K from saved refs (no model call, real gates), a broken ref is a SPEC
    failure, check-only proves the replay; --draft-check and --try-diagnosis gate a hand-finished attempt with no model."""
    refs = Path(d) / "chain-pass" / STEP_REFS  # saved by _self_test_steps' passing chain
    s1 = {"task": "GOAL-ONE: add must return the sum.\n\n## Must contain\n\n- `sumFix`\n", "test_file": "calc.test.js",
          "test_cmd": "node --test calc.test.js", "allowed": ["calc.js"], "target_fn": "add"}
    s2 = {"task": "GOAL-TWO: other must return 2.\n\n## Must contain\n\n- `otherFix`\n", "test_file": "calc2.test.js",
          "test_cmd": "node --test calc2.test.js", "allowed": ["calc.js"], "target_fn": "other"}
    bad_refs = Path(d) / "bad-refs"
    shutil.copytree(refs, bad_refs)
    (bad_refs / "s1" / "calc.js").write_text(CALC)  # the unfixed file: step 1 goes RED
    p1_bad = {"match": "GOAL-ONE", "A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC}}  # would fail step 1 if the model ran
    p2 = {"match": "GOAL-TWO", "A": {"calc2.test.js": T2}, "B": {"calc.js": CALC_STEP2}}

    def run(name, sp, extra=(), plan=None, envx=None):
        rd = Path(d) / name
        (rd / "work").mkdir(parents=True)
        Path(rd, "spec.json").write_text(json.dumps(sp))
        Path(rd, "plan.json").write_text(json.dumps(plan or {"A": {}, "B": {}}))
        r = subprocess.run([sys.executable, __file__, *extra, "--cwd", str(rd / "work"), "--task-file", str(rd / "spec.json")], capture_output=True, text=True,
                           env={**os.environ, "STUB_PLAN": str(rd / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON), **(envx or {})})
        return rd, r, [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]

    chain = {"baseline_from": str(repo), "timeout": 600, "fix_attempts": 1, "steps": [s1, s2]}
    plan = {"by_task": [p2, p1_bad], "A": {}, "B": {}}
    rd, r, res = run("replay-ok", {**chain, "replay": {"refs": str(refs), "through": 1}}, plan={**plan, "argv_dump": str(Path(d, "replay-argv.txt"))})
    assert r.returncode == 0 and res == ["RESULT PASS mode=two-phase steps=2 step-1=ok step-2=ok replayed_through=1"], (r.stdout[-1500:], r.stderr[-500:])
    assert "context s1" not in r.stdout and "replay step 1: GREEN" in r.stdout, r.stdout[-1500:]
    assert [x.split()[0] for x in Path(d, "replay-argv.txt").read_text().splitlines()] == ["A", "B"]  # only step 2 ran the model
    rv = (rd / "REVIEW.md").read_text()
    assert "| 1 | `calc.test.js` | PASS (replayed) |" in rv and "| 2 | `calc2.test.js` | PASS |" in rv and "replayed from" in rv, rv[:900]
    assert "otherFix" in (rd / "work" / "calc.js").read_text() and "sumFix" in (rd / "work" / "calc.js").read_text()
    assert ref_manifest(rd / STEP_REFS / "s1")["files"] == ["calc.js", "calc.test.js"]  # a replayed step is saved again for the next finish
    rd, r, res = run("replay-bad", {**chain, "replay": {"refs": str(bad_refs), "through": 1}}, plan=plan)
    assert r.returncode == 1 and len(res) == 1 and res[0].startswith("RESULT FAIL step-1 bad-spec replay RED") and res[0].endswith("class=SPEC"), res
    assert "context s1" not in r.stdout and "context s2" not in r.stdout, r.stdout[-800:]
    rd, r, res = run("replay-chk", {**chain, "replay": {"refs": str(refs), "through": 1}}, ["--check-only"])
    assert r.returncode == 0 and "replay step 1: GREEN" in r.stdout and "check-only: 2 steps ok" in r.stdout, r.stdout[-1200:]
    assert not (rd / STEP_REFS).exists()  # check-only never writes refs
    rd, r, res = run("replay-chk-bad", {**chain, "replay": {"refs": str(bad_refs), "through": 1}}, ["--check-only"])
    assert r.returncode == 1 and "replay step 1: RED" in r.stdout, r.stdout[-1200:]
    # --draft-check: a test_draft spec's acceptance check on a hand-finished draft
    gt = Path(d, "good_t.js")
    gt.write_text(GOOD_T)
    draft = {**base, "phase_a_only": True}
    for name, cmd, rc, want in (("draft-ok", "echo '  s1 V4 ref green: ok'; echo ACCEPT", 0, "draft-check: PASS mode=test-draft accepted"),
                                ("draft-no", "echo REJECT; exit 3", 1, "draft-check: FAIL mode=test-draft rejected rc=3")):
        rd, r, _ = run(name, {**draft, "draft_accept_cmd": cmd}, ["--check-only", "--try-test", str(gt), "--draft-check"])
        assert r.returncode == rc and want in r.stdout and (rd / "draft_calc.test.js").read_text() == GOOD_T, (name, r.stdout[-1200:])
    rd, r, _ = run("draft-noflag", {**draft, "draft_accept_cmd": "true"}, ["--check-only", "--draft-check"])
    assert r.returncode == 1 and "--draft-check needs --check-only and --try-test" in r.stdout, r.stdout[-400:]
    # --try-diagnosis: a hand-finished DIAGNOSIS.md through the (fake) diag_check
    fake = Path(d, "fake_diag.py")
    fake.write_text("import os\ndef check(repo, report, spec=None):\n    t = open(os.path.join(repo, report)).read()\n"
                    "    return ('GOOD' in t, [] if 'GOOD' in t else ['no GOOD marker'])\n")
    diag = {"mode": "diagnose", "baseline_from": str(repo), "allowed": []}
    for name, body, rc, want in (("diag-ok", "GOOD report", 0, "ACCEPTED"), ("diag-no", "weak report", 1, "REJECTED: no GOOD marker")):
        Path(d, name + ".md").write_text(body)
        rd, r, _ = run(name, diag, ["--check-only", "--try-diagnosis", str(Path(d, name + ".md"))], envx={"DIAG_CHECK": str(fake)})
        assert r.returncode == rc and want in r.stdout and (rd / "work" / "DIAGNOSIS.md").read_text() == body, (name, r.stdout[-800:])
    rd, r, _ = run("diag-wrongmode", base, ["--check-only", "--try-diagnosis", str(Path(d, "diag-ok.md"))])
    assert r.returncode == 1 and "--try-diagnosis needs --check-only and a diagnose spec" in r.stdout, r.stdout[-400:]


def _self_test_draft(d, repo, stub, base):
    """The draft nudge's settings reach each run: phase A gets its test file (skeleton mark, the step its prompt names, the fail-fast
    step as the cap), phase B none; a diagnose attempt gets DIAGNOSIS.md (a later attempt that starts with no report is nudged from
    step 8; NEEDS.md counts as written when the needs-info channel is on); MINI_DRAFT_* in the runner's own environment never
    reaches a run; MINI_DRAFT_NUDGE=off sends none."""
    old = os.environ.pop("MINI_DRAFT_NUDGE", None)
    try:
        assert draft_nudge("x/f.py", 8, 0, 30, "say  it\n now")["MINI_DRAFT_SAY"] == "say it now"
        assert not {"MINI_DRAFT_MARK", "MINI_DRAFT_ALT", "MINI_DRAFT_HEADS"} & set(draft_nudge("x/f.py", 8, 0, 30, "s"))
        assert draft_nudge("x/f.py", 8, 0, 30, "s", heads=("A b", "C"))["MINI_DRAFT_HEADS"] == "A b|C"
        assert draft_nudge("x/f.py", 8, 0, 30, "s", alt=["a", "b"])["MINI_DRAFT_ALT"] == f"{Path('a').absolute()},{Path('b').absolute()}"
        os.environ["MINI_DRAFT_NUDGE"] = "Off"
        assert draft_nudge("x/f.py", 8, 0, 30, "s") == {}
    finally:
        os.environ.pop("MINI_DRAFT_NUDGE", None)
        if old is not None:
            os.environ["MINI_DRAFT_NUDGE"] = old
    fake = Path(d, "fake_diag_draft.py")
    fake.write_text("import os\ndef check(repo, report, spec=None):\n    p = os.path.join(repo, report)\n"
                    "    t = open(p).read() if os.path.isfile(p) else ''\n    return ('GOOD' in t, [] if 'GOOD' in t else ['no GOOD marker'])\n"
                    "def fix_ranges(repo, report):\n    return []\n")

    def run(name, sp, plan, envx=None):
        rd = Path(d) / name
        (rd / "work").mkdir(parents=True)
        dump = rd / "draft.jsonl"
        Path(rd, "spec.json").write_text(json.dumps(sp))
        Path(rd, "plan.json").write_text(json.dumps({**plan, "draft_dump": str(dump)}))
        r = subprocess.run([sys.executable, __file__, "--cwd", str(rd / "work"), "--task-file", str(rd / "spec.json")], capture_output=True,
                           text=True, env={**os.environ, "STUB_PLAN": str(rd / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON),
                                           "MINI_DRAFT_FILE": "/inherited", "MINI_DRAFT_SAY": "stale", "MINI_NEEDS_INFO": "off", **(envx or {})})
        rows = [json.loads(x) for x in dump.read_text().splitlines()] if dump.is_file() else []
        return rd, r, [(x["phase"], x["env"]) for x in rows]

    two = {"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED}}
    rd, r, rows = run("draft-2p", base, two)
    assert r.returncode == 0 and [x for x, _ in rows] == ["A", "B"], (r.stdout[-1500:], rows)
    a_env = rows[0][1]
    assert a_env == {"MINI_DRAFT_FILE": str(Path(rd, "work", "calc.test.js").absolute()), "MINI_DRAFT_NAME": "calc.test.js",
                     "MINI_DRAFT_STEP": "10", "MINI_DRAFT_EVERY": "6", "MINI_DRAFT_CAP": "30", "MINI_DRAFT_MARK": SKELETON_MARK,
                     "MINI_DRAFT_SAY": a_env.get("MINI_DRAFT_SAY")}, a_env
    assert "now (replace every placeholder with a real test):" in a_env["MINI_DRAFT_SAY"] and "ended at step 30 if" in a_env["MINI_DRAFT_SAY"], a_env
    assert rows[1][1] == {}, rows[1]  # phase B: none, and the runner's own MINI_DRAFT_* (/inherited) reached neither run
    rd, r, rows = run("draft-off", {**base, "test_steps": 18}, two, {"MINI_DRAFT_NUDGE": "off"})
    assert r.returncode == 0 and rows == [("A", {}), ("B", {})], (r.stdout[-1500:], rows)
    rd, r, rows = run("draft-18", {**base, "test_steps": 18}, two)
    assert r.returncode == 0 and [(e["MINI_DRAFT_STEP"], e["MINI_DRAFT_EVERY"], e["MINI_DRAFT_CAP"]) for _, e in rows[:1]] == [("8", "4", "18")], rows
    diag = {"mode": "diagnose", "baseline_from": str(repo), "allowed": []}
    plan = {"A": {}, "B": {}, "by_task": [{"match": "left no usable DIAGNOSIS.md", "B": {"DIAGNOSIS.md": "GOOD report"}},
                                         {"match": "--- TASK.md ---", "B": {}}]}
    rd, r, rows = run("draft-diag", diag, plan, {"DIAG_CHECK": str(fake)})
    assert r.returncode == 0 and "RESULT PASS mode=diagnose attempts=2" in r.stdout and [x for x, _ in rows] == ["D", "D"], (r.stdout[-1500:], rows)
    d1 = rows[0][1]
    assert d1 == {"MINI_DRAFT_FILE": str(Path(rd, "work", "DIAGNOSIS.md").absolute()), "MINI_DRAFT_NAME": "DIAGNOSIS.md", "MINI_DRAFT_STEP": "20",
                  "MINI_DRAFT_EVERY": "10", "MINI_DRAFT_CAP": str(DIAG_STEPS), "MINI_DRAFT_SAY": DIAG_SAY}, d1
    assert rows[1][1] == {**d1, "MINI_DRAFT_STEP": "8"}, rows[1]  # D1 left no report: D2 has the carried notes, so from step 8
    fake2 = Path(d, "fake_diag_heads.py")  # a report that lacks a heading: both runs must have it; D2 is told to append it, from step 8
    fake2.write_text("import os\nHEADINGS = ['Code path', 'Root cause']\ndef check(repo, report, spec=None):\n"
                     "    p = os.path.join(repo, report)\n    t = open(p).read() if os.path.isfile(p) else ''\n"
                     "    return ('## Root cause' in t, [] if '## Root cause' in t else [\"missing section '## Root cause'\"])\n"
                     "def fix_ranges(repo, report):\n    return []\n")
    plan = {"A": {}, "B": {}, "by_task": [{"match": "has no ## Root cause section. FIRST", "B": {"DIAGNOSIS.md": "## Code path\nx\n## Root cause\ny\n"}},
                                         {"match": "--- TASK.md ---", "B": {"DIAGNOSIS.md": "## Code path\nx\n"}}]}
    rd, r, rows = run("draft-diag-heads", diag, plan, {"DIAG_CHECK": str(fake2)})
    assert r.returncode == 0 and "RESULT PASS mode=diagnose attempts=2" in r.stdout and len(rows) == 2, (r.stdout[-1500:], rows)
    assert rows[0][1] == {**d1, "MINI_DRAFT_FILE": str(Path(rd, "work", "DIAGNOSIS.md").absolute()),
                          "MINI_DRAFT_HEADS": "Code path|Root cause"}, rows[0]
    assert rows[1][1] == {**rows[0][1], "MINI_DRAFT_STEP": "8", "MINI_DRAFT_SAY": DIAG_FIX_SAY,
                          "MINI_DRAFT_SHA": hashlib.sha256(b"## Code path\nx\n").hexdigest()}, rows[1]
    # sqlmetar3 f28c586f4214: D1's report has every heading but is rejected; D2 must change it: the nudge counts the rejected bytes
    # as not written from step 8 (DIAG_FIX_SAY), and D2's budget asks for the fix by step 8 instead of more investigation
    tasks = Path(d, "draft-diag-fix.tasks")
    plan = {"A": {}, "B": {}, "task_dump": str(tasks), "by_task": [{"match": "DIAGNOSIS.md was REJECTED", "B": {"DIAGNOSIS.md": "GOOD report"}},
                                                                   {"match": "--- TASK.md ---", "B": {"DIAGNOSIS.md": "weak report"}}]}
    rd, r, rows = run("draft-diag-fix", diag, plan, {"DIAG_CHECK": str(fake)})
    assert r.returncode == 0 and "RESULT PASS mode=diagnose attempts=2" in r.stdout and len(rows) == 2, (r.stdout[-1500:], rows)
    assert rows[0][1] == {**d1, "MINI_DRAFT_FILE": str(Path(rd, "work", "DIAGNOSIS.md").absolute())}, rows[0]
    assert rows[1][1] == {**rows[0][1], "MINI_DRAFT_STEP": str(DIAG_FIX_STEP), "MINI_DRAFT_SAY": DIAG_FIX_SAY,
                          "MINI_DRAFT_SHA": hashlib.sha256(b"weak report").hexdigest()}, rows[1]
    t1, t2 = tasks.read_text().split("\n=====\n")[:2]
    assert "keep investigating" in t1 and "already written" not in t1, t1[-1500:]
    assert f"DIAGNOSIS.md is already written: fix the listed problems in it by step {DIAG_FIX_STEP}" in t2 and "keep investigating" not in t2, t2[-1500:]
    assert diag_missing_note(["Root cause is empty"]) == "" and "cat >> DIAGNOSIS.md <<'EOF'\n  ## Root cause\n  ...\n  ## Not verified\n" in \
        diag_missing_note(["missing section '## Root cause'", "x", "missing section '## Not verified'"])
    task = Path(d, "draft-TASK.md")
    task.write_text("# TASK\nWhy does add subtract?\n")
    rd, r, rows = run("draft-diag-ni", {**diag, "task": str(task)}, {"A": {}, "B": {"DIAGNOSIS.md": "GOOD report"}},
                      {"DIAG_CHECK": str(fake), "MINI_NEEDS_INFO": "on"})
    assert r.returncode == 0 and len(rows) == 1, (r.stdout[-1500:], rows)
    assert rows[0][1]["MINI_DRAFT_ALT"] == str(Path(rd, "work", "NEEDS.md").absolute()), rows
    assert rows[0][1]["MINI_DRAFT_SAY"].endswith("(Or NEEDS.md instead, if TASK.md cannot be diagnosed without an answer.)"), rows


def _self_test_signals(d, base):
    """SIGTERM (queue preemption) kills the mini tree and prints one RESULT; the total deadline does the same."""
    for name, extra, sig in (("sigterm", {"timeout": 600}, True), ("deadline", {"timeout": 8, "test_timeout": 100, "deadline_grace": 1}, False)):
        run = Path(d) / name
        (run / "work").mkdir(parents=True)
        pidf = run / "stub.pid"
        Path(run, "plan.json").write_text(json.dumps({"A": {}, "B": {}, "sleep": 300, "pidfile": str(pidf)}))
        Path(run, "spec.json").write_text(json.dumps({**base, **extra}))
        t0 = time.time()
        p = subprocess.Popen([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             env={**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": MINI, "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
        while not pidf.exists() and time.time() - t0 < 60:
            time.sleep(0.2)
        assert pidf.exists(), name
        if sig:
            p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=120)
        stub_pid = int(pidf.read_text())
        time.sleep(0.5)
        try:
            os.kill(stub_pid, 0)
            alive = True
        except OSError:
            alive = False
        res = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
        assert p.returncode == 1 and not alive and len(res) == 1 and time.time() - t0 < 90, (name, p.returncode, alive, out[-800:])
        assert ("preempted" in res[0]) if sig else ("timeout" in res[0] or "FAIL" in res[0]), (name, res)


def _self_test_signals_bg(d, base):
    """SIGTERM while the background baselines run (beside phase A): the suite's process tree dies with the mini tree, one RESULT,
    and the copy of the work dir is removed."""
    run = Path(d) / "sigterm-bg"
    (run / "work").mkdir(parents=True)
    pidf, spidf = run / "stub.pid", run / "suite.pid"
    Path(run, "plan.json").write_text(json.dumps({"A": {}, "B": {}, "sleep": 300, "pidfile": str(pidf)}))
    Path(run, "spec.json").write_text(json.dumps({**base, "timeout": 600, "suite_cmd": f"echo $$ > {spidf}; sleep 300"}))
    t0 = time.time()
    p = subprocess.Popen([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                         env={**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": MINI, "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
    ready = lambda: pidf.is_file() and spidf.is_file() and spidf.read_text().strip() != ""
    while not ready() and time.time() - t0 < 60 and p.poll() is None:
        time.sleep(0.2)
    copied = (run / "wor0").is_dir()
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)  # also when the wait ran out: the handler kills whatever did start
    out, _ = p.communicate(timeout=120)
    assert ready() and copied, ("the stub and the background suite never both ran", out[-1500:])
    time.sleep(0.5)

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    res = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
    assert p.returncode == 1 and len(res) == 1 and "preempted" in res[0] and time.time() - t0 < 90, (p.returncode, res, out[-1500:])
    assert not alive(int(pidf.read_text())) and not alive(int(spidf.read_text())), "a child outlived the preemption"
    assert not (run / "wor0").exists() and "baselines run in the background on a copy of the work dir (wor0)" in out, out[-1500:]


STUB_SS =r"""import json, os, sys
req = json.load(sys.stdin); plan = json.load(open(os.environ["SS_PLAN"]))
kp = os.environ.pop("MINI_KEY_FILE"); key = open(kp).read(); os.unlink(kp)
log = plan["log"]; n = len(open(log).read().splitlines()) if os.path.exists(log) else 0
open(log, "a").write(json.dumps({"no_think": req["no_think"], "prompt": req["messages"][0]["content"], "key_ok": key == "k-SECRET"}) + "\n")
r = plan["replies"][min(n, len(plan["replies"]) - 1)]
print("noise before the marker")
print("@@SS@@ " + json.dumps(r if "error" in r else {"content": r["content"], "finish_reason": r.get("finish_reason", "stop"), "usage": {"prompt_tokens": 500, "completion_tokens": 50}, "secs": 1.0}))
"""

SS_GOOD = "function add(a, b) {\n  // sumFix\n  return a + b;\n}"
SS_FENCE = lambda code, tag="javascript": f"The bug is the operator.\n```{tag}\n{code}\n```\n"


def _self_test_mutation():
    """mutation_gate offline: count parsers, verdict thresholds, precheck, and run_mutation_gate against FAKE stryker / mutmut / custom tools."""
    assert mut_counts_stryker({"files": {"a.js": {"mutants": [{"status": "Killed"}, {"status": "Survived"}, {"status": "Timeout"}, {"status": "CompileError"}]}}}) == {"killed": 1, "survived": 1, "timeout": 1, "compileerror": 1}
    canned = ("    m.x_add__mutmut_1: killed\n    m.x_add__mutmut_2: survived\n    m.x_add__mutmut_3: not checked\n    m.x_other__mutmut_1: survived\n"
              "    m.xǁKlassǁadd__mutmut_1: killed\n    m.x_address__mutmut_1: survived\n    m.xǁKlassǁother__mutmut_1: killed\n")
    assert mut_counts_mutmut(canned, ["add"]) == {"killed": 2, "survived": 1, "unselected": 1}, mut_counts_mutmut(canned, ["add"])
    assert mut_score(mut_counts_mutmut(canned, ["add"])) == (2 / 3, 2, 3)  # "not checked" is not a mutant
    assert mut_counts_mutmut(canned)["survived"] == 3
    assert mut_selector_default("src/pkg/mod.py", "add") == ["pkg.mod.x_add__mutmut_*", "pkg.mod.xǁ*ǁadd__mutmut_*"] and mut_selector_default("calc.py", "f")[0] == "calc.x_f__mutmut_*"
    assert mut_counts_mutmut("    m.x_f__mutmut_1: killed\n    m.x_f__mutmut_2: survived\n    m.x_f__mutmut_3: no tests\nnoise") == {"killed": 1, "survived": 1, "nocoverage": 1}
    assert mut_counts_line("x\nMUTATION killed=3 survived=1 timeout=1") == {"killed": 3, "survived": 1, "timeout": 1}
    assert mut_score({"killed": 3, "timeout": 1, "survived": 1, "compileerror": 9}) == (0.8, 4, 5) and mut_score({})[0] is None
    ok, d, sc = mut_verdict({"killed": 8, "survived": 2}, {"fail_below": 0.5, "warn_below": 0.9})
    assert ok and d.startswith("WARN") and sc == 0.8
    assert mut_verdict({"killed": 1, "survived": 4}, {"fail_below": 0.5})[0] is False
    assert mut_verdict({"killed": 1, "survived": 4}, {})[1].startswith("WARN") and mut_verdict({"killed": 1}, {"fail_below": 0.9})[0]  # too few mutants: warn only
    assert mut_verdict({"killed": 4, "survived": 0}, {"warn_below": 0.6})[1].startswith("score=1.00")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "calc.js").write_text(CALC)
        Path(d, "calc.test.js").write_text(GOOD_T)
        spec = {"target_fn": "add", "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js"}
        assert mut_targets(d, spec, ["calc.js"]) == ([("calc.js", "add", 1, 1)], []) and mut_targets(d, {"target_fn": "nope"}, ["calc.js"])[0] == []
        fake = Path(d, "fake-stryker")
        fake.write_text("#!/bin/sh\n"  # stands in for `stryker run <conf>`: records how it was called, writes a mutation-testing-elements report
                        "cat \"$2\" > \"$(dirname \"$2\")/seen.json\"; cp \"$2\" \"$MUT_SEEN\"\n"
                        "python3 - \"$2\" <<'PY'\nimport json, sys\nc = json.load(open(sys.argv[1]))\n"
                        "json.dump({'files': {'calc.js': {'mutants': [{'status': 'Killed'}] * 3 + [{'status': 'Survived'}]}}}, open(c['jsonReporter']['fileName'], 'w'))\nPY\n")
        fake.chmod(0o755)
        before = sorted(p.name for p in Path(d).iterdir())
        seen = Path(d, "seen-conf.json")
        os.environ["MUT_SEEN"] = str(seen)
        os.environ["MINI_STRYKER_BIN"] = str(fake)
        try:
            ok, det, sc = run_mutation_gate(d, {**spec, "mutation_gate": {"warn_below": 0.9}}, ["calc.js"], 300)
            conf = json.loads(seen.read_text())
            assert ok and sc == 0.75 and "WARN" in det and "calc.js:1-1" in det, (ok, det, sc)
            assert conf["mutate"] == ["calc.js:1-1"] and conf["testRunner"] == "command" and conf["commandRunner"]["command"] == spec["test_cmd"], conf
            ok, det, sc = run_mutation_gate(d, {**spec, "mutation_gate": {"fail_below": 0.8}}, ["calc.js"], 300)
            assert not ok and sc == 0.75 and "fail_below" in det, det
            os.environ["MINI_STRYKER_BIN"] = "/nonexistent/stryker"  # tool missing: skipped (ok) unless required
            ok, det, sc = run_mutation_gate(d, {**spec, "mutation_gate": {}}, ["calc.js"], 300)
            assert ok and sc is None and det.startswith("skipped") and "not installed" in det, det
            assert run_mutation_gate(d, {**spec, "mutation_gate": {"required": True}}, ["calc.js"], 300)[0] is False
        finally:
            os.environ.pop("MINI_STRYKER_BIN", None)
            os.environ.pop("MUT_SEEN", None)
        assert sorted(p.name for p in Path(d).iterdir()) == sorted(before + ["seen-conf.json"]), "gate must not write into the work dir"
        fm = Path(d, "fake-mutmut")  # `mutmut run <sel> && mutmut results --all true`
        fm.write_text("#!/bin/sh\n[ \"$1\" = run ] && { echo \"sel=$2\" > \"$MUT_SEEN\"; exit 0; }\necho '    calc.x_add__mutmut_1: killed'; echo '    calc.x_add__mutmut_2: killed'\n"
                      "echo '    calc.x_add__mutmut_3: survived'; echo '    calc.x_add__mutmut_4: killed'\n")
        fm.chmod(0o755)
        Path(d, "calc.py").write_text("def add(a, b):\n    return a - b\n")
        os.environ["MINI_MUTMUT_BIN"], os.environ["MUT_SEEN"] = str(fm), str(seen)
        try:
            ok, det, sc = run_mutation_gate(d, {"target_fn": "add", "test_file": "test_calc.py", "test_cmd": "pytest -q", "mutation_gate": {}}, ["calc.py"], 300)
            assert ok and sc is None and "selector unverified" in det and "calc.x_add__mutmut_*" in det, (det, sc)  # no explicit selector: skipped, not failed
            assert not run_mutation_gate(d, {"target_fn": "add", "test_file": "test_calc.py", "test_cmd": "pytest -q", "mutation_gate": {"required": True}}, ["calc.py"], 300)[0]
            ok, det, sc = run_mutation_gate(d, {"target_fn": "add", "test_file": "test_calc.py", "test_cmd": "pytest -q",
                                                "mutation_gate": {"mutmut_selector": "{mod}.x_{fn}__mutmut_*"}}, ["calc.py"], 300)
            assert ok and sc == 0.75 and "mutmut on calc.py:1-2" in det, (det, sc)
            assert "calc.x_add__mutmut_*" in seen.read_text()
            ok, det, sc = run_mutation_gate(d, {"target_fn": "add", "test_file": "test_calc.py", "test_cmd": "pytest -q",
                                                "mutation_gate": {"mutmut_mode": "indir"}}, ["calc.py"], 300)  # verified-recipe mode needs no selector
            assert ok and sc == 0.75 and "mutmut on calc.py:1-2" in det, (det, sc)
            assert "calc.x_add__mutmut_*" in seen.read_text()
        finally:
            os.environ.pop("MINI_MUTMUT_BIN", None)
            os.environ.pop("MUT_SEEN", None)
        ok, det, sc = run_mutation_gate(d, {**spec, "mutation_gate": {"cmd": "echo noise", "format": "line"}}, ["calc.js"], 300)
        assert ok and sc is None and "no readable result" in det, det
        ok, det, sc = run_mutation_gate(d, {**spec, "mutation_gate": {"cmd": "echo MUTATION killed=$MUT_FIRST survived={last}"}}, ["calc.js"], 300)
        assert ok and sc == 0.5 and "killed" in det, (det, sc)  # {last}/$MUT_FIRST substitution: 1 killed, 1 survived
    print("  mutation gate ok")


def _self_test_single_shot():
    global MINI, LOCAL_JSON
    """fix_mode single_shot offline: extraction/splice units, then the whole runner with a stub model helper (MINI_SINGLE_BIN) and a stub
    mini: good splice, unparseable output, wrong fence, multi-function (and NEEDS_MORE) fallback to mini, exhausted attempts, no_think."""
    py = "class A:\n    def m(self, x):\n        if x:\n            return 1\n        return 0\n    def n(self):\n        return 2\n"
    a, b = functions("a.py", py)["m"]
    new = ss_splice(py, a, b, "def m(self, x):\n    return x + 1")
    assert new == "class A:\n    def m(self, x):\n        return x + 1\n    def n(self):\n        return 2\n", new  # dedented reply re-indented
    js = CALC
    assert ss_splice(js, 1, 1, "function add(a, b) { return a + b; }") == CALC.replace("a - b", "a + b")
    assert ss_extract(SS_FENCE(SS_GOOD), "x.js", "add") == SS_GOOD
    assert ss_extract("<think>```js\nfunction add(){}\n```</think>\n" + SS_FENCE(SS_GOOD), "x.js", "add") == SS_GOOD  # fences inside thinking are ignored
    assert ss_extract(SS_FENCE("def f(x):\n    return x", "python"), "x.py", "f").startswith("def f")
    for bad, frag in (("just prose", "no fenced"), (SS_FENCE("@@ -1 +1 @@\n-a\n+b", "diff"), "tagged ```diff"), (SS_FENCE("npm test", "bash"), "tagged ```bash"),
                      (SS_FENCE("function other() {}"), "does not define `add`"), ("Thinking Process:\n1. hmm", "only reasoning"),
                      (SS_FENCE("<<<<<<< SEARCH\nfunction add(a,b){}\n=======\nx\n>>>>>>> REPLACE"), "diff / SEARCH")):
        try:
            ss_extract(bad, "x.js", "add")
        except ValueError as e:
            assert frag in str(e), (frag, str(e))
        else:
            raise AssertionError(("accepted", bad))
    try:
        ss_extract("NEEDS_MORE: also edit parse()", "x.js", "add")
    except NeedsMore as e:
        assert "parse()" in str(e)
    assert ss_toplevel_extra("function add() {}\nfunction helper() {}", "x.js", "add") == ["helper"]
    assert ss_toplevel_extra("function add() {\n  function inner() {}\n}", "x.js", "add") == []
    assert ss_toplevel_extra("def f():\n    def g(): pass\ndef h(): pass", "x.py", "f") == ["h"]
    with tempfile.TemporaryDirectory() as d:
        Path(d, "bad.py").write_text("def f(:\n")
        Path(d, "ok.js").write_text("function f() { return 1 }\n")
        Path(d, "bad.js").write_text("function f() { return ; ) }\n")
        assert "SyntaxError" in (parse_check(d, "bad.py") or "") and parse_check(d, "ok.js") is None
        if shutil.which("node"):
            assert parse_check(d, "bad.js")
    with tempfile.TemporaryDirectory() as d:  # eligibility: exactly one target function with a known, short span
        Path(d, "c.js").write_text(CALC)
        Path(d, "big.py").write_text("def big():\n" + "    x = 1\n" * 250)
        assert ss_target(d, {"target_fn": ["add", "other"]}, ["c.js"])[0] is None and ss_target(d, {}, ["c.js"])[0] is None
        assert ss_target(d, {"target_fn": "add"}, ["c.js"])[0][2:4] == (1, 1) and ss_target(d, {"target_fn": "nope"}, ["c.js"])[0] is None
        assert "251 lines" in ss_target(d, {"target_fn": "big"}, ["big.py"])[1] and ss_target(d, {"target_fn": "big", "single_shot_max_lines": 300}, ["big.py"])[0]
    venv_py = Path(MINI).parent / "python"
    if venv_py.exists():  # the helper builds the SAME sampling as the overlay, no_think only flips enable_thinking
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import single_shot_call as h\n"
                "t = open(sys.argv[2]).read(); a = h.kwargs_for(t, False); b = h.kwargs_for(t, True, 100)\n"
                "print('JSON' + json.dumps([a, b]))")
        out = subprocess.run([str(venv_py), "-c", code, str(MINI_EXT), str(OVERLAY)], capture_output=True, text=True).stdout
        ka, kb = json.loads(out.split("JSON", 1)[1])
        assert ka["temperature"] == 0.6 and ka["presence_penalty"] == 1.0 and ka["max_tokens"] == 12288 and ka["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True, ka
        assert kb["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False, "preserve_thinking": True} and kb["temperature"] == 0.6 and kb["timeout"] == 100, kb
    for ov, want in ((DIAG_OVERLAY, "READ-ONLY"), (TEST_OVERLAY, "WRITE A FAILING TEST"), (POLISH_OVERLAY, "EDIT ONE SECTION")):  # mini's own merge + render: the stage's workflow, not the fix one
        if not (venv_py.exists() and ov.is_file()):
            continue
        code = ("import sys, jinja2\nfrom minisweagent.config import get_config_from_spec as g\nfrom minisweagent.utils.serialize import recursive_merge as m\n"
                "c = m(g('mini.yaml'), g(sys.argv[1]), g(sys.argv[2]))\n"
                "print(jinja2.Template(c['agent']['instance_template'], undefined=jinja2.StrictUndefined).render("
                "task='TASKTEXT', system='Darwin', release='r', version='v', machine='m'))\n"
                "print('STEPS', c['agent']['step_limit'], c['model']['model_class'])")
        r = subprocess.run([str(venv_py), "-c", code, str(OVERLAY), str(ov)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]
        out = r.stdout
        assert want in out and "TASKTEXT" in out and "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in out, (ov, out[:600])
        assert "Edit the source code" not in out and "Please solve this issue" not in out and "implement the necessary changes" not in out, (ov, out[:600])
        assert "STEPS 60 mini_ctx.CtxLitellmModel" in out, out[-200:]  # everything else still comes from the fix overlay
    if not shutil.which("node"):
        return
    old = MINI, LOCAL_JSON, os.environ.get("STUB_PLAN"), os.environ.get("MINI_SINGLE_BIN"), os.environ.get("SS_PLAN")
    with tempfile.TemporaryDirectory() as d:
        repo = _toy(d)
        stub, helper = Path(d) / "mini", Path(d) / "ss_helper.py"
        stub.write_text(STUB_MINI); stub.chmod(0o755); helper.write_text(STUB_SS)
        Path(d, "local.json").write_text(json.dumps({"base_url": "http://127.0.0.1:9", "api_key": "k-SECRET"}))
        MINI, LOCAL_JSON = str(stub), Path(d, "local.json")
        base = {"mode": "two-phase", "allowed": ["calc.js"], "baseline_from": str(repo), "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js",
                "target_fn": "add", "timeout": 600, "fix_attempts": 1, "fix_mode": "single_shot"}
        wrong = SS_FENCE("function add(a, b) {\n  // sumFix\n  return a * b;\n}")
        cases = {  # name: (spec extras, model replies, mini B file written on fallback, want rc, RESULT fragment, model calls, mini B runs)
            "good": ({}, [SS_FENCE(SS_GOOD)], None, 0, "PASS", 1, 0),
            "unparseable": ({}, [SS_FENCE("function add(a, b) {\n  // sumFix\n  return a + ;\n}"), SS_FENCE(SS_GOOD)], None, 0, "PASS", 2, 0),
            "wrong fence": ({}, [SS_FENCE("@@ -1 +1 @@\n-a\n+b", "diff"), SS_FENCE(SS_GOOD)], None, 0, "PASS", 2, 0),
            "multi function": ({}, [SS_FENCE(SS_GOOD + "\nfunction helper() { return 1; }")], CALC_FIXED, 0, "fix_mode=single_shot>mini", 1, 1),
            "needs more": ({}, ["NEEDS_MORE: the parser also has to change"], CALC_FIXED, 0, "fix_mode=single_shot>mini", 1, 1),
            "attempts exhausted": ({}, [wrong], CALC_FIXED, 0, "fix_mode=single_shot>mini", 3, 1),
            "retries 0": ({"single_shot_retries": 0}, [wrong], CALC_FIXED, 0, "fix_mode=single_shot>mini", 1, 1),
            "best of 2": ({"best_of": 2, "single_shot_retries": 0}, [wrong, SS_FENCE(SS_GOOD)], None, 0, "PASS", 2, 0),
            "model error": ({"single_shot_retries": 0}, [{"error": "APIConnectionError: refused"}], CALC_FIXED, 0, "fix_mode=single_shot>mini", 1, 1),
            "no think": ({"no_think": {"single": True}}, [SS_FENCE(SS_GOOD)], None, 0, "PASS", 1, 0),
            "mutation warn": ({"mutation_gate": {"cmd": "echo MUTATION killed=2 survived=2", "warn_below": 0.6}}, [SS_FENCE(SS_GOOD)], None, 0, "mutation_score=0.50", 1, 0),
            "mutation fail": ({"mutation_gate": {"cmd": "echo MUTATION killed=1 survived=3", "fail_below": 0.5}}, [SS_FENCE(SS_GOOD)], None, 1, "mutation=FAIL", 1, 0)}
        try:
            for name, (extra, replies, mini_b, want_rc, want, calls, mini_runs) in cases.items():
                run = Path(d) / name.replace(" ", "-")
                (run / "work").mkdir(parents=True)
                log = run / "ss.log"
                Path(run, "plan.json").write_text(json.dumps({"A": {"calc.test.js": GOOD_T}, "B": ({"calc.js": mini_b} if mini_b else {}), "argv_dump": str(run / "argv.txt")}))
                Path(run, "ss-plan.json").write_text(json.dumps({"replies": [{"content": r} if isinstance(r, str) else r for r in replies], "log": str(log)}))
                Path(run, "spec.json").write_text(json.dumps({**base, **extra}))
                env = {**os.environ, "STUB_PLAN": str(run / "plan.json"), "SS_PLAN": str(run / "ss-plan.json"), "MINI_BIN": str(stub),
                       "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON), "MINI_SINGLE_BIN": f"{sys.executable} {helper}"}
                r = subprocess.run([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")], capture_output=True, text=True, env=env)
                res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
                assert r.returncode == want_rc and res and want in res[-1], (name, r.returncode, r.stdout[-2500:], r.stderr[-800:])
                reqs = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
                argv = Path(run, "argv.txt").read_text().splitlines() if Path(run, "argv.txt").exists() else []
                assert len(reqs) == calls and sum(1 for x in argv if x.startswith("B ")) == mini_runs, (name, len(reqs), argv, r.stdout[-2000:])
                assert all(x["key_ok"] for x in reqs) and "k-SECRET" not in r.stdout and "[worker] USAGE SUMMARY" in r.stdout, name
                if name == "unparseable":
                    assert "no longer parses" in reqs[1]["prompt"] and "return a + ;" in reqs[1]["prompt"], reqs[1]["prompt"][-800:]
                if name == "wrong fence":
                    assert "tagged ```diff" in reqs[1]["prompt"], reqs[1]["prompt"][-600:]
                if name == "attempts exhausted":
                    assert "the acceptance test still fails" in reqs[1]["prompt"] and "return a * b" in reqs[2]["prompt"], reqs[2]["prompt"][-900:]
                if name == "good":
                    pr = reqs[0]["prompt"]
                    assert "PHASE" not in pr and "   1\tfunction add(a, b) { return a - b; }" in pr and "Other functions in the file (name:first-last): other:2-2" in pr and "calc.test.js" in pr, pr[-1500:]
                    assert "Must contain" in pr and "acceptance test" in pr and not reqs[0]["no_think"]
                    assert Path(run, "work/calc.js").read_text() == CALC.replace("function add(a, b) { return a - b; }", SS_GOOD), Path(run, "work/calc.js").read_text()
                    assert "single-shot 1/3: phase B gate GREEN" in r.stdout and "B-single-shot: attempts=1 green=True" in (run / "REVIEW.md").read_text()
                    assert "prompt_tokens=" in r.stdout and "fix_mode=single_shot " in res[-1] + " "
                if name == "no think":
                    assert reqs[0]["no_think"] is True
                if mini_b:
                    assert Path(run, "work/calc.js").read_text() == mini_b  # restored to baseline, then the mini loop's edit
        finally:
            MINI, LOCAL_JSON = old[0], old[1]
            for k, v in zip(("STUB_PLAN", "MINI_SINGLE_BIN", "SS_PLAN"), (old[2], old[3], old[4])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _self_test_outage(d, base, stub):
    """Provider outage (job 7531cd855697): a run whose model calls all hit 'capacity unavailable' ends PAUSED (exit 3, no gate, no RESULT line,
    queue-readable pause marker); an outage the client rode out is excluded from the budgets and the run is graded normally; a genuine
    abnormal exit still fails as before."""
    # an older mini_ctx's outage line (no credited suffix: inferred from "outage so far"), then today's lines: an outage nap, a
    # hold credited while the call waits, the rest of it when the answer comes
    wait = "[provider-outage] unavailable (BadRequestError); waiting 15s, outage so far 15s of 604800s (waiting for Darkbloom, phase=switching, 0m)"
    cwait = wait + "; credited 15.0s"
    held = ("[provider-hold] the call has waited 40s so far in the relay for the provider (a Darkbloom Sandbox hold or a model reload); "
            "credited 40.0s")
    hold = ("[provider-hold] the call took 70s, 50s of it in the relay waiting for the provider (a Darkbloom Sandbox hold or a model "
            "reload); credited 10.0s")
    cases = {"outage-paused": ({"A": {}, "B": {}, "exit_status": "ProviderUnavailable", "outage_lines": [wait, wait.replace("15s, outage so far 15s", "30s, outage so far 45s")]}, 3),
             "outage-rode-out": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED}, "outage_lines": [cwait, held, hold, cwait.replace("so far 15s", "so far 75s").replace("credited 15.0s", "credited 60.0s")]}, 0),
             "model-gone": ({"A": {}, "B": {}, "exit_status": "ModelNotServed"}, 1),
             "genuine-failure": ({"A": {}, "B": {}, "exit_status": "RepeatedFormatError"}, 1)}
    for name, (plan, want_rc) in cases.items():
        run = Path(d) / name
        (run / "work").mkdir(parents=True)
        Path(run, "plan.json").write_text(json.dumps(plan))
        Path(run, "spec.json").write_text(json.dumps(base))
        os.environ["STUB_PLAN"] = str(run / "plan.json")
        r = subprocess.run([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                           capture_output=True, text=True, env={**os.environ, "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
        res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        assert r.returncode == want_rc, (name, r.returncode, r.stdout[-1200:], r.stderr[-600:])
        if name == "outage-paused":
            pf = run / "work.provider-pause.json"
            info = json.loads(pf.read_text())
            assert not res and "Phase A REJECTED" not in r.stdout and "skeleton placeholder" not in r.stdout, (res, r.stdout[-800:])  # nothing graded
            assert f"[worker] RESUMABLE TRANSCRIPT: {pf.resolve()}" in r.stdout and "PAUSED" in r.stdout, r.stdout[-800:]
            assert info["pause_reason"] == "chat_request_failed" and info["pause_meta"]["lane_cause"].startswith("darkbloom capacity wait"), info
            assert "provider unavailable 45s" in r.stdout, r.stdout[-800:]  # the 15s then 30s steps of one episode add up to 45, not 60
            assert "k-SECRET" not in r.stdout, "key leaked"
        elif name == "outage-rode-out":
            # 15 s, then a call whose relay wait was credited 40 s while it waited and 10 s when it was answered, then 60 s more
            assert res and res[-1].startswith("RESULT PASS") and "provider unavailable 125s" in r.stdout, (res, r.stdout[-800:])
        elif name == "model-gone":  # failed fast as infrastructure: one RESULT, no gate, no pause marker
            assert len(res) == 1 and res[0].startswith("RESULT FAIL model-not-served") and "class=INFRA" in res[0], (res, r.stdout[-800:])
            assert "PAUSED" not in r.stdout and "Phase A REJECTED" not in r.stdout and not (run / "work.provider-pause.json").exists(), r.stdout[-800:]
            # stage 24: its incident went to the self-test's scratch log (_scratch_incidents), never the live one
            ip = os.environ.get("INFRA_INCIDENTS_PATH") or ""
            assert "mini-selftest-incidents-" in ip, f"the self-test runs with a scratch incident log, not {ip or 'the default'}"
            if (Path(__file__).resolve().parent / "infra_incidents.py").is_file():
                assert Path(ip).is_file() and '"kind": "model-not-served"' in Path(ip).read_text(errors="replace"), ip
        else:
            assert res and res[-1].startswith("RESULT FAIL") and "PAUSED" not in r.stdout, (res, r.stdout[-800:])
    # the time clocks: an outage gives back both the total deadline and the budget clock
    saved = _OUTAGE[0], _DEADLINE[0]
    try:
        _OUTAGE[0], _DEADLINE[0] = 0.0, None
        t = _now()
        note_outage(100)
        assert _OUTAGE[0] == 100 and _now() < t - 99 and _DEADLINE[0] is None
        assert OUTAGE_LINE.search(wait).group(1) == "15" and WALL_BACKSTOP == 4 and not CREDIT_LINE.search(wait)
        assert [CREDIT_LINE.search(x).group(1) for x in (cwait, held, hold)] == ["15.0", "40.0", "10.0"] and not OUTAGE_LINE.search(hold)
        assert not CREDIT_LINE.search("echo " + held) and not CREDIT_LINE.search(held + " ")
        assert credit_cap(680.0, 0.0, 15.0) == 135.0 and credit_cap(10.0, 1000.0, 0.0) == 10.0 and credit_cap(5.0, 0.0, 200.0) == 0.0
        assert REMOTE_LOG_PASS_RE.match(hold) and REMOTE_LOG_PASS_RE.match(wait)
    finally:
        _OUTAGE[0], _DEADLINE[0] = saved


def _self_test_breadth(d, base, stub):
    """spec breadth_retry: a green fix whose mutation run names a NARROWING `breadth` survivor gets ONE local retry. A green changed
    fix replaces it (mutation re-run); a red one, an edited test, a stray file or an edit outside allowed puts the green fix back byte
    for byte; an unchanged fix keeps the model's claim for the packet; no time left, or the flag off, means no retry."""
    det = ("custom on a.py:1-9: score=0.50 (2/4 killed); survivors: a.py:3 breadth: n.startswith('ab') -> ((n) == 'ab') ;; "
           "a.py:4 breadth: n == 'x' -> (str(n).startswith('x')) ;; a.py:5 const: 'x' -> '' ;; a.py:6 breadth: 'log' in p -> ((p) == 'log') ;; (+2 more)")
    assert narrow_survivors(det) == [("a.py:3", "n.startswith('ab')", "((n) == 'ab')"), ("a.py:6", "'log' in p", "((p) == 'log')")], narrow_survivors(det)
    assert narrow_survivors("score=1.00 (4/4 killed)") == [] and narrow_survivors(None) == []
    assert narrow_survivors(det, {"a.py": "def f(n, p):\n    return n.startswith('ab')\n", "b.py": "'log' in p"}) == [  # the baseline's own match: not the fix's
        ("a.py:6", "'log' in p", "((p) == 'log')")], narrow_survivors(det, {"a.py": "n.startswith('ab')"})
    assert narrow_survivors(det, {"a.py": None}) == narrow_survivors(det)  # a file the fix created
    tj = Path(d, "said.traj.json")
    tj.write_text(json.dumps({"messages": [
        {"role": "user", "content": 'BREADTH a.py:9 narrowed: "a prompt example, not a claim"'},
        {"role": "assistant", "content": 'BREADTH a.py:3 kept: "x"'},
        {"role": "assistant", "content": [{"type": "text", "text": '**BREADTH `a.py:3`** narrowed: `"only the name ab"`\n- BREADTH a.py:6 kept: "all | any" **'}]},
        {"role": "assistant", "content": 'BREADTH <file>:<line> kept: "<the TASK.md words>"'}]}))
    assert breadth_said(tj) == ['a.py:3 narrowed: "only the name ab"', 'a.py:6 kept: "all / any"'], breadth_said(tj)
    assert breadth_said(Path(d, "no-such.traj.json")) == []
    broad = CALC_FIXED + "// startsWith\n"  # the fake mutation run below reports a narrowing survivor while calc.js says startsWith
    mut = {"cmd": "if grep -q startsWith calc.js; then echo 'MUTATION killed=3 survived=1'; "
                  "echo \"survived calc.js:1 breadth: s.startsWith('ab') -> ((s) == 'ab')\"; else echo 'MUTATION killed=4 survived=0'; fi", "format": "line"}
    Path(d, "r3").mkdir()
    repo3 = _toy(Path(d, "r3"))  # a second tracked file: an edit outside allowed must be put back too
    (repo3 / "util.js").write_text("module.exports = 1;\n")
    for c in (["add", "util.js"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "util"]):
        subprocess.run(["git", *c], cwd=repo3, check=True, capture_output=True)
    Path(d, "r4").mkdir()
    repo4, old = _toy(Path(d, "r4")), "// s.startsWith('ab') was the old rule\n"  # the baseline already makes the survivor's match: not the fix's
    (repo4 / "calc.js").write_text(old + CALC)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "old"], cwd=repo4, check=True, capture_output=True)
    said_n, said_k = 'BREADTH calc.js:1 narrowed: "TASK.md names add only"', 'BREADTH calc.js:1 kept: "every name that starts with ab"'
    back = "so the green fix was put back; model: calc.js:1 narrowed"
    cases = {"narrowed": ({"calc.js": CALC_FIXED}, said_n, {}, CALC_FIXED, ["asked: ", "fix changed (the diff is the result); model: calc.js:1 narrowed: \"TASK.md names add only\""]),
             "red": ({"calc.js": CALC_FIXED.replace("a + b", "a * b")}, said_n, {}, broad, ["asked: ", "the changed fix failed (", "test_green: ", back]),
             "kept": ({}, said_k, {}, broad, ["asked: ", "fix unchanged; model: calc.js:1 kept: \"every name that starts with ab\""]),
             "test edit": ({"calc.js": CALC_FIXED, "calc.test.js": GOOD_T + "// x\n"}, said_n, {}, broad, ["asked: ", "failed (test_unchanged: the frozen test was edited)", back]),
             "stray": ({"calc.js": CALC_FIXED, "helper.js": "x"}, said_n, {}, broad, ["asked: ", "the changed fix failed (", "scope: ", back]),
             "outside": ({"calc.js": CALC_FIXED, "util.js": "module.exports = 2;\n"}, said_n, {"baseline_from": str(repo3)}, broad, ["asked: ", "scope: ", back]),
             "off": ({"calc.js": CALC_FIXED}, said_n, {"breadth_retry": None}, broad, None),
             "baseline": ({"calc.js": CALC_FIXED}, said_n, {"baseline_from": str(repo4), "_B": old + broad}, old + broad, None),
             "short": ({"calc.js": CALC_FIXED}, said_n, {"timeout": 600}, broad, ["not asked (only "])}
    for name, (bn, say, extra, want_calc, want_row) in cases.items():
        run = Path(d) / ("breadth-" + name.replace(" ", "-"))
        (run / "work").mkdir(parents=True)
        tasks = run / "tasks.txt"
        Path(run, "plan.json").write_text(json.dumps({"A": {}, "B": {}, "task_dump": str(tasks), "argv_dump": str(run / "argv.txt"), "by_task": [
            {"match": "PHASE 1 OF 2", "A": {"calc.test.js": GOOD_T}}, {"match": "LAST CHECK", "B": bn, "say": say},
            {"match": "PHASE 2 OF 2: MAKE", "B": {"calc.js": extra.get("_B", broad)}}]}))
        spec = {k: v for k, v in {**base, "timeout": 1500, "breadth_retry": True, "mutation_gate": mut, **extra}.items() if v is not None and k[0] != "_"}
        Path(run, "spec.json").write_text(json.dumps(spec))
        r = subprocess.run([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")], capture_output=True, text=True,
                           env={**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
        res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        assert r.returncode == 0 and res and res[-1].startswith("RESULT PASS"), (name, r.returncode, res, r.stdout[-1500:], r.stderr[-600:])
        assert (run / "work/calc.js").read_text() == want_calc and (run / "work/calc.test.js").read_text() == GOOD_T, (name, r.stdout[-1500:])
        rv, asked = (run / "REVIEW.md").read_text(), tasks.read_text().count("LAST CHECK")
        row = _auto_breadth(run)
        if want_row is None:
            assert not row and "breadth=" not in res[-1] and asked == 0, (name, row, res[-1])
            assert name != "baseline" or "calc.js:1 breadth: s.startsWith('ab')" in rv, rv[:2500]  # listed, just not asked
            continue
        assert all(w in row for w in want_row) and row.startswith(want_row[0]) and "calc.js:1 s.startsWith('ab')" in row \
            and "breadth=ok" in res[-1], (name, row, rv[:2500])
        assert asked == (0 if name == "short" else 1), (name, asked)
        mrow = re.search(r"^\| mutation \| \w+ \| (.*)$", rv, re.M).group(1)
        assert ("survivors:" not in mrow) == (name == "narrowed"), (name, mrow)  # re-run on the changed fix only
        if name == "narrowed":
            t = tasks.read_text().split("=====")
            bt = next(x for x in t if "LAST CHECK" in x)
            assert "- `calc.js:1`: `s.startsWith('ab')` (the test also passes with `((s) == 'ab')`)" in bt and "+// startsWith" in bt, bt[:1500]
            assert "--- TASK.md ---" in bt and "BREADTH <file>:<line> narrowed" in bt, bt[:1500]
            assert f"agent.step_limit={BREADTH_STEPS}" in (run / "argv.txt").read_text().splitlines()[-1], (run / "argv.txt").read_text()
        if name == "stray":
            assert not (run / "work/helper.js").exists(), name
        if name == "outside":
            assert (run / "work/util.js").read_text() == "module.exports = 1;\n" and not run_git(run / "work", "status", "--short", "util.js").stdout.strip(), name


def _self_test_e2e():
    global MINI, LOCAL_JSON
    old = MINI, LOCAL_JSON, os.environ.get("STUB_PLAN")
    with tempfile.TemporaryDirectory() as d:
        repo = _toy(d)
        stub = Path(d) / "mini"
        stub.write_text(STUB_MINI)
        stub.chmod(0o755)
        Path(d, "local.json").write_text(json.dumps({"base_url": "http://127.0.0.1:9", "api_key": "k-SECRET"}))
        MINI, LOCAL_JSON = str(stub), Path(d, "local.json")
        try:
            base = {"mode": "two-phase", "allowed": ["calc.js"], "baseline_from": str(repo), "test_file": "calc.test.js",
                    "test_cmd": "node --test calc.test.js", "target_fn": "add", "timeout": 600, "fix_attempts": 1}
            dump = Path(d, "agent-env.txt")
            kdirs = set(glob.glob(os.path.join(tempfile.gettempdir(), "mini-k-*")))
            dead = CALC_FIXED + "function unused(x) {\n  const y = x * 3;\n  const z = y + 1;\n  return z;\n}\n"
            Path(d, "r2").mkdir()  # a baseline whose header comment states the old rule (bfmr-split-tiebreak's stale-doc shape)
            repo2, doc = _toy(Path(d, "r2")), "// add returns the difference of a and b\n"
            (repo2 / "calc.js").write_text(doc + CALC)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "doc"], cwd=repo2, check=True, capture_output=True)
            stale_task = Path(d, "stale-TASK.md")
            stale_task.write_text("# TASK\n`add(a, b)` must return the sum; update its comment.\n\n## Must contain\n\n- `sumFix`\n\n"
                                  "## Must not contain\n\n- `returns the difference`\n")
            tdump = {k: Path(d, f"{k}-tasks.txt") for k in ("pass", "stale")}
            plans = {"pass": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED}, "expect_key": "k-SECRET", "env_dump": str(dump),
                               "task_dump": str(tdump["pass"])}, 0, "test_green=ok"),
                     "ws noise": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED.replace("other() {", "other()  {")}}, 0, "ws_only=ok"),
                     "untested hunk": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED.replace("other };", "other, extra: 1 };")},
                                        "spec": {"fn_scope": "off"}}, 0, "PASS needs-review soft=fix_hunks"),
                     "stale doc": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": doc + CALC_FIXED}, "task_dump": str(tdump["stale"]),
                                    "spec": {"baseline_from": str(repo2), "task": str(stale_task)}}, 0, "PASS needs-review soft=must_not_contain"),
                     "dead code": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": dead}}, 1, "diff_reach=FAIL"),
                     "edits test": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED, "calc.test.js": GOOD_T + "\n"}}, 1, "test_unchanged=FAIL"),
                     "no literal": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC.replace("a - b", "a + b")}}, 0, "PASS needs-review soft=literals"),  # soft gate only: amber needs-review, rc 0
                     "stray file": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED, "helper.js": "x"}}, 1, "scope=FAIL"),
                     "unedited skeleton": ({"A": {}, "B": {}}, 1, "skeleton placeholder"),
                     "bad test": ({"A": {"calc.test.js": GOOD_T.replace("3);", "-1);").replace("4);", "-6);")}, "B": {}, "say": "I wrote the test"}, 1, "test-gate"),
                     # the baselines run on a copy beside the work dir while phase A works; a copy that breaks mid-run falls back
                     "bg suite": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED}, "spec": {"suite_cmd": "sleep 2; true"}}, 0, "suite=ok"),
                     "bg fallback": ({"A": {"calc.test.js": GOOD_T}, "B": {"calc.js": CALC_FIXED},
                                      "spec": {"suite_cmd": 'case $PWD in */wor0) rm -rf "$PWD";; esac; true', "typecheck_cmd": "true"}}, 0, "typecheck=ok")}
            for name, (plan, want_rc, want) in plans.items():
                run = Path(d) / name.replace(" ", "-")
                (run / "work").mkdir(parents=True)
                Path(run, "plan.json").write_text(json.dumps(plan))
                Path(run, "spec.json").write_text(json.dumps({**base, **plan.get("spec", {})}))
                os.environ["STUB_PLAN"] = str(run / "plan.json")
                r = subprocess.run([sys.executable, __file__, "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                                   capture_output=True, text=True, env={**os.environ, "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(LOCAL_JSON)})
                res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
                assert r.returncode == want_rc and res and want in res[-1], (name, r.returncode, r.stdout[-1500:], r.stderr[-800:])
                assert "k-SECRET" not in r.stdout and "[worker] USAGE SUMMARY" in r.stdout, name
                rv = (run / "REVIEW.md").read_text()  # every outcome, phase A rejection included, leaves a review packet
                assert "k-SECRET" not in rv, name
                assert "wrote test skeleton calc.test.js" in r.stdout, name
                if name == "unedited skeleton":
                    assert "TODO(mini-skeleton)" in rv and "Phase A REJECTED" in rv, rv[:600]
                if name == "bad test":
                    assert "Phase A REJECTED" in rv and "passes at baseline" in rv and "I wrote the test" in rv, rv[:800]
                if name == "ws noise":  # the double space was put back; the real edit stayed
                    assert (run / "work/calc.js").read_text() == CALC_FIXED and "| ws_only | ok | restored the original whitespace on 1 line(s)" in rv, rv[:1500]
                if name == "untested hunk":  # needs-review, never fed back: one attempt, the hunk named in the packet
                    assert "fix_hunks=FAIL" in res[-1] and "| fix_hunks | FAIL | 1 of 2 code hunk(s)" in rv and "calc.js:3-4" in rv, rv[:1500]
                    assert "PASS needs-review soft=fix_hunks" in rv, rv[:600]
                if name == "stale doc":
                    assert "| must_not_contain | FAIL | still present" in rv and "returns the difference" in rv, rv[:1500]
                if name == "bg suite":
                    assert "baselines run in the background on a copy of the work dir (wor0) while phase A works" in r.stdout, r.stdout[-2000:]
                    assert re.search(r"baseline suite_cmd: rc=0 failure lines=0 \([2-9]s\)", r.stdout), r.stdout[-2000:]  # sleep 2, under load
                if name == "bg fallback":
                    # py3.14 shows a Path filename as PosixPath('...'): one more ")"
                    assert re.search(r"background baselines failed \(FileNotFoundError: .*wor0'\)?\): running them here", r.stdout), r.stdout[-2000:]
                    assert "baseline typecheck_cmd: rc=0 failure lines=0" in r.stdout, r.stdout[-2000:]
                assert not (run / "wor0").exists(), (name, "the baselines' copy was left behind")
                assert "[mini-runner] context A1: steps=1" in r.stdout, name
            assert "`## Must not contain` list" in tdump["stale"].read_text() and "Must not contain" not in tdump["pass"].read_text()
            _self_test_outage(d, base, stub)
            env_seen = dump.read_text()  # what an agent command saw
            assert "PATH=" in env_seen and "k-SECRET" not in env_seen and "MINI_KEY_FILE" not in env_seen and "OPENAI_API_KEY" not in env_seen
            assert set(glob.glob(os.path.join(tempfile.gettempdir(), "mini-k-*"))) == kdirs, "key file left behind"
            _self_test_steps(d, repo, stub)
            _self_test_replay(d, repo, stub, base)
            _self_test_draft(d, repo, stub, base)
            _self_test_signals(d, base)
            _self_test_signals_bg(d, base)
            _self_test_breadth(d, base, stub)
        finally:
            MINI, LOCAL_JSON = old[0], old[1]
            if old[2] is None:
                os.environ.pop("STUB_PLAN", None)
            else:
                os.environ["STUB_PLAN"] = old[2]


if __name__ == "__main__":
    sys.exit(main())
