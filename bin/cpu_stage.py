#!/usr/bin/env python3
"""cpu_stage.py -- the pipeline's single entry point for moving a WHOLE CPU stage onto the
Unraid CPU runner, with an automatic, loud fallback to the stage's own local code.

    import cpu_stage
    r = cpu_stage.run_stage(wt, "bash verify.sh", 600, stage="preflight-verify",
                            bundle_id=cpu_stage.bundle_key(wt=wt),
                            local_fn=lambda: (rc, stdout, stderr, timed_out))
    r.exit_code, r.stdout, r.stderr, r.timed_out, r.ran_on, r.why

Contract (Phase 6, 2026-10-08):
  * ONE log line per stage on stderr (+ JSONL ~/.ollama-dispatch/cpu-lane.log):
        [cpu-lane] stage=X path=runner|local reason=... snap=... exit=... wall=...
  * KILL SWITCH: env CPU_LANE=0|off|false|no, or the file ~/.ollama-dispatch/cpu-lane.disabled,
    forces the local path with NO lane markers at all (exactly the pre-lane behaviour).
  * The local path is the CALLER'S OWN code (`local_fn`), never a re-implementation, so a
    fallback is byte-for-byte what the stage always produced.
  * ELIGIBILITY (the runner has NO network and a different toolchain): only node projects
    with a package-lock.json whose verify needs nothing but what the runner has. Anything
    that needs network, secrets, host paths we cannot ship, sqlite3 or a venv/pip install runs
    local (see eligibility()). First `npm ci` happens inside the runner's dep phase (network
    allowed there ONLY, cached by lockfile hash); the stage itself never installs.
  * TREE-LOCK / STALE SNAPSHOTS: callers keep holding their worktree tree lock around
    run_stage (the runner works on a shipped copy, the lock keeps protecting the real
    target meanwhile). A snapshot hash = sha256(HEAD sha + `git diff --binary` of a throwaway
    index) is taken BEFORE shipping and re-checked AFTER the result lands; if the worktree
    moved meanwhile (a writer ignoring the lock) the remote result is DISCARDED and the stage
    re-runs locally. The hash also travels in the job env (DISPATCH_SNAPSHOT) and label.
  * Never raises for lane/infrastructure reasons.

Seams are test-patchable: `_submit`, `_runner_online`, `_marker`, `snapshot`.
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import ast
import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE_DIR = HOME / ".ollama-dispatch"
DISABLE_FILE = STATE_DIR / "cpu-lane.disabled"
LOG_FILE = STATE_DIR / "cpu-lane.log"
HOST_BIN = str(HOME / "bin")

# Tools every remote stage may need, shipped under $JOB_TOOLS/bin (and $HOME/bin -> that dir
# on the runner, so the stages' own `Path.home()/"bin"/X` lookups resolve unchanged).
_TOOL_FILES = ("verify-relevance.py", "pgrun.py", "source_text_harness.py", "plan_lint.py",
               "cpu_vr_measure.py",
               # verify-relevance imports mutant_sweep (equivalent-mutant exclusion + witnesses). Not
               # shipped -> on the runner the import failed silently (rec["sweep_error"]), every
               # equivalent survivor stayed counted and a correct fixture read 'low' (cardBonuses-v2
               # 964c4a833b82, gate relevance ran path=runner). Keep in sync: test-cpu-stage-ships-vr-imports.py
               "mutant_sweep.py",
               # parity: stages that shell out to the GPU runner by path
               "gpu-exclusive-runner.py")
_TS_FILES = ("ts-mutator/ts-parse.mjs", "ts-mutator/ts-mutator.mjs")
_TS_PKG = ("ts-mutator/node_modules/typescript/package.json",
           "ts-mutator/node_modules/typescript/lib/typescript.js")

# Host paths a verify may legitimately name are exactly the ones host_tools() ships (anything else
# under /Users, $HOME or ~ makes the stage ineligible).
_NEVER_RE = re.compile(r"security\s+find-|osascript|launchctl|\bbrew\b|\bpip3?\s+install|\bcurl\b|\bwget\b|"
                       r"\bgit\s+(push|fetch|clone|pull)\b|\bssh\b|keychain", re.I)
_HOSTPATH_RE = re.compile(r"(/Users/[^\s'\"):;]+|\$HOME[/\w.-]*|\$\{HOME\}[/\w.-]*|~/[\w./-]*)")
_TEXT_HARNESS = ("verify.sh", "check_literals.py", "auto-harness-check.py", "refimpl.py")


class StageResult:
    def __init__(self, exit_code, stdout="", stderr="", timed_out=False, ran_on="local", why="",
                 job_id=None, snap=None, wall=0.0, timings=None):
        self.exit_code, self.stdout, self.stderr, self.timed_out = exit_code, stdout, stderr, timed_out
        self.ran_on, self.why, self.job_id, self.snap, self.wall = ran_on, why, job_id, snap, wall
        self.timings = timings or {}

    @property
    def fell_back(self):
        return self.ran_on == "local" and bool(self.why)

    def __repr__(self):
        return "StageResult(exit=%r ran_on=%s why=%r)" % (self.exit_code, self.ran_on, self.why)


# ----------------------------------------------------------------------------- switches
def kill_switch():
    """(True, why) when the lane is disabled."""
    v = (os.environ.get("CPU_LANE") or "").strip().lower()
    if v in ("0", "off", "false", "no", "disabled"):
        return True, "kill switch: CPU_LANE=%s" % v
    try:
        if DISABLE_FILE.exists():
            return True, "kill switch: %s exists" % DISABLE_FILE
    except OSError:
        pass
    return False, ""


def _log(stage, bundle, path, why, snap, exit_code, wall, job_id=None, owner=None):
    line = "[cpu-lane] stage=%s bundle=%s path=%s reason=%s snap=%s exit=%s wall=%.1fs%s" % (
        stage, bundle or "-", path, why or "ok", (snap or "-")[:14], exit_code, wall,
        (" job=" + job_id) if job_id else "")
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    if os.environ.get("CPU_LANE_LOG_OFF") == "1":      # canary/tests must not pollute the real lane log
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({"t": int(time.time()), "stage": stage, "bundle": bundle, "path": path,
                                "reason": why, "snap": snap, "exit": exit_code,
                                "wall": round(wall, 2), "job": job_id,
                                **({"owner": owner} if owner else {})}) + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------- local-running (display only)
# 2026-10-09: a stage that falls back to the Mac was invisible on the dashboard's CPU LANE panel (the
# scheduling marker above is opt-in because it frees the GPU lane). This record is DISPLAY ONLY: a tiny
# JSON file per running local stage, never read by outstanding_by_bundle()/bundle_commit_status, so it
# cannot change any scheduling. Stale-proof: valid only while its pid is alive AND its heartbeat (file
# mtime, touched by a daemon thread) is fresh AND it is younger than timeout_s + slack.
LOCAL_RUN_HB_S = 10
LOCAL_RUN_STALE_S = 60


def _local_run_dir():
    return Path(os.environ.get("CPU_LANE_LOCAL_RUN_DIR") or (STATE_DIR / "cpu-lane-local-running"))


def local_run_begin(stage, bundle, why, timeout_s, owner=None):
    """Register a display-only 'running locally' record; returns end() (idempotent). Never raises."""
    if os.environ.get("CPU_LANE_LOG_OFF") == "1" and not os.environ.get("CPU_LANE_LOCAL_RUN_DIR"):
        return lambda: None                      # canary/tests must not pollute the real dashboard
    try:
        import threading
        d = _local_run_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("%d-%d.json" % (os.getpid(), int(time.time() * 1000)))
        rec = {"pid": os.getpid(), "stage": stage, "bundle": bundle, "reason": why or "",
               "started": time.time(), "timeout_s": float(timeout_s or 600), "owner": owner or ""}
        tmp = str(p) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, str(p))
        # the fallback REASON is logged at stage START (not only when it ends): a "start" event with no
        # `path` key, so why_local()/the lane-log readers (which count rows that have a path) ignore it.
        try:
            sys.stderr.write("[cpu-lane] START stage=%s bundle=%s path=local reason=%s pid=%d\n" % (
                stage, bundle or "-", why or "local", os.getpid()))
            sys.stderr.flush()
        except Exception:
            pass
        try:
            if os.environ.get("CPU_LANE_LOG_OFF") == "1":
                raise OSError("lane log off")
            with open(LOG_FILE, "a") as lf:
                lf.write(json.dumps({"t": int(time.time()), "event": "local-start", "stage": stage,
                                     "bundle": bundle, "reason": why or "", "pid": os.getpid()}) + "\n")
        except Exception:
            pass
        stop = threading.Event()

        def beat():
            while not stop.wait(LOCAL_RUN_HB_S):
                try:
                    os.utime(str(p))
                except OSError:
                    return
        threading.Thread(target=beat, name="cpu-local-hb", daemon=True).start()

        def end():
            stop.set()
            try:
                p.unlink()
            except OSError:
                pass
        return end
    except Exception:                                # noqa: BLE001
        return lambda: None


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def short_reason(why):
    """One-line, human reason a stage ran locally (also the grouping key for the 'why local' summary)."""
    w = (why or "").strip()
    if w.startswith("ineligible: "):
        w = w[len("ineligible: "):]
    m = re.match(r"(\S+) uses node:test mock\.module \(runner node (\d+) API differs from host node (\d+)\)", w)
    if m:
        return "node:test mock.module needs node %s, runner has %s" % (m.group(3), m.group(2))
    if w.startswith("not a node project"):     # keep the cause: "(no package.json)" is implied, the rest is the reason
        rest = re.sub(r"^not a node project(?: \([^)]*\))?:?\s*", "", w)
        return ("not a node project: " + rest)[:110] if rest else "not a node project"
    w = re.sub(r"[0-9a-f]{12}:[0-9a-f]{8,}", "<snap>", w)
    w = re.sub(r"\b\d+(\.\d+)?s\b", "Ns", w)
    return (w or "local fallback")[:110]


def local_running(now=None):
    """[{stage, bundle, reason, running_s}] of live display-only local-stage records (stale ones ignored
    and reaped). Never raises."""
    now = now or time.time()
    out = []
    try:
        for p in sorted(_local_run_dir().glob("*.json")):
            try:
                rec = json.loads(p.read_text())
                hb = p.stat().st_mtime
            except (OSError, ValueError):
                continue
            age = now - float(rec.get("started") or now)
            if (not _pid_alive(rec.get("pid")) or now - hb > LOCAL_RUN_STALE_S
                    or age > float(rec.get("timeout_s") or 600) + 300):
                if now - hb > LOCAL_RUN_STALE_S or not _pid_alive(rec.get("pid")):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                continue
            out.append({"stage": rec.get("stage"), "bundle": rec.get("bundle"), "owner": rec.get("owner") or "", "path": "local",
                        "pid": rec.get("pid"), "started": rec.get("started"),
                        "reason": short_reason(rec.get("reason")), "running_s": round(age, 1)})
    except Exception:                                # noqa: BLE001
        pass
    return out


def why_local(n=50, log_file=None):
    """Routing decisions of the last n lane-log lines grouped by (path, short reason):
    {"n": int, "groups": [{"path", "reason", "count"}...]} sorted by count desc. Never raises."""
    lf = Path(log_file or LOG_FILE)
    rows = []
    try:
        size = lf.stat().st_size
        with open(lf, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", "replace")
        for line in data.splitlines()[-(n + 1):]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("path"):
                rows.append(rec)
    except OSError:
        pass
    rows = rows[-n:]
    g = {}
    for r in rows:
        key = (r["path"], "ok" if r["path"] == "runner" else short_reason(r.get("reason")))
        g[key] = g.get(key, 0) + 1
    groups = [{"path": k[0], "reason": k[1], "count": c} for k, c in sorted(g.items(), key=lambda kv: -kv[1])]
    return {"n": len(rows), "groups": groups}


# ----------------------------------------------------------------------------- owner (display provenance)
def owner_label(wt, stage=None, explicit=None):
    """Human label for WHO submitted a CPU stage, so a bare 12-hex worktree/snapshot hash on the dashboard
    is attributable: env CPU_LANE_OWNER / explicit wins; else derived from the worktree path
    (ab-sampling replay set, slice worktree, plain wt-<label>) plus the calling tool. Display only; never raises."""
    try:
        tag = (explicit or os.environ.get("CPU_LANE_OWNER") or "").strip()
        if tag:
            return tag[:160]
        tool = Path(sys.argv[0] or "").name or "python"
        for pre in ("ollama-dispatch-", "ollama-"):
            if tool.startswith(pre):
                tool = tool[len(pre):]
                break
        tool = re.sub(r"\.py$", "", tool)
        ps = str(wt)
        name = Path(ps).name
        m = re.search(r"/ab-sampling/([^/]+)/wt/", ps + "/")
        if m:
            return ("%s: ab-sampling %s %s" % (tool, m.group(1), name))[:160]
        for p in ("wt-slice-", "wt-"):
            if name.startswith(p):
                return ("slice " if p == "wt-slice-" else "%s: " % tool) + name[len(p):][:140]
        return ("%s: %s" % (tool, name))[:160]
    except Exception:                                # noqa: BLE001
        return ""


# ----------------------------------------------------------------------------- bundle key
QUEUE_STATE = queue_config.state_dir() / "ollama-queue-state.json"
QUEUE_LOGS = queue_config.state_dir() / "ollama-queue-logs"


def _queue_row_bundle(job_id=None, label=None):
    """The REAL queue bundle tag (`bundle` field) of a job: the live row off ollama-queue-state.json
    first, then the durable <id>.done.json sidecar (the queue prunes finished rows at once). By job id,
    else by exact label among live rows. None on any trouble (fail-open)."""
    def _live():
        jobs = json.loads(QUEUE_STATE.read_text()).get("jobs") or []
        return list(jobs.values()) if isinstance(jobs, dict) else jobs
    try:
        rows = []
        if job_id:
            try:
                rows += [j for j in _live() if isinstance(j, dict) and j.get("id") == job_id]
            except Exception:                        # noqa: BLE001
                pass
            try:
                rows.append(json.loads((QUEUE_LOGS / ("%s.done.json" % job_id)).read_text()))
            except Exception:                        # noqa: BLE001
                pass
        elif label:
            rows = [j for j in _live() if isinstance(j, dict) and j.get("label") == label]
        for r in rows:
            t = r.get("bundle") if isinstance(r, dict) else None
            if isinstance(t, str) and t.strip():
                return t.strip()
    except Exception:                                # noqa: BLE001
        pass
    return None


def bundle_key(label=None, explicit=None, wt=None, job_id=None):
    """The queue bundle key (what `pk(j)` / job_group_key return) so the lane release matches
    the bundle's rows. explicit tag wins; $DISPATCH_BUNDLE next; then the REAL `bundle` tag of the
    queue row (job_id, else $OLLAMA_DISPATCH_VIA_QUEUE, else an exact live-label match); else
    job_group_key(label); else a label guessed from the worktree dir name (wt-slice-<plan>-<sid>-...,
    wt-<label>). The row lookup is what stops a stage nesting under a fake per-job header."""
    tag = (explicit or os.environ.get("DISPATCH_BUNDLE") or "").strip()
    if tag:
        return tag
    tag = _queue_row_bundle(job_id=job_id or os.environ.get("OLLAMA_DISPATCH_VIA_QUEUE") or None)
    if tag:
        return tag
    if label:
        tag = _queue_row_bundle(label=label)
        if tag:
            return tag
    if not label and wt:
        n = Path(str(wt)).name
        for p in ("wt-slice-", "wt-"):
            if n.startswith(p):
                n = n[len(p):]
                break
        label = n
        tag = _queue_row_bundle(label=label)
        if tag:
            return tag
    if not label:
        return None
    try:
        import importlib.util as iu
        sp = iu.spec_from_file_location("_olq_bundle_key", str(HERE / "ollama-queue.py"))
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m.job_group_key({"label": label}) or label
    except Exception:
        return label


# ----------------------------------------------------------------------------- snapshot
def _git(wt, *args, env=None):
    r = subprocess.run(["git", "-C", str(wt)] + list(args), capture_output=True, env=env, timeout=120)
    if r.returncode:
        raise RuntimeError("git %s: %s" % (args[0], r.stderr[-200:].decode("utf-8", "replace")))
    return r.stdout


def snapshot(wt):
    """HEAD sha + hash of (tracked + untracked non-ignored) changes, via a throwaway index --
    the same view cpu_job.make_payload ships. Never touches the real index."""
    head = _git(wt, "rev-parse", "--verify", "HEAD").decode().strip()
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "idx"))
        _git(wt, "read-tree", "HEAD", env=env)
        _git(wt, "add", "-A", "--", ".", env=env)
        patch = _git(wt, "diff", "--cached", "--binary", "HEAD", env=env)
    return "%s:%s" % (head[:12], hashlib.sha256(patch).hexdigest()[:24])


# ----------------------------------------------------------------------------- eligibility
_SHIPPED_CACHE = []


def _shipped_host_paths():
    """Host paths (under ~/bin) the runner gets via host_tools(), so a verify naming them is fine."""
    if not _SHIPPED_CACHE:
        _SHIPPED_CACHE.append({HOST_BIN + "/" + n for n in _TOOL_FILES + _TS_FILES + _TS_PKG}
                              | {HOST_BIN + "/ollama-queue.py"})
    return _SHIPPED_CACHE[0]


def _find_pkg_dir(wt, cwd_rel):
    d = (Path(wt) / (cwd_rel or ".")).resolve()
    root = Path(wt).resolve()
    while True:
        if (d / "package.json").is_file():
            return d
        if d == root or root not in d.parents:
            return None
        d = d.parent


_BIN_PKG = {"tsc": "typescript", "tsserver": "typescript"}     # binary name -> npm package
_SHIPPED_NPX = {"tsx"}          # tools we can put in node_modules/.bin on the runner (see tsx_bundle)


def _uses_prisma(wt):
    """True when the worktree has any prisma schema/config (tracked or untracked, non-ignored). Conservative:
    True on any error. The dispatch scaffold's verify.sh always CONTAINS `npx --yes prisma ...` but only runs it
    behind `[ -f prisma/schema.prisma ]`-style guards, so a repo without one never reaches the network fallback."""
    try:
        out = _git(wt, "ls-files", "-co", "--exclude-standard", "--", "*.prisma", "prisma.config.*", "*/prisma.config.*")
        return bool(out.strip())
    except Exception:                                 # noqa: BLE001
        return True


def _lock_packages(pkg_dir):
    try:
        d = json.loads((pkg_dir / "package-lock.json").read_text())
        return {k.split("node_modules/")[-1] for k in (d.get("packages") or {}) if k}
    except Exception:
        return set()


def tsx_needed(wt, cmd, cwd_rel=None):
    """True when a verify text falls back to `npx --yes tsx` and the lockfile does not carry tsx."""
    pkg = _find_pkg_dir(wt, cwd_rel)
    if pkg is None:
        return False
    if "tsx" in _lock_packages(pkg):
        return False
    for n in _TEXT_HARNESS:
        p = Path(wt) / n
        try:
            if p.is_file() and re.search(r"npx\s+(?:--yes|-y)\s+tsx\b", p.read_text(errors="replace")):
                return True
        except OSError:
            pass
    return bool(re.search(r"npx\s+(?:--yes|-y)\s+tsx\b", cmd if isinstance(cmd, str) else " ".join(cmd)))


TSX_BUNDLE = STATE_DIR / "cpu-lane" / "tsx-linux-x64.tgz"


def tsx_bundle():
    """Path of a linux-x64 tsx (+esbuild) tarball, built once on the host with network (cached).
    None when it cannot be built -> the stage stays local."""
    if TSX_BUNDLE.is_file() and TSX_BUNDLE.stat().st_size > 1_000_000:
        return str(TSX_BUNDLE)
    try:
        TSX_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["npm", "init", "-y"], cwd=td, capture_output=True, timeout=60, check=True)
            subprocess.run(["npm", "i", "--no-audit", "--no-fund", "--os=linux", "--cpu=x64", "tsx"],
                           cwd=td, capture_output=True, timeout=240, check=True)
            tmp = str(TSX_BUNDLE) + ".tmp"
            subprocess.run(["tar", "czf", tmp, "-C", td, "node_modules"], capture_output=True,
                           timeout=120, check=True)
            os.replace(tmp, str(TSX_BUNDLE))
        return str(TSX_BUNDLE)
    except Exception:
        return None


LEGACY_RUNNER_NODE = 22            # a runner that reports no caps (pre-1.3 image) is known to have node 22
MOCK_MODULE_EXPORTS_MIN_NODE = 23  # node:test mock.module `{ exports }` shape (22 only has namedExports/defaultExport)
_PIP_BOOTSTRAP_RE = re.compile(r"\.venv/bin/pip\s+install\s+(?:-q\s+)?-r\s+requirements\.txt")
_LEGACY_NONODE = "not a node project (no package.json): runner python/toolchain differs from the host"  # old logs; kept for readers
# The two causes the old text hid (2026-11-02): which one applies is what the dashboard should show.
_NONODE_NO_PY = "not a node project (no package.json): no root-level *.py, so runner parity cannot be checked"
_NONODE_NO_CAPS = "not a node project (no package.json): the runner reports no python caps (offline or pre-1.3 image)"
_HOST_NODE = []


def _host_node_major():
    """Major of the host's node (display only, for the refusal reason); cached; 0 when unknown."""
    if not _HOST_NODE:
        try:
            v = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=10).stdout.strip()
            _HOST_NODE.append(int(v.lstrip("v").split(".")[0]))
        except Exception:                                   # noqa: BLE001
            _HOST_NODE.append(0)
    return _HOST_NODE[0]


def _host_python_minor():
    return os.environ.get("CPU_LANE_HOST_PYTHON") or "%d.%d" % sys.version_info[:2]


def _py_files_named(text):
    return re.findall(r"""[\w./-]+\.py\b""", text or "")


def python_third_party_imports(wt, texts, cwd_rel=None):
    """Third-party top-level modules a python stage imports, or None when they cannot be determined.
    Seeds: the harness .py files, test_*.py / verify*.py, the harness target (.dispatch-harness.json) and every
    existing root .py named in the stage's texts or in string literals of an analysed file (the target is often
    loaded by path, e.g. arr-webhook.py). Closure over root-level local modules. Dynamic imports are not seen."""
    std = getattr(sys, "stdlib_module_names", None)
    if std is None:
        return None
    root = Path(wt)
    try:
        local = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if p.is_dir()}
    except OSError:
        return None
    todo = [root / n for n in _TEXT_HARNESS if n.endswith(".py")] + sorted(root.glob("test_*.py")) \
        + sorted(root.glob("verify*.py"))
    try:
        tgt = json.loads((root / ".dispatch-harness.json").read_text()).get("target")
        if isinstance(tgt, str):
            todo.append(root / tgt)
    except Exception:                                       # noqa: BLE001
        pass
    for t in texts.values():
        todo += [root / n for n in _py_files_named(t)]
    seen, found = set(), set()
    while todo and len(seen) < 300:
        p = todo.pop()
        try:
            if p in seen or not p.is_file() or root.resolve() not in p.resolve().parents:
                continue
            seen.add(p)
            tree = ast.parse(p.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue          # an unparsable file fails identically anywhere (the stage reports it)
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith(".py"):
                todo.append(root / n.value)
            for m in mods:
                if m in local:
                    todo.append(root / (m + ".py"))
                elif m not in std:
                    found.add(m)
    return found


# Runtime deps a static import scan cannot see: (imported module, usage regex, any-of module names that satisfy it, display).
# 2026-10-14 voicemail-inbox: fastapi raises at route definition for Form/File/UploadFile without python-multipart; the runner
# advertised fastapi but not multipart, so the refimpl went red there ("contract-broken") while VERIFY_OK on the host.
_IMPLIED_RUNTIME = (("fastapi", re.compile(r"\b(?:Form|File|UploadFile)\b"), ("python_multipart", "multipart"),
                     "python-multipart (fastapi Form/File/UploadFile)"),)


def implied_runtime_missing(wt, need, have):
    """Display names of runtime deps the stage needs (per _IMPLIED_RUNTIME) that `have` lacks. Root .py files only."""
    out = []
    for mod, use_re, provides, name in _IMPLIED_RUNTIME:
        if mod not in need or any(p in have for p in provides):
            continue
        for f in sorted(Path(wt).glob("*.py")):
            try:
                if f.stat().st_size < 2_000_000 and use_re.search(f.read_text(errors="replace")):
                    out.append(name)
                    break
            except OSError:
                continue
    return out


def _python_stage_check(wt, texts, cwd_rel, caps):
    """(ok, why) for a project with no package.json. Only a runner that reports a python matching the host's
    minor AND the modules the stage imports qualifies; anything else keeps today's local fallback."""
    root = Path(wt)
    if not list(root.glob("*.py")):
        return False, _NONODE_NO_PY
    rp = (caps or {}).get("python")
    if not rp:
        return False, _NONODE_NO_CAPS           # old runner image / caps unknown: stays local as before
    host = _host_python_minor()
    if rp != host:
        return False, "python project: runner python %s differs from host python %s" % (rp, host)
    need = python_third_party_imports(wt, texts, cwd_rel)
    if need is None:
        return False, "python project: cannot determine its third-party imports on this host"
    missing = sorted(need - set(caps.get("py_modules") or ()))
    if missing:
        return False, "python project imports %s, which the runner image does not carry (offline)" % ", ".join(missing[:8])
    implied = implied_runtime_missing(wt, need, set(caps.get("py_modules") or ()))
    if implied:
        return False, "python project needs %s at runtime, which the runner image does not carry (offline)" % ", ".join(implied)
    return True, ""


# ----------------------------------------------------------------------------- sub-package env-parity block
# 2026-10-14 (egiftRedemption/egiftPrint ran 'CPU LANE relevance' on the Mac): the scaffold's TS bootstrap embeds
# `if [ -d '/Users/..../sidecar/node_modules' ]; then ln -s ... else echo WARN ...; fi` (best-effort env parity for a
# sub-package with its own node_modules). That absolute path tripped _HOSTPATH_RE and pinned the stage local. The
# runner only installs the ROOT lockfile, so the link can never happen there -- the block degrades to its WARN
# branch, which is harmless exactly when nothing the test loads needs that sub-package's deps. We prove that
# before dropping the block from the host-path scan; otherwise the stage stays local (unchanged behaviour).
# Kill switch: CPU_LANE_SUBPKG_GUARD=0.
_SUBPKG_MARK = "# --- sub-package node_modules (env parity) ---"
_SUBPKG_RE = re.compile(r"^" + re.escape(_SUBPKG_MARK) + r".*?^fi[ \t]*$\n?", re.S | re.M)
_SUBPKG_REL_RE = re.compile(r"^if \[ ! -e ('[^']+'|\"[^\"]+\"|[^\s\]]+) \]; then$", re.M)
_REQ_RE = re.compile(r"""(?:\brequire\(\s*|\bimport\s*\(\s*|\bfrom\s+|\bimport\s+)['"]([^'"]+)['"]""")
_SRC_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".json")
_NODE_BUILTINS = {"assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https", "net", "os",
                  "path", "readline", "stream", "test", "timers", "tls", "url", "util", "vm", "worker_threads",
                  "zlib", "module", "process", "querystring", "string_decoder", "dns", "cluster", "perf_hooks",
                  "v8", "tty", "dgram", "async_hooks", "inspector", "constants", "punycode", "http2", "console"}


def _bare_pkg(spec):
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _subpkg_deps_unneeded(wt, rel_nm):
    """True when no file reachable (relative require/import graph) from the worktree's verify.test.* files
    imports a bare package that the sub-package at dirname(rel_nm) declares but the root lockfile lacks.
    Conservative: any unreadable/unresolvable edge or sub-package dep reached -> False."""
    wt = Path(wt).resolve()
    sub = wt / Path(rel_nm).parent
    try:
        sub_deps = set()
        pj = json.loads((sub / "package.json").read_text())
        for k in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            sub_deps |= set((pj.get(k) or {}))
    except Exception:
        return False
    root_lock = _lock_packages(wt) if (wt / "package-lock.json").is_file() else set()
    seen, todo = set(), [p for p in sorted(wt.glob("verify.test.*")) if p.is_file()]
    if not todo:
        return False
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        if len(seen) > 400:
            return False
        if f.suffix == ".json":
            continue
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            return False
        for spec in _REQ_RE.findall(txt):
            if spec.startswith("."):
                base = (f.parent / spec).resolve()
                cands = [base] + [Path(str(base) + e) for e in _SRC_EXT] + [base / ("index" + e) for e in _SRC_EXT]
                hit = next((c for c in cands if c.is_file()), None)
                if hit is None or wt not in hit.resolve().parents:
                    return False
                todo.append(hit.resolve())
            elif spec.startswith("/"):
                return False
            elif not (spec.startswith("node:") or _bare_pkg(spec) in _NODE_BUILTINS):
                pk = _bare_pkg(spec)
                if pk in sub_deps and pk not in root_lock:
                    return False
    return True


def subpkg_lock_dirs(wt, text):
    """Relative sub-package dirs named by the scaffold env-parity block(s) in verify.sh `text` that carry their
    OWN package.json + package-lock.json (the runner can `npm ci` them: spec.extra_pkg_dirs). [] when none."""
    out = []
    if os.environ.get("CPU_LANE_SUBPKG_GUARD") == "0" or _SUBPKG_MARK not in text:
        return out
    wt = Path(wt).resolve()
    for m in _SUBPKG_RE.finditer(text):
        blk = m.group(0)
        r = _SUBPKG_REL_RE.search(blk)
        if not r or "WARN: sub-package deps" not in blk:
            continue
        rel = r.group(1).strip("'\"")
        if rel.startswith(("/", "..")):
            # the scaffold embeds the absolute Mac path of <wt-repo>/<sub>/node_modules; only the part below the
            # checkout matters, and the worktree mirrors the repo layout -> take the trailing "<sub>"
            parts = Path(rel).parts
            if "node_modules" not in parts:
                continue
            sub = "/".join(parts[parts.index("node_modules") - 1:parts.index("node_modules")])
        else:
            sub = str(Path(rel).parent)
        if not sub or sub in (".", "..") or sub.startswith(("/", "..")):
            continue
        d = wt / sub
        if (d / "package.json").is_file() and (d / "package-lock.json").is_file() and sub not in out:
            out.append(sub)
    return out


def strip_subpkg_parity(wt, text, caps=None):
    """verify.sh text with the scaffold's sub-package env-parity block removed, but only when
    _subpkg_deps_unneeded proves the runner's WARN branch is harmless, OR the sub-package has its own lockfile and
    the runner reports subpkg_install (it then npm-ci's the sub-package itself). Otherwise text unchanged."""
    if os.environ.get("CPU_LANE_SUBPKG_GUARD") == "0" or _SUBPKG_MARK not in text:
        return text
    if (caps or {}).get("subpkg_install"):
        subs = subpkg_lock_dirs(wt, text)
        if subs:
            return _SUBPKG_RE.sub("", text)
    out = text
    for m in list(_SUBPKG_RE.finditer(text)):
        blk = m.group(0)
        r = _SUBPKG_REL_RE.search(blk)
        if not r:
            continue
        rel = r.group(1).strip("'\"")
        if "WARN: sub-package deps" not in blk or rel.startswith(("/", "..")):
            continue
        if _subpkg_deps_unneeded(wt, rel):
            out = out.replace(blk, "")
    return out


def eligibility(wt, cmd, cwd_rel=None, shipped_extra=(), caps=None):
    """(ok, why). Conservative: anything the runner cannot reproduce faithfully stays local.
    shipped_extra: tool names ('bin/vr-args.json') the caller ships beside host_tools(); a cmd naming
    $HOME/bin/<that> is fine (2026-10-09: the relevance stage named its own shipped args file and was
    declared host-only -> ran 17 min locally).
    caps: what the runner reports about itself (cpu_dispatch.combine_caps: node, python, py_modules, sqlite3).
    None / {} = a legacy runner (node 22, no job python, no sqlite3), i.e. exactly the pre-caps behaviour."""
    caps = caps or {}
    runner_node = caps.get("node") if isinstance(caps.get("node"), int) else LEGACY_RUNNER_NODE
    extra = {HOST_BIN + "/" + n[4:] for n in (shipped_extra or ()) if n.startswith("bin/")}
    wt = Path(wt)
    pkg = _find_pkg_dir(wt, cwd_rel)
    pymode = False
    if pkg is not None and not (pkg / "package-lock.json").is_file():
        return False, "package.json without package-lock.json: the runner cannot install offline"
    texts = {"cmd": cmd if isinstance(cmd, str) else " ".join(cmd)}
    for n in _TEXT_HARNESS:
        p = wt / n
        if p.is_file():
            try:
                texts[n] = p.read_text(errors="replace")
            except OSError:
                pass
    if pkg is None:
        ok_py, why_py = _python_stage_check(wt, texts, cwd_rel, caps)
        if not ok_py:
            return False, why_py
        pymode = True
    for _ft in sorted(wt.glob("verify.test.*")):
        try:
            _s = _ft.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"\bmock\.module\(", _s) and runner_node < MOCK_MODULE_EXPORTS_MIN_NODE:
            # 2026-10-09 (rt-walmart-cancel-import): a node 22 runner's mock.module takes
            # namedExports/defaultExport; these fixtures use the node 23+ `{ exports }` shape, so every
            # case 500s on the runner while passing on the host (node 26). Capability-driven since the
            # runner reports its node major: a node>=23 runner passes this; a legacy/22 runner still refuses.
            return False, "%s uses node:test mock.module (runner node %d API differs from host node %d)" % (
                _ft.name, runner_node, _host_node_major())
    for n, t in texts.items():
        # comments are not behaviour: drop whole-line comments before scanning
        if n == "verify.sh":
            t = strip_subpkg_parity(wt, t, caps)
        t = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith(("#", "//")))
        if pymode:
            # the scaffold's venv bootstrap (`.venv/bin/pip install -q -r requirements.txt`) fails fast offline
            # (PIP_NO_INDEX on the runner) and falls back to the baked python; the imports check above already
            # proved the baked set covers what the stage needs. Any OTHER pip install still needs network.
            t = _PIP_BOOTSTRAP_RE.sub("", t)
        m = _NEVER_RE.search(t)
        if m:
            return False, "%s needs network/secrets/host services (%s)" % (n, m.group(0).strip())
        for hp in _HOSTPATH_RE.findall(t):
            ship = _shipped_host_paths() | extra
            if hp in ship or (
                    hp.startswith(("~/bin/", "$HOME/bin/", "${HOME}/bin/")) and
                    HOST_BIN + "/" + hp.split("/bin/", 1)[1] in ship):
                continue
            return False, "%s names a host-only path (%s)" % (n, hp[:80])
    # `npx --yes X` fallbacks: the runner has no network, so X must come from the lockfile install
    # (or be a tool we ship: tsx).
    lock_pk = _lock_packages(pkg)
    for n, t in texts.items():
        for tool in re.findall(r"npx\s+(?:--yes|-y)\s+(@?[\w./-]+)", t):
            if tool in lock_pk or _BIN_PKG.get(tool) in lock_pk or (tool in _SHIPPED_NPX and tsx_bundle()):
                continue
            if tool == "prisma" and not _uses_prisma(wt):
                continue        # the scaffold's `npx --yes prisma` branches are dead code without a prisma schema
            return False, "%s runs `npx --yes %s` but %s is not in package-lock.json (needs network)" % (n, tool, tool)
    # the prisma migration gate in the scaffold's verify.sh shells out to sqlite3 (not in the image)
    # only when migrations/schema changed; replicate its trigger.
    if "sqlite3" in texts.get("verify.sh", "") and not (caps.get("sqlite3") or os.environ.get("CPU_RUNNER_HAS_SQLITE")):
        try:
            st = _git(wt, "status", "--porcelain", "--untracked-files=all", "--", "prisma").decode()
        except Exception:
            st = "?"
        if st.strip():
            return False, "prisma schema/migrations changed and the runner image has no sqlite3"
    return True, ""


# ----------------------------------------------------------------------------- tools + cmd
def _scaffold_basenames_stub():
    """A stand-in ollama-queue.py (verify-relevance imports it for _SCAFFOLD_BASENAMES) built
    from the REAL value, so the runner copy agrees with the host byte for byte."""
    try:
        import importlib.util as iu
        sp = iu.spec_from_file_location("_olq_stub_src", str(HERE / "ollama-queue.py"))
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        names = sorted(getattr(m, "_SCAFFOLD_BASENAMES", None) or [])
        if names:
            return ("_SCAFFOLD_BASENAMES = frozenset(%r)\n" % (names,)).encode()
    except Exception:
        pass
    return None


def host_tools(extra=None):
    """{name: path|bytes} for cpu_job.make_tools_tgz; every name lives under bin/ (-> $HOME/bin)."""
    t = {}
    for n in _TOOL_FILES:
        p = HERE / n
        if p.is_file():
            t["bin/" + n] = str(p)
    for n in _TS_FILES + _TS_PKG:
        p = HERE / n
        if p.is_file():
            t["bin/" + n] = str(p)
    stub = _scaffold_basenames_stub()
    if stub:
        t["bin/ollama-queue.py"] = stub
    t.update(extra or {})
    return t


def venv_prelude(wt, cwd_rel=None):
    """Runner prelude for a python stage that has a requirements.txt: pre-create `.venv` with
    --system-site-packages (and no pip) so the scaffold's venv bootstrap resolves to the BAKED interpreter.
    2026-10-14 (voicemail-ui-s4-apilist re-validation): the older scaffold bootstrap is
    `python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt` with no `|| rm -rf .venv`, so
    offline (PIP_NO_INDEX) the pip step failed but the empty, isolated venv survived; `PY=.venv/bin/python`
    then could not import fastapi and the refimpl went red on the runner ("contract-broken") while green
    on the host. The venv carries a .pth naming python3's own site dirs (the runner's python3 is a venv). eligibility() already proved the baked set carries every module the stage imports, so
    seeing it from the venv is exactly the faithful run. A newer bootstrap (`.venv/bin/pip install ... ||
    rm -rf .venv`) finds no pip, removes the venv and falls back to python3: also the baked set.
    Kill switch: CPU_LANE_VENV_SITE=0."""
    if os.environ.get("CPU_LANE_VENV_SITE") == "0":
        return ""
    try:
        if _find_pkg_dir(wt, cwd_rel) is not None:
            return ""
        if not (Path(wt) / (cwd_rel or ".") / "requirements.txt").is_file():
            return ""
    except OSError:
        return ""
    # The runner's python3 is itself a venv (/opt/venv over /opt/python), so --system-site-packages alone
    # reaches /opt/python's site-packages, NOT the baked /opt/venv set: a .pth carries python3's own site dirs.
    return ('{ [ -e .venv ] || { python3 -m venv --system-site-packages --without-pip .venv >/dev/null 2>&1 && '
            'python3 -c \'import glob,site;[open(d+"/_dispatch_baked.pth","w").write(chr(10).join(site.getsitepackages())+chr(10))'
            ' for d in glob.glob(".venv/lib/python*/site-packages")]\'; }; }; ')


def remote_cmd(cmd, rewrite=True, group=False, harness=False):
    """Wrap `cmd` for the runner: $HOME/bin -> the shipped tools; host /Users/<u>/bin/ paths in the
    harness files of the (shipped copy only) rewritten to the same place. Plain POSIX sh/bash."""
    pre = ('mkdir -p "$HOME" && ln -sfn "$JOB_TOOLS/bin" "$HOME/bin"; ')
    if harness:
        # .dispatch-harness.json is git-excluded (info/exclude) so the snapshot never carries it, yet
        # verify-relevance --applied reads it (creation_task target) -> on the runner it saw "no tracked
        # diff" (rc=3) for every creation task. Restore the shipped copy at the checkout root.
        pre += ('_r=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_r" ] && [ ! -e "$_r/.dispatch-harness.json" ] '
                '&& cp "$JOB_TOOLS/bin/dispatch-harness.json" "$_r/.dispatch-harness.json"; ')
    if "auto-harness-check.py" in (cmd if isinstance(cmd, str) else " ".join(cmd)):
        # auto-harness-check.py is git-excluded too (info/exclude), so the snapshot never carries it:
        # on the runner `python3 auto-harness-check.py` died "No such file" (rc=2) and --resume-harness
        # refused a converged harness. Restore the shipped copy at the checkout root.
        pre += ('_r=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_r" ] && [ ! -e "$_r/auto-harness-check.py" ] '
                '&& cp "$JOB_TOOLS/bin/auto-harness-check.py" "$_r/auto-harness-check.py"; ')
    if rewrite:
        hb = shlex.quote(HOST_BIN + "/")
        pre += ('for _f in $(grep -lIsF %s ./* 2>/dev/null); do '
                'sed -i "s#%s#$HOME/bin/#g" "$_f"; done; ' % (hb, HOST_BIN + "/"))
    body = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
    return pre + (("( " + body + "\n)") if group else body)


def passthrough_env(extra=None):
    """The env a local run would see that the runner will accept (VERIFY_*, DISPATCH_*, TEST_*, TZ...)."""
    e = {k: v for k, v in os.environ.items() if re.match(r"^(VERIFY_|DISPATCH_|TEST_)", k)}
    e.update({k: str(v) for k, v in (extra or {}).items()})
    return e


# ----------------------------------------------------------------------------- remote seam
def _runner_online():
    try:
        import cpu_dispatch as cd
        cj = cd._load_client()
        api = os.environ.get("CPU_RUNNER_API") or cd.DEFAULT_API
        return cd._runner_online(api, cj._token()), api, cj
    except Exception:
        return False, None, None


def _runner_caps(api, cj):
    """Combined capability report of the fresh runners (cpu_dispatch.combine_caps) or None = unknown, which
    eligibility() treats exactly like a legacy runner. Never raises."""
    try:
        import cpu_dispatch as cd
        return cd._runner_caps(api, cj._token())
    except Exception:
        return None


def _marker(stage, bundle_id, timeout_s):
    try:
        import cpu_dispatch as cd
        return cd._marker(stage, bundle_id, timeout_s)
    except Exception:
        return lambda code=None: None


SATURATION_WAIT_S_DEFAULT = 1200     # how long a stage queues for a busy-but-alive runner before the Mac may take it


def saturation_wait_s():
    """Bounded wait (s) for a free runner slot. operator 2026-10-10: the Mac runs CPU-lane stages ONLY when Unraid
    is saturated (every slot busy for this long) or down/ineligible -- never merely because the runner is busy."""
    try:
        return max(0.0, float(os.environ.get("CPU_LANE_SATURATION_WAIT_S", SATURATION_WAIT_S_DEFAULT)))
    except ValueError:
        return float(SATURATION_WAIT_S_DEFAULT)


def effective_claim_timeout(caller_s):
    """claim timeout actually used: the caller's value is a floor, the saturation wait the normal case."""
    return max(float(caller_s or 0), saturation_wait_s())


def routing_decision(runner_fresh, waited_s, sat_wait_s):
    """PURE. What a queued-but-unclaimed stage does next: 'wait' | 'local:runner-down' | 'local:saturated'."""
    if not runner_fresh:
        return "local:runner-down"
    if waited_s > sat_wait_s:
        return "local:saturated"
    return "wait"


def _runner_down_probe():
    """should_abort callable for cpu_job: truthy reason only when the runner heartbeat is stale/unreachable."""
    def probe():
        online, _api, _cj = _runner_online()
        return None if online else "runner offline (heartbeat stale) while the stage waited for a slot"
    return probe


def _submit(cj, api, wt, cmd, timeout_s, stage, bundle_id, tools, env, cwd_rel, label, claim_timeout_s, owner=None, extra_pkg_dirs=None):
    """Raises RuntimeError when the runner path is unavailable (cpu_job fallback=False)."""
    return cj.submit_cpu_job(wt, cmd, timeout_s, cwd_rel=cwd_rel, env=env, stage=stage, label=label,
                             bundle_id=bundle_id, tools=tools, mode="bundle", api=api,
                             claim_timeout_s=effective_claim_timeout(claim_timeout_s), fallback=False,
                             should_abort=_runner_down_probe(), owner=owner,
                             extra_pkg_dirs=extra_pkg_dirs or None)


def decode_gzb64(s):
    return gzip.decompress(base64.b64decode((s or "").strip())).decode("utf-8", "replace")


GZB64_SUFFIX = (' | python3 -c "import sys,gzip,base64;'
                'sys.stdout.write(base64.b64encode(gzip.compress(sys.stdin.buffer.read())).decode())"')


# ----------------------------------------------------------------------------- the entry point
def _norm_local(v):
    if isinstance(v, StageResult):
        return v
    rc, so, se, to = (list(v) + [False])[:4] if len(v) == 3 else v
    return StageResult(rc, so or "", se or "", bool(to), "local")


def run_stage(wt, cmd, timeout_s, stage, bundle_id, local_fn, *, tools=None, env=None, cwd_rel=None,
              check=None, claim_timeout_s=60, gzb64=False, tolerate_nonzero=True):
    """Run one CPU stage remotely when it can faithfully run there, else via `local_fn()` (a callable
    returning (exit_code, stdout, stderr, timed_out) or a StageResult). `check(StageResult)` may veto
    a remote result (return a reason string) -> local re-run. gzb64: the command's stdout is
    gzip+base64 transported (big JSON) and decoded here."""
    t0 = time.time()
    bundle_id = bundle_id or bundle_key(wt=wt)
    owner = owner_label(wt, stage)
    off, why = kill_switch()
    if off:
        r = _norm_local(local_fn())
        r.why = why
        _log(stage, bundle_id, "local", why, None, r.exit_code, time.time() - t0, owner=owner)
        return r
    why = None
    snap0 = None
    job_id = None
    extra_dirs = []
    try:
        # the runner is asked FIRST: eligibility compares the stage's needs to the capabilities it reports
        # (node major, python minor + installed modules, sqlite3). Unknown caps == a legacy runner.
        online, api, cj = _runner_online()
        up = bool(online and cj is not None)
        _caps = _runner_caps(api, cj) if up else None
        ok, why_e = eligibility(wt, cmd, cwd_rel, shipped_extra=tuple((tools or {}).keys()), caps=_caps)
        if ok and (_caps or {}).get("subpkg_install"):
            try:
                extra_dirs = subpkg_lock_dirs(wt, (Path(wt) / "verify.sh").read_text(errors="replace"))
            except OSError:
                extra_dirs = []
        if not ok:
            why = "ineligible: " + why_e          # (reason priority unchanged: ineligible beats offline)
        elif not up:
            why = "no CPU runner online"
        else:
            snap0 = snapshot(wt)
    except Exception as e:                               # noqa: BLE001
        why = "lane precheck error: %s: %s" % (type(e).__name__, e)
    if why is None:
        try:
            xt = dict(tools or {})
            pre = ""
            if tsx_needed(wt, cmd, cwd_rel):
                xt["bin/tsx-linux-x64.tgz"] = tsx_bundle()
                pre = ('mkdir -p "$JOB_TOOLS/tsx" && tar xzf "$JOB_TOOLS/bin/tsx-linux-x64.tgz" -C "$JOB_TOOLS/tsx" '
                       '&& mkdir -p node_modules/.bin && { [ -e node_modules/.bin/tsx ] || '
                       'ln -s "$JOB_TOOLS/tsx/node_modules/.bin/tsx" node_modules/.bin/tsx; }; ')
            pre += venv_prelude(wt, cwd_rel)
            hf = Path(wt) / ".dispatch-harness.json"
            if hf.is_file() and "bin/dispatch-harness.json" not in xt:
                try:
                    xt["bin/dispatch-harness.json"] = hf.read_bytes()
                except OSError:
                    pass
            ahc = Path(wt) / "auto-harness-check.py"
            if ahc.is_file() and "bin/auto-harness-check.py" not in xt and "auto-harness-check.py" in str(cmd):
                try:
                    xt["bin/auto-harness-check.py"] = ahc.read_bytes()
                except OSError:
                    pass
            full = pre + remote_cmd(cmd, group=gzb64, harness="bin/dispatch-harness.json" in xt) + (GZB64_SUFFIX if gzb64 else "")
            if gzb64:
                full = "set -o pipefail; " + full
            res = _submit(cj, api, str(wt), full, timeout_s, stage, bundle_id, host_tools(xt),
                          passthrough_env(dict(env or {}, DISPATCH_SNAPSHOT=snap0)), cwd_rel,
                          "%s@%s" % (stage, snap0), claim_timeout_s, owner=owner, **({"extra_pkg_dirs": extra_dirs} if extra_dirs else {}))
            job_id = getattr(res, "job_id", None)
            snap1 = snapshot(wt)
            if snap1 != snap0:
                why = "STALE SNAPSHOT: worktree changed while the runner ran it (%s -> %s) -- result discarded" % (
                    snap0[:20], snap1[:20])
            else:
                out = res.stdout_tail or ""
                if gzb64 and not res.timed_out:
                    try:
                        out = decode_gzb64(out)
                    except Exception as e:                # noqa: BLE001
                        why = "remote result undecodable (%s)" % type(e).__name__
                r = StageResult(res.exit_code, out, res.stderr_tail or "", bool(res.timed_out), "runner",
                                "", job_id, snap0, time.time() - t0, getattr(res, "timings", None))
                if why is None and check is not None:
                    veto = check(r)
                    if veto:
                        why = "remote result vetoed after %.0fs of runner work (re-running locally): %s" % (
                            time.time() - t0, veto)
                if why is None:
                    _log(stage, bundle_id, "runner", "", snap0, r.exit_code, r.wall, job_id, owner=owner)
                    return r
        except RuntimeError as e:
            why = str(e)
        except Exception as e:                            # noqa: BLE001
            why = "%s: %s" % (type(e).__name__, e)
    # ---- local path: the caller's own code, with a lane marker so we still do not hold GPU lanes
    # Default: NO marker on the local path -- the bundle holds its lane exactly as it did before the
    # lane existed (bundle-to-completion; a foreign bundle must not interleave just because the runner
    # was down). CPU_LANE_LOCAL_MARKER=1 opts in to freeing the lane during a local-fallback stage.
    if os.environ.get("CPU_LANE_LOCAL_MARKER") == "1":
        end = _marker(stage, bundle_id, timeout_s)
    else:
        end = (lambda c=None: None)
    code = None
    _show_end = local_run_begin(stage, bundle_id, why, timeout_s, owner=owner)    # display only (see local_run_begin)
    try:
        r = _norm_local(local_fn())
        code = r.exit_code
    finally:
        end(code)
        _show_end()
    r.why = why or "local"
    r.snap = snap0
    _log(stage, bundle_id, "local", why, snap0, r.exit_code, time.time() - t0, owner=owner)
    return r
