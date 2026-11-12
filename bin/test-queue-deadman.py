#!/usr/bin/env python3
"""Tests for queue-deadman.py (backlog H2): symptom detection, de-duped rolled-up output, TTL file,
kill switch, read-only, independence from the key file. Runs the CLI against a temp HOME.
Usage: python3 test-queue-deadman.py [--tool PATH]      (revert-prove: --tool <mutated copy>)"""
import argparse, http.server, json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--tool", default=str(Path(__file__).resolve().parent / "queue-deadman.py"))
A = ap.parse_args()
H = Path(tempfile.mkdtemp(prefix="deadman-"))
DD = H / ".ollama-dispatch"
QS = H / "bin" / "ollama-queue-state.json"
(DD / "escalations").mkdir(parents=True)
QS.parent.mkdir(parents=True)
fails = 0
T0 = 1_800_000_000.0


def chk(name, ok, extra=""):
    global fails
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  {extra}"))
    fails += 0 if ok else 1


def setup(jobs, wait_jobs=None, hb=T0, pid=None, bloom=None, wait=True):
    QS.write_text(json.dumps({"jobs": jobs}))
    if wait:
        (DD / "queue-wait.json").write_text(json.dumps({"updated_at": hb, "daemon_pid": pid or os.getpid(),
                                                        "jobs": wait_jobs or {}}))
    elif (DD / "queue-wait.json").exists():
        (DD / "queue-wait.json").unlink()
    bp = DD / "bloom-control-state.json"
    bp.write_text(json.dumps(bloom)) if bloom else (bp.unlink() if bp.exists() else None)


def run(now, *extra):
    p = subprocess.run([sys.executable, A.tool, "--home", str(H), "--now", str(now), "--no-health", *extra],
                       capture_output=True, text=True, timeout=60)
    chk(f"exit 0 at t+{int(now - T0)}", p.returncode == 0, p.stderr[-200:])
    return p.stdout


def log():
    p = DD / "deadman.log"
    return p.read_text().splitlines() if p.exists() else []


def md():
    return (DD / "escalations" / "DEADMAN.md").read_text()


PEND = [{"id": "p1", "status": "pending", "bundle": "B1"}, {"id": "p2", "status": "pending", "bundle": "B1"}]
WJ = {"p1": {"code": "bundle-hold", "short": "behind bundle B0"}}

# 1. healthy: something running -> silent, clear
setup(PEND + [{"id": "r", "status": "running"}], WJ, hb=T0)
run(T0)
chk("running job => no log, DEADMAN.md clear", log() == [] and "- clear" in md())

# 2. stall timer: no cause until > 15 min, then ONE line, de-duped
setup(PEND, WJ, hb=T0)
run(T0 + 1)
setup(PEND, WJ, hb=T0 + 600)
run(T0 + 600)
chk("10 min of stall => still silent", log() == [])
setup(PEND, WJ, hb=T0 + 1000)
run(T0 + 1000)
chk("16+ min => one stalled:bundle-hold line", len(log()) == 1 and "stalled:bundle-hold" in log()[0], str(log()))
chk("line names count/head/bundle", "2 runnable pending" in log()[0] and "p1" in log()[0] and "B1" in log()[0])
for t in (1060, 1120, 1180):
    setup(PEND, WJ, hb=T0 + t)
    run(T0 + t)
chk("repeated polls do not spam the log", len(log()) == 1, str(log()))
chk("DEADMAN.md lists the cause with an expires TTL", "stalled:bundle-hold" in md() and "expires" in md())

# 3. resolve once
setup(PEND + [{"id": "r", "status": "running"}], WJ, hb=T0 + 1240)
run(T0 + 1240)
chk("RESOLVED logged once, md clear", len(log()) == 2 and "RESOLVED" in log()[1] and "- clear" in md(), str(log()))
run(T0 + 1250)
chk("no second RESOLVED", len(log()) == 2)

# 4. intentional holds are not stalls
for name, jobs, wj, bloom in (
        ("user_hold rows", [dict(p, user_hold=True) for p in PEND], WJ, None),
        ("needs-action rows", PEND, {"p1": {"code": "needs-action"}, "p2": {"code": "needs-action"}}, None),
        ("bloom app owns the GPU", PEND, WJ, {"mode": "bloom", "phase": "idle"}),
        ("bloom switching (fresh)", PEND, WJ, {"mode": "queue", "phase": "switching", "heartbeat": T0 + 99_000})):
    base = 99_000
    setup(jobs, wj, hb=T0 + base, bloom=bloom)
    run(T0 + base)
    setup(jobs, wj, hb=T0 + base + 3000, bloom=bloom)
    run(T0 + base + 3000)
    chk(f"intentional hold ({name}) => no stall", log()[2:] == [], str(log()[2:]))
    base += 10_000

# 5. a stall timer resets when work runs again
setup(PEND, WJ, hb=T0 + 200_000)
run(T0 + 200_000)
setup(PEND + [{"id": "r", "status": "running"}], WJ, hb=T0 + 200_600)
run(T0 + 200_600)
setup(PEND, WJ, hb=T0 + 201_200)
run(T0 + 201_200)
n = len(log())
setup(PEND, WJ, hb=T0 + 201_900)
run(T0 + 201_900)
chk("timer restarted after running: 12 min since => silent", len(log()) == n, str(log()[n:]))

# 6. daemon heartbeat: stale / dead pid / missing
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
n = len(log())
setup([], hb=T0 + 300_000 - 400)
run(T0 + 300_000)
chk("stale heartbeat => daemon-heartbeat", any("daemon-heartbeat" in l and "400s" in l for l in log()[n:]), str(log()[n:]))
setup([], hb=T0 + 305_000)
run(T0 + 305_000)
setup([], hb=T0 + 310_000, pid=dead.pid)
run(T0 + 310_000)
chk("dead daemon pid => daemon-heartbeat detail names the pid", any(f"pid {dead.pid} dead" in l for l in log()[n:]), str(log()[n:]))
setup([], wait=False)
run(T0 + 320_000)
chk("missing queue-wait.json => daemon-heartbeat", "daemon-heartbeat" in md() and "missing" in md(), md())
setup([], hb=T0 + 330_000)
run(T0 + 330_000)
chk("fresh heartbeat resolves it", "- clear" in md() and any("daemon-heartbeat: RESOLVED" in l for l in log()))

# 7. Darkbloom health: needs 2 consecutive failures; clusters the stall under it
class H200(http.server.BaseHTTPRequestHandler):
    code = 200

    def do_GET(self):
        self.send_response(H200.code)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H200)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/health"


def run_h(now, hurl):
    p = subprocess.run([sys.executable, A.tool, "--home", str(H), "--now", str(now), "--health-url", hurl],
                       capture_output=True, text=True, timeout=60)
    chk(f"exit 0 (health run t+{int(now - T0)})", p.returncode == 0, p.stderr[-200:])


n = len(log())
H200.code = 500
setup([], hb=T0 + 400_000)
run_h(T0 + 400_000, url)
chk("one health failure => silent", len(log()) == n)
setup([], hb=T0 + 400_060)
run_h(T0 + 400_060, url)
chk("two consecutive => darkbloom-health line", any("darkbloom-health" in l for l in log()[n:]), str(log()[n:]))
H200.code = 200
setup([], hb=T0 + 400_120)
run_h(T0 + 400_120, url)
chk("healthy again => RESOLVED", any("darkbloom-health: RESOLVED" in l for l in log()[n:]))
# cluster: infra-darkbloom stall + health down = ONE line
n = len(log())
infra_wj = {"p1": {"code": "infra-darkbloom", "short": "darkbloom down"}}
H200.code = 503
for t in (500_000, 500_060, 501_200, 501_260):
    setup(PEND, infra_wj, hb=T0 + t)
    run_h(T0 + t, url)
new = log()[n:]
chk("infra stall + health failure roll up into one darkbloom-health line", len(new) == 1 and "darkbloom-health" in new[0], str(new))
srv.shutdown()

# 8. state unreadable (2 consecutive)
setup([], hb=T0 + 599_000)
run(T0 + 599_000)          # settles the cluster test's still-open darkbloom-health
n = len(log())
QS.write_text("{not json")
(DD / "queue-wait.json").write_text(json.dumps({"updated_at": T0 + 600_000, "daemon_pid": os.getpid()}))
run(T0 + 600_000)
chk("one unreadable run => silent", len(log()) == n, str(log()[n:]))
run(T0 + 600_060)
chk("two => state-unreadable", any("state-unreadable" in l for l in log()[n:]))

# 9. reminder after remind-s, once
n = len(log())
setup(PEND, WJ, hb=T0 + 700_000)
run(T0 + 700_000)
setup(PEND, WJ, hb=T0 + 701_000)
run(T0 + 701_000)
setup(PEND, WJ, hb=T0 + 701_000 + 6 * 3600 + 5)
run(T0 + 701_000 + 6 * 3600 + 5)
rem = [l for l in log()[n:] if "(still, since" in l]
chk("one stalled line, then one reminder after 6 h", len(log()[n:]) >= 2 and len(rem) == 1, str(log()[n:]))

# 10. read-only vs the queue state, key never read, kill switch
setup(PEND, WJ, hb=T0 + 800_000)
before = (QS.read_bytes(), QS.stat().st_mtime_ns)
(H / ".darkbloom").mkdir(exist_ok=True)
(H / ".darkbloom" / "local.json").write_text(json.dumps({"api_key": "SECRETKEY123", "base_url": "http://127.0.0.1:1/v1"}))
run(T0 + 800_000)
chk("queue state untouched (bytes + mtime)", before == (QS.read_bytes(), QS.stat().st_mtime_ns))
blob = "".join(p.read_text() for p in list(DD.rglob("*")) if p.is_file() and p.name != "queue-wait.json")
chk("the API key never reaches any output", "SECRETKEY123" not in blob)
snap = {p: p.stat().st_mtime_ns for p in DD.rglob("*") if p.is_file()}
(DD / "deadman.disabled").write_text("")
time.sleep(0.01)
setup(PEND, WJ, hb=T0 + 900_000)
snap2 = {p: p.stat().st_mtime_ns for p in DD.rglob("*") if p.is_file() and p.name not in ("queue-wait.json", "deadman.disabled", "bloom-control-state.json")}
run(T0 + 900_000)
after = {p: p.stat().st_mtime_ns for p in DD.rglob("*") if p.is_file() and p.name not in ("queue-wait.json", "deadman.disabled", "bloom-control-state.json")}
chk("kill switch: nothing written", snap2 == after)
(DD / "deadman.disabled").unlink()

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
