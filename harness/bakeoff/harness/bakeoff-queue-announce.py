#!/usr/bin/env python3
"""Visibility-only bridge: make a bake-off cell VISIBLE on the ollama-queue
without letting the queue MANAGE it.

WHY THIS EXISTS (and why it is not `ollama-queue.py enqueue`)
------------------------------------------------------------
The bake-off must keep executing via /Users/user/bin/ollama-worker-v7.py, the
pinned comparability anchor for the already-completed result rows. Routing a
cell through `ollama-queue.py enqueue --runner ollama-worker-v7.py` cannot
reproduce v7's invocation: the queue's runner path (ollama-queue.py _build_cmd)
passes an alternate runner ONLY `--model --host --num-ctx --cwd --task-file`,
dropping the sampling flags (--temperature/--top-p/--top-k), --verify,
--max-iters and --manual-tools that the bake-off passes and that change model
output. The queue also picks its own host and owns the process, which breaks the
bake-off's synchronous wait / per-cell host restart+unload / before-after
telemetry / exact $LOG path. So execution stays a direct v7 launch; THIS script
only writes an advisory record into the queue's state so the run shows up.

SAFETY: the record uses status "external", a value the queue daemon never acts
on. The daemon only (a) LAUNCHES jobs with status "pending" and (b) RECOVERS /
ADOPTS jobs with status "running" on restart (ollama-queue.py cmd_run startup
loop + reap loop). "external" is neither, so no unchanged daemon will ever
relaunch, adopt, requeue, kill, or gate one of these records -- it is inert
scheduling-wise and merely displays in `ollama-queue.py status` and the web API
(both read the same state file; the dashboard shows every non-done job). No
change to ollama-queue.py is required. Writes go through the SAME lock file and
the SAME atomic tmp+rename that ollama-queue.py's _Locked uses, so a concurrent
daemon poll can never see a torn file.

This whole path is OFF unless the driver sets BAKEOFF_QUEUE_ANNOUNCE=1; the
driver calls to this script are the only thing gated, so a normal (un-announced)
bake-off run is byte-identical to before and its v7 command is untouched.
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Mirror ollama-queue.py's own constants exactly (kept in sync by value, not by
# import, so this helper has no dependency on the machine-config file's internals
# and cannot be broken by an unrelated refactor there).
STATE_PATH = Path.home() / "bin" / "ollama-queue-state.json"
LOCK_PATH = Path.home() / "bin" / "ollama-queue-state.lock"

# The status the queue daemon does not manage. If ollama-queue.py ever grows a
# meaning for this string, change it here -- it must stay a value that is neither
# "pending" nor "running" nor any terminal status the reaper writes.
STATUS_RUNNING = "external"


class _Locked:
    """Byte-for-byte the lock protocol of ollama-queue.py's _Locked: exclusive
    fcntl flock on LOCK_PATH held across one read-modify-write, released on exit.
    """
    def __enter__(self):
        LOCK_PATH.touch(exist_ok=True)
        self._fh = open(LOCK_PATH, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()

    def load(self):
        if not STATE_PATH.exists():
            return {"jobs": []}
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # Never clobber a state file we can't parse -- the daemon owns
            # corruption handling; we simply refuse to touch it.
            raise
    def save(self, state):
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp.announce")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_PATH)


def _now():
    return datetime.now(timezone.utc).isoformat()


def cmd_start(a):
    rec = {
        "id": a.id,
        "label": a.label,
        "model": a.model,
        "host_pref": a.host,
        "cwd": a.cwd,
        "task_file": None,
        "runner": None,
        "num_ctx": int(a.num_ctx) if a.num_ctx is not None else None,
        "status": STATUS_RUNNING,
        "enqueued_at": _now(),
        "pid": int(a.pid) if a.pid is not None else None,
        "lane": None,
        "log_path": a.log,
        "exit_code": None,
        "live_log_path": None,
        # Provenance marker so anyone reading state knows the queue does NOT own
        # this process -- it is a bake-off cell announced for visibility only.
        "external": True,
        "external_source": "bakeoff",
        "launched_by_session": os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID"),
    }
    with _Locked() as lock:
        state = lock.load()
        jobs = state.setdefault("jobs", [])
        # Idempotent on id: replace any prior record with the same id rather than
        # duplicating (a retried cell reuses its id).
        jobs[:] = [j for j in jobs if j.get("id") != a.id]
        jobs.append(rec)
        lock.save(state)
    print(a.id)


def cmd_finish(a):
    status = "done" if int(a.exit) == 0 else "failed"
    with _Locked() as lock:
        state = lock.load()
        for j in state.get("jobs", []):
            if j.get("id") == a.id:
                j["status"] = status
                j["exit_code"] = int(a.exit)
                j["pid"] = None
                j["finished_at"] = _now()
                break
        lock.save(state)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="write the visibility record for a launched cell")
    s.add_argument("--id", required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--host", required=True)
    s.add_argument("--pid", required=True)
    s.add_argument("--cwd", required=True)
    s.add_argument("--num-ctx", dest="num_ctx", default=None)
    s.add_argument("--log", default=None)
    s.set_defaults(func=cmd_start)

    f = sub.add_parser("finish", help="mark a cell's record terminal")
    f.add_argument("--id", required=True)
    f.add_argument("--exit", required=True)
    f.set_defaults(func=cmd_finish)

    a = p.parse_args()
    try:
        a.func(a)
    except Exception as e:
        # Visibility must NEVER break a bake-off run. Fail soft, non-zero for the
        # caller's logs but the driver ignores our exit (|| true).
        print(f"[announce] non-fatal: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
