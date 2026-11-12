"""I/O for queue-driven BloomGauge control: config, the BloomGauge client, the lease file and the
ledger. Design: queue_bloomgauge_control_design.md (2026-10-31). Decisions live in
bloomgauge_policy.py; bloomgauge_tick.py wires both into the queue daemon.

Transport: the app's own agent_mcp.py (inside the BloomGauge app bundle) is loaded as a module and
its functions are called in-process (get / set_manager / pick_model), so request shapes always
match the installed app. It talks to BloomGauge on 127.0.0.1 (BLOOMGAUGE_PORT, default 8765) with
the key the app keeps while More > Settings > Let your agent make changes is on. This module never
reads, stores or logs that key, never runs darkbloom and never touches provider.toml.

Mode: QUEUE_BLOOMGAUGE_CONTROL=off|shadow|on (env wins when set), else the first word of
~/.ollama-dispatch/bloomgauge-control.mode, default off. The file ~/.ollama-dispatch/bloomgauge-control.off
forces off. shadow never POSTs: it only logs the calls it would make.
"""
import importlib.util
import json
import os
from pathlib import Path

MCP_CANDIDATES = (
    "/Applications/BloomGauge Beta.app/Contents/Resources/agent_mcp.py",
    "/Applications/BloomGauge.app/Contents/Resources/agent_mcp.py",
)
WRITE_ACTIONS = ("mgr_off", "mgr_on", "pick")
_LAST_LOGGED = {}   # ledger path -> (action, reason) of the last none/wait row written


def config(env=None, home=None):
    """-> dict of settings, read fresh on every call (no daemon restart needed). Keys: mode
    (off|shadow|on), home, lease_path, shadow_lease_path, ledger_path, mcp_path, picks_per_h (<= 2),
    mgr_per_h (<= 6), idle_s, quiet_s, yield_s, lease_ttl_s, tick_s, heartbeat_at, allow_picks
    (default on), handback (always|prior). heartbeat_at is the mtime of <home>/session-heartbeat
    (touched by a Claude session hook), None when missing. Unknown or unreadable values fall back
    to the defaults; an unknown mode is off."""
    if env is None:
        env = os.environ

    # Determine home
    if home is not None:
        home = str(home)
    else:
        home = env.get("QUEUE_BG_HOME", str(Path.home() / ".ollama-dispatch"))
    home = str(home)

    # Determine mode
    mode = "off"  # default
    raw = str(env.get("QUEUE_BLOOMGAUGE_CONTROL") or "").strip().lower()
    if raw:
        # env wins when set: an unknown value is off; it never falls through to the mode file
        mode = raw if raw in ("off", "shadow", "on") else "off"
    else:
        # Try reading from file
        mode_file = Path(home) / "bloomgauge-control.mode"
        try:
            text = mode_file.read_text().strip()
            if text:
                first_word = text.split()[0].lower()
                if first_word in ("off", "shadow", "on"):
                    mode = first_word
        except Exception:
            pass

    # Check kill switch file
    off_file = Path(home) / "bloomgauge-control.off"
    if off_file.exists():
        mode = "off"

    # Paths
    lease_path = os.path.join(home, "bloomgauge-lease.json")
    shadow_lease_path = os.path.join(home, "bloomgauge-lease.shadow.json")
    ledger_path = os.path.join(home, "bloomgauge-control.jsonl")

    # mcp_path
    mcp_path = env.get("QUEUE_BG_MCP_PATH", "")

    # Numbers with defaults and clamping
    def safe_float(key, default):
        try:
            return float(env.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def clamp_int(key, default, lo, hi):
        try:
            val = int(float(env.get(key, str(default))))
            return max(lo, min(hi, val))
        except (ValueError, TypeError, OverflowError):   # "inf" overflows int(): a bad value, so the default
            return default

    picks_per_h = clamp_int("QUEUE_BG_PICKS_PER_H", 2, 0, 2)
    mgr_per_h = clamp_int("QUEUE_BG_MANAGER_PER_H", 4, 0, 6)
    idle_s = safe_float("QUEUE_BG_IDLE_MIN", 15.0) * 60
    quiet_s = safe_float("QUEUE_BG_QUIET_MIN", 15.0) * 60
    yield_s = safe_float("QUEUE_BG_YIELD_S", 7200.0)
    lease_ttl_s = safe_float("QUEUE_BG_LEASE_TTL_S", 600.0)
    tick_s = safe_float("QUEUE_BG_TICK_S", 30.0)

    # allow_picks: default True (env != "0")
    allow_picks = env.get("QUEUE_BG_ALLOW_PICKS", "1").strip() != "0"

    # handback: "prior" when env == "prior", else "always"
    handback = "prior" if env.get("QUEUE_BG_HANDBACK", "").strip() == "prior" else "always"

    # heartbeat_at: mtime of session-heartbeat file, None when missing
    hb_file = Path(home) / "session-heartbeat"
    try:
        heartbeat_at = hb_file.stat().st_mtime
    except Exception:
        heartbeat_at = None

    return {
        "mode": mode,
        "home": home,
        "lease_path": lease_path,
        "shadow_lease_path": shadow_lease_path,
        "ledger_path": ledger_path,
        "mcp_path": mcp_path,
        "picks_per_h": picks_per_h,
        "mgr_per_h": mgr_per_h,
        "idle_s": idle_s,
        "quiet_s": quiet_s,
        "yield_s": yield_s,
        "lease_ttl_s": lease_ttl_s,
        "tick_s": tick_s,
        "heartbeat_at": heartbeat_at,
        "allow_picks": allow_picks,
        "handback": handback,
    }


def load_client(path=None):
    """Load BloomGauge's agent_mcp.py by path (path, else MCP_CANDIDATES in order) as a module.
    -> the module, or None when no candidate exists, it fails to load, or it lacks get /
    set_manager / pick_model / Unavailable. Never raises; never guesses routes."""
    candidates = [path] if path else MCP_CANDIDATES
    for cand in candidates:
        if not cand or not Path(cand).is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("bloomgauge_agent_mcp", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if all(hasattr(mod, name) for name in ("get", "set_manager", "pick_model", "Unavailable")):
                return mod
        except Exception:
            continue
    return None


def lease_load(path):
    """-> the lease dict, or None when the file is missing. A corrupt or non-dict file is treated
    as a lease with an unknown prior: {'state': 'DRAIN', 'idle_since': 0, 'prior_manager_on': True,
    'since': <file mtime>, 'corrupt': True}, so the next idle tick hands the Manager back."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:
            since = os.stat(path).st_mtime
        except OSError:
            since = 0.0
        return {"state": "DRAIN", "idle_since": 0, "prior_manager_on": True, "since": since, "corrupt": True}
    if isinstance(data, dict) and data.get("state") in ("HELD", "DRAIN", "HANDBACK"):
        return data
    try:
        since = os.stat(path).st_mtime
    except OSError:
        since = 0.0
    return {"state": "DRAIN", "idle_since": 0, "prior_manager_on": True, "since": since, "corrupt": True}


def lease_save(path, lease):
    """Write the lease atomically (tmp file + os.replace, sorted keys); lease None removes the
    file (missing is fine). Creates the parent dir."""
    p = Path(path)
    if lease is None:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(json.dumps(lease, sort_keys=True))
    os.replace(tmp, str(p))


def _ledger_append(path, row):
    """Append row to the JSONL ledger at path as one line (json.dumps sort_keys=True + newline),
    creating the parent dir. Never truncates existing lines."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def record_manual(ledger_path, manual_at, now, mode):
    """Append one {phase: 'manual', manual_at} row: a manual change the tick saw in the activity log,
    kept in the ledger so it still counts once it scrolls out of the log (bloomgauge_policy.budget
    reads the newest). Never a write to BloomGauge."""
    _ledger_append(ledger_path, {"ts": now, "phase": "manual", "action": "none", "model": None, "mode": mode,
                                 "manual_at": manual_at, "reason": "manual change seen in the activity log"})


def record_alert(ledger_path, kind, model, streak, now, mode, cause=None):
    """Append one {phase: 'alert', kind: pick|manager} row: the owner was alerted to a streak of failed
    writes of that kind (bloomgauge_tick.note_failure), so a later failure in the same streak, from the
    daemon or the stale-lease guard, does not alert again (bloomgauge_policy.budget *_alerted).
    Never a write to BloomGauge."""
    row = {"ts": now, "phase": "alert", "action": "none", "model": model, "mode": mode, "kind": kind,
           "streak": streak, "reason": f"alert raised: {streak} failed {kind} writes in a row"}
    if cause:
        row["cause"] = cause
    _ledger_append(ledger_path, row)


def op_id_of(resp):
    """The id of the Manager-on operation BloomGauge's answer to set-automatic names (its snapshot:
    operation {id, status, detail} and lastRequestId, both the request's requestId when it started a
    turn-on); None when the answer names none, or names a different newest request."""
    if not isinstance(resp, dict):
        return None
    op = resp.get("operation") if isinstance(resp.get("operation"), dict) else {}
    oid = op.get("id")
    if not isinstance(oid, str) or not oid:
        return None
    last = resp.get("lastRequestId")
    if isinstance(last, str) and last and last != oid:
        return None
    return oid


def record_outcome(ledger_path, attempt_ts, op_id, status, detail, now, mode):
    """Append one {phase: 'outcome', action: 'mgr_on', attempt_ts, op_id, status, detail} row: how
    BloomGauge settled the queue's answered Manager-on of attempt_ts (bloomgauge_tick.settle; status
    active, superseded, blocked, cancelled, timeout or lost). Never a write to BloomGauge. -> the row."""
    row = {"ts": now, "phase": "outcome", "action": "mgr_on", "model": None, "mode": mode,
           "attempt_ts": attempt_ts, "op_id": op_id, "status": status,
           "detail": " ".join(str(detail or "").split())[:200], "reason": f"turn-on {status}"}
    _ledger_append(ledger_path, row)
    return row


def apply(decision, client, mode, ledger_path, now):
    """Carry out one decision and log it to the JSONL ledger. Write actions (mgr_off, mgr_on,
    pick): mode 'shadow' appends one row {phase: 'shadow'} and sends nothing; mode 'on' appends
    {phase: 'attempt'} BEFORE calling client.set_manager / client.pick_model (so a crash still
    counts it), then {phase: 'result', result: ok|failed, err}; an answered Manager-on's result row
    also carries op_id (op_id_of its answer, None when it names none), which the tick follows until
    BloomGauge settles it. Other actions append a
    {phase: 'decision'} row only when (action, reason) differs from the last one logged to that
    ledger. Every row carries ts, action, model, reason, mode (and cause when the decision has
    one). Rows are written with _ledger_append.
    -> {'ok': bool, 'posted': bool, 'err': str|None, 'op_id': str|None}."""
    act = decision["action"]
    lp = str(ledger_path)

    row = {"ts": now, "action": act, "model": decision.get("model"),
           "reason": decision.get("reason"), "mode": mode}
    cause = decision.get("cause")
    if cause:
        row["cause"] = cause

    if act in ("mgr_off", "mgr_on", "pick"):
        _LAST_LOGGED.pop(lp, None)
        if mode != "on":
            row["phase"] = "shadow"
            _ledger_append(ledger_path, row)
            return {"ok": True, "posted": False, "err": None, "op_id": None}
        else:
            row["phase"] = "attempt"
            _ledger_append(ledger_path, row)
            try:
                if act == "pick":
                    client.pick_model({"model": decision.get("model")})
                    oid = None
                else:
                    resp = client.set_manager({"on": act == "mgr_on"})
                    oid = op_id_of(resp) if act == "mgr_on" else None
                result_row = dict(row)
                result_row["phase"] = "result"
                result_row["result"] = "ok"
                result_row["err"] = None
                if act == "mgr_on":
                    result_row["op_id"] = oid
                _ledger_append(ledger_path, result_row)
                return {"ok": True, "posted": True, "err": None, "op_id": oid}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:120]
                result_row = dict(row)
                result_row["phase"] = "result"
                result_row["result"] = "failed"
                result_row["err"] = err
                _ledger_append(ledger_path, result_row)
                return {"ok": False, "posted": True, "err": err, "op_id": None}
    else:
        key = (act, decision.get("reason"))
        last = _LAST_LOGGED.get(lp)
        if last != key:
            _LAST_LOGGED[lp] = key
            row["phase"] = "decision"
            _ledger_append(ledger_path, row)
        return {"ok": True, "posted": False, "err": None, "op_id": None}
