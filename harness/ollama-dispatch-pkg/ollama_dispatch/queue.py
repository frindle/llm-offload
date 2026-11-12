"""A persistent dispatch queue for the Ollama worker.

The queue serializes GPU work across one or more hosts and runs each job by
invoking the worker. It is NOT strictly FIFO: pending jobs are ordered to
minimize model swaps (jobs needing an already-resident model run first), and a
job is only routed to an overflow host when its model provably fits that host's
budget (see config.pick_host semantics, mirrored by the worker's router).

State is a single JSON file under $OLLAMA_DISPATCH_HOME. Writes are atomic
(temp-then-rename). A lightweight file lock guards concurrent daemon/enqueue
access. This is a clean, self-contained core; the private pipeline layers
additional scheduling (focus/bundle/dependency lanes, gate lanes, slice plans)
on top of this same state model.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config


def state_path() -> Path:
    return config.dispatch_home() / "queue-state.json"


def log(msg: str) -> None:
    print(f"[queue] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# State load/save (atomic)
# --------------------------------------------------------------------------

def load_state() -> dict:
    path = state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            log("state file unreadable -- starting fresh")
    return {"jobs": []}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


class FileLock:
    """A minimal advisory lock via O_CREAT|O_EXCL, with stale-lock reclaim."""

    def __init__(self, path: Path, stale_after_s: int = 3600):
        self.path = path
        self.stale_after_s = stale_after_s

    def __enter__(self):
        for _ in range(600):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale_after_s:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.1)
        raise TimeoutError(f"could not acquire lock {self.path}")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _lock() -> FileLock:
    return FileLock(config.dispatch_home() / "queue-state.lock")


# --------------------------------------------------------------------------
# Enqueue / status / clear
# --------------------------------------------------------------------------

def enqueue(label: str, cwd: str, task: str, *, model: str | None = None,
            host: str = "auto", verify: str | None = None,
            task_kind: str = "coding") -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "label": label,
        "cwd": str(Path(cwd).expanduser().resolve()),
        "task": task,
        "model": model or config.load_defaults()["model"],
        "host": host,
        "verify": verify,
        "task_kind": task_kind,
        "status": "pending",
        "created": time.time(),
    }
    config.dispatch_home().mkdir(parents=True, exist_ok=True)
    with _lock():
        state = load_state()
        state["jobs"].append(job)
        save_state(state)
    log(f"enqueued {job['id']} ({label})")
    return job


def _resident_models(host_url: str) -> set:
    """Models currently loaded on a host (/api/ps)."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{host_url}/api/ps", timeout=5) as resp:
            data = json.loads(resp.read())
        return {m.get("name") or m.get("model") for m in data.get("models", [])}
    except Exception:  # noqa: BLE001
        return set()


def pending_launch_order(jobs: list) -> list:
    """Order pending jobs to minimize model swaps.

    Jobs whose model is already resident on the primary host sort first (in
    creation order); the rest follow in creation order. This keeps the queue
    from thrashing a big model in and out between every job.
    """
    pending = [j for j in jobs if j.get("status") == "pending"]
    primary = config.BIG_HOST_NAME
    primary_url = config.host_url(primary)
    resident = _resident_models(primary_url) if primary_url else set()

    def key(j):
        return (0 if j.get("model") in resident else 1, j.get("created", 0))

    return sorted(pending, key=key)


def status() -> dict:
    state = load_state()
    counts = {}
    for j in state["jobs"]:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    return {"counts": counts, "jobs": state["jobs"]}


def clear_finished() -> int:
    with _lock():
        state = load_state()
        before = len(state["jobs"])
        state["jobs"] = [j for j in state["jobs"] if j["status"] not in ("done", "failed")]
        save_state(state)
    return before - len(state["jobs"])


# --------------------------------------------------------------------------
# Runner / daemon
# --------------------------------------------------------------------------

def run_one(job: dict) -> dict:
    """Run a single job by invoking the worker. Returns an updated job."""
    import tempfile

    log(f"running {job['id']} ({job['label']}) model={job['model']}")
    with tempfile.NamedTemporaryFile("w", suffix=".task", delete=False) as tf:
        tf.write(job["task"])
        task_file = tf.name
    cmd = [
        sys.executable, "-m", "ollama_dispatch.worker",
        "--task-file", task_file,
        "--cwd", job["cwd"],
        "--model", job["model"],
        "--host", job.get("host", "auto"),
        "--json",
    ]
    if job.get("verify"):
        cmd += ["--verify", job["verify"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        job["status"] = "done" if proc.returncode == 0 else "failed"
        job["result"] = proc.stdout.strip()[-4000:]
        job["stderr"] = proc.stderr.strip()[-4000:]
    finally:
        try:
            os.unlink(task_file)
        except OSError:
            pass
    job["finished"] = time.time()
    return job


def daemon(poll_s: int = 5, once: bool = False) -> None:
    """Serialize and run pending jobs one at a time until drained.

    With once=True, drains the current queue and exits (useful in CI/Docker).
    Otherwise polls forever, picking up newly enqueued jobs.
    """
    log(f"daemon started (poll={poll_s}s, once={once})")
    while True:
        with _lock():
            state = load_state()
        order = pending_launch_order(state["jobs"])
        if not order:
            if once:
                log("queue drained -- exiting (once mode)")
                return
            time.sleep(poll_s)
            continue

        job = order[0]
        # mark running
        with _lock():
            state = load_state()
            for j in state["jobs"]:
                if j["id"] == job["id"]:
                    j["status"] = "running"
                    j["started"] = time.time()
                    break
            save_state(state)

        updated = run_one(job)

        with _lock():
            state = load_state()
            for i, j in enumerate(state["jobs"]):
                if j["id"] == updated["id"]:
                    state["jobs"][i] = updated
                    break
            save_state(state)
        log(f"finished {updated['id']} status={updated['status']}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ollama dispatch queue.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="Add a job.")
    e.add_argument("--label", required=True)
    e.add_argument("--cwd", required=True)
    e.add_argument("--task", help="Task text.")
    e.add_argument("--task-file", help="Path to task text.")
    e.add_argument("--model")
    e.add_argument("--host", default="auto")
    e.add_argument("--verify")
    e.add_argument("--task-kind", default="coding")

    s = sub.add_parser("status", help="Show queue status.")
    s.add_argument("--json", action="store_true")

    sub.add_parser("clear", help="Remove done/failed jobs.")

    d = sub.add_parser("daemon", help="Run the queue runner.")
    d.add_argument("--once", action="store_true", help="Drain and exit.")
    d.add_argument("--poll", type=int, default=5)

    args = ap.parse_args(argv)

    if args.cmd == "enqueue":
        task = args.task
        if args.task_file:
            task = Path(args.task_file).read_text()
        if not task:
            ap.error("one of --task or --task-file is required")
        job = enqueue(args.label, args.cwd, task, model=args.model, host=args.host,
                      verify=args.verify, task_kind=args.task_kind)
        print(job["id"])
        return 0

    if args.cmd == "status":
        st = status()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print("counts:", st["counts"])
            for j in st["jobs"]:
                print(f"  {j['id']}  {j['status']:8}  {j['label']}  ({j['model']})")
        return 0

    if args.cmd == "clear":
        print(f"removed {clear_finished()} finished job(s)")
        return 0

    if args.cmd == "daemon":
        daemon(poll_s=args.poll, once=args.once)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
