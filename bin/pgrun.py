#!/usr/bin/env python3
"""pgrun -- run a shell command in its OWN process group and, on timeout, kill the
WHOLE group (not just the `sh -c` child).

WHY (2026-10-02, Rivian s3): subprocess.run(shell=True, timeout=N) kills only the
direct shell on timeout. `bash verify.sh` -> `npm exec tsx` -> `node verify_impl.mts`
survived every timeout, reparented to launchd (ppid 1), and spun at 100% CPU for
4.5h+: FIVE of them at once, each from a verify-relevance mutant
(`i < n` -> `i >= n`) that loops forever on an empty array. Same fix the worker
(_run_shell_group) and ollama-dispatch-auto (_run_in_own_process_group) already
carry; this is the shared copy for every other verify caller.

Also: if THIS process is told to stop (SIGTERM / normal exit) while a group is
live, the group is killed first, so a parent being torn down does not orphan it.
(SIGKILL of the parent cannot be caught -- verify-orphan-reap covers that.)
"""
import atexit
import os
import signal
import subprocess
import time

_LIVE = set()           # pgids of groups started here and not yet reaped
TIMEOUT_RC = 124
SANDBOX_ENV = "DISPATCH_VERIFY_SANDBOX"   # see ollama-queue.py VERIFY SANDBOX


def _kill_group(pgid, grace=3.0):
    """SIGTERM the group, then SIGKILL whatever is left after `grace` seconds.
    Never our own group (start_new_session guarantees pgid == child pid, so this
    guard is defence against a future edit)."""
    try:
        if pgid == os.getpgid(0):
            return
    except OSError:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        end = time.time() + wait
        while time.time() < end:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                return
            time.sleep(0.1)


def _kill_all_live():
    for g in list(_LIVE):
        _kill_group(g, grace=1.0)
        _LIVE.discard(g)


atexit.register(_kill_all_live)
_PREV_TERM = None


def _on_term(signum, frame):
    _kill_all_live()
    prev = _PREV_TERM
    if callable(prev):
        prev(signum, frame)
    else:
        raise SystemExit(128 + signum)


def _install_term_handler():
    global _PREV_TERM
    try:
        cur = signal.getsignal(signal.SIGTERM)
        if cur is _on_term or cur == signal.SIG_IGN:
            return      # ignored on purpose by the host: keep that semantics
        _PREV_TERM = cur if callable(cur) else None
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass            # not the main thread: atexit still covers normal exit


def run_group(cmd, cwd=None, env=None, timeout=None, shell=True):
    """Returns (returncode, stdout, stderr, timed_out). On timeout returncode is
    124 and stdout/stderr hold whatever was printed before the hang.

    VERIFY-SANDBOX (2026-10-05): every caller of this helper runs a verify/test,
    so the child always gets DISPATCH_VERIFY_SANDBOX=1 -- ollama-queue.py then
    refuses to enqueue/mutate REAL queue jobs from code under test."""
    _install_term_handler()
    env = dict(os.environ if env is None else env)
    env[SANDBOX_ENV] = "1"
    p = subprocess.Popen(cmd, cwd=None if cwd is None else str(cwd), env=env,
                         shell=shell, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    _LIVE.add(p.pid)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_group(p.pid)
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            p.kill()
            out, err = "", ""
        return TIMEOUT_RC, out or "", err or "", True
    finally:
        # The leader may exit while its children keep the group alive (a verify
        # that backgrounds something): never leave a group behind on any path.
        try:
            os.killpg(p.pid, 0)
            _kill_group(p.pid, grace=1.0)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _LIVE.discard(p.pid)


def timed_out_note(timeout, what="verify"):
    return (f"\n[{what} TIMED OUT after {timeout}s and its whole process group was "
            f"killed -- a hang, most often an infinite loop (check loop bounds / "
            f"termination conditions) or an await/IO that never resolves]")
