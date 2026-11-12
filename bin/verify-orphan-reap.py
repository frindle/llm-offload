#!/usr/bin/env python3
"""verify-orphan-reap -- find (and with --apply, kill) ORPHANED verify process trees.

WHY (2026-10-02, Rivian s3): verify runs whose parent died or timed out were
reparented to launchd (ppid 1) and spun at 100% CPU for hours -- five at once,
`npm exec tsx ./verify_impl.mts` -> node tsx -> node, all with cwd inside a slice
worktree. Nothing owned them, so nothing ever stopped them. pgrun.py now kills the
whole group on timeout; this covers what that cannot: a parent SIGKILLed (no
handler runs) and orphans that predate the fix.

A ROOT is a process that is ALL of:
  * ppid == 1 (orphaned -- no live parent will ever wait for it);
  * cwd inside a dispatch worktree (~/.ollama-dispatch/worktrees/ by default);
  * a verify-ish command (npm/npx exec, tsx, node, vitest/jest/pytest, bash
    verify.sh, python3 refimpl.py / auto-harness-check.py) -- NEVER a pipeline
    driver (ollama-worker / ollama-dispatch-* / ollama-queue), which can be
    legitimately orphaned by a daemon restart and is the queue's to adopt;
  * older than --min-age seconds.
It and its descendants are killed BY PID (never pgid: the group can be shared with
unrelated live work), children first, SIGTERM then SIGKILL.

Default is a DRY RUN that prints what it would kill. Exit 0 always (it is a
janitor); --json for machine output.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

WORKTREES = Path.home() / ".ollama-dispatch" / "worktrees"
MIN_AGE_S = 120
VERIFY_RE = re.compile(
    r"(^|/)(npm|npx)( |$)|npm exec|(^|/)tsx\b|/tsx/|(^|/)node( |$)|vitest|jest|pytest"
    r"|bash verify\.sh|verify\.sh|refimpl\.py|auto-harness-check\.py|verify_impl")
DRIVER_RE = re.compile(r"ollama-worker|ollama-dispatch-|ollama-queue|dispatch-self-heal"
                       r"|dispatch-escalation-watcher")


def parse_etime(s):
    """ps etime '[[dd-]hh:]mm:ss' -> seconds."""
    s = s.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def ps_table():
    """[{pid, ppid, age, cmd}] for every process."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,etime=,command="],
                         capture_output=True, text=True).stdout
    rows = []
    for ln in out.splitlines():
        f = ln.split(None, 3)
        if len(f) < 4:
            continue
        try:
            rows.append({"pid": int(f[0]), "ppid": int(f[1]), "age": parse_etime(f[2]),
                         "cmd": f[3]})
        except ValueError:
            continue
    return rows


def cwd_of(pids):
    """{pid: cwd} via one lsof call (macOS has no /proc)."""
    if not pids:
        return {}
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    res, cur = {}, None
    for ln in out.splitlines():
        if ln.startswith("p"):
            cur = int(ln[1:])
        elif ln.startswith("n") and cur is not None:
            res[cur] = ln[1:]
    return res


def descendants(rows, root):
    kids = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        for c in kids.get(p, []):
            out.append(c)
            stack.append(c)
    return out


def find_orphans(rows, cwds, worktrees=WORKTREES, min_age=MIN_AGE_S, self_pid=None):
    """PURE. [(root_row, [pids to kill, deepest first])]."""
    wt = str(worktrees).rstrip("/") + "/"
    self_pid = os.getpid() if self_pid is None else self_pid
    out = []
    for r in rows:
        if r["ppid"] != 1 or r["pid"] == self_pid or r["age"] < min_age:
            continue
        if DRIVER_RE.search(r["cmd"]) or not VERIFY_RE.search(r["cmd"]):
            continue
        c = cwds.get(r["pid"]) or ""
        if not (c + "/").startswith(wt):
            continue
        tree = descendants(rows, r["pid"])
        out.append((r, list(reversed(tree)) + [r["pid"]]))
    return out


def kill_pids(pids, cmd_of, grace=3.0, kill=os.kill):
    """SIGTERM each pid (only if its cmdline still matches what was seen -- pids get
    recycled), then SIGKILL survivors. Returns [(pid, action)]."""
    acted = []
    for p in pids:
        if _cmd(p) != cmd_of.get(p):
            acted.append((p, "skipped: pid recycled / gone"))
            continue
        try:
            kill(p, signal.SIGTERM)
            acted.append((p, "sigterm"))
        except ProcessLookupError:
            acted.append((p, "gone"))
    end = time.time() + grace
    while time.time() < end and any(_alive(p) for p, a in acted if a == "sigterm"):
        time.sleep(0.2)
    for i, (p, a) in enumerate(acted):
        if a == "sigterm" and _alive(p) and _cmd(p) == cmd_of.get(p):
            try:
                kill(p, signal.SIGKILL)
                acted[i] = (p, "sigkill")
            except ProcessLookupError:
                pass
    return acted


def _alive(p):
    try:
        os.kill(p, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cmd(p):
    return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(p)],
                          capture_output=True, text=True).stdout.strip() or None


def reap(apply=False, worktrees=WORKTREES, min_age=MIN_AGE_S, log=print):
    rows = ps_table()
    cand = [r["pid"] for r in rows if r["ppid"] == 1 and VERIFY_RE.search(r["cmd"])
            and not DRIVER_RE.search(r["cmd"])]
    orphans = find_orphans(rows, cwd_of(cand), worktrees, min_age)
    cmd_of = {r["pid"]: r["cmd"] for r in rows}
    report = []
    for root, pids in orphans:
        ent = {"root": root["pid"], "age_s": root["age"], "cmd": root["cmd"][:160],
               "pids": pids}
        if apply:
            ent["actions"] = kill_pids(pids, cmd_of)
        report.append(ent)
        log(("REAPED" if apply else "would reap") +
            f" orphaned verify tree root={root['pid']} age={root['age']}s pids={pids}: "
            f"{root['cmd'][:120]}")
    return report


LOG = Path.home() / "bin" / "ollama-queue-logs" / "verify-orphan-reap.jsonl"


def record_and_alert(report, applied, log_path=LOG, notifier=None):
    """Append one JSONL line per tick that found something; alert the owner ONCE per
    tick that actually killed something (dedupe per root pid set)."""
    if not report:
        return None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "applied": applied,
                                "trees": report}) + "\n")
    except OSError:
        pass
    if not applied:
        return None
    if notifier is None:
        import importlib.util as _ilu
        _s = _ilu.spec_from_file_location("notify_owner",
                                          Path(__file__).resolve().parent / "notify-owner.py")
        _m = _ilu.module_from_spec(_s)
        _s.loader.exec_module(_m)
        notifier = _m.notify
    roots = sorted(e["root"] for e in report)
    msg = (f"Killed {len(report)} orphaned verify tree(s) (ppid 1, in a dispatch "
           f"worktree): " + "; ".join(f"{e['root']} age {e['age_s']}s {e['cmd'][:60]}"
                                     for e in report)[:700] +
           f". Log: {log_path}. An orphan means a verify outlived its parent -- "
           f"look for the slice that leaked it.")
    return notifier("verify orphan reaped", msg,
                    dedupe_key="verify-orphan-reap:" + ",".join(map(str, roots)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="actually kill (default: dry run)")
    ap.add_argument("--min-age", type=int, default=MIN_AGE_S)
    ap.add_argument("--worktrees", default=str(WORKTREES))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    rep = reap(a.apply, Path(a.worktrees), a.min_age,
               log=(lambda *_: None) if a.json else (lambda m: print(f"{stamp} {m}")))
    record_and_alert(rep, a.apply)
    if a.json:
        print(json.dumps(rep))
    elif not rep:
        print("no orphaned verify trees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
