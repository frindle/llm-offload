#!/usr/bin/env python3
"""pipeline-watch.py -- ONE long-lived needs-eyes watcher for the dispatch pipeline.

Replaces the per-event watchers a session used to arm by hand (one Monitor per job
label, one per slice, one per plan, one for a config file ...): each was a bash loop
scraping `ollama-queue.py status` text (fragile -- see the status-parse gotchas) or
`ollama-dispatch-slice --status`, scoped to one event, and they piled up (40+ in one
session on 2026-10-04). This one reads the durable state directly and prints ONE
line per event, forever, across everything:

  JOB    a queue job changes status (incl. a row that vanished -> its <id>.done.json)
  SLICE  a slice in any plan under slice-runs/ changes status
  EYES   a new unchecked "- [ ]" row in escalations/ESCALATIONS.md or READY-TO-LAND.md
  FILE   a --watch-file path changed (mtime/size)

Lines that need a human/Claude start with "NEEDS EYES". The first pass is a silent
baseline (prints a single "armed" line), so arming it mid-run does not replay history.

Arm once per session (Monitor tool, persistent):
    python3 -u ~/bin/pipeline-watch.py
Narrow it:  --filter 'chat-fixes|regate-'   (regex on job label / plan label)
One-shot (tests, cron):  --once --state FILE  (diffs against FILE, then rewrites it)
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
QUEUE_STATE = Path(os.environ.get("OLLAMA_QUEUE_STATE", HOME / "bin" / "ollama-queue-state.json"))
LOG_DIR = Path(os.environ.get("OLLAMA_QUEUE_LOG_DIR", HOME / "bin" / "ollama-queue-logs"))
DISPATCH = Path(os.environ.get("OLLAMA_DISPATCH_DIR", HOME / ".ollama-dispatch"))

# A job in one of these is still in motion: its transitions are reported only when
# they leave this set (or enter a needs-eyes state), so the stream stays readable.
LIVE_JOB = {"running", "queued", "pending", "scheduled", "held", "planned"}
EYES_JOB = {"needs_opus", "failed", "done_unconverged", "blocked", "paused", "error"}
EYES_SLICE = {"escalated", "failed", "blocked"}
_UNCHECKED = re.compile(r"^\s*-\s\[ \]\s+(.*\S)\s*$")


def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def queue_jobs(path):
    d = _read_json(path)
    if not isinstance(d, dict):
        return None
    jobs = d.get("jobs") or []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out = {}
    for j in jobs:
        if isinstance(j, dict) and j.get("id"):
            out[str(j["id"])] = {"status": str(j.get("status") or "?"),
                                 "label": str(j.get("label") or "")}
    return out


def sidecar_status(job_id, log_dir):
    for base in (Path(log_dir), Path(log_dir) / "archive"):
        d = _read_json(base / f"{job_id}.done.json")
        if isinstance(d, dict) and d.get("status"):
            return str(d["status"])
    return None


def slice_states(runs_dir):
    out = {}
    try:
        files = sorted(Path(runs_dir).glob("*.json"))
    except OSError:
        return out
    for f in files:
        d = _read_json(f)
        if not isinstance(d, dict) or not isinstance(d.get("slices"), dict):
            continue
        plan = str(d.get("label") or f.stem)
        for sid, s in d["slices"].items():
            if isinstance(s, dict):
                out[f"{plan}::{sid}"] = {"status": str(s.get("status") or "?"),
                                         "plan": plan, "slice": sid,
                                         "why": str(s.get("escalation_reason")
                                                    or s.get("failure_reason") or "")}
    return out


def unchecked_rows(md_path):
    try:
        lines = Path(md_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [m.group(1) for m in (_UNCHECKED.match(x) for x in lines) if m]


def file_sig(p):
    try:
        st = os.stat(p)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "missing"


def snapshot(queue_state=None, runs_dir=None, esc_dir=None, watch_files=()):
    esc = Path(esc_dir or DISPATCH / "escalations")
    return {
        "jobs": queue_jobs(queue_state or QUEUE_STATE),
        "slices": slice_states(runs_dir or DISPATCH / "slice-runs"),
        "eyes": {name: unchecked_rows(esc / name)
                 for name in ("ESCALATIONS.md", "READY-TO-LAND.md")},
        "files": {str(p): file_sig(p) for p in watch_files},
    }


def _short(s, n=240):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def diff(prev, cur, filt=None, log_dir=None):
    """PURE except the sidecar lookup for a vanished job. One string per event."""
    rx = re.compile(filt) if filt else None
    keep = (lambda text: bool(rx.search(text))) if rx else (lambda text: True)
    ev = []
    pj, cj = prev.get("jobs"), cur.get("jobs")
    if isinstance(pj, dict) and isinstance(cj, dict):
        for jid, c in cj.items():
            p = pj.get(jid)
            if not keep(c["label"]):
                continue
            if p is None:
                if c["status"] not in LIVE_JOB:
                    ev.append(f"JOB new {c['label']} [{jid}] -> {c['status']}")
                continue
            if p["status"] != c["status"] and (c["status"] not in LIVE_JOB
                                               or p["status"] not in LIVE_JOB):
                tag = "NEEDS EYES " if c["status"] in EYES_JOB else ""
                ev.append(f"{tag}JOB {c['label']} [{jid}] {p['status']} -> {c['status']}")
        for jid, p in pj.items():
            if jid not in cj and p["status"] in LIVE_JOB and keep(p["label"]):
                fin = sidecar_status(jid, log_dir or LOG_DIR) or "gone (no done.json)"
                tag = "NEEDS EYES " if fin in EYES_JOB or fin.startswith("gone") else ""
                ev.append(f"{tag}JOB {p['label']} [{jid}] {p['status']} -> {fin}")
    ps, cs = prev.get("slices") or {}, cur.get("slices") or {}
    for key, c in cs.items():
        p = ps.get(key)
        if not keep(c["plan"]) or (p is not None and p["status"] == c["status"]):
            continue
        if p is None and c["status"] not in EYES_SLICE:
            continue
        tag = "NEEDS EYES " if c["status"] in EYES_SLICE else ""
        was = p["status"] if p else "new"
        why = f" -- {_short(c['why'], 200)}" if c["status"] in EYES_SLICE and c["why"] else ""
        ev.append(f"{tag}SLICE {c['plan']} {c['slice']} {was} -> {c['status']}{why}")
    for name, rows in (cur.get("eyes") or {}).items():
        seen = set((prev.get("eyes") or {}).get(name) or [])
        for r in rows:
            if r not in seen and keep(r):
                ev.append(f"NEEDS EYES {name}: {_short(r)}")
    pf = prev.get("files") or {}
    for path, sig in (cur.get("files") or {}).items():
        if path in pf and pf[path] != sig:
            ev.append(f"FILE changed: {path} ({sig})")
    return ev


def _emit(lines):
    for line in lines:
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--filter", help="regex on job label / plan label / escalation row")
    ap.add_argument("--watch-file", action="append", default=[], metavar="PATH")
    ap.add_argument("--once", action="store_true", help="one diff against --state, then exit")
    ap.add_argument("--state", help="snapshot file for --once")
    ap.add_argument("--queue-state")
    ap.add_argument("--runs-dir")
    ap.add_argument("--esc-dir")
    ap.add_argument("--log-dir")
    a = ap.parse_args(argv)
    files = [os.path.expanduser(p) for p in a.watch_file]
    snap = lambda: snapshot(a.queue_state, a.runs_dir, a.esc_dir, files)
    if a.once:
        if not a.state:
            ap.error("--once needs --state FILE")
        prev = _read_json(a.state)
        cur = snap()
        if isinstance(prev, dict):
            _emit(diff(prev, cur, a.filter, a.log_dir))
        Path(a.state).write_text(json.dumps(cur))
        return 0
    prev = snap()
    njobs = len(prev["jobs"]) if isinstance(prev["jobs"], dict) else "?"
    print(f"pipeline watch armed ({njobs} queue rows, {len(prev['slices'])} slices, "
          f"filter={a.filter or 'none'})", flush=True)
    err_seen = None
    while True:
        time.sleep(max(1.0, a.interval))
        try:
            cur = snap()
            if cur["jobs"] is None:          # torn/unreadable queue file: keep last view
                cur["jobs"] = prev["jobs"]
            _emit(diff(prev, cur, a.filter, a.log_dir))
            prev = cur
            err_seen = None
        except Exception as e:               # never die: report a NEW error once
            msg = f"{type(e).__name__}: {e}"
            if msg != err_seen:
                print(f"pipeline watch error (continuing): {_short(msg)}", flush=True)
                err_seen = msg


if __name__ == "__main__":
    sys.exit(main())
