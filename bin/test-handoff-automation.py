#!/usr/bin/env python3
"""handoff-emit AUTOMATED clearing guards + per-job processing (2026-10-12, LLM-only pipeline).

The panel is processed by automation (handoff-autoclear: mechanical rules, Sonnet batch,
follow-up queue jobs). Under HANDOFF_AUTOMATED=1 `--acted` must REFUSE, before any write:
  * a job that is still live in the queue (incl. needs_opus / escalated / held),
  * a job whose worktree another live job uses,
  * a job with a required, unapproved sign-off (and --override-signoff is never available),
  * a merge/landed claim without --commit (the existing guard applies to automation too).
A human (no HANDOFF_AUTOMATED) is unchanged. The per-job hook (--job-id) starts ONE debounced,
detached autoclear pass.
Run: python3 test-handoff-automation.py      prints HANDOFF_AUTOMATION_OK
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HANDOFF_SRC") or HERE / "handoff-emit.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hoauto-"))
    home = tmp / "home"
    (home / "bin").mkdir(parents=True)
    (tmp / "ho").mkdir()
    shutil.copy(SRC, tmp / "handoff-emit.py")
    shutil.copy(Path(SRC).parent / "queue_config.py", tmp / "queue_config.py")
    (home / "bin" / "signoff.json").write_text(json.dumps({"fffff0000005": {"required": True, "reviewer": "owner",
                                                                    "verdict": None, "reason": "t"}}))
    rows = [
        {"id": "fffff0000001", "label": "live-one", "status": "needs_opus", "cwd": "/w/1"},
        {"id": "fffff0000002", "label": "done-shared", "status": "done", "cwd": "/w/2"},
        {"id": "fffff0000003", "label": "other-live", "status": "running", "cwd": "/w/2"},
        {"id": "fffff0000004", "label": "done-free", "status": "done", "cwd": "/w/4"},
        {"id": "fffff0000005", "label": "done-signoff", "status": "done", "cwd": "/w/5"},
        {"id": "fffff0000006", "label": "failed-held", "status": "held", "cwd": "/w/6"},
    ]
    (home / "bin" / "ollama-queue-state.json").write_text(json.dumps({"jobs": rows}))

    def run(auto, *args):
        env = dict(os.environ, HOME=str(home), HANDOFF_DIR=str(tmp / "ho"))
        env.pop("HANDOFF_AUTOMATED", None)
        if auto:
            env["HANDOFF_AUTOMATED"] = "1"
        p = subprocess.run([sys.executable, str(tmp / "handoff-emit.py"), *args], capture_output=True,
                           text=True, env=env)
        ap_ = tmp / "ho" / "acted.json"
        return p.returncode, (json.loads(ap_.read_text()) if ap_.exists() else {}), p.stdout

    rc, acted, out = run(True, "--acted", "fffff0000001", "--reason", "routine")
    chk("automated: a needs_opus (live) job is REFUSED, nothing written", (rc, "fffff0000001" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000002", "--reason", "routine")
    chk("automated: a job whose worktree a live job uses is REFUSED", (rc, "fffff0000002" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000006", "--reason", "routine")
    chk("automated: a held job is REFUSED", (rc, "fffff0000006" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000005", "--reason", "routine")
    chk("automated: required unapproved sign-off is REFUSED", (rc, "fffff0000005" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000005", "--override-signoff", "x")
    chk("automated: --override-signoff is never available", (rc, "fffff0000005" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000004", "fffff0000001", "--reason", "routine")
    chk("automated: one bad id blocks the whole batch (no partial write)", (rc, acted), (2, {}))
    rc, acted, out = run(True, "--acted", "fffff0000004", "--reason", "landed in main")
    chk("automated: a merge claim without --commit is still REFUSED", (rc, "fffff0000004" in acted), (2, False))
    rc, acted, out = run(True, "--acted", "fffff0000004", "--reason", "superseded by a later job")
    chk("automated: a finished job with nothing live is cleared with its reason",
        (rc, (acted.get("fffff0000004") or {}).get("reason")), (0, "superseded by a later job"))
    rc, acted, out = run(False, "--acted", "fffff0000001", "--reason", "human decision")
    chk("a HUMAN (no HANDOFF_AUTOMATED) is unchanged: may clear a live row", (rc, "fffff0000001" in acted), (0, True))

    # ---- per-job processing hook
    loader = importlib.machinery.SourceFileLoader("he_auto", str(tmp / "handoff-emit.py"))
    spec = importlib.util.spec_from_loader("he_auto", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    saved = {k: os.environ.pop(k, None) for k in ("DISPATCH_VERIFY_SANDBOX", "HANDOFF_DIR", "HANDOFF_AUTO_PROCESS")}
    try:
        tool = tmp / "handoff-autoclear.py"
        tool.write_text("#")
        stamp = tmp / "stamp"
        spawned = []
        popen = lambda cmd, **kw: spawned.append((cmd, kw.get("env", {}).get("HANDOFF_AUTOMATED")))
        chk("kick: starts one detached autoclear --auto under HANDOFF_AUTOMATED=1",
            (m.kick_autoclear("j", tool, popen, 1000.0, stamp), [(c[-1], e) for c, e in spawned]), ("started", [("--auto", "1")]))
        chk("kick: a second job within 45s is debounced",
            (m.kick_autoclear("j2", tool, popen, 1010.0, stamp), len(spawned)), ("skipped:debounced", 1))
        chk("kick: after the debounce window it starts again", m.kick_autoclear("j3", tool, popen, 2000.0, stamp), "started")
        chk("kick: a COPY of the script (canary / replay sandbox) never spawns the pass",
            m.kick_autoclear("j", None, popen, 9500.0, tmp / "stamp2"), "skipped:not the installed copy (sandbox/replay)")
        os.environ["HANDOFF_AUTO_PROCESS"] = "off"
        chk("kick: HANDOFF_AUTO_PROCESS=off disables", m.kick_autoclear("j", tool, popen, 9000.0, stamp), "skipped:HANDOFF_AUTO_PROCESS=off")
        os.environ.pop("HANDOFF_AUTO_PROCESS")
        os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
        chk("kick: never under the verify sandbox", m.kick_autoclear("j", tool, popen, 9000.0, stamp).startswith("skipped:sandbox"), True)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    src = SRC.read_text()
    chk("wiring: --job-id (completion hook) path kicks the processing",
        'print("[handoff] auto-process: " + kick_autoclear(a.job_id))' in src, True)
    chk("wiring: --auto-process runs the pass synchronously under the guards",
        'HANDOFF_AUTOMATED="1")' in src and "--auto-process" in src, True)
    wsrc = (HERE / "dispatch-escalation-watcher.py").read_text()
    chk("wiring: the watcher's autoclear pass runs with HANDOFF_AUTOMATED=1",
        'os.environ["HANDOFF_AUTOMATED"] = "1"' in wsrc, True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nHANDOFF_AUTOMATION_OK" if not FAILS else "\nFAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
