"""plan_cancel -- a HUMAN cancel of a slice plan is terminal until a human undoes it.

WHY (2026-09-27, live: ev-service-screen-1-rivian-service). The owner cancelled every
job of the plan and killed its advance at ~18:05Z. Nothing recorded that intent
anywhere the automation reads: the slicer maps a cancelled job to FAILED, FAILED
is auto-retried, and at 18:42:07Z dispatch-self-heal.py ran `--retry-slice
s1-request-status-map`, which re-enqueued job 4965ead4e782. An earlier slicer
update re-queued 8437877ce7b0 the same way. A cancel read as "a failure to heal".

THE RECORD is a sibling marker file, `<slice-runs>/<plan>.cancelled` (JSON: at,
reason, by), NOT a key in the run state: the slicer's save_state() merges its
in-memory dict over the file, and a long-running --execute would silently erase a
key written by the queue mid-run. A separate file has no writer race at all.

Every automated actor asks `cancelled(label)` before acting: the slicer (every
entry point but --uncancel/--status), its --sweep, gate-on-complete's
--advance-detached (that IS the slicer), dispatch-self-heal.py, the escalation
watcher, and the queue's bundle commitment. A sub-plan (label <root>-<sid>, repo =
the root's chain worktree) inherits its parent's cancel.
Undo: delete the plan's cancel marker (the old slicer is retired; use mini-swe).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

SUFFIX = ".cancelled"


def _runs_dir(runs_dir=None) -> Path:
    return Path(runs_dir) if runs_dir else Path.home() / ".ollama-dispatch" / "slice-runs"


def cancel_path(label: str, runs_dir=None) -> Path:
    return _runs_dir(runs_dir) / f"{label}{SUFFIX}"


def mark_cancelled(label: str, reason: str = "", by: str = "", runs_dir=None) -> Path:
    p = cancel_path(label, runs_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"label": label, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "reason": reason or "cancelled by a human", "by": by or "operator"}
    tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rec, indent=1))
    os.replace(tmp, p)
    return p


def clear_cancelled(label: str, runs_dir=None) -> bool:
    try:
        cancel_path(label, runs_dir).unlink()
        return True
    except OSError:
        return False


def _parent_label(label: str, runs_dir=None):
    """The parent plan of a sub-plan: its run state's `repo` is the parent's chain
    worktree `.../wt-slice-<parent>-chain`. None for a root plan / unreadable."""
    try:
        st = json.loads((_runs_dir(runs_dir) / f"{label}.json").read_text())
    except (OSError, ValueError):
        return None
    repo = str((st or {}).get("repo") or "") if isinstance(st, dict) else ""
    m = re.match(r"^wt-slice-(.+)-chain$", os.path.basename(repo.rstrip("/")))
    return m.group(1) if m else None


def cancelled(label: str, runs_dir=None, _depth: int = 8):
    """The cancel record ({at, reason, by, label}) covering `label` -- its own or an
    ancestor plan's -- or None. Never raises."""
    seen = set()
    cur = label
    for _ in range(_depth):
        if not cur or cur in seen:
            return None
        seen.add(cur)
        p = cancel_path(cur, runs_dir)
        if p.exists():
            try:
                rec = json.loads(p.read_text())
                return rec if isinstance(rec, dict) else {"label": cur}
            except (OSError, ValueError):
                return {"label": cur, "reason": "(unreadable cancel marker)"}
        cur = _parent_label(cur, runs_dir)
    return None
