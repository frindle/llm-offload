#!/usr/bin/env python3
"""lane_remote.py: the CPU-runner end of a remote mini-swe-runner auto job.

mini-swe-runner.py remote_auto() (on the queue host) ships this file, the runner and its helpers, and the job's TASK.md and
spec as the tools of a dispatch-cpu-runner job (spec.setup + spec.llm + spec.artifacts), then reads back what `run` leaves
in $CPU_RUNNER_ARTIFACTS. The checkout is the job's worktree at the queue host's HEAD (same commit id).

  setup --python X [--deps-lock P] --mini-lock P [--mini-python X] [--tools-lock P]
      The runner's setup phase: job user, WITH network, cwd = the checkout. Builds $HOME/.ollama-dispatch/mini-venv from the
      queue host's pinned mini.lock (no resolution), and the project venv <checkout>/.venv: deps.lock installed --no-deps
      (else `-e <checkout> pytest`, resolved), then the project itself editable without deps (else a metadata-only dist).
      The venv then imports the source of the tree it runs in (_lane_walkup, below), so a copy of the checkout (the work
      dir, a mutation-scoring clone) tests its own code, not the checkout's. Files the build leaves in the checkout go to
      .git/info/exclude, so they never count as changes.
  run --name N --model M --wall S [--hold-allow S] [--outage-max S]
      The isolated run phase: no network; the model only through $CPU_RUNNER_LLM_URL (the queue relays it and adds the key,
      which never reaches the runner). Runs mini-swe-runner.py on $HOME/run/<N>/work, echoes its output, and leaves the
      files the run changed (work/), its review files (run/) and manifest.json in $CPU_RUNNER_ARTIFACTS. A run still
      going after --wall seconds, plus the provider waits it credited (its "; credited Ns" lines, at most --hold-allow
      seconds of them), is stopped (manifest wall_hit, credited_s). Exits with the runner's code.

Stdlib only: it runs under the runner's job python.
"""
import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

RUN_FILE = re.compile(r"^(?:REVIEW[\w.-]*\.md|DIAGNOSIS\.md|TASK-D\.md|TASK\.fix\.md|spec\.auto\.json|auto-state\.json|[\w.-]+\.traj\.json)$",
                      re.A)
FILE_MAX = 20 << 20     # one returned file
TOTAL_MAX = 150 << 20   # everything returned (the runner caps a job's artifacts at 256 MiB)
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}$")
TERM_GRACE_S = 3        # the runner SIGKILLs the job 5 s after its SIGTERM: stop the run and still write the manifest
WALL_GRACE_S = 30       # our own wall: time for the runner to kill its children and print its RESULT
# the run's wait lines (mini_ctx, echoed by the runner): seconds a provider outage or a Darkbloom Sandbox hold took off the
# run's clocks, each second once. The wall gives them back, up to --hold-allow; never more than the run's own time so far
# + CREDIT_SLACK_S (an outage nap is credited before it is slept), so a stray line cannot hold the wall open.
CREDIT_LINE = re.compile(rb"^\[provider-(?:outage|hold)\].*; credited (\d+(?:\.\d+)?)s\r?$")
CREDIT_SLACK_S = 150

WALKUP = r'''"""Written by lane_remote.py setup. The interpreter of this venv imports the project from the tree it runs in: the
first directory at or above the working directory whose .venv is this venv (a copy of the checkout links its .venv here),
else LANE_PROJECT_ROOT (set below, so the subprocesses of a test see the same tree), else the checkout. That root and
root/src go before site-packages, ahead of the editable install that points at the checkout."""
import os
import sys


def _root():
    try:
        pre = os.path.realpath(sys.prefix)
        d = os.getcwd()
        while True:
            v = os.path.join(d, ".venv")
            if os.path.lexists(v) and os.path.realpath(v) == pre:
                return d
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
    except OSError:
        pass
    r = os.environ.get("LANE_PROJECT_ROOT")
    if r and os.path.isdir(r):
        return r
    return os.path.dirname(os.path.realpath(sys.prefix))


def _install():
    root = _root()
    add = [root] + ([os.path.join(root, "src")] if os.path.isdir(os.path.join(root, "src")) else [])
    sys.path[:] = [p for p in sys.path if p not in add]
    i = next((n for n, p in enumerate(sys.path) if p.rstrip("/").endswith("site-packages")), len(sys.path))
    sys.path[i:i] = add
    os.environ["LANE_PROJECT_ROOT"] = root


try:
    _install()
except Exception:  # noqa: BLE001 -- never break the interpreter's start
    pass
'''


def say(msg):
    print(f"[lane-remote] {msg}", flush=True)


def progress(text):
    p = os.environ.get("CPU_RUNNER_PROGRESS")
    if not p:
        return
    try:
        with open(p, "w") as fh:
            fh.write(re.sub(r"[^\x20-\x7e]", " ", str(text))[:200] + "\n")
    except OSError:
        pass


def sh(argv, timeout=1800, check=True, **kw):
    """Run argv with its output in our log; -> CompletedProcess. check: a non-zero exit raises SystemExit naming the step."""
    say("$ " + " ".join(str(a) for a in argv))
    r = subprocess.run([str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=timeout, **kw)
    if r.stdout.strip():
        print(r.stdout.rstrip()[-6000:], flush=True)
    if check and r.returncode:
        raise SystemExit(f"[lane-remote] setup failed: `{' '.join(str(a) for a in argv[:6])}` exited {r.returncode}")
    return r


def toplevel(cwd="."):
    r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=60)
    if r.returncode or not r.stdout.strip():
        raise SystemExit(f"[lane-remote] {os.path.abspath(cwd)} is not inside a git checkout")
    return Path(r.stdout.strip())


def untracked(root):
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"], capture_output=True,
                       timeout=300)
    return {e[3:].decode("utf-8", "surrogateescape") for e in r.stdout.split(b"\0") if e.startswith(b"?? ")}


def site_packages(venv):
    got = sorted(glob.glob(str(Path(venv, "lib", "python3*", "site-packages"))))
    if not got:
        raise SystemExit(f"[lane-remote] no site-packages under {venv}")
    return Path(got[0])


# ---------------------------------------------------------------------------------------------------- setup
def _project_table(root):
    """(pyproject dict, its [project] table) read statically; ({}, {}) when there is none or it does not parse."""
    try:
        import tomllib
        pp = tomllib.loads((root / "pyproject.toml").read_text())
    except (ImportError, OSError, ValueError):
        return {}, {}
    return pp, dict(pp.get("project") or {})


def _setup_cfg_name(root):
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(root / "setup.cfg")
        return cp.get("metadata", "name", fallback=None)
    except (OSError, configparser.Error):
        return None


def metadata_only_dist(venv, root):
    """The editable build failed: importlib.metadata still sees the project (name, version, entry points) and a gitignored
    version file the build would have generated (hatch-vcs / setuptools-scm) gets a stub. Imports come from the tree (walkup)."""
    pp, proj = _project_table(root)
    name = proj.get("name") or _setup_cfg_name(root)
    if not name:
        say("no static project name: no dist-info written (imports still come from the tree)")
        return
    ver = str(proj.get("version") or "0.0.0+lane")
    d = site_packages(venv) / f"{re.sub(r'[-.]+', '_', name)}-{ver.replace('-', '_')}.dist-info"
    d.mkdir(exist_ok=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n")
    (d / "INSTALLER").write_text("lane-remote\n")
    (d / "RECORD").write_text("")
    eps = {k: v for k, v in (proj.get("entry-points") or {}).items() if isinstance(v, dict)}
    if isinstance(proj.get("scripts"), dict):
        eps["console_scripts"] = proj["scripts"]
    if eps:
        (d / "entry_points.txt").write_text("".join(f"[{g}]\n" + "".join(f"{k} = {v}\n" for k, v in m.items()) + "\n"
                                                    for g, m in eps.items()))
    t = pp.get("tool") or {}
    hooks = (((t.get("hatch") or {}).get("build") or {}).get("hooks") or {})
    scm = t.get("setuptools_scm") or {}
    vf = (hooks.get("vcs") or {}).get("version-file") or scm.get("version_file") or scm.get("write_to")
    if isinstance(vf, str) and vf and not os.path.isabs(vf) and ".." not in Path(vf).parts and not (root / vf).exists():
        (root / vf).parent.mkdir(parents=True, exist_ok=True)
        (root / vf).write_text('__version__ = version = "0.0.0+lane"\n__version_tuple__ = version_tuple = (0, 0, 0, "lane")\n')
    say(f"metadata-only dist for {name} {ver}")


def cmd_setup(a):
    root = toplevel(".")
    home = Path(os.environ.get("HOME") or Path.home())
    t0 = time.time()
    # 1. mini-swe-agent's venv, at the queue host's versions
    progress("setup: mini venv")
    mv = home / ".ollama-dispatch" / "mini-venv"
    if mv.exists():
        shutil.rmtree(mv)
    mv.parent.mkdir(parents=True, exist_ok=True)
    mpy = a.mini_python or ("/opt/python/bin/python3" if os.access("/opt/python/bin/python3", os.X_OK) else sys.executable)
    sh(["uv", "venv", "-q", "--python", mpy, mv])
    sh(["uv", "pip", "install", "-q", "--python", mv / "bin" / "python", "--no-deps", "-r", a.mini_lock])
    sh([mv / "bin" / "python", "-c", "import litellm, yaml, jinja2, minisweagent"])
    if not os.access(mv / "bin" / "mini", os.X_OK):
        raise SystemExit("[lane-remote] setup failed: the mini venv has no bin/mini")
    if a.tools_lock:   # the lane's own tools (ruff: the lint gate and the style pass) next to mini; optional, never fatal
        try:
            r = sh(["uv", "pip", "install", "-q", "--python", mv / "bin" / "python", "--no-deps", "-r", a.tools_lock], timeout=600, check=False)
            if r.returncode:
                say(f"WARN lane tools not installed (uv exited {r.returncode}): the lint gate and the style pass run without ruff")
        except (OSError, subprocess.SubprocessError) as e:
            say(f"WARN lane tools not installed ({type(e).__name__}): the lint gate and the style pass run without ruff")
    # 2. the project's venv, in the checkout (the work dir links its .venv here)
    progress("setup: project venv")
    before = untracked(root)
    pv = root / ".venv"
    if os.path.lexists(pv):
        raise SystemExit(f"[lane-remote] setup failed: the checkout already has a .venv ({pv})")
    sh(["uv", "venv", "-q", "--python", a.python, pv])
    py = pv / "bin" / "python"
    if a.deps_lock:
        sh(["uv", "pip", "install", "-q", "--python", py, "--no-deps", "-r", a.deps_lock])
    else:
        sh(["uv", "pip", "install", "-q", "--python", py, "-e", root, "pytest"])
    r = sh(["uv", "pip", "install", "-q", "--python", py, "--no-deps", "-e", root], check=False)
    how = "editable"
    if r.returncode:
        metadata_only_dist(pv, root)
        how = "metadata-only (the editable build failed)"
    sp = site_packages(pv)
    (sp / "_lane_walkup.py").write_text(WALKUP)
    (sp / "_lane_walkup.pth").write_text("import _lane_walkup\n")
    sh([py, "-c", "import pytest, sys; print('project python', sys.version.split()[0], 'pytest', pytest.__version__)"])
    new = sorted(untracked(root) - before)
    with open(root / ".git" / "info" / "exclude", "a") as fh:
        fh.write("\n/.venv\n" + "".join("/" + x.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?").replace("[", "\\[")
                                       + "\n" for x in new if not x.startswith(".venv/")))
    say(f"setup done in {time.time() - t0:.0f}s: project install {how}; {len(new)} build file(s) excluded")
    progress("setup: done")
    return 0


# ---------------------------------------------------------------------------------------------------- run
def _copy_capped(src, dst, budget):
    """Copy one regular file into the artifacts; -> None, or why it was left out."""
    st = os.lstat(src)
    if st.st_size > FILE_MAX or st.st_size > budget[0]:
        return f"size {st.st_size}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst, follow_symlinks=False)
    budget[0] -= st.st_size
    return None


def collect(work, run_dir, art, budget):
    """Changed files of the work dir (vs its HEAD: modified, added, untracked) into art/work, the run's review files into
    art/run; -> the manifest's file fields."""
    out = {"changed": [], "deleted": [], "modes": {}, "skipped": [], "run_files": []}
    r = subprocess.run(["git", "-C", str(work), "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
                       capture_output=True, timeout=60)
    if r.returncode:
        out["error"] = "git status failed: " + r.stderr.decode("utf-8", "replace")[-300:]
        return out
    for e in r.stdout.split(b"\0"):
        if len(e) < 4:
            continue
        try:
            rel = e[3:].decode("utf-8")
        except UnicodeDecodeError:
            out["skipped"].append({"path": e[3:].decode("utf-8", "replace"), "why": "not a UTF-8 path"})
            continue
        parts = rel.split("/")
        if any(p in SKIP_PARTS for p in parts) or rel.endswith((".pyc", ".pyo")):
            continue
        fp = work / rel
        if os.path.islink(fp):
            out["skipped"].append({"path": rel, "why": "symlink"})
        elif not os.path.lexists(fp):
            out["deleted"].append(rel)
        elif not os.path.isfile(fp):
            out["skipped"].append({"path": rel, "why": "not a regular file"})
        else:
            why = _copy_capped(fp, art / "work" / rel, budget)
            if why:
                out["skipped"].append({"path": rel, "why": why})
            else:
                out["changed"].append(rel)
                out["modes"][rel] = os.lstat(fp).st_mode & 0o777
    for f in sorted(os.listdir(run_dir)):
        fp = run_dir / f
        if RUN_FILE.match(f) and os.path.isfile(fp) and not os.path.islink(fp):
            why = _copy_capped(fp, art / "run" / f, budget)
            if why:
                out["skipped"].append({"path": "run/" + f, "why": why})
            else:
                out["run_files"].append(f)
    return out


def cmd_run(a):
    t0 = time.time()
    if not NAME_RE.match(a.name):
        raise SystemExit(f"[lane-remote] bad --name {a.name!r}")
    home = Path(os.environ.get("HOME") or Path.home())
    tools = Path(os.environ["JOB_TOOLS"])
    lane = tools / "lane"
    art = Path(os.environ["CPU_RUNNER_ARTIFACTS"])
    url = os.environ.get("CPU_RUNNER_LLM_URL")
    checkout = toplevel(".")
    run_dir = home / "run" / a.name
    work = run_dir / "work"
    work.mkdir(parents=True)
    shutil.copyfile(lane / "TASK.md", run_dir / "TASK.md")
    spec = json.loads((lane / "spec.json").read_text())
    spec.pop("remote", None)
    spec.update(baseline_from=str(checkout), task=str(run_dir / "TASK.md"))
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=1) + "\n")
    for f in ("auto-state.json", "DIAGNOSIS.md"):  # a re-launch after a pause reuses the accepted diagnosis
        if (lane / f).is_file():
            shutil.copyfile(lane / f, run_dir / f)
    lj = home / ".darkbloom" / "local.json"
    lj.parent.mkdir(parents=True, exist_ok=True)
    lj.write_text(json.dumps({"base_url": url or "http://127.0.0.1:9/v1", "api_key": "lane-relay"}))  # the relay adds the key
    env = {k: v for k, v in os.environ.items() if k not in ("LANE_PROJECT_ROOT", "OPENAI_API_KEY", "OPENAI_API_BASE")}
    env.update(DARKBLOOM_LOCAL_JSON=str(lj), MINI_BIN=str(home / ".ollama-dispatch" / "mini-venv" / "bin" / "mini"),
               INFRA_INCIDENTS_SCREEN_LOCKED="0", MINI_OUTAGE_MAX_S=str(a.outage_max), MINI_MUTATE_SANDBOX="off",
               PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, str(home / "bin" / "mini-swe-runner.py"), "--model", a.model, "--cwd", str(work),
            "--task-file", str(run_dir / "spec.json")]
    say(f"run {a.name}: model {a.model}, wall {a.wall}s (+ up to {a.hold_allow}s of provider waits), checkout {checkout}")
    progress("run: starting")
    # same process group as us: the runner's group kill reaches the run too (the run puts its own children in their own
    # sessions and kills them when it gets SIGTERM)
    p = subprocess.Popen(argv, cwd=str(run_dir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    st = {"term": None, "fwd": False, "result": None, "last": "", "credit": 0.0}

    def on_term(signum, _frame):
        st["term"] = st["term"] or time.time()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    def pump():
        buf = b""
        while True:
            chunk = p.stdout.read1(65536)
            if not chunk:
                break
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except OSError:
                pass
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                if ln.startswith(b"RESULT "):
                    st["result"] = ln.decode("utf-8", "replace").rstrip("\r")
                elif ln.startswith(b"[mini-runner] "):
                    st["last"] = ln.decode("utf-8", "replace")[14:]
                elif ln.startswith(b"[provider-"):
                    c = CREDIT_LINE.match(ln)
                    if c:
                        st["credit"] = min(st["credit"] + float(c.group(1)), time.time() - t0 + CREDIT_SLACK_S)
        if buf.startswith(b"RESULT "):
            st["result"] = buf.decode("utf-8", "replace").rstrip("\r")

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    wall_hit, killed_at, last_prog = False, None, 0.0
    while p.poll() is None:
        now = time.time()
        given = min(st["credit"], a.hold_allow)
        deadline = t0 + a.wall + given
        if st["term"] and not st["fwd"] and now - st["term"] > 0.3:
            st["fwd"] = True  # a signal to us alone (a group kill reached the run already): pass it on
            _signal(p, signal.SIGTERM)
        if st["term"] and now - st["term"] > TERM_GRACE_S:
            _signal(p, signal.SIGKILL)
        if not wall_hit and now > deadline:
            wall_hit, killed_at = True, now
            say(f"the run passed its wall ({a.wall}s + {given:.0f}s of provider waits, of {st['credit']:.0f}s credited; "
                f"at most {a.hold_allow}s given back): stopping it")
            _signal(p, signal.SIGTERM)
        if killed_at and now - killed_at > WALL_GRACE_S:
            _signal(p, signal.SIGKILL)
        if now - last_prog >= 20:
            last_prog = now
            progress(f"run {int(now - t0)}s: {st['last'][:150]}")
        try:
            p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    th.join(2 if st["term"] else 10)
    rc = p.returncode
    budget = [TOTAL_MAX]
    man = {"v": 1, "rc": rc, "result": st["result"], "wall_hit": wall_hit, "terminated": bool(st["term"]),
           "elapsed_s": int(time.time() - t0), "credited_s": round(st["credit"], 1), "hold_allow_s": a.hold_allow,
           "remote_run_dir": str(run_dir), "remote_work": str(work),
           "remote_checkout": str(checkout), "remote_home": str(home),
           "real": {"run_dir": os.path.realpath(run_dir), "work": os.path.realpath(work), "checkout": os.path.realpath(checkout),
                    "home": os.path.realpath(home)}}
    try:
        man.update(collect(work, run_dir, art, budget))
    except Exception as e:  # noqa: BLE001 -- the manifest must still be written
        man["error"] = f"collect: {type(e).__name__}: {e}"[:400]
    (art / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    say(f"run {a.name} ended rc={rc} after {man['elapsed_s']}s: {len(man.get('changed', []))} changed, "
        f"{len(man.get('deleted', []))} deleted, {len(man.get('run_files', []))} run files, {len(man.get('skipped', []))} left out")
    progress(f"run: ended rc={rc}")
    return rc if isinstance(rc, int) and rc >= 0 else 1


def _signal(p, sig):
    try:
        p.send_signal(sig)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--python", required=True)
    s.add_argument("--deps-lock")
    s.add_argument("--mini-lock", required=True)
    s.add_argument("--mini-python")
    s.add_argument("--tools-lock")
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--wall", type=int, required=True)
    r.add_argument("--hold-allow", type=int, default=0)
    r.add_argument("--outage-max", type=int, default=1200)
    a = ap.parse_args(argv)
    return cmd_setup(a) if a.cmd == "setup" else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
