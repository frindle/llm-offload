#!/usr/bin/env python3
"""Stale-lease fail-safe for queue-driven BloomGauge control (design section 6, path 3).

If the queue daemon died while it held BloomGauge's Manager off (real lease file
~/.ollama-dispatch/bloomgauge-lease.json with a heartbeat older than QUEUE_BG_LEASE_TTL_S and a
dead daemon_pid), hand the Manager back the way the daemon would (same waits, same budget, same
yield to a manual change), log it to the ledger with cause=deadman and clear the lease. Never
acts on the shadow lease; never runs darkbloom, launchctl or touches provider.toml. Meant for a
com.example.* LaunchAgent every few minutes (com.example.bloomgauge-lease-guard, every 300 s). Run: python3 bloomgauge_lease_guard.py  -> prints the decision as JSON.
"""
import json
import os
import sys
import time
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def pid_alive(pid):
    """True when pid is a live process (signal 0; a PermissionError means it exists)."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def guard(now=None, cfg=None, client=None, ctl=None, policy=None, alive=None, tick=None, alert=None):
    """One fail-safe check: the daemon's own hand-back, run for a daemon that died holding the lease.
    No real lease, a fresh heartbeat (a corrupt lease file: its mtime) or a live daemon_pid: None (no
    read, no write). A lease kept on a yield to a manual change less than cfg yield_s ago
    (policy._yielded): wait, no read (the daemon would not hand back yet either). Else read BloomGauge,
    its activity log (tick.LOG_LIMIT rows) and the control ledger as a tick does (tick.read_inputs),
    keep a manual change in the ledger (tick.note_manual), settle the queue's last turn-on (tick.settle,
    its failure alert through alert) and decide as an idle tick whose hand-back
    is due (policy.decide on the lease as DRAIN with idle_since 0, cfg idle_s 0; 2026-11-02, the
    simulated run). So the guard waits on everything the daemon waits on (app down, agent changes
    off, the activity log unreadable, mid-change, a blocker, the hourly Manager budget with the
    queue's own writes counted, the failure backoff), and yields to a manual change made after the
    lease was taken (or after its yielded_at): Manager off -> the lease kept with yielded_at (no change
    for yield_s, then the hand-back), Manager on -> released. Nothing to hand back (Manager on, or
    off before with handback 'prior') -> released, no write. Else Manager on through ctl.apply (mode
    'on'); the lease is kept when it failed, and when BloomGauge answered with an operation (op_id) until
    a later run or the daemon sees it settled (2026-11-03: a blocked turn-on must be retried, so the
    lease outlives the answer); cleared when the answer named no operation. While a turn-on is under way
    the decision waits. Every decision carries cause 'deadman' and a 'deadman: ' reason.
    -> the decision dict or None."""
    if ctl is None:
        import bloomgauge_control as ctl
    if policy is None:
        import bloomgauge_policy as policy
    if tick is None:
        import bloomgauge_tick as tick
    now = time.time() if now is None else now
    cfg = cfg or ctl.config()
    alive = alive or pid_alive
    lease = ctl.lease_load(cfg["lease_path"])
    if lease is None:
        return None
    beat = lease.get("heartbeat")
    if beat is None and lease.get("corrupt"):
        beat = lease.get("since")   # the file's mtime: a live daemon rewrites it on its next tick
    if now - float(beat or 0) < cfg.get("lease_ttl_s", 600):
        return None
    if alive(lease.get("daemon_pid")):
        return None
    if policy._yielded(lease, now, cfg):
        dec = {"action": "wait", "model": None, "cause": "deadman",
               "reason": "deadman: yielded to a manual change less than yield_s ago; lease kept"}
        ctl.apply(dec, None, "on", cfg["ledger_path"], now)
        return dec
    client = client or ctl.load_client(cfg.get("mcp_path"))
    deliver = alert or (lambda key, msg: print(f"[guard] !!! {key}: {msg}", file=sys.stderr))
    view, events, ledger = tick.read_inputs(client, cfg, lease, "on", policy)
    bud = policy.budget(ledger, events, now, cfg)
    tick.note_manual(bud, now, cfg, ctl, "on")
    bud, _st = tick.settle(view, events, bud, now, cfg, policy, ctl, "on", deliver=deliver, cause="deadman")
    due = dict(lease, state="DRAIN", idle_since=0.0)
    idle = {"busy": False, "why": "deadman: queue daemon gone", "target": None}
    d, new = policy.decide(due, view, idle, now, dict(cfg, idle_s=0), bud)
    act = d["action"]
    if view is None:
        reason = "deadman: BloomGauge not answering; lease kept"
    elif act == "mgr_on":
        reason = "deadman: stale lease, daemon gone; Manager on"
    elif act == "none" and new is None:
        reason = "deadman: nothing to hand back; lease released"
    else:
        reason = "deadman: " + str(d.get("reason"))
    dec = {"action": act, "model": None, "cause": "deadman", "reason": reason}
    res = ctl.apply(dec, client, "on", cfg["ledger_path"], now)
    if not res.get("ok"):
        # its failures count in the daemon's streak: raise the streak's alert when this one makes it
        tick.note_failure(dec, bud, res, policy, ctl, cfg, "on", now, deliver=deliver)
    if act == "mgr_on":
        if res["ok"] and not res.get("op_id"):
            ctl.lease_save(cfg["lease_path"], None)
        # answered with an operation: the lease stays (still stale) until a later run, or the daemon,
        # sees BloomGauge settle it (tick.settle); a blocked turn-on is retried after the backoff
    elif new is None:
        ctl.lease_save(cfg["lease_path"], None)
    elif new.get("yielded_at") != lease.get("yielded_at"):
        kept = dict(lease, yielded_at=new["yielded_at"])   # heartbeat and daemon_pid stay stale: still the guard's
        kept.pop("mgr_on_seen_at", None)
        ctl.lease_save(cfg["lease_path"], kept)
    return dec


def queue_alert(key, msg):
    """ollama-queue's _control_alert (an ESCALATIONS.md row + a desktop notification), loaded from this
    directory, so the guard's alert rows match the queue's and the janitor closes them. Printed when it
    cannot be loaded. -> its result, or False."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ollama_queue_for_guard", os.path.join(_HERE, "ollama-queue.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._control_alert(key, msg)
    except Exception as e:
        print(f"[guard] !!! {key}: {msg} (not raised: {type(e).__name__}: {e})", file=sys.stderr)
        return False


if __name__ == "__main__":
    print(json.dumps(guard(alert=queue_alert)))
