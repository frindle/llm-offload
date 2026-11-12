"""dispatch_progress.py -- live progress records for OFF-GPU dispatch work.

WHY (the owner 2026-09-27: "is there a way to show these tasks so it doesn't look
hung?"). While a committed bundle has no queue job running, its work still goes
on, just not on the GPU: preflight's both-ways proof, the verify-relevance
mutation run (minutes of CPU), a slicer advance, a self-heal regate. The queue
held every other bundle, so `ollama-queue.py status` and the dashboard showed
only held pending rows and it looked hung.

Each long-running tool drops ONE small JSON record per (worktree, tool) here
and refreshes it as it goes; the dashboard reads them back (ollama-queue-api's
_bundle_views -> the "live now" row and each slice's phase). A record is trusted only while its
writer pid is alive and it was refreshed recently, so a crashed tool never
leaves a phantom "working" row.

Record: {tool, wt, pid, stage, detail, done, total, started_at, updated_at}.
Location: $OLLAMA_DISPATCH_PROGRESS_DIR, else ~/.ollama-dispatch/progress
(HOME-relative, so a sandboxed test with a temp HOME never touches the real one).
Every function here is best-effort and NEVER raises: progress is display only.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import time
from pathlib import Path

STALE_S = 30 * 60.0          # a record not refreshed for this long is ignored


def progress_dir() -> Path:
    env = os.environ.get("OLLAMA_DISPATCH_PROGRESS_DIR")
    return Path(env) if env else Path.home() / ".ollama-dispatch" / "progress"


def _path(wt, tool, base=None) -> Path:
    h = hashlib.sha1(str(Path(str(wt)).resolve()).encode()).hexdigest()[:16]
    return Path(base or progress_dir()) / f"{h}-{tool}.json"


_STARTED: dict = {}
_REGISTERED: set = set()


def write(wt, tool, stage, detail="", done=None, total=None, base=None):
    """Create/refresh this process's record for (wt, tool). Removed at exit."""
    try:
        p = _path(wt, tool, base)
        key = str(p)
        now = time.time()
        started = _STARTED.setdefault(key, now)
        rec = {"tool": tool, "wt": str(Path(str(wt)).resolve()), "pid": os.getpid(),
               "stage": stage, "detail": detail, "done": done, "total": total,
               "started_at": started, "updated_at": now}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(p)
        if key not in _REGISTERED:
            _REGISTERED.add(key)
            atexit.register(clear, wt, tool, base)
    except Exception:
        pass


def clear(wt, tool, base=None):
    """Drop (wt, tool)'s record -- only if THIS process wrote it."""
    try:
        p = _path(wt, tool, base)
        rec = json.loads(p.read_text())
        if rec.get("pid") == os.getpid():
            p.unlink()
        _STARTED.pop(str(p), None)
    except Exception:
        pass


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def read_all(base=None, now=None, alive=None, stale_s=STALE_S) -> list:
    """Every LIVE record (writer alive, refreshed within stale_s), oldest first."""
    alive = alive or _alive
    now = time.time() if now is None else now
    out = []
    try:
        files = sorted(Path(base or progress_dir()).glob("*.json"))
    except Exception:
        return out
    for f in files:
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if now - float(rec.get("updated_at") or 0) > stale_s:
            continue
        if not alive(rec.get("pid")):
            continue
        out.append(rec)
    out.sort(key=lambda r: float(r.get("started_at") or 0))
    return out


def describe(rec) -> str:
    """'verify-relevance 18/40 mutants' / 'preflight: baseline-fails'."""
    try:
        s = str(rec.get("stage") or rec.get("tool") or "?")
        if rec.get("total"):
            s += f" {rec.get('done') or 0}/{rec['total']}"
        if rec.get("detail"):
            s += f" {rec['detail']}"
        return s
    except Exception:
        return "?"
