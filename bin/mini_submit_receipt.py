"""mini_submit_receipt -- the SUBMIT-OK.json receipt shared by ~/bin/mini-submit (writer) and ollama-queue.py enqueue (verifier).

mini-submit runs checks C0-C8 on a mini-swe-runner spec, then writes <spec dir>/SUBMIT-OK.json:
  {spec_sha, cwd, runner_sha, label, ts, checks, [override]}
The queue refuses a mini-swe-runner enqueue unless that receipt matches the spec bytes, the resolved --cwd and the runner
bytes, is under MAX_AGE_S old, and the cwd exists and is empty (the runner rebuilds cwd; a populated cwd is somebody's tree).
Pure stdlib, no side effects on import.
"""
import hashlib
import json
import os
import time
from pathlib import Path

RECEIPT = "SUBMIT-OK.json"
MAX_AGE_S = 15 * 60


def sha_file(p):
    """Full sha256 hex of a file's bytes, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(os.path.expanduser(str(p))).read_bytes()).hexdigest()
    except OSError:
        return None


def receipt_path(task_file):
    return Path(os.path.expanduser(str(task_file))).resolve().parent / RECEIPT


def write_receipt(spec_path, cwd, runner, label, checks, override=None, ts=None):
    """Write the receipt next to the spec; returns its path."""
    spec_path = Path(os.path.expanduser(str(spec_path))).resolve()
    rec = {
        "spec_sha": sha_file(spec_path),
        "cwd": str(Path(os.path.expanduser(str(cwd))).resolve()),
        "runner_sha": sha_file(runner),
        "label": label,
        "ts": ts if ts is not None else time.time(),
        "checks": checks,
    }
    if override:
        rec["override"] = override
    p = receipt_path(spec_path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)
    return p


def verify_receipt(task_file, cwd, runner, now=None, max_age_s=MAX_AGE_S):
    """None when the receipt authorises enqueueing (task_file, cwd, runner); else a short text naming the failing field."""
    p = receipt_path(task_file)
    if not p.is_file():
        return "missing"
    try:
        rec = json.loads(p.read_text())
        assert isinstance(rec, dict)
    except (OSError, ValueError, AssertionError):
        return "unreadable"
    cur = sha_file(Path(os.path.expanduser(str(task_file))).resolve())
    if not rec.get("spec_sha") or rec.get("spec_sha") != cur:
        return "spec_sha mismatch (the spec changed since mini-submit ran)"
    c = str(Path(os.path.expanduser(str(cwd))).resolve()) if cwd else ""
    if not c or rec.get("cwd") != c:
        return f"cwd mismatch (receipt {rec.get('cwd')!r}, enqueue --cwd {c or 'none'!r})"
    rs = sha_file(runner)
    if not rec.get("runner_sha") or rec.get("runner_sha") != rs:
        return "runner_sha mismatch (the runner changed since mini-submit ran)"
    ts = rec.get("ts")
    age = (now if now is not None else time.time()) - (ts if isinstance(ts, (int, float)) else 0)
    if not isinstance(ts, (int, float)) or age > max_age_s or age < -60:
        return f"ts stale ({int(age)}s old, max {max_age_s}s)"
    d = Path(c)
    if not d.is_dir():
        return "cwd does not exist"
    if any(d.iterdir()):
        return "cwd is not empty"
    return None
