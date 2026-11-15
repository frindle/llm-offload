#!/usr/bin/env python3
"""queue-deadman.py -- external dead-man's switch for the ollama queue (backlog H2).

Detects "queue stalled" by SYMPTOM, from outside the daemon, and rolls each cause cluster up
into ONE line (no per-poll spam):
  stalled:<code>     pending runnable jobs exist, NO job running, lane not intentionally held,
                     continuously for > --stall-min (default 15). <code> = wait reason of the
                     head job (bundle-hold, lane-busy, evaluating, ...) so one cause = one line.
  daemon-heartbeat   queue-wait.json (the daemon rewrites it at least every 60 s) older than
                     --hb-max (default 300 s), missing, or its daemon_pid is dead.
  darkbloom-health   GET <darkbloom>/health fails on 2 consecutive runs (unless bloom-control
                     has deliberately given the GPU to the app).
  state-unreadable   the queue state file does not parse on 2 consecutive runs.
A stall whose head wait reason is infra-darkbloom/host-unavailable while darkbloom-health is
active is folded into that cause (one root cause, one line). A stall on a wait the queue explains and
that ends on its own (darkbloom-sandbox: a sandbox hold; infra-path-access: macOS holds ~/Desktop
while the screen is locked) is flagged only past LONG_HOLD_MIN.

Independence rules (research 2026-10-09 s6): stdlib only; imports nothing from the queue;
READ-ONLY against queue state (no locks, no writes to it); writes only
  ~/.ollama-dispatch/deadman.log        one line when a cause appears / changes / resolves,
                                        plus a reminder at most every --remind-s (6 h)
  ~/.ollama-dispatch/escalations/DEADMAN.md   the currently-active causes, each with an
                                        `expires` timestamp (TTL, refreshed while active; an
                                        empty file when clear)
  ~/.ollama-dispatch/deadman-state.json its own memory (first_seen / last_logged per cause)
Kill switch: ~/.ollama-dispatch/deadman.disabled (exists => exit 0, touch nothing).
Never prints or reads any API key. Exit 0 always except usage errors (a watchdog must not fail loud).
Usage: queue-deadman.py [--dd DIR] [--queue-state FILE] [--now EPOCH] [--stall-min N]
                        [--hb-max S] [--ttl-s S] [--remind-s S] [--no-health] [--health-url URL]
"""
import argparse, json, os, sys, time, urllib.request
from pathlib import Path
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config  # noqa: E402  (the queue state wherever the queue runs: load_queue_state)

HOME = Path.home()
LOG_KEEP_LINES = 400
NOT_RUNNABLE_CODES = {"needs-action"}                    # a human must act first: not "runnable"
INFRA_CODES = {"infra-darkbloom", "host-unavailable"}
# Waits the queue explains and that END ON THEIR OWN (2026-11-02): Darkbloom lent the machine to a
# sandbox job (hours are normal), and the path-access gate holding jobs while macOS holds ~/Desktop
# for the locked screen (overnight is normal). A stall on one of these is flagged only past this many
# minutes, not --stall-min.
LONG_HOLD_MIN = {"darkbloom-sandbox": 6 * 60.0, "infra-path-access": 12 * 60.0}
BLOOM_HOLD_PHASES = {"switching", "releasing", "release-pending"}


def jload(p):
    try:
        v = json.loads(Path(p).read_text())
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def atomic_write(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def health_url(dd_home):
    d = jload(Path(dd_home) / ".darkbloom" / "local.json") or {}      # base_url only; the key is never read
    base = str(os.environ.get("DARKBLOOM_BASE_URL") or d.get("base_url") or "http://127.0.0.1:8000/v1").rstrip("/")
    return (base[:-3] if base.endswith("/v1") else base) + "/health"


def probe_health(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
            return r.status == 200 and str(body.get("status", "ok")).lower() == "ok"
    except Exception:
        return False


def snapshot(a):
    dd = Path(a.dd)
    return {"state": queue_config.load_queue_state(a.queue_state), "wait": jload(dd / "queue-wait.json"),
            "bloom": jload(dd / "bloom-control-state.json"),
            "health_ok": None if a.no_health else probe_health(a.health_url or health_url(Path(a.home)))}


def evaluate(snap, mem, now, a):
    """PURE except pid_alive. Returns {cause_key: detail_text}; updates `mem` counters/timers."""
    causes, jobs = {}, None
    st = snap["state"]
    mem["bad_state"] = 0 if st else mem.get("bad_state", 0) + 1
    if not st and mem["bad_state"] >= 2:
        causes["state-unreadable"] = "queue state file does not parse (2 consecutive runs)"
    if st:
        jobs = st.get("jobs") or []
    w = snap["wait"] or {}
    hb = float(w.get("updated_at") or 0)
    if not w or now - hb > a.hb_max or (w.get("daemon_pid") and not pid_alive(w["daemon_pid"])):
        why = ("queue-wait.json missing" if not w else
               f"daemon pid {w.get('daemon_pid')} dead" if w.get("daemon_pid") and not pid_alive(w["daemon_pid"])
               else f"heartbeat {int(now - hb)}s old (> {int(a.hb_max)}s)")
        causes["daemon-heartbeat"] = f"queue daemon silent: {why}"
    bloom = snap["bloom"] or {}
    bloom_hold = (bloom.get("mode") == "bloom" or (bloom.get("phase") in BLOOM_HOLD_PHASES
                  and now - float(bloom.get("heartbeat") or 0) <= 300))
    if snap["health_ok"] is False and not bloom_hold:
        mem["bad_health"] = mem.get("bad_health", 0) + 1
        if mem["bad_health"] >= 2:
            causes["darkbloom-health"] = f"Darkbloom /health failing for {mem['bad_health']} consecutive runs"
    else:
        mem["bad_health"] = 0
    # symptom: runnable work, nothing running, lane not intentionally held
    stall = None
    if jobs is not None and not bloom_hold:
        running = [j for j in jobs if j.get("status") == "running"]
        wj = (w.get("jobs") or {})
        runnable = [j for j in jobs if j.get("status") == "pending" and not j.get("user_hold")
                    and not j.get("fit_hold") and (wj.get(j.get("id")) or {}).get("code") not in NOT_RUNNABLE_CODES]
        if runnable and not running:
            head = runnable[0]
            codes = [(wj.get(j.get("id")) or {}).get("code") or "evaluating" for j in runnable]
            code = (wj.get(head.get("id")) or {}).get("code") or "evaluating"
            if "darkbloom-health" in causes and code in INFRA_CODES:
                code = "darkbloom-health"
            short = (wj.get(head.get("id")) or {}).get("short") or ""
            bundles = sorted({str(j.get("bundle")) for j in runnable if j.get("bundle")})
            stall = (code, f"{len(runnable)} runnable pending, 0 running; head {head.get('id')} "
                           f"({short[:90]}); bundles {','.join(bundles[:4]) or '-'}")
    if stall and stall[0] != "darkbloom-health":
        s = mem.setdefault("stall", {})
        if s.get("code") != stall[0]:
            s.clear()
            s.update(code=stall[0], since=now)
        if now - s["since"] > LONG_HOLD_MIN.get(stall[0], a.stall_min) * 60:
            causes[f"stalled:{stall[0]}"] = f"{stall[1]}; no job running for {int((now - s['since']) // 60)} min"
    elif not stall:
        mem.pop("stall", None)
    return causes


def apply(causes, mem, now, a):
    """Roll causes into log lines (only on appear/change/resolve/remind) and the DEADMAN.md body."""
    known, lines = mem.setdefault("causes", {}), []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    for k, detail in sorted(causes.items()):
        c = known.get(k)
        if c is None:
            known[k] = c = {"first": now, "logged": 0, "detail": ""}
            lines.append(f"{ts} DEADMAN {k}: {detail}")
        elif now - c["logged"] >= a.remind_s:
            lines.append(f"{ts} DEADMAN {k} (still, since {time.strftime('%H:%M', time.localtime(c['first']))}): {detail}")
        else:
            c["detail"] = detail
            continue
        c["logged"], c["detail"] = now, detail
    for k in [k for k in known if k not in causes]:
        lines.append(f"{ts} DEADMAN {k}: RESOLVED after {int((now - known.pop(k)['first']) // 60)} min")
    exp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now + a.ttl_s))
    body = ["# DEADMAN (auto-expiring; rewritten each run, ignore entries past `expires`)", ""]
    body += [f"- `{k}` since {time.strftime('%H:%M', time.localtime(known[k]['first']))} expires {exp} -- {d}"
             for k, d in sorted(causes.items())] or ["- clear"]
    return lines, "\n".join(body) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=str(HOME))
    ap.add_argument("--dd", default=None, help="dispatch dir (default <home>/.ollama-dispatch)")
    ap.add_argument("--queue-state", default=None)
    ap.add_argument("--now", type=float, default=None)
    ap.add_argument("--stall-min", type=float, default=15.0)
    ap.add_argument("--hb-max", type=float, default=300.0)
    ap.add_argument("--ttl-s", type=float, default=3600.0)
    ap.add_argument("--remind-s", type=float, default=6 * 3600.0)
    ap.add_argument("--no-health", action="store_true")
    ap.add_argument("--health-url", default=None)
    a = ap.parse_args(argv)
    a.dd = a.dd or str(Path(a.home) / ".ollama-dispatch")
    a.queue_state = a.queue_state or str(Path(a.home) / "bin" / "ollama-queue-state.json")
    dd = Path(a.dd)
    if (dd / "deadman.disabled").exists():
        return 0
    try:
        now = a.now if a.now is not None else time.time()
        mem = jload(dd / "deadman-state.json") or {}
        causes = evaluate(snapshot(a), mem, now, a)
        lines, md = apply(causes, mem, now, a)
        if lines:
            lp = dd / "deadman.log"
            old = lp.read_text().splitlines() if lp.exists() else []
            atomic_write(lp, "\n".join((old + lines)[-LOG_KEEP_LINES:]) + "\n")
            for ln in lines:
                print(ln)
        atomic_write(dd / "escalations" / "DEADMAN.md", md)
        atomic_write(dd / "deadman-state.json", json.dumps(mem, indent=1))
    except Exception as e:                       # a watchdog never fails loud into its scheduler
        print(f"deadman internal error (ignored): {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
