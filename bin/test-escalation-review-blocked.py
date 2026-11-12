#!/usr/bin/env python3
"""Standalone regression test for the session-limit false negative.

BUG (live, 2026-09-22, job 3ca17c902224 / esim-global-s4-badge-and-merge):
headless `claude -p` answered a session-limit refusal on STDOUT -- non-empty,
exit-0-looking -- and dispatch-escalation-watcher.spawn_review only treated
EMPTY stdout as a failure. The refusal text was written out as a completed
review with "proposed fix: none written", and the job sat in needs_opus
forever because nothing knew the review had never run.

This test never needs a real rate-limited CLI: it monkeypatches
subprocess.run inside the watcher module and asserts BOTH ways --
  * a canned session-limit stdout is classified as a retryable infra failure
    (marked REVIEW BLOCKED, ledger key un-announced so the next pass retries,
    no "proposed fix: none written" footer written);
  * a canned REAL review still flows through the normal path unchanged.

Run:  python3 ~/bin/test-escalation-review-blocked.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

MOD = Path.home() / "bin" / "dispatch-escalation-watcher.py"
spec = importlib.util.spec_from_file_location("escwatch", MOD)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
# This test is about the REVIEW BLOCKED path; the pre-spawn evidence gate (a bare row context spawns no
# review) has its own test, test-esc-review-bounds.py, and would otherwise make this one depend on whatever
# driver state the machine happens to hold for the job id. Hermetic: the gate is open here.
W.handle_no_evidence = lambda *a, **k: False

LIMIT_STDOUT = "You've hit your session limit · resets 7:30pm (America/Los_Angeles)"
REAL_STDOUT = ("VERDICT: c -- harness defect: the verify command never ran.\n"
               "Evidence: the log shows no verify invocation.\n")

FAILED = []
RAN = []


def check(name, cond):
    RAN.append(name)
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        FAILED.append(name)


class _R:
    def __init__(self, out, rc=0, err=""):
        self.stdout, self.returncode, self.stderr = out, rc, err


def _fake_run(out, rc=0, err=""):
    def run(cmd, **kw):
        # osascript (desktop notification) must still be a no-op success
        if cmd and "osascript" in str(cmd[0]):
            return _R("")
        # the review is now a QUEUE job: `enqueue` captures the model's final answer
        # to --capture-final-as (here: the canned text), `status` lists nothing active
        if "enqueue" in cmd:
            if out:
                Path(cmd[cmd.index("--capture-final-as") + 1]).write_text(out)
            return _R("enqueued aaaaaaaaaaaa  esc-review\n")
        if "status" in cmd:
            return _R("")
        return _R(out, rc, err)
    return run


def scenario(stdout, rc=0, job_id="3ca17c902224"):
    """One full run_once pass with the CLI mocked. Returns (esc_dir, ledger)."""
    td = Path(tempfile.mkdtemp())
    real_run = subprocess.run
    W.ESC_DIR = td / "escalations"
    W.LEDGER = td / "ledger.json"
    W.INDEX_MD = td / "INDEX.md"
    W.SLICE_RUNS = td / "slice-runs"
    W.SLICE_RUNS.mkdir(parents=True)
    # Source D: one job parked in needs_opus, straight off the queue status text.
    W.detect_stuck_jobs = lambda **kw: [{
        "kind": "job", "source": "D", "job_id": job_id,
        "label": "esim-global-s4-badge-and-merge", "signature": "needs_opus",
        "reason": "queue parked this job as needs_opus"}]
    W.subprocess.run = _fake_run(stdout, rc)
    try:
        W.run_once()
    finally:
        W.subprocess.run = real_run
    return td, (W._load_json(W.LEDGER) or {})


def reviews(td):
    return sorted((td / "escalations").glob("*.review.md"))


print("escalation-watcher: session-limit blocked-review test")

# --- 1. the bug, as captured live -----------------------------------------
td, led = scenario(LIMIT_STDOUT)
txt = reviews(td)[0].read_text()
check("a session-limit refusal is marked REVIEW BLOCKED",
      txt.lstrip().startswith("REVIEW BLOCKED"))
check("the reason names the session limit and its reset time",
      "session limit" in txt and "7:30pm" in txt)
check("it is NOT written out as a completed review with a fix footer",
      "proposed fix: none written" not in txt)
check("the raw refusal is preserved for a human", "hit your session limit" in txt)
key = "job:3ca17c902224:needs_opus"
check("the escalation is UN-announced, i.e. retryable next pass",
      key not in (led.get("announced") or []))
check("the blocked attempt is counted", W._blocked_attempts(led, key) == 1)
check("the index does not gain a verdict row for a review that never ran",
      not (td / "INDEX.md").exists() or "VERDICT" not in (td / "INDEX.md").read_text())

# --- 2. a REAL review is unaffected ---------------------------------------
td2, led2 = scenario(REAL_STDOUT)
txt2 = reviews(td2)[0].read_text()
check("a real review is still filed normally",
      txt2.startswith("VERDICT: c") and "proposed fix: none written" in txt2)
check("a real review stays ANNOUNCED (terminal, not retried)",
      key in (led2.get("announced") or []))
check("a real review is indexed with its verdict",
      "VERDICT: c" in (td2 / "INDEX.md").read_text())

# --- 3. retry is bounded --------------------------------------------------
td3 = Path(tempfile.mkdtemp())
W.LEDGER = td3 / "l.json"
(td3 / "l.json").write_text(json.dumps(
    {"announced": [key],
     "records": {key: {"review_blocked_attempts": W.REVIEW_BLOCKED_MAX_ATTEMPTS - 1}}}))
td4, led4 = None, None
real_run = subprocess.run
W.ESC_DIR = td3 / "escalations"
W.INDEX_MD = td3 / "INDEX.md"
W.SLICE_RUNS = td3 / "slice-runs"
W.SLICE_RUNS.mkdir(parents=True)
W.detect_stuck_jobs = lambda **kw: [{
    "kind": "job", "source": "D", "job_id": "3ca17c902224",
    "label": "esim-global-s4-badge-and-merge", "signature": "needs_opus",
    "reason": "queue parked this job as needs_opus"}]
# the key is already announced, so force a fresh detection by clearing it
(td3 / "l.json").write_text(json.dumps(
    {"announced": [],
     "records": {key: {"review_blocked_attempts": W.REVIEW_BLOCKED_MAX_ATTEMPTS - 1}}}))
W.subprocess.run = _fake_run(LIMIT_STDOUT)
try:
    W.run_once()
finally:
    W.subprocess.run = real_run
led3 = W._load_json(td3 / "l.json") or {}
check("after the attempt cap the escalation stops retrying",
      key in (led3.get("announced") or []))
check("...and a human-visible index row says the review never ran",
      "NO REVIEW after" in (td3 / "INDEX.md").read_text())

print("%d checks, %d failed" % (len(RAN), len(FAILED)))
sys.exit(1 if FAILED else 0)
