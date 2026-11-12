#!/usr/bin/env python3
"""Standing regression suite for ~/bin/harness_admission.py, built from the 2026-10-10 backtest.

Each slice in test-fixtures-backtest/<slice>/ (plus optional shared base overlay, see expected.json)
is copied to a temp dir, a throwaway git repo is made (everything committed EXCEPT the harness
files, which stay untracked like in a real dispatch worktree), then
  harness_admission.py check <copy> coding-strict-x --no-dynamic   (static)
  harness_admission.py check <copy> coding-strict-x                (strict, dynamic)
are run with HARNESS_ADMISSION_DIR pointing at a temp dir (~/.ollama-dispatch/admission untouched).
Results are compared with expected.json. Lines: PASS / FAIL / XFAIL (expected-fail, still refused) /
XPASS (xfail slice now admitted: fix landed, drop the xfail flag) / SKIP (missing node_modules cache).
XPASS counts as a failure-to-update only with --strict-xpass; by default it is reported and passes.
Ends with BACKTEST_OK (exit 0) or BACKTEST_FAILED (exit 1). Runs 2 slices at a time, 600s per run.
Usage: test-harness-admission-backtest.py [slice-substring ...] [--strict-xpass]
"""
import concurrent.futures as cf
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "test-fixtures-backtest"
GATE = HERE / "harness_admission.py"
TIMEOUT = 600
JOBS = 2
UNTRACKED = {".dispatch-harness.json", ".preflight-state.json", "AUTO-TASK.md", "auto-harness-check.py"}


def sh(*a, cwd=None, **kw):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, **kw)


VENV_ROOT = Path(os.environ.get("BACKTEST_VENV_ROOT", os.path.expanduser("~/.cache/harness-admission-backtest")))
_venv_lock = threading.Lock()


def cached_venv(req):
    """One venv per distinct requirements.txt (system-site-packages + pip install -r), built once and reused.
    verify.sh only builds .venv itself when missing, so we hand it a ready one (symlinked as .venv)."""
    import hashlib
    txt = req.read_text() if req.exists() else ""
    v = VENV_ROOT / ("venv-" + hashlib.sha1(txt.encode()).hexdigest()[:10])
    with _venv_lock:
        if (v / ".ok").exists():
            return v
        shutil.rmtree(v, ignore_errors=True)
        VENV_ROOT.mkdir(parents=True, exist_ok=True)
        if sh(sys.executable, "-m", "venv", "--system-site-packages", str(v)).returncode:
            return None
        if txt.strip():
            r = sh(str(v / "bin" / "pip"), "install", "-q", "-r", str(req), timeout=900)
            if r.returncode:
                shutil.rmtree(v, ignore_errors=True)
                return None
        (v / ".ok").write_text("ok")
        return v


def build_copy(name, ent, dest):
    if ent.get("base"):
        shutil.copytree(FIX / ent["base"], dest, symlinks=True)
        for d in ent.get("delete", []):
            p = dest / d
            if p.exists():
                p.unlink()
    if (FIX / name).is_dir():
        shutil.copytree(FIX / name, dest, symlinks=True, dirs_exist_ok=True)
    manifest = {}
    try:
        manifest = json.loads((dest / ".dispatch-harness.json").read_text())
    except Exception:
        pass
    harness = UNTRACKED | set(manifest.get("authored", [])) | {manifest.get("fixture", "")}
    harness.discard("")
    # target stays tracked (fixtures hold the HEAD version); authored files + manifest untracked
    harness.discard(manifest.get("target", ""))
    (dest / ".gitignore").write_text((dest / ".gitignore").read_text() if (dest / ".gitignore").exists() else "")
    sh("git", "init", "-q", cwd=dest)
    sh("git", "config", "user.email", "t@t", cwd=dest)
    sh("git", "config", "user.name", "t", cwd=dest)
    allf = [str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() and ".git/" not in str(p)]
    tracked = [f for f in allf if f not in harness]
    # git ls-files -o -i would hide ignored files: drop .gitignore so nothing is ignored
    if (dest / ".gitignore").exists() and ".gitignore" in tracked:
        pass
    for i in range(0, len(tracked), 500):
        sh("git", "add", "-f", "--", *tracked[i:i + 500], cwd=dest)
    r = sh("git", "commit", "-q", "-m", "base", cwd=dest)
    if r.returncode:
        raise RuntimeError("git commit failed: " + r.stderr[-300:])
    # env parity shims
    req = dest / "requirements.txt"
    if (dest / "verify.sh").exists() and ".venv/bin/python" in (dest / "verify.sh").read_text():
        v = cached_venv(req)
        if v is None:  # pinned reqs not installable here (e.g. greenlet on py3.14): use system python3 as .venv python
            (dest / ".venv" / "bin").mkdir(parents=True)
            (dest / ".venv" / "bin" / "python").symlink_to(sys.executable)
        else:
            (dest / ".venv").symlink_to(v)
    nm = ent.get("node_modules")
    if nm:
        c = sorted(glob.glob(os.path.expanduser(f"~/.ollama-dispatch/node_modules-cache/{nm}-*/node_modules")))
        if not c:
            return "skip-no-node_modules"
        os.symlink(c[0], dest / "node_modules")
    return None


def run_mode(copy, adm, mode):
    env = dict(os.environ, HARNESS_ADMISSION_DIR=str(adm))
    args = [sys.executable, str(GATE), "check", str(copy), "coding-strict-x"]
    if mode == "static":
        args.append("--no-dynamic")
    try:
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": None, "cause": "TIMEOUT", "err": "timeout"}
    try:
        d = json.loads(r.stdout)
        return {"ok": bool(d.get("ok")), "cause": d.get("cause"), "causes": [x.get("cause") for x in d.get("defects", [])]}
    except Exception:
        return {"ok": None, "cause": "UNPARSEABLE", "err": (r.stdout + r.stderr)[-300:]}


def check_slice(name, ent, root):
    tmp = Path(tempfile.mkdtemp(prefix=f"bt-{name[:20]}-", dir=root))
    copy, adm = tmp / "wt", tmp / "adm"
    adm.mkdir()
    try:
        sk = build_copy(name, ent, copy)
        if sk:
            return name, "SKIP", sk
        probs = []
        got = {}
        for mode in ("static", "strict"):
            got[mode] = run_mode(copy, adm / mode, mode)
            (adm / mode).mkdir(exist_ok=True)
            e = ent[mode]
            g = got[mode]
            if g["ok"] is None:
                probs.append(f"{mode}: {g['cause']} {g.get('err', '')}")
            elif g["ok"] != e["ok"]:
                probs.append(f"{mode}: expected ok={e['ok']} got ok={g['ok']} cause={g['cause']} causes={g.get('causes')}")
            elif not e["ok"] and e.get("cause") and g["cause"] != e["cause"]:
                probs.append(f"{mode}: expected cause={e['cause']} got {g['cause']}")
        if ent.get("xfail"):
            return name, ("XFAIL" if probs else "XPASS"), "; ".join(probs) if probs else "now admitted: remove xfail flag in expected.json"
        return name, ("FAIL" if probs else "PASS"), "; ".join(probs)
    except Exception as ex:  # harness error is a failure, never silently skipped
        return name, "FAIL", f"test error: {ex!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    argv = sys.argv[1:]
    strict_xpass = "--strict-xpass" in argv
    pats = [a for a in argv if not a.startswith("--")]
    exp = json.loads((FIX / "expected.json").read_text())["slices"]
    names = [n for n in exp if not pats or any(p in n for p in pats)]
    root = tempfile.mkdtemp(prefix="harness-bt-")
    counts = {}
    try:
        with cf.ThreadPoolExecutor(JOBS) as ex:
            for name, st, msg in ex.map(lambda n: check_slice(n, exp[n], root), names):
                counts[st] = counts.get(st, 0) + 1
                print(f"{st:5} {name}" + (f"  -- {msg}" if msg else ""), flush=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("SUMMARY " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    bad = counts.get("FAIL", 0) + (counts.get("XPASS", 0) if strict_xpass else 0)
    if not names:
        bad = 1
    print("BACKTEST_OK" if not bad else "BACKTEST_FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
