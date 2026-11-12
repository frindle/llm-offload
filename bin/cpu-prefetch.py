#!/usr/bin/env python3
"""cpu-prefetch.py -- start the CPU-only stages of QUEUED work early, so the GPU never waits on them.

THE GAP (2026-10-09). Chain drivers (ollama-dispatch-auto) only advance when somebody launches them.
Everybody launched them reactively -- one at a time, after the previous piece finished -- so the
GPU lane drained and sat idle while the next piece did its scaffold / harness self-check / preflight /
relevance measurement (rt-walmart-cancel-import: ~35 min of harness-check + preflight before the job
could even be enqueued). a7c2ba3 already lets a bundle in a CPU-only step release its GPU lanes; what
was missing is the other direction: nothing started a driver for work that was merely QUEUED.

THE MECHANISM. A small separate watcher (launchd, one pass per minute; NO queue-daemon change, NO
restart) over an EXPLICIT backlog under ~/.ollama-dispatch/prefetch/backlog/<label>.json:

    cpu-prefetch.py add   --label L [--bundle B] [--priority N] -- <ollama-dispatch-auto argv ...>
    cpu-prefetch.py add   --label L --resume         # authored harness, driver gone: argv from
                                                     # auto-runs/argv/L.json + --resume-harness
    cpu-prefetch.py status | hold L | unhold L | cancel L | requeue L | --once [--dry-run]

Explicit, not discovered: argv records outlive their work (rows are pruned, chains end `exit 0`), so
"has an argv record" cannot tell an unfinished piece from a landed one; guessing would resurrect
finished work. An entry is the operator's/agent's statement "this is queued, start its CPU prep when
there is room".

Each pass launches (detached, same code path as dispatch-self-heal.resume_auto_driver) the driver of
as many entries as the GATES allow. Per entry, ALL must hold or it is skipped with a named reason:
  * kill switch: ~/.ollama-dispatch/cpu-prefetch.disabled absent; entry not `hold`; label not in holds.txt
  * not a held / parked chain: bundle not in the queue's _bundle_parked (except the benign cpu_wait /
    yielded parks), no needs_opus or user_hold/fit_hold row, no unchecked `label` row in
    READY-TO-LAND.md / ESCALATIONS.md (HARNESS GO held for review), not superseded/accepted
  * kind=resume only: harness authored (TASK.md + refimpl.py + verify.sh) in the recorded worktree.
    (kind=start launches the driver from scratch; the driver itself scaffolds, authors on the GPU
    through the queue, then does its CPU stages as ever.)
  * tree safety: the worktree's dispatch-tree.lock is not held, no .hand-harness, no live queue row
    (running/pending/paused/needs_opus) with cwd == worktree, the bundle is not a slicer plan with
    unfinished slices (the slicer owns it)
  * idempotent: no live driver for the label (ps ground truth on `--label L` AND the chain record,
    pid-reuse safe: the pid's command must be ollama-dispatch-auto), flock around decide+launch+ledger,
    an entry launches ONCE (re-launch only through `requeue`).
BOUNDS. slots = min(max_prep - drivers_advancing, target_ready - (ready_bundles + drivers_advancing)),
and none when the Mac's 1-min load >= load_frac * ncpu or the CPU lane is saturated (running+pending
remote jobs >= lane_busy_max). ready_bundles = bundles with a running / runnable-pending MODEL row,
i.e. what the GPU can start right now. So prep runs only while the GPU's runway is short, never
more than max_prep at once, and the GPU itself is never touched: if prep is still running when the
GPU frees up, the queue backfills with whatever IS ready (existing behaviour) -- nothing here blocks.
ORDER = when the GPU will need them: entries of a bundle the queue already resumes first (parked
cpu_wait/yielded) or has committed come first, then kind=resume (GPU authoring already spent), then
priority (higher first), then age (FIFO) -- the same order the queue's depth-first launch loop will
pick their rows in.

Every decision is appended to prefetch/decisions.jsonl. Seams are patchable for tests (module level
functions; state dirs via env CPU_PREFETCH_DIR / OLLAMA_DISPATCH_DIR / OLLAMA_DISPATCH_AUTO_RUNS_DIR /
OLLAMA_QUEUE_STATE).
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
DISPATCH = Path(os.environ.get("OLLAMA_DISPATCH_DIR") or HOME / ".ollama-dispatch")
PDIR = Path(os.environ.get("CPU_PREFETCH_DIR") or DISPATCH / "prefetch")
RUNS = Path(os.environ.get("OLLAMA_DISPATCH_AUTO_RUNS_DIR") or DISPATCH / "auto-runs")
QUEUE_STATE = Path(os.environ.get("OLLAMA_QUEUE_STATE") or HOME / "bin" / "ollama-queue-state.json")
AUTO = Path(os.environ.get("CPU_PREFETCH_AUTO") or HERE / "ollama-dispatch-auto")
KILL_FILE = DISPATCH / "cpu-prefetch.disabled"
DEFAULTS = {"max_prep": 2, "target_ready": 3, "load_frac": 0.75, "lane_busy_max": 2,
            "spawn_grace_s": 600}
BENIGN_PARKS = ("cpu_wait", "yielded")
HARNESS_FILES = ("TASK.md", "refimpl.py", "verify.sh")
LIVE_ROW = ("running", "pending", "paused", "needs_opus")
# The DRIVER process: `[interpreter [-flags]] [dir/]ollama-dispatch-auto ...` -- the script must be what
# is being EXECUTED. A bare substring match also hit any shell/grep/tail whose command line merely
# mentioned the tool and a label (found 2026-10-09: the agent shell that registered an entry blocked its
# own launch as "a driver is already alive").
DRIVER_RE = re.compile(r"^\s*(?:\S*/)?(?:[Pp]ython[\d.]*(?:\s+-\S+)*\s+)?(?:\S*/)?ollama-dispatch-auto(?:\s|$)")


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def config():
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (_read_json(PDIR / "config.json", {}) or {}).items() if k in DEFAULTS})
    return c


# ----------------------------------------------------------------------------- backlog store
def safe(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or "")).strip("-")


def entry_path(label):
    return PDIR / "backlog" / (safe(label) + ".json")


def load_entries():
    out = []
    d = PDIR / "backlog"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            e = _read_json(f)
            if isinstance(e, dict) and e.get("label") and e.get("argv"):
                out.append(e)
    return out


def argv_label(argv):
    for i, a in enumerate(argv):
        if a == "--label" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--label="):
            return a.split("=", 1)[1]
    return None


def argv_opt(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def add_entry(label, argv=None, kind="start", bundle=None, priority=0, cwd=None, worktree=None):
    """Register work. kind=resume takes argv/worktree/bundle from the driver's own argv record."""
    if kind == "resume":
        rec = _read_json(RUNS / "argv" / (safe(label) + ".json"))
        if not isinstance(rec, dict) or not rec.get("argv"):
            raise SystemExit("no recorded argv for %s (auto-runs/argv/%s.json)" % (label, safe(label)))
        argv = [x for x in rec["argv"] if x != "--resume-harness"] + ["--resume-harness"]
        bundle = bundle or rec.get("bundle")
        worktree = worktree or rec.get("worktree")
        cwd = cwd or rec.get("cwd")
    argv = list(argv or [])
    if not argv:
        raise SystemExit("no driver argv given (put it after --)")
    if argv_label(argv) not in (None, label):
        raise SystemExit("--label %s does not match the argv's --label %s" % (label, argv_label(argv)))
    if argv_label(argv) is None:
        argv += ["--label", label]
    e = {"label": label, "bundle": bundle or argv_opt(argv, "--bundle") or label, "kind": kind,
         "argv": argv, "cwd": cwd or str(HOME / "bin"), "priority": int(priority),
         "worktree": worktree, "added_at": now_iso(), "hold": False, "status": "queued",
         "launches": []}
    p = entry_path(label)
    old = _read_json(p)
    if isinstance(old, dict) and old.get("status") in ("queued", "launched"):
        raise SystemExit("%s is already %s in the backlog" % (label, old["status"]))
    _write_json(p, e)
    return e


TERMINAL = ("done", "failed", "cancelled")


def register(label, argv=None, kind="start", bundle=None, priority=0, source="manual", force=False, **kw):
    """The ONE registration entry point for automatic callers (ollama-dispatch-auto --prefetch,
    dispatch-self-heal). Idempotent and failure-tolerant: NEVER raises, returns a status string.
      registered   new entry written
      already:<s>  a queued/launched entry exists (second call is a no-op)
      skip:<s>     entry already finished (done/failed/cancelled): a finished entry is never resurrected
                   by an automatic caller -- only `requeue` (explicit) or force=True re-arms it
      error:<why>  could not register (caller must carry on)
    The launch-time gates in block_reason stay the single authority on whether it may START."""
    try:
        old = _read_json(entry_path(label))
        if isinstance(old, dict):
            st = old.get("status")
            if st in ("queued", "launched"):
                return "already:%s" % st
            if st in TERMINAL and not force:
                return "skip:%s" % st
        e = add_entry(label, argv, kind, bundle, priority, **kw)
        e["source"] = source
        _write_json(entry_path(label), e)
        log_decision({"label": label, "action": "registered", "why": "%s kind=%s bundle=%s"
                      % (source, kind, e.get("bundle"))})
        return "registered"
    except BaseException as ex:    # SystemExit from add_entry's validation included
        if isinstance(ex, KeyboardInterrupt):
            raise
        return "error:%s" % (ex if isinstance(ex, SystemExit) else "%s: %s" % (type(ex).__name__, ex))


def candidates():
    """Read-only advisory: argv-recorded runs whose authored harness sits in an existing worktree, with
    no live driver, no backlog entry and a chain record that did not end clean (exit 0). Nothing is
    registered from this list automatically (a record outliving its work is exactly how landed work
    would be resurrected); the operator vets it and runs `add --label L --resume`."""
    rows, out = ps_rows() or [], []
    have = {e["label"] for e in load_entries()}
    for f in sorted((RUNS / "argv").glob("*.json")) if (RUNS / "argv").is_dir() else []:
        r = _read_json(f)
        if not isinstance(r, dict) or not r.get("label") or r["label"] in have:
            continue
        wt = r.get("worktree")
        if not wt or not all((Path(wt) / n).exists() for n in HARNESS_FILES):
            continue
        ch = _read_json(RUNS / (safe(r.get("bundle") or r["label"]) + ".json")) or {}
        rec = (ch.get("runs") or {}).get(r["label"]) or ch
        if str(rec.get("outcome") or "") == "exit 0" or live_driver(r["label"], r.get("bundle"), rows):
            continue
        out.append(r["label"])
    return out


def set_entry(label, **kw):
    p = entry_path(label)
    e = _read_json(p)
    if not isinstance(e, dict):
        raise SystemExit("no backlog entry %s" % label)
    e.update(kw)
    _write_json(p, e)
    return e


# ----------------------------------------------------------------------------- process / driver liveness
def ps_rows():
    """[(pid, lstart, command)] of every process (ground truth for 'is a driver alive')."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return None
    rows = []
    for ln in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)$", ln)
        if m:
            rows.append((int(m.group(1)), m.group(2), m.group(3)))
    return rows


def _cmd_is_driver(cmd, label=None):
    if not DRIVER_RE.match(cmd or ""):
        return False
    if label is None:
        return True
    return re.search(r"--label[ =]" + re.escape(label) + r"(\s|$)", cmd) is not None


def driver_pids(label, rows):
    return [p for p, _s, c in (rows or []) if _cmd_is_driver(c, label)]


def pid_is_driver(pid, rows, label=None):
    """pid-reuse safe: the pid must be alive AND its command must still be a dispatch-auto driver."""
    for p, _s, c in rows or []:
        if p == pid:
            return _cmd_is_driver(c, label)
    return False


def chain_runs():
    """[(run_record)] for every run in auto-runs/<key>.json (shared multi-run shape + legacy)."""
    out = []
    if not RUNS.is_dir():
        return out
    for f in RUNS.glob("*.json"):
        if f.name.endswith(".attempts.json"):
            continue
        d = _read_json(f)
        if not isinstance(d, dict):
            continue
        runs = d.get("runs") if isinstance(d.get("runs"), dict) else {}
        recs = list(runs.values()) if runs else [d]
        out += [r for r in recs if isinstance(r, dict) and r.get("label")]
    return out


def drivers_advancing(rows, entries=(), now=None, grace=600):
    """Drivers that are doing LOCAL work right now (phase `advancing`, pid alive and really a driver),
    plus drivers we launched within `grace` seconds that have not written a record yet."""
    now = time.time() if now is None else now
    recs = chain_runs()
    seen = {r["label"] for r in recs if r.get("phase") == "advancing" and r.get("pid")
            and pid_is_driver(int(r["pid"]), rows)}
    waiting = {r["label"] for r in recs if r.get("phase") == "waiting" and r.get("pid")
               and pid_is_driver(int(r["pid"]), rows)}
    for e in entries:
        last = (e.get("launches") or [{}])[-1]
        if (e.get("status") == "launched" and e["label"] not in waiting
                and now - float(last.get("t") or 0) < grace
                and pid_is_driver(int(last.get("pid") or 0), rows, e["label"])):
            seen.add(e["label"])      # spawned moments ago, no chain record yet: it is prepping
    return seen


def live_driver(label, bundle, rows):
    """True when a dispatch-auto driver for `label` is alive: ps ground truth OR a live chain record."""
    if driver_pids(label, rows):
        return True
    for r in chain_runs():
        if r.get("label") == label and r.get("phase") != "ended" and r.get("pid") \
                and pid_is_driver(int(r["pid"]), rows):
            return True
    return False


# ----------------------------------------------------------------------------- queue view
def group_key(j):
    b = j.get("bundle")
    if b:
        return b
    lbl = str(j.get("label") or "")
    for p in ("auto-author-", "auto-refine-", "needs-opus-auto-", "gate-", "regate-"):
        if lbl.startswith(p):
            lbl = lbl[len(p):]
    lbl = re.sub(r"\s*\[auto-fix-r\d+\]$", "", lbl)
    return re.sub(r"-(r|c)\d+$", "", lbl)


def read_queue():
    st = _read_json(QUEUE_STATE)
    if not isinstance(st, dict):
        return None
    jobs = st.get("jobs") or []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return {"jobs": jobs, "parked": st.get("_bundle_parked") or {}, "commit": st.get("_bundle_commit") or {}}


def _gpu_exclusive(j):
    return j.get("lane") == "gpu-exclusive" or j.get("model") == "gpu-exclusive"


def ready_bundles(jobs):
    """Bundles whose MODEL row the GPU can start (or is running) right now -- the runway."""
    out = set()
    for j in jobs:
        if _gpu_exclusive(j) or j.get("user_hold") or j.get("fit_hold"):
            continue
        if j.get("status") == "running" or j.get("status") == "pending":
            out.add(group_key(j))
    return out


def open_index_rows(keys):
    """Unchecked `- [ ] `key`` rows (held HARNESS GO / READY-TO-LAND / escalations) naming a key."""
    hit = set()
    for fn in ("READY-TO-LAND.md", "ESCALATIONS.md"):
        try:
            for ln in (DISPATCH / "escalations" / fn).read_text().splitlines():
                m = re.match(r"\s*-\s\[ \]\s+`([^`]+)`", ln)
                if m and m.group(1) in keys:
                    hit.add(m.group(1))
        except OSError:
            pass
    return hit


def holds_file():
    try:
        return {l.strip() for l in (PDIR / "holds.txt").read_text().splitlines()
                if l.strip() and not l.startswith("#")}
    except OSError:
        return set()


def tree_lock_held(wt):
    """True when somebody holds the worktree's dispatch-tree.lock (non-blocking probe, released at once)."""
    try:
        g = subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"], capture_output=True,
                           text=True, timeout=15)
        if g.returncode != 0 or not g.stdout.strip():
            return False
        lp = Path(g.stdout.strip()) / "dispatch-tree.lock"
        fh = open(lp, "a+")
    except Exception:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def slicer_owns(bundle):
    d = _read_json(DISPATCH / "slice-runs" / (safe(bundle) + ".json"))
    if not isinstance(d, dict):
        return False
    sl = d.get("slices") or {}
    sts = [str((v or {}).get("status") if isinstance(v, dict) else v) for v in
           (sl.values() if isinstance(sl, dict) else [])]
    return any(s not in ("done", "skipped", "dropped", "cancelled") for s in sts)


def superseded(keys):
    d = _read_json(DISPATCH / "bundle-superseded.json", {}) or {}
    return {k for k in keys if k in d}


def block_reason(e, view, rows, holds=None, helpers=None):
    """None when the entry may start now, else the named reason it must not."""
    h = helpers or {}
    label, bundle = e["label"], e.get("bundle") or e["label"]
    keys = {label, bundle}
    if e.get("hold"):
        return "entry on hold"
    if keys & (holds if holds is not None else holds_file()):
        return "listed in holds.txt"
    pk = view["parked"]
    for k in keys:
        pv = pk.get(k)
        if pv and pv.get("kind") not in BENIGN_PARKS:
            return "bundle %s is parked (%s)" % (k, pv.get("kind"))
    wt = e.get("worktree")
    for j in view["jobs"]:
        if group_key(j) in keys or str(j.get("label") or "") in keys:
            if j.get("status") == "needs_opus":
                return "bundle has a needs_opus row (%s)" % j.get("id")
            if j.get("user_hold") or j.get("fit_hold"):
                return "bundle has a human-held row (%s)" % j.get("id")
        if wt and j.get("cwd") == wt and j.get("status") in LIVE_ROW:
            return "a live queue row (%s %s) owns the worktree" % (j.get("id"), j.get("status"))
    hit = (h.get("open_index_rows") or open_index_rows)(keys)
    if hit:
        return "held/open index row for %s (READY-TO-LAND / ESCALATIONS)" % sorted(hit)[0]
    if (h.get("superseded") or superseded)(keys):
        return "bundle superseded/accepted"
    if (h.get("slicer_owns") or slicer_owns)(bundle):
        return "a slice plan owns this bundle (the slicer advances it)"
    if live_driver(label, bundle, rows):
        return "a driver for this label is already alive"
    if wt and Path(wt).is_dir():
        if (Path(wt) / ".hand-harness").exists():
            return "worktree is hand-locked (.hand-harness)"
        if (h.get("tree_lock_held") or tree_lock_held)(wt):
            return "worktree tree lock is held by another process"
    if e.get("kind") == "resume":
        if not wt or not Path(wt).is_dir():
            return "resume: worktree missing"
        miss = [f for f in HARNESS_FILES if not (Path(wt) / f).exists()]
        if miss:
            return "resume: harness not authored yet (missing %s)" % ",".join(miss)
    return None


def order_key(e, view):
    pk, cm = view["parked"], (view["commit"] or {}).get("key")
    b = e.get("bundle") or e["label"]
    first = 0 if (b == cm or (pk.get(b) or {}).get("kind") in BENIGN_PARKS) else 1
    return (first, 0 if e.get("kind") == "resume" else 1, -int(e.get("priority") or 0),
            e.get("added_at") or "", e["label"])


def headroom(cfg, load=None, ncpu=None, lane=None):
    """(ok, why): the Mac has CPU to spare and the Unraid CPU lane is not saturated."""
    try:
        load = os.getloadavg()[0] if load is None else load
        ncpu = ncpu or os.cpu_count() or 1
    except OSError:
        load, ncpu = 0.0, 1
    if load >= cfg["load_frac"] * ncpu:
        return False, "Mac load %.1f >= %.0f%% of %d cpus" % (load, 100 * cfg["load_frac"], ncpu)
    if lane is None:
        lane = lane_busy()
    if lane >= cfg["lane_busy_max"]:
        return False, "CPU lane busy (%d running+pending remote stages)" % lane
    return True, "ok"


def lane_busy():
    try:
        sys.path.insert(0, str(HERE))
        import cpu_lane
        s = cpu_lane.Store(cpu_lane.BASE).summary()
        return len(s.get("running") or []) + int(s.get("queue_depth") or len(s.get("pending") or []))
    except Exception:
        return 0


def compute_slots(cfg, n_ready, n_advancing):
    return max(0, min(cfg["max_prep"] - n_advancing, cfg["target_ready"] - (n_ready + n_advancing)))


# ----------------------------------------------------------------------------- launch + pass
def log_decision(rec):
    rec = dict(rec, t=now_iso())
    try:
        PDIR.mkdir(parents=True, exist_ok=True)
        with open(PDIR / "decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def spawn(cmd, cwd, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a")
    p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    return p.pid


def reconcile(entries, rows):
    """A launched entry whose driver is gone becomes done/failed (never re-launched automatically)."""
    for e in entries:
        if e.get("status") != "launched":
            continue
        if live_driver(e["label"], e.get("bundle"), rows):
            continue
        last = (e.get("launches") or [{}])[-1]
        if time.time() - float(last.get("t") or 0) < 120:      # give a fresh spawn time to show in ps
            continue
        rec = [r for r in chain_runs() if r.get("label") == e["label"]]
        ok = bool(rec) and all(str(r.get("outcome") or "").endswith("exit 0") for r in rec if r.get("phase") == "ended")
        e["status"] = "done" if ok else "failed"
        e["finished_at"] = now_iso()
        _write_json(entry_path(e["label"]), e)
        log_decision({"label": e["label"], "action": "reconcile", "status": e["status"]})


def run_pass(dry_run=False, launch=None, rows=None, view=None, cfg=None, helpers=None, load=None, lane=None):
    """One scheduler pass. Returns [(label, action, reason)]. `launch(cmd, cwd, log)->pid` is injectable."""
    cfg = cfg or config()
    out = []
    if KILL_FILE.exists():
        return [("*", "skip", "kill switch %s" % KILL_FILE)]
    rows = ps_rows() if rows is None else rows
    view = view if view is not None else read_queue()
    if rows is None or view is None:
        return [("*", "skip", "fail closed: cannot read %s" % ("process table" if rows is None else "queue state"))]
    PDIR.mkdir(parents=True, exist_ok=True)
    with open(PDIR / "launch.lock", "a+") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [("*", "skip", "another pass holds launch.lock")]
        try:
            entries = load_entries()
            reconcile(entries, rows)
            queued = [e for e in entries if e.get("status") == "queued"]
            adv = drivers_advancing(rows, entries, grace=cfg["spawn_grace_s"])
            ready = ready_bundles(view["jobs"])
            slots = compute_slots(cfg, len(ready), len(adv))
            ok, why = headroom(cfg, load=load, lane=lane)
            for e in sorted(queued, key=lambda x: order_key(x, view)):
                r = block_reason(e, view, rows, helpers=helpers)
                if r:
                    out.append((e["label"], "skip", r))
                    continue
                if not ok:
                    out.append((e["label"], "wait", why))
                    continue
                if slots <= 0:
                    out.append((e["label"], "wait", "no slot (ready=%d advancing=%d max_prep=%d target_ready=%d)"
                                % (len(ready), len(adv), cfg["max_prep"], cfg["target_ready"])))
                    continue
                if dry_run:
                    out.append((e["label"], "would-launch", "slots=%d" % slots))
                    slots -= 1
                    continue
                cmd = [sys.executable, str(AUTO)] + list(e["argv"])
                logp = RUNS / "logs" / ("%s-prefetch-%s.log" % (safe(e["label"]), time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
                cwd = e.get("cwd") if e.get("cwd") and Path(e["cwd"]).is_dir() else str(HOME)
                pid = (launch or spawn)(cmd, cwd, logp)
                e["status"] = "launched"
                e.setdefault("launches", []).append({"pid": pid, "t": time.time(), "at": now_iso(), "log": str(logp)})
                _write_json(entry_path(e["label"]), e)
                slots -= 1
                out.append((e["label"], "launched", "pid %s log %s" % (pid, logp)))
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    for lbl, act, why in out:
        if act != "skip" or why not in ("entry on hold",):
            log_decision({"label": lbl, "action": act, "why": why})
    return out


# ----------------------------------------------------------------------------- CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    pa = sub.add_parser("add")
    pa.add_argument("--label", required=True)
    pa.add_argument("--bundle")
    pa.add_argument("--priority", type=int, default=0)
    pa.add_argument("--resume", action="store_true")
    for n in ("hold", "unhold", "cancel", "requeue"):
        sub.add_parser(n).add_argument("label")
    sub.add_parser("status")
    sub.add_parser("candidates")
    po = sub.add_parser("once")
    po.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "add":
        e = add_entry(a.label, tail, "resume" if a.resume else "start", a.bundle, a.priority)
        print("queued %s (%s) bundle=%s" % (e["label"], e["kind"], e["bundle"]))
    elif a.cmd in ("hold", "unhold"):
        set_entry(a.label, hold=(a.cmd == "hold")); print(a.cmd, a.label)
    elif a.cmd == "cancel":
        set_entry(a.label, status="cancelled"); print("cancelled", a.label)
    elif a.cmd == "requeue":
        set_entry(a.label, status="queued"); print("requeued", a.label)
    elif a.cmd == "candidates":
        print("# ADVISORY: records outlive their work -- many of these have landed. Vet each, then "
              "`add --label L --resume`.", file=sys.stderr)
        for l in candidates():
            print(l)
    elif a.cmd == "status":
        for e in load_entries():
            print("%-9s %-6s p=%-3s %-40s hold=%s" % (e["status"], e["kind"], e.get("priority"), e["label"], e.get("hold")))
        cfg, rows, v = config(), ps_rows(), read_queue()
        if rows is not None and v is not None:
            ready, adv = ready_bundles(v["jobs"]), drivers_advancing(rows, load_entries(), grace=cfg["spawn_grace_s"])
            print("runway: %d ready bundle(s) %s | prepping: %d %s | slots now: %d | headroom: %s"
                  % (len(ready), sorted(ready)[:6], len(adv), sorted(adv)[:6],
                     compute_slots(cfg, len(ready), len(adv)), headroom(cfg)))
        print("kill switch:", "ON" if KILL_FILE.exists() else "off", "| config:", cfg)
    else:
        dry = a.dry_run or getattr(a, "dry_run", False)
        for lbl, act, why in run_pass(dry_run=dry):
            print("%-13s %-40s %s" % (act, lbl, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
