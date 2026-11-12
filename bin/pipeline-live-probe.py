#!/usr/bin/env python3
"""pipeline-live-probe.py -- scheduled LIVE PROBE (hardening design item 2).

Does the real wire still match the profile? Every run does ONE of:
  1. VERIFY the previous probe (if one is pending and its job is terminal): its job["conformance"] verdict (the
     daemon's reap-time comparison of the worker's conformance records against the lane PLAN) must be ok with
     >= 1 recorded call; anything else -> alert (notify-owner, deduped).
  2. ENQUEUE a tiny synthetic probe job -- ONLY when nothing is queued/running/held and no bundle is committed
     (a probe must never take a lane from real work or break a bundle's commitment): `_bundle_commit` empty,
     no job in pending/running/paused/held/planned. Otherwise skip silently (the next tick tries again).

Kill switches: LIVE_PROBE=0, ~/.ollama-dispatch/live-probe.disabled. --dry-run prints the decision and command.
State: ~/.ollama-dispatch/live-probe.json. NO GPU work here: the job goes through the queue like any other.
Run by launchd (machine-config/LaunchAgents/com.example.pipeline-live-probe.plist, NOT loaded by default).
Usage: pipeline-live-probe.py [--dry-run] [--state PATH] [--queue-state PATH] [--self-test]"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(os.environ.get("BIN") or Path(__file__).resolve().parent)
QUEUE = BIN / "ollama-queue.py"
QSTATE = HOME / "bin" / "ollama-queue-state.json"
PSTATE = HOME / ".ollama-dispatch" / "live-probe.json"
DISABLE = HOME / ".ollama-dispatch" / "live-probe.disabled"
MIN_GAP_S = 3 * 3600                 # at most one probe per 3 h
BUSY = ("pending", "running", "paused", "held", "planned", "escalated")
TERMINAL = ("done", "failed", "done_unconverged", "needs_opus", "blocked")
MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"


def enabled(env=None):
    e = os.environ if env is None else env
    return str(e.get("LIVE_PROBE", "1")).strip().lower() not in ("0", "off", "false", "no") and not DISABLE.exists()


def _jobs(qs):
    j = qs.get("jobs")
    if isinstance(j, dict):
        return list(j.values())
    return [x for x in (j or []) if isinstance(x, dict)]


def decide(qs, ps, now):
    """PURE. ('verify', job) | ('enqueue', why) | ('skip', why)."""
    pend = (ps or {}).get("job")
    jobs = _jobs(qs)
    if pend:
        for j in jobs:
            if j.get("id") == pend:
                if j.get("status") in TERMINAL:
                    return "verify", j
                if now - (ps.get("at") or 0) > 6 * 3600:
                    return "verify", dict(j, _stuck=True)
                return "skip", f"probe {pend} still {j.get('status')}"
        return "verify", {"id": pend, "_missing": True}
    if now - ((ps or {}).get("last_done") or 0) < MIN_GAP_S:
        return "skip", "probe ran recently"
    if (qs.get("_bundle_commit") or {}).get("key"):
        return "skip", "a bundle is committed"
    busy = [j.get("id") for j in jobs if j.get("status") in BUSY]
    if busy:
        return "skip", f"{len(busy)} job(s) in flight/queued"
    return "enqueue", "idle"


def verdict(job):
    """PURE. (ok, why) for a finished probe job row."""
    if job.get("_missing"):
        return False, "probe job vanished from the queue state"
    if job.get("_stuck"):
        return False, f"probe stuck in {job.get('status')} for > 6 h"
    if job.get("status") != "done":
        return False, f"probe job ended {job.get('status')}"
    c = job.get("conformance")
    if not isinstance(c, dict):
        return False, "no conformance verdict on the probe job (plan/record layer inactive?)"
    if not c.get("ok"):
        return False, "wire != profile: " + json.dumps(c.get("drift"))[:300]
    if int(c.get("n") or 0) < 1:
        return False, "probe made no recorded request"
    return True, f"ok ({c.get('n')} request(s) conform)"


def alert(title, msg, key, dry):
    cmd = [sys.executable, str(BIN / "notify-owner.py"), title, msg, "--dedupe-key", key, "--dedupe-s", "21600"]
    if dry:
        print("ALERT:", cmd[2:])
        return
    subprocess.run(cmd, timeout=60, check=False)


def enqueue(dry):
    d = Path(tempfile.mkdtemp(prefix="live-probe-"))
    tf = d / "TASK.txt"
    tf.write_text("This is a synthetic wire probe. Call task_complete immediately with summary 'probe'. Do not use any other tool.")
    label = "live-probe-" + time.strftime("%Y%m%d-%H%M")
    cmd = [sys.executable, str(QUEUE), "enqueue", "--model", MODEL, "--label", label, "--bundle", "live-probe",
           "--cwd", str(d), "--task-file", str(tf), "--max-iters", "2", "--allow-no-verify", "--allow-unisolated"]
    if dry:
        print("ENQUEUE:", " ".join(cmd))
        return None
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    import re
    m = re.search(r"\b([0-9a-f]{12})\b", out)
    return (m.group(1) if (r.returncode == 0 and m) else None), out[-300:]


def run(args):
    if not enabled():
        print("live probe disabled")
        return 0
    qs = json.loads(Path(args.queue_state).read_text()) if Path(args.queue_state).exists() else {}
    ps = json.loads(Path(args.state).read_text()) if Path(args.state).exists() else {}
    now = time.time()
    act, what = decide(qs, ps, now)
    print(f"decision: {act} ({what if act != 'verify' else what.get('id')})")
    if act == "verify":
        ok, why = verdict(what)
        print(("PROBE OK: " if ok else "PROBE FAIL: ") + why)
        if not ok:
            alert("pipeline live probe FAILED", why, "live-probe-fail", args.dry_run)
        if not args.dry_run:
            Path(args.state).write_text(json.dumps({"last_done": now, "last_ok": ok, "last_why": why}))
    elif act == "enqueue":
        r = enqueue(args.dry_run)
        if r and r[0] and not args.dry_run:
            Path(args.state).parent.mkdir(parents=True, exist_ok=True)
            Path(args.state).write_text(json.dumps({"job": r[0], "at": now, "last_done": (ps or {}).get("last_done")}))
        elif r and not r[0]:
            alert("pipeline live probe could not enqueue", r[1], "live-probe-enq", args.dry_run)
    return 0


def self_test():
    ok = True

    def c(n, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("ok   - " if cond else "FAIL - ") + n)
    now = 1_000_000.0
    c("idle -> enqueue", decide({"jobs": {}}, {}, now)[0] == "enqueue")
    c("committed bundle -> skip", decide({"jobs": {}, "_bundle_commit": {"key": "b"}}, {}, now)[0] == "skip")
    c("pending job -> skip", decide({"jobs": {"a": {"id": "a", "status": "pending"}}}, {}, now)[0] == "skip")
    c("held job -> skip", decide({"jobs": {"a": {"id": "a", "status": "held"}}}, {}, now)[0] == "skip")
    c("recent probe -> skip", decide({"jobs": {}}, {"last_done": now - 60}, now)[0] == "skip")
    c("pending probe, running -> skip", decide({"jobs": {"p": {"id": "p", "status": "running"}}}, {"job": "p", "at": now - 60}, now)[0] == "skip")
    c("pending probe, done -> verify", decide({"jobs": {"p": {"id": "p", "status": "done"}}}, {"job": "p", "at": now - 60}, now)[0] == "verify")
    c("vanished probe -> verify(fail)", not verdict(decide({"jobs": {}}, {"job": "p", "at": now}, now)[1])[0])
    c("verdict ok", verdict({"status": "done", "conformance": {"ok": True, "n": 2}})[0])
    c("verdict drift", not verdict({"status": "done", "conformance": {"ok": False, "drift": [{"class": "HOST_DRIFT"}]}})[0])
    c("verdict no conformance", not verdict({"status": "done"})[0])
    c("verdict failed job", not verdict({"status": "failed"})[0])
    c("verdict zero requests", not verdict({"status": "done", "conformance": {"ok": True, "n": 0}})[0])
    c("kill switch", not enabled({"LIVE_PROBE": "0"}))
    print("LIVE_PROBE_SELFTEST_OK" if ok else "LIVE_PROBE_SELFTEST_FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", default=str(PSTATE))
    ap.add_argument("--queue-state", default=str(QSTATE))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    return self_test() if a.self_test else run(a)


if __name__ == "__main__":
    sys.exit(main())
