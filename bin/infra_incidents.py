#!/usr/bin/env python3
"""infra_incidents -- one append-only log of infrastructure stalls and failures, built to be analyzed.

WHY (2026-11-02): while the screen was locked (20:05-21:38) every launchd job that touched ~/Desktop or
/Volumes/NVMe-Models froze in the kernel: a runner's `git rev-parse HEAD` for 30+ minutes, the power sampler's
read of the sandbox status.json for 33 minutes, dispatch-ack-reconcile's git calls, and Darkbloom's provider
restarts failed to open its model cache. macOS listed the apps as allowed; the kernel only logged "watchdog
expired for approval entry N (kTCCServiceSystemPolicy..., pid P)". Each tool noticed on its own, if at all. This
log puts every such event in one place with the screen-lock state attached, so the next one is a query, not an
investigation.

Writers call record(); it never raises and never touches a protected path (the log lives under
~/.ollama-dispatch, and the lock state comes from ioreg). Records are JSON lines:

    {"ts": 1793684100.5, "iso": "2026-11-03T05:35:00Z", "kind": "path-blocked", "component": "queue",
     "host": "studio", "pid": 25225, "screen_locked": true, "path": "/Users/example/Desktop/x/.git", ...}

Kinds in use (open set; summary() counts anything):
    path-blocked / path-cleared     a path did not answer / answers again (blocked_for_s on the clear)
    git-stall, disk-stall           a runner gave up on git / on copying the work dir (class=INFRA)
    setup-slow                      the runner's setup finished but took longer than it should
    darkbloom-down / darkbloom-up   the local Darkbloom endpoint stopped / resumed serving (cause, down_for_s)
    sandbox-hold / sandbox-release  the Darkbloom sandbox took / gave back the machine
    privacy-approval-stall          the kernel's approval watchdog fired for a process (scan-tcc --record;
                                    the kernel keeps those lines in memory only for minutes, so scan DURING a stall)

CLI:
    infra_incidents.py summary [--since 24h] [--json]
    infra_incidents.py tail [-n 20]
    infra_incidents.py record KIND COMPONENT [key=value ...]
    infra_incidents.py scan-tcc [--since 2h] [--record]

Env: INFRA_INCIDENTS_PATH (default ~/.ollama-dispatch/infra-incidents.jsonl), INFRA_INCIDENTS_MAX_BYTES
(default 5 MB; the full file becomes <path>.1), INFRA_INCIDENTS_SCREEN_LOCKED=0|1 (tests: skip ioreg).
"""
import argparse
import collections
import fcntl
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path.home() / ".ollama-dispatch" / "infra-incidents.jsonl"
DEFAULT_MAX_BYTES = 5_000_000
LOCK_CACHE_S = 30
LOCK_PROBE_TIMEOUT_S = 3
LOCK_NONE_CACHE_S = 10   # an unknown answer (ioreg slow or unreadable) is cached this long, so a stuck ioreg
                         # costs one LOCK_PROBE_TIMEOUT_S wait per 10 s, not one per call
STALL_KINDS = ("path-blocked", "git-stall", "disk-stall", "setup-slow", "privacy-approval-stall")
PAIRS = {"path-blocked": "path-cleared", "darkbloom-down": "darkbloom-up", "sandbox-hold": "sandbox-release"}
FDA_HINT = ("System Settings > Privacy & Security > Full Disk Access: add Homebrew's Python.app (under "
            "/opt/homebrew/Cellar/python@3.x/<version>/Frameworks/Python.framework/Versions/3.x/Resources; re-add "
            "after a Homebrew Python upgrade) and ~/.darkbloom/Darkbloom.app, or keep the repos outside ~/Desktop")

_lock_cache = [0.0, None]


def log_path():
    return Path(os.environ.get("INFRA_INCIDENTS_PATH") or DEFAULT_PATH)


def max_bytes():
    try:
        return int(os.environ.get("INFRA_INCIDENTS_MAX_BYTES") or DEFAULT_MAX_BYTES)
    except ValueError:
        return DEFAULT_MAX_BYTES


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_lock_state(plist_bytes, uid=None):
    """True when no session of `uid` (any user if None) is on the console unlocked; None if unreadable."""
    try:
        users = plistlib.loads(plist_bytes).get("IOConsoleUsers") or []
    except Exception:
        return None
    mine = [u for u in users if uid is None or u.get("kCGSSessionUserIDKey") in (None, uid)]
    return not any(u.get("kCGSSessionOnConsoleKey") and not u.get("CGSSessionScreenIsLocked") for u in mine)


def screen_locked(now=None):
    """Cached for LOCK_CACHE_S (an unknown answer for LOCK_NONE_CACHE_S). ioreg reads no protected files; a
    slow one is abandoned, never waited on."""
    env = os.environ.get("INFRA_INCIDENTS_SCREEN_LOCKED")
    if env in ("0", "1"):
        return env == "1"
    now = time.time() if now is None else now
    if _lock_cache[0] and 0 <= now - _lock_cache[0] < (LOCK_CACHE_S if _lock_cache[1] is not None
                                                          else LOCK_NONE_CACHE_S):
        return _lock_cache[1]
    val = None
    try:
        p = subprocess.Popen(["ioreg", "-n", "Root", "-d1", "-a"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=LOCK_PROBE_TIMEOUT_S)
            val = parse_lock_state(out, os.getuid())
        except subprocess.TimeoutExpired:
            p.kill()          # reaped by subprocess's own cleanup on the next Popen; never waited on here
    except Exception:
        val = None
    _lock_cache[:] = [now, val]
    return val


def _rotate(path):
    try:
        if path.stat().st_size <= max_bytes():
            return
    except OSError:
        return
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # another writer is rotating: let it
        if path.stat().st_size > max_bytes():
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    finally:
        os.close(fd)


def record(kind, component, /, now=None, **fields):
    """Append one incident. Returns the record, or None if it could not be written. Never raises. `kind` and
    `component` are positional-only, so a field of the same name lands in **fields and cannot replace them."""
    try:
        now = time.time() if now is None else float(now)
        # a caller that already knows the lock state (a backfill, the sampler) passes it; the rest are probed
        locked = fields.pop("screen_locked") if "screen_locked" in fields else screen_locked(now)
        rec = {"ts": round(now, 3), "iso": iso(now), "kind": str(kind), "component": str(component),
               "host": socket.gethostname().split(".")[0], "pid": os.getpid(), "screen_locked": locked}
        for k, v in fields.items():
            if v is not None and k not in rec:
                rec[k] = v
        line = json.dumps(rec, default=str) + "\n"
        if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("INFRA_INCIDENTS_PATH"):
            return None       # a test that did not choose a log never writes the real one
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        return rec
    except Exception:
        return None


def load(since=None, path=None):
    """Records from <path>.1 then <path>, oldest first; bad lines are skipped."""
    path = Path(path) if path else log_path()
    out = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and (since is None or r["ts"] >= since):
                out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def _key(r):
    """What pairs a start record with its end: the component, the path (or model), and the queue's probe
    target -- two jobs' probes held on the same path are two incidents, and one clearing closes only its own."""
    return (r.get("component"), r.get("path") or r.get("model") or "", r.get("target") or "")


def open_incidents(recs):
    """Start records (path-blocked, darkbloom-down, sandbox-hold) with no matching end record after them."""
    live = {}
    ends = {v: k for k, v in PAIRS.items()}
    for r in recs:
        k = r.get("kind")
        if k in PAIRS:
            live.setdefault((k, _key(r)), r)
        elif k in ends:
            live.pop((ends[k], _key(r)), None)
    return sorted(live.values(), key=lambda r: r["ts"])


def parse_since(s, now=None):
    now = time.time() if now is None else now
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(s or ""))
    if not m:
        raise ValueError(f"--since wants e.g. 90m, 24h or 7d, not {s!r}")
    mult = {"": 3600, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return now - float(m.group(1)) * mult


def summarize(recs, now=None):
    now = time.time() if now is None else now
    by_kind = collections.Counter(r.get("kind") for r in recs)
    by_comp = collections.Counter(f"{r.get('component')}:{r.get('kind')}" for r in recs)
    by_path = collections.Counter(r.get("path") for r in recs if r.get("path"))
    by_hour = collections.Counter(datetime.fromtimestamp(r["ts"]).strftime("%H:00") for r in recs
                                  if r.get("kind") in STALL_KINDS)
    durations = collections.defaultdict(list)
    for r in recs:
        for f in ("blocked_for_s", "down_for_s", "held_for_s", "seconds"):
            if isinstance(r.get(f), (int, float)):
                durations[r["kind"]].append(float(r[f]))
    stalls = [r for r in recs if r.get("kind") in STALL_KINDS]
    locked = sum(1 for r in stalls if r.get("screen_locked") is True)
    known = sum(1 for r in stalls if r.get("screen_locked") in (True, False))
    opened = open_incidents(recs)
    hints = []
    if known >= 3 and locked / known >= 0.8:
        hints.append(f"{locked} of {known} stalls happened with the screen locked: macOS holds launchd jobs' "
                     f"access to ~/Desktop and external volumes while locked. Fix: {FDA_HINT}.")
    for r in opened:
        age = int(now - r["ts"])
        what = r.get("path") or r.get("cause") or r.get("model") or ""
        hints.append(f"still open: {r['kind']} ({r.get('component')}) {what} for {age // 60}m")
    downs = [r for r in recs if r.get("kind") == "darkbloom-down"]
    causes = collections.Counter(r.get("cause") or "unknown" for r in downs)
    if causes:
        hints.append("Darkbloom down causes: " + ", ".join(f"{c} x{n}" for c, n in causes.most_common()))
    return {
        "records": len(recs),
        "first": recs[0]["iso"] if recs else None,
        "last": recs[-1]["iso"] if recs else None,
        "by_kind": dict(by_kind.most_common()),
        "by_component": dict(by_comp.most_common()),
        "by_path": dict(by_path.most_common(10)),
        "stalls_by_hour": dict(sorted(by_hour.items())),
        "stalls_while_locked": [locked, known],
        "durations": {k: {"n": len(v), "max_s": round(max(v)), "total_s": round(sum(v))}
                      for k, v in durations.items()},
        "open": [{k: r.get(k) for k in ("iso", "kind", "component", "path", "cause", "model") if r.get(k)}
                 for r in opened],
        "hints": hints,
    }


def format_summary(s, since_label):
    out = [f"infra incidents, last {since_label}: {s['records']} records"
           + (f" ({s['first']} .. {s['last']})" if s["records"] else "")]
    if not s["records"]:
        return "\n".join(out)
    out.append("by kind:      " + ", ".join(f"{k} {v}" for k, v in s["by_kind"].items()))
    out.append("by component: " + ", ".join(f"{k} {v}" for k, v in s["by_component"].items()))
    if s["by_path"]:
        out.append("paths:        " + ", ".join(f"{k} x{v}" for k, v in s["by_path"].items()))
    if s["stalls_by_hour"]:
        out.append("stalls/hour:  " + " ".join(f"{h}={n}" for h, n in s["stalls_by_hour"].items()))
    lk, kn = s["stalls_while_locked"]
    if kn:
        out.append(f"screen locked during {lk} of {kn} stalls")
    for k, d in s["durations"].items():
        out.append(f"{k}: {d['n']} timed, longest {d['max_s']}s, total {d['total_s']}s")
    for h in s["hints"]:
        out.append("hint: " + h)
    return "\n".join(out)


TCC_RX = re.compile(r"^(\S+ \S+).*?watchdog expired for approval entry (\d+) \((kTCCService\w+), pid (\d+)\)")


def parse_tcc_lines(text):
    """[(local_time, entry, service, pid)] from `log show` output."""
    out = []
    for ln in text.splitlines():
        m = TCC_RX.search(ln)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3), int(m.group(4))))
    return out


def _proc_name(pid):
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    a = r.stdout.strip()
    return "" if re.search(r"(?i)key|token|secret|passw", a) else a[:160]


def scan_tcc(since_s, record_new=False, now=None):
    now = time.time() if now is None else now
    start = datetime.fromtimestamp(now - since_s).strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = subprocess.run(["log", "show", "--style", "compact", "--start", start, "--predicate",
                            'eventMessage CONTAINS "approval entry"'], capture_output=True, text=True, timeout=180)
        text = r.stdout
    except subprocess.TimeoutExpired:
        return None, "log show did not answer in 180s"
    hits = parse_tcc_lines(text)
    seen = {(x.get("entry"), x.get("pid")) for x in load(now - since_s - 86400)
            if x.get("kind") == "privacy-approval-stall"}
    rows = []
    for t, entry, svc, pid in hits:
        name = _proc_name(pid)
        rows.append({"time": t, "entry": entry, "service": svc.replace("kTCCServiceSystemPolicy", ""),
                     "pid": pid, "process": name or "(gone)"})
        if record_new and (entry, pid) not in seen:
            record("privacy-approval-stall", "tcc", service=svc, entry=entry, target_pid=pid,
                   process=name or None, logged_at=t)
            seen.add((entry, pid))
    return rows, ""


def _coerce(v):
    for f in (int, float):
        try:
            return f(v)
        except ValueError:
            pass
    return {"true": True, "false": False, "null": None}.get(v, v)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="infra_incidents.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("--since", default="24h")
    s.add_argument("--json", action="store_true")
    t = sub.add_parser("tail")
    t.add_argument("-n", type=int, default=20)
    rc = sub.add_parser("record")
    rc.add_argument("kind")
    rc.add_argument("component")
    rc.add_argument("fields", nargs="*", help="key=value")
    sc = sub.add_parser("scan-tcc")
    sc.add_argument("--since", default="2h")
    sc.add_argument("--record", action="store_true", help="log each new approval stall as an incident")
    a = ap.parse_args(argv)
    now = time.time()
    if a.cmd == "summary":
        recs = load(parse_since(a.since, now))
        s = summarize(recs, now)
        print(json.dumps(s, indent=2) if a.json else format_summary(s, a.since))
        return 0
    if a.cmd == "tail":
        for r in load()[-max(a.n, 0):]:
            extra = {k: v for k, v in r.items() if k not in ("ts", "iso", "kind", "component", "host", "pid")}
            print(f"{r['iso']} {r['kind']:<22} {r.get('component', ''):<14} {json.dumps(extra, default=str)}")
        return 0
    if a.cmd == "record":
        fields = {}
        for kv in a.fields:
            if "=" not in kv:
                ap.error(f"field {kv!r} is not key=value")
            k, v = kv.split("=", 1)
            fields[k] = _coerce(v)
        r = record(a.kind, a.component, **fields)
        print(json.dumps(r) if r else "not recorded")
        return 0 if r else 1
    rows, err = scan_tcc(now - parse_since(a.since, now), a.record, now)
    if rows is None:
        print(err, file=sys.stderr)
        return 1
    by = collections.Counter((r["service"], r["process"]) for r in rows)
    print(f"{len(rows)} approval-watchdog expiries in the last {a.since}")
    for (svc, proc), n in by.most_common():
        print(f"{n:5}  {svc:<16} {proc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
