"""usage_report_sched.py -- weekly Claude-usage report, run by the existing queue daemon (offload plan step 6).

Same pattern as canary_prove_sched.py: a daemon-internal MAINTENANCE task, NOT a queued job. ollama-queue.py
calls tick() once per poll (outside the state lock); tick() never blocks (the child is polled) and never raises.
No new Mac service. The metrics are computed on Unraid by the dispatch-cpu-runner (see usage_report.py);
this scheduler only decides WHEN and runs `usage_report.py run` as a child.

  due      >= DUE_AGE_S (7 d) since the last ok run AND (local hour in [WINDOW_START, WINDOW_END) OR age >= FORCE_AGE_S)
  skip     QUEUE_USAGE_REPORT=0 | child running | last attempt < RETRY_S ago
  failure  non-zero exit or timeout -> notify-owner.py (deduped). A down runner just retries hourly: the
           report never falls back to computing on the Mac.
State: ~/.ollama-dispatch/usage-report/sched.json   Log: ~/bin/ollama-queue-logs/usage-report.log
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(__file__).resolve().parent
STATE_PATH = HOME / ".ollama-dispatch" / "usage-report" / "sched.json"
LOG_PATH = BIN / "ollama-queue-logs" / "usage-report.log"
DUE_AGE_S = 7 * 86400
FORCE_AGE_S = 8 * 86400
RETRY_S = 3600
TIMEOUT_S = 1800
CHECK_EVERY_S = 300
WINDOW_START, WINDOW_END = 3, 8


class Run:
    """Real side effects; the seam test replaces this whole object."""

    def hour(self, now):
        return time.localtime(now).tm_hour

    def spawn(self):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lf = open(LOG_PATH, "ab")
        lf.write(f"\n=== queue-scheduled usage report {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        lf.flush()
        p = subprocess.Popen(["nice", "-n", "10", sys.executable, str(BIN / "usage_report.py"), "run"],
                             stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(BIN))
        lf.close()
        return p

    def kill(self, p):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

    def notify(self, title, msg, key):
        subprocess.Popen([sys.executable, str(BIN / "notify-owner.py"), title, msg[:900],
                          "--dedupe-key", key, "--dedupe-s", "21600"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Scheduler:
    def __init__(self, io=None, state_path=None, env=None):
        self.io = io or Run()
        self.state_path = Path(state_path or STATE_PATH)
        self.env = os.environ if env is None else env
        self.proc = None
        self.started = None
        self.last_check = 0.0
        self._state = self._load()

    def _load(self):
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self, **kw):
        self._state.update(kw)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def decide(self, now):
        if self.env.get("QUEUE_USAGE_REPORT", "1") == "0":
            return "disabled"
        if self.proc is not None:
            return "running"
        age = now - float(self._state.get("last_ok") or 0)
        if age < DUE_AGE_S:
            return "fresh"
        if now - float(self._state.get("last_attempt") or 0) < RETRY_S:
            return "retry-wait"
        if not (WINDOW_START <= self.io.hour(now) < WINDOW_END) and age < FORCE_AGE_S:
            return "outside-window"
        return "start"

    def _poll(self, now):
        rc = self.proc.poll()
        if rc is None:
            if now - self.started > TIMEOUT_S:
                self.io.kill(self.proc)
                self._done("timeout", f"killed after {TIMEOUT_S // 60} min", now)
            return
        self._done("ok" if rc == 0 else "failed", f"exit {rc}", now)

    def _done(self, result, detail, now):
        self.proc = None
        kw = dict(last_result=result, last_detail=detail, finished=now)
        if result == "ok":
            kw["last_ok"] = now
        self._save(**kw)
        if result != "ok":
            self.io.notify("Weekly usage report FAILED", f"{result}: {detail}. Log: {LOG_PATH}. "
                           "Retry by hand: python3 ~/bin/usage_report.py run", "usage-report-queue")

    def tick(self, now=None):
        """Once per daemon poll. Never raises, never blocks. Returns the decision string (tests)."""
        now = time.time() if now is None else now
        try:
            if self.proc is not None:
                self._poll(now)
                return "running" if self.proc else "finished"
            if now - self.last_check < CHECK_EVERY_S:
                return "throttled"
            self.last_check = now
            d = self.decide(now)
            if d == "start":
                self.proc = self.io.spawn()
                self.started = now
                self._save(started=now, last_attempt=now)
            return d
        except Exception as e:      # noqa: BLE001 -- maintenance must never take the daemon down
            try:
                print(f"[queue] usage-report scheduler error (ignored): {e!r}", file=sys.stderr)
            except Exception:
                pass
            return "error"
