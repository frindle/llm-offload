#!/usr/bin/env python3
"""Revert-tests for Bug #1 (2026-09-18): preemption must free the lane in BOUNDED
time even when the worker cannot honor a graceful SIGTERM mid-generation.

Today a runaway reasoning generation held the Studio lane 12+ minutes; the regate
that was supposed to preempt it stayed pending and ~24 coding jobs stayed held
behind it. Graceful SIGTERM alone could not free the lane. The fix has three legs:

  1. queue: _preempt_should_escalate() -> the daemon SIGKILLs a victim that has not
     honored the graceful pause within PREEMPT_SIGKILL_GRACE_S.
  2. queue: _parse_checkpoint_transcript() -> a SIGKILL'd victim is reaped RESUMABLE
     from its last per-iteration checkpoint transcript (no completed work lost).
  3. worker: call_ollama_streaming() aborts the in-flight request the instant an
     external SIGTERM pause is requested (ChatAbortedForPause), so the streaming path
     frees the lane in seconds and the SIGKILL escalation is only a backstop.

Red-on-revert:
  - Leg 1: make _preempt_should_escalate always return False -> the escalation
    assertions fail (a long-overdue victim would never be killed).
  - Leg 2: make _parse_checkpoint_transcript ignore the CHECKPOINT marker -> the
    "recovers last checkpoint" assertion fails.
  - Leg 3: remove the _sigterm_pause_requested check in call_ollama_streaming's read
    loop -> the "aborts mid-stream" assertion fails (it would drain all lines).

Run: python3 test-preempt-escalation.py
"""
import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "ollama-queue.py"
WORKER = HERE / "ollama-worker.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def test_escalation(q):
    grace = q.PREEMPT_SIGKILL_GRACE_S
    now = datetime.now(timezone.utc)

    def job(**kw):
        base = {"status": "running", "pid": 4242}
        base.update(kw)
        return base

    old = (now - timedelta(seconds=grace + 5)).isoformat()
    fresh = (now - timedelta(seconds=max(0, grace - 10))).isoformat()

    ok("escalate: running victim SIGTERM'd > grace ago -> escalate",
       q._preempt_should_escalate(job(preempt_sigterm_at=old), now=now) is True)
    ok("escalate: SIGTERM'd < grace ago -> wait, do NOT escalate",
       q._preempt_should_escalate(job(preempt_sigterm_at=fresh), now=now) is False)
    ok("escalate: never SIGTERM'd for a preempt -> never escalate",
       q._preempt_should_escalate(job(), now=now) is False)
    ok("escalate: already escalated -> do not double-kill",
       q._preempt_should_escalate(
           job(preempt_sigterm_at=old, preempt_escalated=True), now=now) is False)
    ok("escalate: not running -> never escalate",
       q._preempt_should_escalate(
           job(status="paused", pid=None, preempt_sigterm_at=old), now=now) is False)
    ok("escalate: no pid (already reaped) -> never escalate",
       q._preempt_should_escalate(
           job(pid=None, preempt_sigterm_at=old), now=now) is False)
    ok("escalate: malformed timestamp -> do not escalate on garbage",
       q._preempt_should_escalate(job(preempt_sigterm_at="not-a-date"), now=now) is False)


def test_checkpoint_parse(q, tmp):
    log = tmp / "job.log"
    # Two checkpoints then no clean end (a killed job): last-wins.
    log.write_text(
        "[worker] starting\n"
        "[worker] CHECKPOINT TRANSCRIPT: /t/iter1.json\n"
        "[worker] CHECKPOINT TRANSCRIPT: /t/iter2.json\n"
        "[worker] SIGKILL by escalation\n")
    ok("checkpoint: recovers the LAST checkpoint transcript from a killed job's log",
       q._parse_checkpoint_transcript(str(log)) == "/t/iter2.json")

    log2 = tmp / "clean.log"
    log2.write_text("[worker] RESUMABLE TRANSCRIPT: /t/final.json\n")
    ok("checkpoint: falls back to RESUMABLE marker when no checkpoint present",
       q._parse_checkpoint_transcript(str(log2)) == "/t/final.json")

    ok("checkpoint: None when neither marker present",
       q._parse_checkpoint_transcript(str(tmp / "missing.log")) is None)


class _FakeResp:
    """Minimal stand-in for urllib's response: a context-managed iterable of bytes
    lines with a .close(). Each __next__ is where the worker's mid-stream pause check
    runs, so we can assert it aborts before draining."""
    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


def test_worker_streaming_abort(w):
    # A stream that would otherwise deliver two content chunks then done.
    lines = [
        json.dumps({"message": {"content": "hello"}}).encode(),
        json.dumps({"message": {"content": " world"}}).encode(),
        json.dumps({"done": True, "eval_count": 2}).encode(),
    ]
    fake = _FakeResp(lines)

    orig_urlopen = w.urllib.request.urlopen
    w.urllib.request.urlopen = lambda *a, **k: fake
    orig_flag = w._sigterm_pause_requested
    w._sigterm_pause_requested = True  # pause requested BEFORE the first chunk
    try:
        raised = False
        try:
            w.call_ollama_streaming("http://x", "m", [{"role": "user", "content": "hi"}],
                                    0.0, 4096, tools=False, live=None)
        except w.ChatAbortedForPause:
            raised = True
        ok("worker: streaming loop raises ChatAbortedForPause when paused mid-stream",
           raised)
        ok("worker: aborting the stream closes the response (frees the connection/GPU)",
           fake.closed is True)
    finally:
        w.urllib.request.urlopen = orig_urlopen
        w._sigterm_pause_requested = orig_flag

    # Control: with NO pause requested, the same stream returns normally (proves the
    # abort is conditional on the flag, not always-on).
    fake2 = _FakeResp(lines)
    w.urllib.request.urlopen = lambda *a, **k: fake2
    w._sigterm_pause_requested = False
    try:
        resp = w.call_ollama_streaming("http://x", "m", [{"role": "user", "content": "hi"}],
                                       0.0, 4096, tools=False, live=None)
        ok("worker: no pause -> stream completes normally",
           resp["message"]["content"] == "hello world")
    finally:
        w.urllib.request.urlopen = orig_urlopen
        w._sigterm_pause_requested = orig_flag


def main():
    import tempfile
    q = _load("oq_preempt", QUEUE)
    w = _load("ow_preempt", WORKER)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_escalation(q)
        test_checkpoint_parse(q, tmp)
        test_worker_streaming_abort(w)
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall preempt-escalation tests passed")


if __name__ == "__main__":
    main()
