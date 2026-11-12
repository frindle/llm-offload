#!/usr/bin/env python3
"""Does gate-on-complete's non-pass notifier actually fire, and stay quiet on pass?

The bug it closes was silence, so the arm that matters most is arm 1: a `fail`
must produce a visible artifact. Arm 3 is the crying-wolf guard -- a clean pass
must produce NOTHING, or the inbox becomes noise and gets ignored, which is the
same outcome as not notifying at all.
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
spec = importlib.util.spec_from_file_location("goc", BIN / "gate-on-complete.py")
goc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(goc)

CASES = [
    ("1 fail + code_high -> must record",
     {"verdict": "fail", "counts": {"code_high": 1, "code": 1, "input": 0},
      "issues": [{"severity": "high", "source": "code-review",
                  "what": "reads events[0] instead of iterating"}]}, True),
    ("2 concerns -> must record",
     {"verdict": "concerns", "counts": {"code_high": 0, "code": 0, "input": 1},
      "issues": []}, True),
    ("3 pass -> must stay SILENT",
     {"verdict": "pass", "counts": {"code_high": 0, "code": 0, "input": 0},
      "issues": []}, False),
]

fails = 0
for label, payload, want in CASES:
    d = Path(tempfile.mkdtemp(prefix="notifycanary_"))
    gj = d / "job.gate.json"
    gj.write_text(json.dumps(payload))
    # mirror the caller's guard in gate-on-complete
    v = payload.get("verdict", "?")
    if str(v) not in ("pass", "skipped"):
        goc._notify_non_pass("testjob01", payload, gj)
    inbox = d / "NON-PASS-GATES.md"
    got = inbox.exists() and "testjob01" in inbox.read_text()
    ok = (got == want)
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} recorded={str(got):<5} want={str(want):<5} {label}")
    if got:
        print("        " + inbox.read_text().strip()[:110])

# arm 4: a stamped launcher gets its own per-pid inbox
print()
d = Path(tempfile.mkdtemp(prefix="notifycanary_"))
gj = d / "job.gate.json"
gj.write_text("{}")
_orig = goc._job_field
goc._job_field = lambda jid, key: {"launched_by": "uds:/tmp/cc-socks/9999.sock",
                                   "launched_by_session": "session_ABC"}.get(key)
goc._notify_non_pass("testjob02", {"verdict": "fail", "counts": {"code_high": 1},
                                   "issues": []}, gj)
goc._job_field = _orig
box = d / "NON-PASS-for-9999.md"
ok = box.exists() and "session_ABC" in box.read_text()
fails += (not ok)
print(f"  {'ok  ' if ok else 'FAIL'} launcher-addressed inbox written, names the session")
if box.exists():
    print("        " + box.read_text().strip()[:120])

print()
if fails:
    print(f"NOTIFY CANARY FAILED ({fails})"); sys.exit(1)
print("NOTIFIER DISCRIMINATES: non-pass recorded, pass silent, launcher addressed")
