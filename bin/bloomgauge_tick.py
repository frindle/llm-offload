"""Queue-daemon glue for BloomGauge control: one control step per call, run off the daemon's
thread. ollama-queue.py calls run_async(work) every tick with work = {busy, why, target}; busy is
False only when the queue has no running or launchable Darkbloom-lane work left
(bloom_queue_work_remaining). Design: queue_bloomgauge_control_design.md (2026-10-31).

Modes (bloomgauge_control.config): off = nothing (but a real lease left from 'on' is handed back
once); shadow = reads BloomGauge and logs would-do calls to the ledger, its lease lives in a
separate shadow file; on = acts.

Failed writes (2026-11-02): the policy backs off after each one (bloomgauge_policy._fail_backoff,
read from the ledger, so it survives a daemon restart); a failed pick leaves the lease as decided
minus the pick, any other failed write keeps the old lease. When policy.ALERT_STREAK writes of one
kind fail in a row, one stable message is raised (note_failure: through the queue's _control_alert,
which the hook hands over as _STATE['deliver'], into ESCALATIONS.md + a desktop notification; else
_STATE['alert'] for the hook to pop); one per streak, kept in the ledger (phase 'alert').
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_STATE = {"thread": None, "last": 0.0, "idle_since": None, "next_poll": 0.0, "alert": None,
          "log_bad_since": None, "log_bad_n": 0, "log_alerted": False}
OWNERLESS_POLL_S = 60.0
OWNERLESS_BACKOFF_S = 3600.0
# Manager activity-log rows read per step: BloomGauge's PAGE_MAX (activity_log.py). 100 rows spanned
# ~25 h on 2026-11-02 (peak 21 rows/h), so 500 keeps a manual change in view far past yield_s.
LOG_LIMIT = 500
# The log unreadable while BloomGauge itself answers, this long and this many reads in a row: the
# control cannot act at all (policy waits on bud log_ok), so it raises one alert (note_log).
LOG_ALERT_S = 1800.0
LOG_ALERT_READS = 3
LOG_ALERT_KEY = "bloomgauge-control-log"
LOG_BACK_REASON = "log: activity log readable again"   # ledger row that closes the alert (janitor)


def note_log(view, events, now, cfg=None, ctl=None, eff="on"):
    """Track reads of the manager activity log. BloomGauge answered (view not None) but the log did
    not (events None): count it; once LOG_ALERT_READS reads in a row spanning LOG_ALERT_S failed, set
    _STATE['alert'] (key LOG_ALERT_KEY, one stable sentence) once per streak (only when eff is 'on':
    shadow makes no changes anyway). App down (view None)
    neither counts nor resets. A good read resets the streak; after an alert it also logs one
    LOG_BACK_REASON decision row to the ledger (cfg ledger_path, via ctl.apply) so the janitor can
    close the alert. -> the alert dict or None."""
    st = _STATE
    if view is None:
        return None
    if events is not None:
        alerted = st.get("log_alerted")
        st.update(log_bad_since=None, log_bad_n=0, log_alerted=False)
        if alerted and cfg is not None and ctl is not None:
            try:
                ctl.apply({"action": "none", "model": None, "reason": LOG_BACK_REASON}, None, eff, cfg["ledger_path"], now)
            except Exception:
                pass
        return None
    if st.get("log_bad_since") is None:
        st["log_bad_since"] = now
    st["log_bad_n"] = int(st.get("log_bad_n") or 0) + 1
    if (eff != "on" or st.get("log_alerted") or st["log_bad_n"] < LOG_ALERT_READS
            or now - st["log_bad_since"] < LOG_ALERT_S):
        return None
    st["log_alerted"] = True
    msg = ("BloomGauge answers but its manager activity log (GET /api/activity/log) has been unreadable for "
           "30+ min, so the queue's BloomGauge control cannot tell a manual change from its own and makes no "
           "Manager changes or picks: during queue work the Manager may stay on, after it the Manager may stay "
           "off. It resumes on its own once the log reads again; this row clears then. Check the BloomGauge app "
           "(update or restart it). Never call BloomGauge write tools or darkbloom by hand")
    alert = {"key": LOG_ALERT_KEY, "msg": msg}
    _raise(alert)
    return alert


def note_manual(bud, now, cfg, ctl, eff):
    """Keep a manual change the activity log shows (bud manual_log_at) in the ledger when it is newer
    than the one already kept (bud manual_kept_at): ctl.record_manual. The log holds LOG_LIMIT rows,
    so an old manual change scrolls out of it; the policy reads the kept one (Fable review #2).
    Never raises. -> True when a row was written."""
    at = (bud or {}).get("manual_log_at")
    kept = (bud or {}).get("manual_kept_at")
    if at is None or (kept is not None and at <= kept):
        return False
    try:
        ctl.record_manual(cfg["ledger_path"], at, now, eff)
        return True
    except Exception as e:
        print(f"[queue] bloomgauge-control: could not keep the manual change: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _raise(alert):
    """Hand an alert to the queue: through _STATE['deliver'] when set (ollama-queue bloomgauge_hook sets
    it to _control_alert, so the row is written at once, even when the daemon dies before its next
    tick), else into _STATE['alert'] for the hook to pop."""
    deliver = _STATE.get("deliver")
    if callable(deliver):
        try:
            print(f"[queue] !!! bloomgauge-control: {alert['msg']}", file=sys.stderr)
            deliver(alert["key"], alert["msg"])
            return
        except Exception as e:
            print(f"[queue] bloomgauge-control: alert not delivered ({type(e).__name__}: {e}); left for the queue",
                  file=sys.stderr)
    _STATE["alert"] = alert


def note_failure(dec, bud, res, policy, ctl=None, cfg=None, eff="on", now=None, deliver=None, streak=None):
    """A write failed. When it makes policy.ALERT_STREAK or more failures of its kind (picks, or Manager
    changes) in a row (bud counts the earlier ones; streak, when given, is the whole run with this one)
    and that streak has not raised its alert yet (bud
    pick_alerted/mgr_alerted, from a phase 'alert' ledger row: written here with ctl.record_alert when
    ctl and cfg are given, so the daemon and the stale-lease guard raise one alert per streak between
    them, across restarts; 2026-11-02, from the simulated run), raise {key, msg}: key
    bloomgauge-control-pick or bloomgauge-control-manager, msg one stable sentence (no timestamps: the
    queue dedupes on it). Raised through deliver(key, msg) when given (the guard), else _raise.
    -> that dict or None."""
    pick = dec.get("action") == "pick"
    bud = bud or {}
    if streak is None:
        streak = int(bud.get("pick_fails" if pick else "mgr_fails") or 0) + 1
    if streak < getattr(policy, "ALERT_STREAK", 3) or bud.get("pick_alerted" if pick else "mgr_alerted"):
        return None
    what = f"picks of {dec.get('model')}" if pick else "Manager changes"
    err = " ".join(str((res or {}).get("err") or "no error text").split())[:160]
    msg = (f"the queue's BloomGauge control failed {streak} {what} in a row (last: {err}). It keeps waiting and "
           f"retrying on its own (backoff up to 1h); this row clears when a write succeeds. Check BloomGauge "
           f"(app open, agent changes on, Manager not blocked). Never call BloomGauge write tools or darkbloom by hand")
    alert = {"key": "bloomgauge-control-" + ("pick" if pick else "manager"), "msg": msg}
    if callable(deliver):
        try:
            deliver(alert["key"], alert["msg"])
        except Exception as e:
            print(f"[queue] bloomgauge-control: alert not delivered ({type(e).__name__}: {e}): {msg}", file=sys.stderr)
    else:
        _raise(alert)
    if ctl is not None and cfg is not None:
        try:
            ctl.record_alert(cfg["ledger_path"], "pick" if pick else "manager", dec.get("model"), streak,
                             time.time() if now is None else now, eff, dec.get("cause"))
        except Exception as e:
            print(f"[queue] bloomgauge-control: could not log the alert: {type(e).__name__}: {e}", file=sys.stderr)
    return alert


def wanted(cfg=None, ctl=None):
    """True when control should run this tick: mode shadow/on, or a real lease file exists (so
    switching to off still hands back once). Cheap: no network."""
    if ctl is None:
        import bloomgauge_control as ctl
    if cfg is None:
        cfg = ctl.config()
    mode = cfg.get("mode", "off")
    if mode in ("shadow", "on"):
        return True
    # mode == "off": check for a real lease file
    return ctl.lease_load(cfg["lease_path"]) is not None


def read_inputs(client, cfg, lease, eff, policy):
    """-> (view, events, ledger). view: policy.read_view of the three GETs, None when any read
    fails (app down); in shadow, a lease whose (simulated) mgr_off_at is set reads as Manager off.
    events: the newest LOG_LIMIT manager activity log rows, None when unreadable (the read failed,
    or the answer is not a dict whose 'events' is a list). ledger: the parsed JSONL rows of
    cfg ledger_path (bad lines skipped, [] when missing)."""
    try:
        mc = client.get("/api/model-control")
        oc = client.get("/api/optimizer/control")
        access = client.get("/api/agent-access")
        view = policy.read_view(mc, oc, access)
    except Exception:
        view = None
    try:
        resp = client.get("/api/activity/log", {"kind": "manager", "limit": LOG_LIMIT})
        events = resp.get("events") if isinstance(resp, dict) else None
        if not isinstance(events, list):
            events = None
    except Exception:
        events = None
    if view is not None and eff == "shadow" and isinstance(lease, dict) and lease.get("mgr_off_at") is not None:
        view = {**view, "manager_on": False}
    return view, events, read_ledger(cfg["ledger_path"])


def read_ledger(path):
    """The parsed JSONL rows of the control ledger at path (bad lines skipped, [] when missing)."""
    try:
        text = Path(path).read_text()
    except Exception:
        return []
    ledger = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            ledger.append(row)
    return ledger


def settle(view, events, bud, now, cfg, policy, ctl, eff="on", deliver=None, cause=None):
    """The queue's accepted Manager-on that BloomGauge has not settled yet (bud turn_on, from the ledger)
    against BloomGauge's operation in view (policy.turn_on_outcome). Once settled, keep the outcome in
    the ledger (ctl.record_outcome) and, when it did not turn the Manager on (blocked, cancelled, timed
    out, lost), count it as a failed Manager change: the policy's backoff applies from the next decision
    on, and note_failure raises the streak alert at policy.ALERT_STREAK (through deliver when given: the
    guard). Runs before every decision (tick, ownerless, the stale-lease guard), so a turn-on is settled
    whoever made it, across daemon restarts (2026-11-03: a blocked hand-back went unseen for 12 h).
    Never raises. -> (bud, status): bud recomputed from the ledger when a row was written (else as
    given), status the outcome or None."""
    try:
        pend = (bud or {}).get("turn_on")
        st, detail = policy.turn_on_outcome(pend, view, now, (bud or {}).get("manual_at"))
        if st is None:
            return bud, None
        ctl.record_outcome(cfg["ledger_path"], pend["ts"], pend["op_id"], st, detail, now, eff)
        new = policy.budget(read_ledger(cfg["ledger_path"]), events, now, cfg)
        if st not in policy.TURN_ON_OK:
            print(f"[queue] bloomgauge-control: the Manager-on of {pend['ts']:.0f} did not turn the Manager on: "
                  f"{st} {detail}", file=sys.stderr)
            dec = {"action": "mgr_on", "model": None, "reason": "turn-on " + st}
            if cause:
                dec["cause"] = cause
            note_failure(dec, new, {"err": policy.turn_on_err(st, detail)}, policy, ctl, cfg, eff, now,
                         deliver=deliver, streak=max(1, int(new.get("mgr_fails") or 0)))
        return new, st
    except Exception as e:
        print(f"[queue] bloomgauge-control: could not settle the turn-on: {type(e).__name__}: {e}", file=sys.stderr)
        return bud, None


def ownerless(now, cfg, client, policy, ctl, eff):
    """No lease, queue idle: turn a Manager that the queue itself left off (policy budget 'leak') back
    on after cfg idle_s; never within cfg yield_s of a manual change. -> decision or None."""
    st = _STATE
    if st["idle_since"] is None:
        st["idle_since"] = now
    if now - st["idle_since"] < max(cfg["idle_s"], 1) or now < st["next_poll"]:
        return None
    st["next_poll"] = now + OWNERLESS_POLL_S
    client = client or ctl.load_client(cfg.get("mcp_path"))
    if client is None:
        return None
    view, events, ledger = read_inputs(client, cfg, None, eff, policy)
    note_log(view, events, now, cfg, ctl, eff)
    bud = policy.budget(ledger, events, now, cfg)
    note_manual(bud, now, cfg, ctl, eff)
    bud, _st = settle(view, events, bud, now, cfg, policy, ctl, eff)
    if bud.get("manual_at") is not None and now - bud["manual_at"] < cfg.get("yield_s", 7200):
        return None  # never fight a recent manual Manager change
    dec = policy.ownerless_step(view, now - st["idle_since"], cfg, bud, now)
    if dec["action"] == "none":
        return None
    res = ctl.apply(dec, client, eff, cfg["ledger_path"], now)
    if not res["ok"] or eff != "on":
        st["next_poll"] = now + OWNERLESS_BACKOFF_S
    else:
        st["idle_since"] = now  # a fresh idle_s must pass before acting again
    if not res["ok"]:
        note_failure(dec, bud, res, policy, ctl, cfg, eff, now)
    return dec


def tick(work, now=None, cfg=None, client=None, policy=None, ctl=None):
    """One control step. Picks the effective mode and lease file (a real lease held while the mode
    is not 'on' is handed back: busy forced False, idle_s and quiet_s 0). Idle with no lease: returns None with
    no network read at all. Otherwise reads BloomGauge (view None when it does not answer), the
    manager activity log (None when unreadable) and the ledger, settles the queue's last turn-on
    (settle), asks policy.decide, applies the
    decision with ctl.apply and saves the new lease with heartbeat/daemon_pid/mode. When a write
    failed: a pick keeps the new lease with the old lease's picked/picked_at (the pick did not
    happen; the policy's failure backoff spaces the retry), any other write keeps the old lease
    (heartbeat refreshed); note_failure may raise the streak alert. An answered Manager-on keeps the
    lease until BloomGauge settles it (settle), unless the answer named no operation (or shadow): then
    the lease is released at once. -> the decision dict or None."""
    if now is None:
        now = time.time()
    if ctl is None:
        import bloomgauge_control as ctl
    if policy is None:
        import bloomgauge_policy as policy
    cfg = dict(cfg or ctl.config())

    # Step 2: Pick effective mode and lease path
    real = ctl.lease_load(cfg["lease_path"])
    eff, path = "on", cfg["lease_path"]
    mode = cfg.get("mode")
    if mode != "on" and real is not None:
        work = {"busy": False, "why": f"control {mode}: handing back", "target": None}
        cfg.update(idle_s=0, quiet_s=0)
    elif mode == "shadow":
        eff = "shadow"
        path = cfg["shadow_lease_path"]
    elif mode != "on":
        _STATE.update(idle_since=None, next_poll=0.0)
        return None

    # Step 3: Load the appropriate lease
    lease = real if path == cfg["lease_path"] else ctl.lease_load(path)

    # Step 4: Idle with no lease -> maybe turn a Manager that is off back on (else no reads)
    if work.get("busy") or lease is not None:
        _STATE["idle_since"] = None
    else:
        return ownerless(now, cfg, client, policy, ctl, eff)

    # Step 5: Client handling - no client means wait
    if client is None:
        client = ctl.load_client(cfg.get("mcp_path"))
        if client is None:
            dec = {"action": "wait", "model": None, "reason": "control-unavailable: agent_mcp.py not found"}
            ctl.apply(dec, None, eff, cfg["ledger_path"], now)
            return dec

    # Step 6: Read inputs and decide
    view, events, ledger = read_inputs(client, cfg, lease, eff, policy)
    note_log(view, events, now, cfg, ctl, eff)
    bud = policy.budget(ledger, events, now, cfg)
    note_manual(bud, now, cfg, ctl, eff)
    bud, _st = settle(view, events, bud, now, cfg, policy, ctl, eff)
    dec, new = policy.decide(lease, view, work, now, cfg, bud)

    # Step 7: Apply decision; a failed write leaves the lease as it was (a pick: minus the pick)
    result = ctl.apply(dec, client, eff, cfg["ledger_path"], now)
    if result["ok"] and dec.get("action") == "mgr_on" and not result.get("op_id"):
        # nothing to settle (shadow sends nothing; an answer without an operation id cannot be
        # followed): release as soon as the write is answered, as before 2026-11-03
        new = None
    if not result["ok"]:
        if dec.get("action") == "pick" and isinstance(new, dict):
            new = dict(new)
            for k in ("picked", "picked_at"):
                if isinstance(lease, dict) and k in lease:
                    new[k] = lease[k]
                else:
                    new.pop(k, None)
        else:
            new = lease
        note_failure(dec, bud, result, policy, ctl, cfg, eff, now)

    # Step 8: Save lease (or remove if new is None)
    # cfg owner_pid (2026-11-12): the Mac status service runs this step for a queue that runs elsewhere and passes
    # None, so the lease names no live process and the stale-lease guard goes by the heartbeat alone (that
    # service outlives the queue; its pid would keep the guard from ever handing back).
    owner = cfg.get("owner_pid", os.getpid())
    ctl.lease_save(path, None if new is None else dict(new, heartbeat=now, daemon_pid=owner, mode=eff))

    return dec


def run_async(work, now=None, cfg=None, fn=None):
    """Start tick(work) on a daemon thread, at most once per cfg tick_s (default 30 s) and never
    while the previous one is still running (a BloomGauge call can take 25 s; the queue tick must
    never wait on it). fn (default tick) is called as fn(work, now, cfg). Errors are printed to
    stderr, never raised. -> True when started."""
    if fn is None:
        fn = tick
    state = _STATE
    th = state["thread"]
    if th is not None and th.is_alive():
        return False
    if now is None:   # resolve first: a bare run_async(work) must be throttled like the hook's call
        now = time.time()
    tick_s = (cfg or {}).get("tick_s", 30)
    last = state["last"]
    if (now - last) < tick_s:
        return False
    def _worker():
        try:
            fn(work, now, cfg)
        except Exception as e:
            print(f"[queue] bloomgauge-control: error {type(e).__name__}: {e}", file=sys.stderr)
    th = threading.Thread(target=_worker, name="bloomgauge-control", daemon=True)
    state["thread"] = th
    state["last"] = now
    th.start()
    return True
