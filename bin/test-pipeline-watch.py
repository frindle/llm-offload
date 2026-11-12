#!/usr/bin/env python3
"""Unattended-readiness (5): ONE long-lived needs-eyes watcher (pipeline-watch.py)
replaces the per-event Monitor loops.

Proves, against temp fixtures (never the live queue): the first pass is a silent
baseline; then ONE process reports, in one stream, a job leaving the live set, a
job row that vanished (resolved through its <id>.done.json), a slice escalating in
ANY plan, a new unchecked ESCALATIONS.md row, and a watched file changing -- with
needs-eyes events tagged; --filter narrows it; a torn queue file does not kill or
blank it. WATCH_SRC=<path> for the revert-test (absent file -> FAIL).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
SRC = Path(os.environ.get("WATCH_SRC") or BIN / "pipeline-watch.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    chk("pipeline-watch.py exists", SRC.is_file(), True)
    if not SRC.is_file():
        print(f"\n{len(FAILS)} FAIL")
        return 1
    d = Path(tempfile.mkdtemp(prefix="pw-test-"))
    qs, runs, esc, logs = d / "q.json", d / "runs", d / "esc", d / "logs"
    for p in (runs, esc, logs):
        p.mkdir()
    cfg = d / "local.json"
    cfg.write_text("{}")
    state = d / "snap.json"

    def setq(rows):
        qs.write_text(json.dumps({"jobs": [{"id": i, "label": l, "status": s} for i, l, s in rows]}))

    def setplan(label, **sl):
        (runs / f"{label}.json").write_text(json.dumps(
            {"label": label, "slices": {k: {"status": v, "escalation_reason":
                                            "verify red" if v == "escalated" else ""}
                                        for k, v in sl.items()}}))

    def once(*extra):
        r = subprocess.run([sys.executable, str(SRC), "--once", "--state", str(state),
                            "--queue-state", str(qs), "--runs-dir", str(runs),
                            "--esc-dir", str(esc), "--log-dir", str(logs),
                            "--watch-file", str(cfg), *extra],
                           capture_output=True, text=True, timeout=60)
        return r.returncode, [l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.strip()]

    setq([("a1", "chat-fixes-s6c", "running"), ("a2", "chat-fixes-s7", "pending"),
          ("b1", "other-job", "running")])
    setplan("chat-fixes", s6c="enqueued", s7="pending")
    setplan("rivian", s1="enqueued")
    (esc / "ESCALATIONS.md").write_text("- [x] old handled\n- [ ] old open row\n")
    rc, out = once()
    chk("first pass is a silent baseline (no history replay)", (rc, out), (0, []))

    setq([("a1", "chat-fixes-s6c", "needs_opus"), ("a2", "chat-fixes-s7", "running"),
          ("b1", "other-job", "running")])
    rc, out = once()
    chk("job entering needs_opus -> one NEEDS EYES line",
        out, ["NEEDS EYES JOB chat-fixes-s6c [a1] running -> needs_opus"])

    setq([("a1", "chat-fixes-s6c", "needs_opus")])          # a2 and b1 vanished
    (logs / "a2.done.json").write_text(json.dumps({"status": "done"}))
    rc, out = once()
    chk("vanished rows resolve through their done.json (or say so)",
        sorted(out), ["JOB chat-fixes-s7 [a2] running -> done",
                      "NEEDS EYES JOB other-job [b1] running -> gone (no done.json)"])

    setplan("rivian", s1="escalated")
    setplan("chat-fixes", s6c="done", s7="pending")
    rc, out = once()
    chk("slice transitions across plans, escalation tagged with its reason",
        sorted(out), ["NEEDS EYES SLICE rivian s1 enqueued -> escalated -- verify red",
                      "SLICE chat-fixes s6c enqueued -> done"])

    (esc / "ESCALATIONS.md").write_text("- [x] old handled\n- [ ] old open row\n"
                                        "- [ ] `rivian` **s1** new escalation\n")
    time.sleep(0.01)
    cfg.write_text('{"api_key": "x", "base_url": "y"}')
    rc, out = once()
    chk("new unchecked escalation row + watched file change",
        sorted(o.split(" (")[0] for o in out),
        sorted(["NEEDS EYES ESCALATIONS.md: `rivian` **s1** new escalation",
                f"FILE changed: {cfg}"]))

    setq([("a1", "chat-fixes-s6c", "failed"), ("c1", "rivian-s2", "failed")])
    rc, out = once("--filter", "^chat-fixes")
    chk("--filter narrows the stream", out,
        ["NEEDS EYES JOB chat-fixes-s6c [a1] needs_opus -> failed"])

    # long-lived mode: one process, survives a torn queue file, keeps reporting
    p = subprocess.Popen([sys.executable, "-u", str(SRC), "--interval", "1",
                          "--queue-state", str(qs), "--runs-dir", str(runs),
                          "--esc-dir", str(esc), "--log-dir", str(logs)],
                         stdout=subprocess.PIPE, text=True)
    try:
        first = p.stdout.readline()
        chk("long-lived: prints one 'armed' line", "pipeline watch armed" in first, True)
        qs.write_text("{torn")
        time.sleep(1.5)
        setplan("rivian", s1="failed")
        lines, t0 = [], time.time()
        while time.time() - t0 < 8:
            ln = p.stdout.readline()
            if ln:
                lines.append(ln.strip())
                if "SLICE rivian s1" in ln:
                    break
        chk("long-lived: still alive and reporting after a torn queue file",
            (p.poll() is None, any("SLICE rivian s1 escalated -> failed" in l for l in lines)),
            (True, True))
        chk("...and the torn file did not blank the job view (no spurious 'gone' lines)",
            [l for l in lines if "gone" in l], [])
    finally:
        p.kill()
        p.wait()
    subprocess.run(["rm", "-rf", str(d)])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
