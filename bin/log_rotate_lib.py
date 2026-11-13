#!/usr/bin/env python3
"""log_rotate_lib.py -- engine for log-rotate.py (see docs/log-rotation.md).

Deletes ONLY files in an explicit ALLOWLIST (root dir + filename regex), only when the
newest mtime is older than the retention for that log, never a symlink, never anything a
live consumer still references. Pure planning (plan()) is separate from deletion (run()).
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import fcntl
import json
import os
import re
import shutil
import time
from pathlib import Path

import log_retention_config as cfgmod

HOME = Path(os.environ.get("LOG_ROTATE_HOME") or Path.home())
LIVE_GRACE_S = 3600          # anything written in the last hour is "live": never touched
TERMINAL_LIVE = None         # every id present in the live queue state is protected, any status
H12 = r"[0-9a-f]{12}"
TS = r"\d{8}T\d{6}Z"

# kind: 'job' (id-keyed log/livelog/answer), 'side' (durable sidecar -> ledger floor),
#       'dir' (per-job review dir, removed as a unit), 'transcript' (worker log, ref-checked),
#       'misc' (id-less dispatch scratch logs)
ALLOWLIST = [
    # (root key, regex, kind)
    ("queue_logs", re.compile(rf"^({H12})-.+\.log$"), "job"),
    ("queue_logs", re.compile(rf"^({H12})\.answer\.md$"), "job"),
    ("queue_logs", re.compile(rf"^({H12})\.(?:gate\.json|done\.json|diff)$"), "side"),
    ("queue_logs", re.compile(rf"^({H12})-(?:review|regate|secondop)$"), "dir"),
    ("livelogs", re.compile(rf"^({H12})-.+\.livelog$"), "job"),
    ("worker_logs", re.compile(rf"^({TS})(?:\.json|\.salvage\.patch|\.cutoff-\d+\.txt)$"), "transcript"),
    ("dispatch", re.compile(r"^[A-Za-z0-9._-]+(?:\.auto\d*\.log|-auto\d*\.log|-slice\d*\.log|\.slice\.log)$"), "misc"),
]
# NEVER matched by any pattern above (documented; tests assert): dispatch-escalation-watcher.log,
# dispatch-ack-reconcile.log, lane-failure-auto-resumed.log, verify-orphan-reap.jsonl, preflight-ledger/,
# web-tasks/, archive/, nsample-tasks/, dispatch-metrics.jsonl, claude-dispatches.jsonl, queue-daemon.log,
# cpu-lane.log, bloom-control.log, worktrees/, ~/.darkbloom/** (power CSVs, earnings archive = tax records).


def make_paths(home=None):
    h = Path(home) if home else HOME
    sd = h / "bin" if (home or os.environ.get("LOG_ROTATE_HOME")) else queue_config.state_dir()   # state dir; an explicit home (tests) keeps the old layout
    ho = os.environ.get("HANDOFF_DIR")
    if not ho and not home and not os.environ.get("LOG_ROTATE_HOME"):
        ho = str(queue_config.handoff_dir())        # env, then paths.handoff_dir, then the Mac default
    return {
        "home": h,
        "queue_logs": sd / "ollama-queue-logs",
        "livelogs": sd / "ollama-queue-livelogs",
        "worker_logs": sd / "ollama-worker-logs",
        "dispatch": h / ".ollama-dispatch",
        "state": sd / "ollama-queue-state.json",
        "signoff": sd / "signoff.json",
        "handoff": Path(ho) if ho else h / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff",
        "escalations": h / ".ollama-dispatch" / "escalations" / "ESCALATIONS.md",
        "lock": h / ".ollama-dispatch" / "log-rotate.lock",
    }


# ---------------------------------------------------------------- protection sources
class SourceError(Exception):
    pass


def _json(p, required=True):
    try:
        return json.loads(Path(p).read_text())
    except FileNotFoundError:
        if required:
            raise SourceError(f"{p} missing")
        return {}
    except (OSError, ValueError) as e:
        raise SourceError(f"{p} unreadable: {e}")


def protections(paths):
    """-> dict(ids=set, paths=set, bundles=set, parked=set, escalated=set, why={id:reason}).
    ANY unreadable source raises SourceError: a protection list we could not build must
    never silently become 'nothing is protected'."""
    ids, why, ppaths, parked, escalated, live_bundles = set(), {}, set(), set(), set(), set()

    def add(i, reason):
        if i:
            ids.add(i)
            why.setdefault(i, reason)

    st = _json(paths["state"])
    jobs = st.get("jobs") or []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    for j in jobs:
        add(j.get("id"), "live-queue-row")
        for k in ("continues", "auto_fix_root", "after"):
            v = j.get(k)
            if isinstance(v, str) and re.fullmatch(H12, v):
                add(v, "live-queue-row")
        for k in ("log_path", "live_log_path", "resume_transcript"):
            if j.get(k):
                ppaths.add(os.path.realpath(str(j[k])))
        if j.get("bundle"):
            live_bundles.add(j["bundle"])
    parked = set((st.get("_bundle_parked") or {}).keys())

    hd = Path(paths["handoff"])
    if not hd.is_dir():
        raise SourceError(f"{hd} missing")
    for sub in ("pending", "complete"):
        d = hd / sub
        if d.is_dir():
            for n in os.listdir(d):
                for m in re.findall(H12, n):
                    add(m, "unprocessed-handoff")
    try:
        for m in re.findall(H12, (hd / "INDEX.md").read_text()):
            add(m, "unprocessed-handoff")
    except FileNotFoundError:
        pass
    except OSError as e:
        raise SourceError(f"INDEX.md unreadable: {e}")

    so = _json(paths["signoff"], required=False)
    for jid, rec in (so or {}).items():
        if isinstance(rec, dict) and rec.get("required") and rec.get("verdict") != "approve":
            add(jid, "awaiting-signoff")

    try:
        txt = Path(paths["escalations"]).read_text()
    except FileNotFoundError:
        txt = ""
    except OSError as e:
        raise SourceError(f"ESCALATIONS.md unreadable: {e}")
    for line in txt.splitlines():
        found = re.findall(H12, line)
        escalated.update(found)
        if line.lstrip().startswith("- [ ]"):
            for m in found:
                add(m, "open-escalation")
    return {"ids": ids, "paths": ppaths, "parked": parked, "live_bundles": live_bundles,
            "escalated": escalated, "why": why}


# ---------------------------------------------------------------- outcome classification
def _rd(p):
    try:
        d = json.loads(Path(p).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def outcome_of(jid, ql, prot):
    """-> (outcome, bundle). outcome 'failures' | 'successful' | None (unknown -> 'all')."""
    done = _rd(Path(ql) / f"{jid}.done.json")
    gate = _rd(Path(ql) / f"{jid}.gate.json")
    bundle = done.get("bundle") or None
    v = str(gate.get("verdict") or "").lower()
    rv = str(gate.get("review_verdict") or "").upper()
    st = str(done.get("status") or gate.get("job_status") or "").lower()
    tr = str(done.get("terminal_reason") or "").lower()
    if (v in ("fail", "concerns") or rv.startswith("FAIL") or st in ("failed", "error")
            or done.get("failure_class") or jid in prot["escalated"]
            or "park" in tr or "escalat" in tr
            or (bundle and bundle in prot["parked"])):
        return "failures", bundle
    if v == "pass" or st == "done":
        return "successful", bundle
    return None, bundle


# ---------------------------------------------------------------- planning
def _size(p, is_dir):
    if not is_dir:
        return os.lstat(p).st_size
    t = 0
    for r, _d, fs in os.walk(p):
        for f in fs:
            try:
                t += os.lstat(os.path.join(r, f)).st_size
            except OSError:
                pass
    return t


def _newest_mtime_and_clean(p):
    """(newest mtime, has_symlink) of a dir tree (lstat; never follows)."""
    m, sym = os.lstat(p).st_mtime, False
    for r, ds, fs in os.walk(p, followlinks=False):
        for n in ds + fs:
            try:
                s = os.lstat(os.path.join(r, n))
            except OSError:
                continue
            if os.path.islink(os.path.join(r, n)):
                sym = True
            m = max(m, s.st_mtime)
    return m, sym


def _pinned(cfg, jid, bundle, ql):
    for p in cfg.get("pins", []):
        if p.get("type") == "job" and p.get("id") == jid:
            return True
        if p.get("type") == "bundle" and bundle and p.get("id") == bundle:
            return True
    return bool(jid) and (Path(ql) / f"{jid}.keep").exists()


def plan(cfg, paths, now=None):
    """-> list of entry dicts (action 'delete' | 'keep'), plus protection info. No mutation."""
    now = now if now is not None else time.time()
    prot = protections(paths)
    entries, ql = [], paths["queue_logs"]
    meta = {}

    def meta_of(jid):
        if jid not in meta:
            meta[jid] = outcome_of(jid, ql, prot)
        return meta[jid]

    for key, rx, kind in ALLOWLIST:
        root = Path(paths[key])
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            m = rx.match(n)
            if not m:
                continue
            p = root / n
            try:
                st = os.lstat(p)
            except OSError:
                continue
            e = {"path": str(p), "root": str(root), "name": n, "kind": kind, "id": None,
                 "bytes": 0, "action": "keep", "reason": "", "level": "all", "days": None}
            if os.path.islink(p):
                e["reason"] = "symlink-refused"; entries.append(e); continue
            isdir = (kind == "dir")
            if isdir != os.path.isdir(p) or (not isdir and not os.path.isfile(p)):
                e["reason"] = "wrong-type"; entries.append(e); continue
            mt, sym = (_newest_mtime_and_clean(p) if isdir else (st.st_mtime, False))
            if sym:
                e["reason"] = "symlink-inside-refused"; entries.append(e); continue
            e["bytes"] = _size(p, isdir)
            e["mtime"] = mt
            jid = m.group(1) if kind in ("job", "side", "dir") else None
            e["id"] = jid
            if kind == "transcript":
                e["ts"] = m.group(1)
                e["reason"] = "pending-ref-check"      # resolved below
                entries.append(e)
                continue
            outc, bundle = meta_of(jid) if jid else (None, None)
            e["level"] = outc or "all"
            e["days"] = cfgmod.resolve_days(cfg, outc, ledger=(kind == "side"))
            if kind == "side":
                e["level"] = "ledger"
            if jid and _pinned(cfg, jid, bundle, ql):
                e["reason"] = "pinned"
            elif jid and jid in prot["ids"]:
                e["reason"] = "protected:" + prot["why"].get(jid, "ref")
            elif os.path.realpath(p) in prot["paths"]:
                e["reason"] = "protected:live-queue-path"
            elif now - mt < LIVE_GRACE_S:
                e["reason"] = "live-file(recent-write)"
            elif now - mt < e["days"] * 86400:
                e["reason"] = "within-retention"
            else:
                e["action"] = "delete"
            entries.append(e)

    # worker transcripts: deletable only when old AND no surviving file references them
    trs = [e for e in entries if e["kind"] == "transcript"]
    if trs:
        survivors = [e for e in entries if e["kind"] != "transcript" and e["action"] == "keep"]
        refs = {}   # ts -> set of job ids referencing it
        rxr = re.compile(rf"ollama-worker-logs/({TS})")
        scan = []
        for e in survivors:
            p = Path(e["path"])
            if e["kind"] == "dir":
                scan += [(p / f, e["id"]) for f in ("run.json", "task.json", "report.md") if (p / f).exists()]
            elif e["name"].endswith((".log", ".json", ".md")):
                scan.append((p, e["id"]))
        for sub in ("pending", "complete"):
            d = Path(paths["handoff"]) / sub
            if d.is_dir():
                scan += [(d / n, None) for n in os.listdir(d) if n.endswith(".md")]
        for p, jid in scan:
            try:
                with open(p, errors="replace") as f:
                    for line in f:
                        for t in rxr.findall(line):
                            refs.setdefault(t, set()).add(jid)
            except OSError:
                pass
        for pth in prot["paths"]:
            for t in rxr.findall(pth):
                refs.setdefault(t, set()).add(None)
        for e in trs:
            rj = refs.get(e["ts"])
            if rj:
                e["reason"] = "referenced-by-surviving-log"
                continue
            e["level"], e["days"] = "all", cfgmod.resolve_days(cfg, None)
            if now - e["mtime"] < LIVE_GRACE_S:
                e["reason"] = "live-file(recent-write)"
            elif now - e["mtime"] < e["days"] * 86400:
                e["reason"] = "within-retention"
            else:
                e["action"] = "delete"
    return entries, prot


# ---------------------------------------------------------------- deletion
def allowed(path, paths):
    """True only for a direct, non-symlink child of an allowlisted root whose NAME matches
    that root's pattern. The deletion gate; refuses everything else."""
    p = Path(path)
    if p.is_symlink():
        return False
    for key, rx, _k in ALLOWLIST:
        root = Path(paths[key])
        if p.parent == root and rx.match(p.name) and not root.is_symlink():
            return True
    return False


def safe_delete(path, paths, min_age_s=None):
    """Delete one allowlisted entry. Returns (ok, reason). Re-checks type and age at the
    moment of deletion (a writer may have touched it since planning)."""
    if not allowed(path, paths):
        return False, "refused:not-allowlisted"
    p = Path(path)
    try:
        isdir = p.is_dir()
        if isdir:
            m, sym = _newest_mtime_and_clean(p)
            if sym:
                return False, "refused:symlink-inside"
        else:
            m = os.lstat(p).st_mtime
        if min_age_s is not None and time.time() - m < min_age_s:
            return False, "skipped:touched-since-plan"
        if isdir:
            shutil.rmtree(p)
        else:
            os.unlink(p)
        return True, "deleted"
    except FileNotFoundError:
        return True, "already-gone"
    except OSError as e:
        return False, f"error:{e}"


def _zero():
    return {"count": 0, "bytes": 0}


def run(cfg, apply=False, paths=None, audit=True, now=None):
    paths = paths or make_paths()
    res = {"levels": {k: _zero() for k in ("all", "failures", "successful", "ledger")},
           "total": _zero(), "protected": _zero(), "pinned": _zero(), "errors": [],
           "skipped": {}, "applied": apply, "busy": False, "by_dir": {}}
    lock_f = None
    if apply:
        try:
            Path(paths["lock"]).parent.mkdir(parents=True, exist_ok=True)
            lock_f = open(paths["lock"], "w")
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            res["busy"] = True
            res["errors"].append("another log-rotate run holds the lock; exiting")
            _audit(res, paths, audit)
            return res
        except OSError as e:
            res["errors"].append(f"lock error: {e}")
    try:
        try:
            entries, prot = plan(cfg, paths, now=now)
        except SourceError as e:
            res["errors"].append(f"protection source unavailable, deleting NOTHING: {e}")
            _audit(res, paths, audit)
            return res
        except Exception as e:  # never crash
            res["errors"].append(f"plan failed: {type(e).__name__}: {e}")
            _audit(res, paths, audit)
            return res
        for e in entries:
            if e["action"] == "delete":
                lv = res["levels"].setdefault(e["level"], _zero())
                ok = True
                if apply:
                    ok, why = safe_delete(e["path"], paths, min_age_s=LIVE_GRACE_S)
                    if not ok:
                        res["skipped"][why] = res["skipped"].get(why, 0) + 1
                        continue
                lv["count"] += 1
                lv["bytes"] += e["bytes"]
                res["total"]["count"] += 1
                res["total"]["bytes"] += e["bytes"]
                d = res["by_dir"].setdefault(e["root"], _zero())
                d["count"] += 1
                d["bytes"] += e["bytes"]
            else:
                r = e["reason"].split(":")[0] if e["reason"].startswith("protected") else e["reason"]
                key = e["reason"] if e["reason"].startswith("protected") else r
                if e["reason"] in ("pinned",):
                    res["pinned"]["count"] += 1; res["pinned"]["bytes"] += e["bytes"]
                if e["reason"].startswith("protected") or e["reason"] == "referenced-by-surviving-log":
                    res["protected"]["count"] += 1; res["protected"]["bytes"] += e["bytes"]
                res["skipped"][key] = res["skipped"].get(key, 0) + 1
        res["entries"] = entries
        _audit(res, paths, audit)
        return res
    finally:
        if lock_f:
            try:
                fcntl.flock(lock_f, fcntl.LOCK_UN); lock_f.close()
            except OSError:
                pass


def _audit(res, paths, enabled):
    if not enabled:
        return
    sk = ",".join(f"{k}={v}" for k, v in sorted(res["skipped"].items())) or "-"
    line = (f"rotate mode={'apply' if res['applied'] else 'dry-run'} deleted={res['total']['count']} "
            f"freed_bytes={res['total']['bytes']} protected={res['protected']['count']} "
            f"pinned={res['pinned']['count']} skipped[{sk}]"
            + (f" errors={res['errors']}" if res["errors"] else ""))
    cfgmod.append_audit(line, home=paths["home"])
