#!/usr/bin/env python3
"""dispatch-stall-detect.py -- nothing hangs silently (the owner 2026-10-02, mandate item 3).

"Any bundle/slice with no state change for >30 min while the lane is idle gets
auto-diagnosed and acted on; if it truly needs a human, ONE notification naming what
is stuck and the exact qctl command."

The queue already parks a bundle that is blocked or stalled (BUNDLE_IDLE_CEILING) and
alerts once at park time -- and then nothing ever acts on it again. This pass closes
that gap. Run every watcher pass (dispatch-escalation-watcher.py --once calls it).

A plan is STALLED when ALL hold:
  * it has open slices (not done/skipped/superseded/dropped) and is not cancelled;
  * its run state has not changed for >= STALL_S (30 min);
  * none of its jobs is live in the queue, and no advance driver holds its lock;
  * the lane is idle: no job is running anywhere in the queue.

ONE action per stale state (the run-state mtime identifies it), then ONE alert:
  * a ROOT slice (dependencies satisfied) that is ESCALATED -> dispatch-self-heal
    (the verdict ladder; its final rung alerts by itself);
  * a root slice that is pending / failed / enqueued -> fire the slicer's advance
    (`--execute`, which takes the per-plan lock; a failed slice auto-retries there,
    within the slicer's own budget);
  * a plan idle for more than STALE_HORIZON_S (a day) is NOT relaunched -- someone
    may have abandoned it -- it is reported instead;
  * nothing applicable, or the action left the state unchanged for another
    STALL_S -> ONE notify-owner alert with the exact qctl commands.
Every decision goes to ~/.ollama-dispatch/decisions.jsonl (shown by `qctl status`).
State: ~/.ollama-dispatch/stall-detect.json. Dry run: --dry-run. Tests: --self-test.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
DISPATCH = Path(os.environ.get("STALL_DISPATCH") or Path.home() / ".ollama-dispatch")
SLICE_RUNS = DISPATCH / "slice-runs"
ESC_DIR = DISPATCH / "escalations"
LEDGER = DISPATCH / "stall-detect.json"
DECISIONS = DISPATCH / "decisions.jsonl"
QSTATE = Path(os.environ.get("STALL_QUEUE_STATE") or BIN / "ollama-queue-state.json")
SELF_HEAL = BIN / "dispatch-self-heal.py"

STALL_S = 30 * 60
STALE_HORIZON_S = 24 * 3600
DONE = {"done", "skipped", "superseded", "dropped"}
KICKABLE = {"pending", "failed", "enqueued", "confirmed", "blocked_retry"}
LIVE_JOB = {"running", "pending", "held", "paused", "queued", "scheduled", "launching", "planned"}
NOTIFY_DEDUPE_S = 7 * 24 * 3600


# ----------------------------------------------------------------------------- pure
def root_slices(st):
    """PURE. Open slices whose dependencies are all satisfied, in plan order."""
    sl = st.get("slices") or {}
    order = st.get("order") or list(sl)
    out = []
    for sid in order:
        s = sl.get(sid) or {}
        if s.get("status") in DONE:
            continue
        deps = s.get("depends_on") or []
        if all((sl.get(d) or {}).get("status") in DONE for d in deps):
            out.append(sid)
    return out


def is_stalled(st, mtime, now, plan_jobs_live, driver_live, lane_busy, cancelled):
    """PURE. (stalled?, why-not)."""
    sl = st.get("slices") or {}
    if not any((s or {}).get("status") not in DONE for s in sl.values()):
        return False, "converged"
    if cancelled:
        return False, "cancelled"
    if now - mtime < STALL_S:
        return False, "changed %d min ago" % ((now - mtime) // 60)
    if plan_jobs_live:
        return False, "a job of it is live"
    if driver_live:
        return False, "an advance driver is running"
    if lane_busy:
        return False, "the lane is busy"
    return True, ""


def decide(st, mtime, now, entry, final_rung_slices=()):
    """PURE. What to do with a STALLED plan:
    ('wait', why) | ('heal', sid) | ('kick', sid) | ('notify', why) | ('noted', why)."""
    if entry and entry.get("mtime") == mtime:
        if entry.get("notified"):
            return ("wait", "already alerted for this state")
        if now - float(entry.get("acted_at") or 0) >= STALL_S:
            return ("notify", "%s %d min ago and nothing moved since"
                    % (entry.get("action") or "acted", (now - float(entry["acted_at"])) // 60))
        return ("wait", "acted %d min ago -- giving it time" % ((now - float(entry["acted_at"])) // 60))
    sl = st.get("slices") or {}
    roots = root_slices(st)
    desc = ", ".join("%s=%s" % (r, (sl.get(r) or {}).get("status")) for r in roots) or "none"
    if now - mtime >= STALE_HORIZON_S:
        return ("notify", "idle %dh (roots: %s) -- too old to relaunch on my own"
                % ((now - mtime) // 3600, desc))
    esc = [r for r in roots if (sl.get(r) or {}).get("status") == "escalated"]
    for sid in esc:
        if sid in final_rung_slices:
            return ("noted", "%s already parked on the self-heal final rung (alerted)" % sid)
        return ("heal", sid)
    kick = [r for r in roots if (sl.get(r) or {}).get("status") in KICKABLE]
    if kick:
        return ("kick", kick[0])
    return ("notify", "no root slice the pipeline can act on (roots: %s)" % desc)


def alert_text(plan, why, sid=None):
    """PURE. The single human alert: what is stuck + the exact qctl commands."""
    s = sid or "<slice>"
    return ("%s is stuck: %s. Look: qctl status %s . Then one of: qctl retry %s %s | "
            "qctl skip %s %s --reason '...'" % (plan, why, plan, plan, s, plan, s))


# ----------------------------------------------------------------------------- io
def _load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(p)


def log_decision(plan, sid, action, outcome, detail, path=None):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": "stall-detect",
           "action": action, "bundle": plan, "slice": sid, "job": None, "outcome": outcome,
           "detail": (detail or "")[:400]}
    try:
        pp = Path(path or DECISIONS)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _mod(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def driver_live(label):
    try:
        m = re.search(r"\d+", (SLICE_RUNS / f"{label}.advance.lock").read_text())
    except OSError:
        return False
    if not m:
        return False
    try:
        os.kill(int(m.group(0)), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def plan_jobs_live(label, jobs):
    for j in jobs:
        if j.get("status") not in LIVE_JOB:
            continue
        if j.get("bundle") == label or label in str(j.get("label") or ""):
            return True
    return False


def final_rung_slices(label, heal_ledger):
    out = set()
    for k, rec in (heal_ledger or {}).items():
        log = (rec or {}).get("log") or []
        if log and log[-1].get("action") == "final-rung" and k.startswith(label + "/"):
            out.add(k[len(label) + 1:])
    return out


def latest_review(label, sid):
    revs = sorted(ESC_DIR.glob(f"*-{label}-{sid}.review.md"))
    if not revs:
        return None, None
    rv = revs[-1]
    ctx = rv.with_name(rv.name[: -len(".review.md")] + ".md")
    return ctx, rv


def run_pass(now=None, dry=False, notifier=None, launcher=None, healer=None, cancelled=None):
    now = now or time.time()
    q = _load(QSTATE, None)
    if not isinstance(q, dict):
        print("# stall-detect: queue state unreadable -- doing nothing")
        return []
    jobs = q.get("jobs") or []
    lane_busy = any(j.get("status") == "running" for j in jobs)
    led = _load(LEDGER, {})
    heal_led = _load(ESC_DIR / "self-heal.json", {})
    if cancelled is None:
        try:
            pc = _mod("plan_cancel", BIN / "plan_cancel.py")
            cancelled = lambda lbl: bool(pc.cancelled(lbl, runs_dir=SLICE_RUNS))
        except Exception:
            cancelled = lambda lbl: False
    if notifier is None:
        notifier = _mod("notify_owner", BIN / "notify-owner.py").notify
    results = []
    for p in sorted(SLICE_RUNS.glob("*.json")):
        st = _load(p, None)
        if not isinstance(st, dict) or "slices" not in st:
            continue
        label = st.get("label") or p.stem
        mtime = p.stat().st_mtime
        stalled, why_not = is_stalled(st, mtime, now, plan_jobs_live(label, jobs),
                                      driver_live(label), lane_busy, cancelled(label))
        if not stalled:
            if label in led and led[label].get("mtime") != mtime:
                led.pop(label, None)          # it moved: a later stall starts fresh
            continue
        entry = led.get(label)
        act, arg = decide(st, mtime, now, entry, final_rung_slices(label, heal_led))
        results.append((label, act, arg))
        print(f"# stall-detect {label}: {act} -- {arg}")
        if dry or act == "wait":
            continue
        if act == "noted":
            led[label] = {"mtime": mtime, "acted_at": now, "action": "noted", "notified": True}
            log_decision(label, None, "stall", "already-alerted", arg)
            continue
        if act == "heal":
            ctx, rv = latest_review(label, arg)
            if not rv:
                act, arg = "notify", f"{arg} is escalated but was never reviewed"
            else:
                out = (healer or _heal)(label, arg, ctx, rv)
                led[label] = {"mtime": mtime, "acted_at": now, "action": f"self-heal {arg} -> {out}"}
                log_decision(label, arg, "stall:self-heal", out, f"stalled {int((now-mtime)//60)} min")
                if out.startswith("park:"):
                    led[label]["notified"] = True        # the final rung alerted
                elif out.startswith(("skip:", "error")):
                    act, arg = "notify", f"{arg} is escalated and self-heal could not act ({out})"
                if act == "heal":
                    continue
        if act == "kick":
            (launcher or _kick)(st, label)
            led[label] = {"mtime": mtime, "acted_at": now, "action": f"advance fired for {arg}"}
            log_decision(label, arg, "stall:kick", "advance-fired",
                         f"stalled {int((now-mtime)//60)} min, lane idle, no driver")
            continue
        if act == "notify":
            sid = arg.split(" ", 1)[0] if arg and arg.split(" ", 1)[0] in (st.get("slices") or {}) else (
                (root_slices(st) or [None])[0])
            msg = alert_text(label, arg, sid)
            try:
                notifier("dispatch stuck: " + label, msg, dedupe_key=f"stall:{label}:{int(mtime)}",
                         dedupe_s=NOTIFY_DEDUPE_S)
            except Exception as e:
                print(f"# stall-detect: notify failed: {e}")
            led[label] = {**(led.get(label) or {}), "mtime": mtime, "acted_at": now,
                          "notified": True, "action": (led.get(label) or {}).get("action") or "notify"}
            log_decision(label, sid, "stall:notify", "parked-for-human", arg)
    if not dry:
        _save(LEDGER, led)
    return results


def _heal(label, sid, ctx, rv):
    try:
        r = subprocess.run([sys.executable, str(SELF_HEAL), "--plan-label", label, "--slice", sid,
                            "--context", str(ctx), "--review", str(rv)],
                           capture_output=True, text=True, timeout=2400)
        out = [l for l in (r.stdout or "").splitlines() if l.strip()]
        return out[-1].strip() if out else f"error: rc={r.returncode}"
    except Exception as e:
        return f"error: {e}"


def _kick(st, label):
    log = SLICE_RUNS / f"{label}.advance.log"
    with open(log, "a") as lf:
        lf.write("\n===== stall-detect advance %s =====\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        lf.flush()
        lf.write("old slicer retired -- not advancing (use mini-swe)\n")


# ----------------------------------------------------------------------------- tests
def self_test():
    import tempfile
    fails = []

    def check(n, got, want):
        ok = got == want
        print(("  PASS " if ok else "  FAIL ") + n + ("" if ok else f" -- got {got!r}"))
        if not ok:
            fails.append(n)

    now = 1_000_000.0
    st = {"order": ["s1", "s2", "s3"], "slices": {
        "s1": {"status": "done"}, "s2": {"status": "escalated", "depends_on": ["s1"]},
        "s3": {"status": "pending", "depends_on": ["s2"]}}}
    check("roots skip slices blocked on an open dependency", root_slices(st), ["s2"])
    old = now - STALL_S - 1
    check("stalled when idle 30+ min, nothing live", is_stalled(st, old, now, False, False, False, False)[0], True)
    check("not stalled under 30 min", is_stalled(st, now - 60, now, False, False, False, False)[0], False)
    check("not stalled with a live job", is_stalled(st, old, now, True, False, False, False)[0], False)
    check("not stalled with a live driver", is_stalled(st, old, now, False, True, False, False)[0], False)
    check("not stalled while the lane is busy", is_stalled(st, old, now, False, False, True, False)[0], False)
    check("not stalled when cancelled", is_stalled(st, old, now, False, False, False, True)[0], False)
    done = {"slices": {"s1": {"status": "done"}}}
    check("a converged plan is never stalled", is_stalled(done, old, now, False, False, False, False)[0], False)
    check("escalated root -> self-heal", decide(st, old, now, None), ("heal", "s2"))
    check("final-rung root -> already alerted, no 2nd alert",
          decide(st, old, now, None, {"s2"})[0], "noted")
    st2 = {"order": ["a"], "slices": {"a": {"status": "failed"}}}
    check("failed root -> fire the advance", decide(st2, old, now, None), ("kick", "a"))
    check("an old plan is reported, never relaunched",
          decide(st2, now - STALE_HORIZON_S - 5, now, None)[0], "notify")
    e = {"mtime": old, "acted_at": now - 60, "action": "kick"}
    check("acted on this state recently -> wait", decide(st2, old, now, e)[0], "wait")
    e = {"mtime": old, "acted_at": now - STALL_S - 1, "action": "kick"}
    check("acted and nothing moved for 30 min -> notify", decide(st2, old, now, e)[0], "notify")
    e["notified"] = True
    check("already notified for this state -> wait (ONE alert)", decide(st2, old, now, e)[0], "wait")
    st3 = {"order": ["b"], "slices": {"b": {"status": "parked_weird"}}}
    check("no actionable root -> notify", decide(st3, old, now, None)[0], "notify")
    check("the alert names the exact qctl commands",
          "qctl retry p s2" in alert_text("p", "x", "s2") and "qctl status p" in alert_text("p", "x", "s2"), True)

    # end to end on a fixture dir (no real notifier / launcher / healer)
    global SLICE_RUNS, ESC_DIR, LEDGER, DECISIONS, QSTATE
    saved = (SLICE_RUNS, ESC_DIR, LEDGER, DECISIONS, QSTATE)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        SLICE_RUNS, ESC_DIR = td / "runs", td / "esc"
        LEDGER, DECISIONS, QSTATE = td / "led.json", td / "dec.jsonl", td / "q.json"
        SLICE_RUNS.mkdir()
        ESC_DIR.mkdir()
        QSTATE.write_text(json.dumps({"jobs": []}))
        pf = SLICE_RUNS / "p.json"
        pf.write_text(json.dumps({"label": "p", "plan_path": "/x", **st2}))
        t0 = time.time()
        os.utime(pf, (t0 - STALL_S - 10, t0 - STALL_S - 10))
        alerts, kicks = [], []
        kw = dict(notifier=lambda t, m, **k: alerts.append(m), launcher=lambda s, l: kicks.append(l),
                  healer=lambda *a: "retry", cancelled=lambda l: False)
        run_pass(now=t0, **kw)
        check("e2e: a stalled failed slice gets its advance fired", (kicks, alerts), (["p"], []))
        run_pass(now=t0 + 60, **kw)
        check("e2e: no second action on the same state", (len(kicks), len(alerts)), (1, 0))
        run_pass(now=t0 + STALL_S + 5, **kw)
        check("e2e: still stuck 30 min later -> ONE alert", (len(kicks), len(alerts)), (1, 1))
        run_pass(now=t0 + 2 * STALL_S, **kw)
        check("e2e: and never a second one", len(alerts), 1)
        dec = [json.loads(l)["action"] for l in DECISIONS.read_text().splitlines()]
        check("e2e: decisions logged", dec, ["stall:kick", "stall:notify"])
        QSTATE.write_text(json.dumps({"jobs": [{"id": "z", "status": "running", "label": "other"}]}))
        LEDGER.unlink()
        kicks.clear()
        run_pass(now=t0, **kw)
        check("e2e: nothing happens while the lane is busy", kicks, [])
    SLICE_RUNS, ESC_DIR, LEDGER, DECISIONS, QSTATE = saved
    print("SELF_TEST_OK" if not fails else f"SELF_TEST_FAILED: {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    run_pass(dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
