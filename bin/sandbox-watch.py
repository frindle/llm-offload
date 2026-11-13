#!/usr/bin/env python3
"""sandbox-watch -- read-only health watcher for the Darkbloom sandbox add-on, plus a limited slot auto-fix.

Runs every 60 s (launchd/com.example.sandbox-watch.plist, not installed by the build). Reads LOCAL FILES ONLY: no
network, no database, no `darkbloom` command. What it reads (all under ~/DarkbloomSandbox/agent unless noted):

  status.json    updated_at / file mtime (stale?), jobs_running, mode ("accepting"), session ("connected"),
                 slots.{linux,macos}.{free,total}, disks[].state ("online"), version, any "reason" strings
  agent.log      NO timestamps. Parsed from its tail (last 1 MiB): `agent_starting`, `slot_needs_recovery profile=
                 slot= reason=`, `app_attest_key_unusable`, `session_proof_failed`, `session_established`,
                 `work_disk_offline` / `work_disk_online`, and `worker_diagnostic ... "event":"job_finished","at":
                 <epoch>` (the only timestamps; they give the usual gap between completed jobs)
  process.json   pid of the agent (liveness via kill -0)
  enroll.json    host_id (fallback state/enrollment.json): only a SALTED HASH is kept in the state file
  agent.json     disks[].images_root / images_root: the disk that holds the sandbox images (must exist)
  ~/.darkbloom/provider-run.json   Darkbloom provider "version" (grace after a self-update)

The owner's alert rule (2026-11-12): no alerts for what Claude or Darkbloom is doing, nor for a model that does not fit
in free memory. So: a maintenance marker (JSON {"until": epoch, "reason", "owner"}) holds every alert while
unexpired; a 30 min grace follows any Darkbloom or sandbox version change; a memory-fit cause suppresses. A
suppressed event is still logged through infra_incidents, only not paged. A page is sent once per condition until
it clears (repeat at most every 6 h), plus one short "cleared" note for conditions that paged.

Tier 3: slots need recovery AND status.json is fresh with 0 jobs -> port of sandbox-slot-recover.sh (TERM the
agent, MOVE the stale runtime.snapshot files to a backup dir, kickstart the agent), at most 2 tries per rolling
24 h, 30 min apart, never while a maintenance marker or version grace is active, its own marker set meanwhile; the owner is paged on failure or when the limit is hit. NEVER `darkbloom
sandbox setup`.

  sandbox-watch.py [--dry-run] [--sandbox-dir D] [--darkbloom-dir D] [--state F] [--marker F]
  sandbox-watch.py --set-maintenance SECONDS [REASON] [--owner NAME]   (called by sandbox-slots.sh before a restart)
  sandbox-watch.py --clear-maintenance

--dry-run reads and prints decisions only: writes nothing, pages nothing, fixes nothing.
Env: SW_SANDBOX_DIR, SW_DARKBLOOM_DIR, SW_STATE, SW_MARKER (the args win); INFRA_INCIDENTS_PATH as usual.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()

VERSION_GRACE_S = 30 * 60
REPEAT_PAGE_S = 6 * 3600
STATUS_STALE_S = 600          # status.json older than this: agent not writing it
FIX_STATUS_MAX_AGE_S = 120    # the fix needs a status.json at least this fresh (agent writes it about every 60 s)
SLOTS_STUCK_S = 600           # 0 free slots with 0 jobs for this long
AGENT_DOWN_S = 180
DISCONNECTED_S = 600
HOST_CHANGE_WINDOW_S = 24 * 3600
FIX_MAX_PER_DAY = 2
FIX_WINDOW_S = 24 * 3600
FIX_COOLDOWN_S = 30 * 60      # spacing between tries, so a failed try is not repeated a minute later
FIX_MARKER_S = 900
FIX_TAIL_MARKER_S = 300
LOG_TAIL_BYTES = 1 << 20
LOG_JOBS_BYTES = 8 << 20
MIN_JOBS_FOR_GAP = 10
NO_JOB_MIN_S = 6 * 3600
NO_JOB_P90_MULT = 3.0
MEMORY_RE = re.compile(r"(insufficient|not enough|no|low)[ _]+(free[ _]+)?memory|memory[_ ]pressure|does not fit|"
                       r"doesn'?t fit|exceeds (free |available )?memory|out of memory|\boom\b|no_memory", re.I)

TITLES = {
    "slots_recovery": "Sandbox slots need recovery",
    "slots_stuck": "Sandbox has no free slots",
    "host_id_changed": "Sandbox host ID changed",
    "app_attest": "Sandbox App Attest failing",
    "agent_down": "Sandbox agent is down",
    "status_stale": "Sandbox status.json is stale",
    "disconnected": "Sandbox agent is disconnected",
    "disk_offline": "Sandbox image disk offline",
    "no_jobs": "No sandbox job for a long time",
    "autofix_failed": "Sandbox slot auto-fix failed",
    "autofix_limit": "Sandbox slot auto-fix limit reached",
}
# conditions that the owner must see even though our own fix owns the marker (they are about the fix itself)
FIX_CONDS = ("autofix_failed", "autofix_limit")


# ---------------------------------------------------------------- small helpers
def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _tail(path, nbytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
        text = data.decode("utf-8", "replace")
        if size > nbytes:
            text = text.split("\n", 1)[-1]      # drop the cut first line
        return text
    except OSError:
        return ""


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _load_module(name, filename):
    sp = importlib.util.spec_from_file_location(name, HERE / filename)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def hash_host(salt, host_id):
    return hashlib.sha256((salt + "|" + str(host_id)).encode()).hexdigest()[:24]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return True if isinstance(pid, int) else False
    return True


class Cfg:
    def __init__(self, sandbox_dir=None, darkbloom_dir=None, state=None, marker=None):
        self.sandbox = Path(sandbox_dir or os.environ.get("SW_SANDBOX_DIR") or HOME / "DarkbloomSandbox")
        self.dark = Path(darkbloom_dir or os.environ.get("SW_DARKBLOOM_DIR") or HOME / ".darkbloom")
        self.state = Path(state or os.environ.get("SW_STATE") or self.dark / "sandbox-watch.state.json")
        self.marker = Path(marker or os.environ.get("SW_MARKER") or self.dark / "sandbox-maintenance.json")
        self.agent = self.sandbox / "agent"
        self.status = self.agent / "status.json"
        self.log = self.agent / "agent.log"
        self.controller = self.agent / "controller"
        self.binary = self.sandbox / "app/DarkbloomSandbox.app/Contents/MacOS/darkbloom-sandbox"
        self.backup_root = self.sandbox


# ---------------------------------------------------------------- maintenance marker
def read_marker(cfg, now):
    m = _load(cfg.marker)
    if isinstance(m, dict):
        try:
            if float(m.get("until", 0)) > now:
                return m
        except (TypeError, ValueError):
            pass
    return None


def set_marker(cfg, seconds, reason, owner="manual", now=None):
    now = time.time() if now is None else now
    cur = read_marker(cfg, now)
    until = now + seconds
    if cur:                                   # never shorten someone else's marker
        until = max(until, float(cur["until"]))
    cfg.marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.marker.with_suffix(".tmp")
    tmp.write_text(json.dumps({"until": until, "reason": reason, "owner": owner, "set_at": now}))
    os.replace(tmp, cfg.marker)


def clear_marker(cfg, owner=None):
    m = _load(cfg.marker)
    if owner and isinstance(m, dict) and m.get("owner") != owner:
        return
    try:
        cfg.marker.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- observation
def parse_log(text):
    """Returns the facts the conditions need from an agent.log tail (no timestamps in this log)."""
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if " agent_starting " in ln:
            start = i
    seg = lines[start:]
    recov = []
    for ln in seg:
        m = re.search(r"slot_needs_recovery profile=(\w+) slot=(\d+)", ln)
        if m:
            recov.append((m.group(1), int(m.group(2))))
    last = {}
    for i, ln in enumerate(lines):
        for key, pat in (("attest", ("app_attest_key_unusable", "session_proof_failed")),
                         ("established", (" session_established ",)),
                         ("disk_off", (" work_disk_offline ",)),
                         ("disk_on", (" work_disk_online ",))):
            if any(p in ln for p in pat):
                last[key] = i
    return {
        "recovery": sorted(set(recov)),
        "app_attest": "attest" in last and last["attest"] > last.get("established", -1),
        "disk_off": "disk_off" in last and last["disk_off"] > last.get("disk_on", -1),
        "memory_fit": any(MEMORY_RE.search(ln) for ln in lines[-100:]),
    }


def job_finish_times(text):
    out = []
    for m in re.finditer(r'"event":"job_finished","at":(\d+(?:\.\d+)?)', text):
        out.append(float(m.group(1)))
    return sorted(set(out))


def usual_gap(finishes):
    """Threshold (seconds) after which silence is 'well past the usual gap', or None if too few records."""
    if len(finishes) < MIN_JOBS_FOR_GAP:
        return None
    finishes = sorted(finishes)
    gaps = sorted(b - a for a, b in zip(finishes, finishes[1:]))
    p90 = gaps[min(len(gaps) - 1, int(0.9 * len(gaps)))]
    return max(NO_JOB_MIN_S, NO_JOB_P90_MULT * p90, 3 * statistics.median(gaps))


def free_slots(status):
    tot = 0
    for v in (status.get("slots") or {}).values():
        try:
            tot += int(v.get("free", 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return tot


def total_slots(status):
    tot = 0
    for v in (status.get("slots") or {}).values():
        try:
            tot += int(v.get("total", 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return tot


def observe(cfg, now, alive=pid_alive):
    status = _load(cfg.status)
    obs = {"status": status if isinstance(status, dict) else None}
    st = obs["status"]
    mt = _mtime(cfg.status)
    ts = None
    if st:
        try:
            ts = float(st.get("updated_at"))
        except (TypeError, ValueError):
            ts = None
    stamps = [x for x in (mt, ts) if x]
    obs["status_age"] = now - max(stamps) if stamps else None
    proc = _load(cfg.agent / "process.json") or {}
    obs["agent_pid"] = proc.get("pid")
    obs["agent_alive"] = alive(proc["pid"]) if isinstance(proc.get("pid"), int) else None
    tail = _tail(cfg.log, LOG_TAIL_BYTES)
    obs["log"] = parse_log(tail)
    obs["finishes"] = job_finish_times(_tail(cfg.log, LOG_JOBS_BYTES)) if cfg.log.exists() else []
    enroll = _load(cfg.agent / "enroll.json") or _load(cfg.agent / "state/enrollment.json") or {}
    obs["host_id"] = enroll.get("host_id")
    agent_json = _load(cfg.agent / "agent.json") or {}
    roots = [d.get("images_root") for d in (agent_json.get("disks") or []) if isinstance(d, dict)]
    obs["disk_paths_missing"] = [p for p in roots if p and not os.path.isdir(_disk_mount(p))]
    prov = _load(cfg.dark / "provider-run.json") or {}
    obs["versions"] = {"darkbloom": str(prov.get("version") or ""),
                       "sandbox": str((st or {}).get("version") or proc.get("version") or "")}
    reasons = []
    for blob in (st or {}).values():
        if isinstance(blob, dict) and isinstance(blob.get("reason"), str):
            reasons.append(blob["reason"])
    obs["memory_fit"] = obs["log"]["memory_fit"] or any(MEMORY_RE.search(r) for r in reasons)
    return obs


def _disk_mount(images_root):
    """The mount point of a disk-held path: /Volumes/<name> for a /Volumes path, else the path itself."""
    p = str(images_root)
    parts = Path(p).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(Path(*parts[:3]))
    return p


# ---------------------------------------------------------------- conditions
def evaluate(obs, state, now, cfg=None):
    """Returns {cond: detail} for every condition active now. Updates the 'since' trackers in state['since']."""
    st = obs["status"]
    since = state.setdefault("since", {})
    act = {}

    def track(key, on):
        if on:
            since.setdefault(key, now)
            return now - since[key]
        since.pop(key, None)
        return None

    # status.json missing or stale
    age = obs["status_age"]
    if st is None or age is None or age > STATUS_STALE_S:
        act["status_stale"] = "status.json %s" % ("missing/unreadable" if age is None else "is %d s old" % age)
    # agent process
    d = track("agent_down", obs["agent_alive"] is False)
    if d is not None and d >= AGENT_DOWN_S:
        act["agent_down"] = "process.json pid not running for %d s" % d
    fresh = st is not None and age is not None and age <= STATUS_STALE_S
    # connection
    disc = fresh and st.get("session") not in (None, "connected")
    d = track("disconnected", bool(disc))
    if d is not None and d >= DISCONNECTED_S:
        act["disconnected"] = "session=%s for %d s" % (st.get("session"), d)
    # slots needing recovery (agent.log since the last agent start)
    if obs["log"]["recovery"]:
        act["slots_recovery"] = "%d slot(s) report recoveryRequired" % len(obs["log"]["recovery"])
    # 0 free with 0 jobs
    stuck = bool(fresh and total_slots(st) > 0 and free_slots(st) == 0 and int(st.get("jobs_running") or 0) == 0
                 and st.get("mode") == "accepting")
    d = track("slots_stuck", stuck)
    if d is not None and d >= SLOTS_STUCK_S:
        act["slots_stuck"] = "0 free slots with 0 jobs for %d min" % (d // 60)
    # host id
    if obs["host_id"]:
        h = hash_host(state["salt"], obs["host_id"])
        old = state.get("host_hash")
        if old and old != h:
            state["host_changed_at"] = now
        state["host_hash"] = h
    hc = state.get("host_changed_at")
    if hc and now - hc < HOST_CHANGE_WINDOW_S:
        act["host_id_changed"] = "enroll.json host ID differs from the one seen before (hash only)"
    # app attest
    if obs["log"]["app_attest"]:
        act["app_attest"] = "App Attest failure in agent.log with no session since"
    # disk
    off = obs["log"]["disk_off"] or bool(obs["disk_paths_missing"])
    disks = (st or {}).get("disks") or []
    if fresh and any(isinstance(x, dict) and x.get("state") not in (None, "online") for x in disks):
        off = True
    if off:
        act["disk_offline"] = "work disk offline (log/status/mount check)"
    # no completed job
    thr = usual_gap(obs["finishes"])
    if fresh and thr and obs["finishes"] and st.get("mode") == "accepting" and st.get("session") == "connected" \
            and int(st.get("jobs_running") or 0) == 0 and free_slots(st) > 0 and obs["agent_alive"] is not False:
        silent = now - obs["finishes"][-1]
        if silent > thr:
            act["no_jobs"] = "no completed job for %.1f h (usual-gap threshold %.1f h)" % (silent / 3600, thr / 3600)
    return act


def suppression(cond, obs, state, now, cfg):
    """Why this condition must not page right now, or None. Fix-owned conditions ignore the marker/grace."""
    if cond in FIX_CONDS:
        return None
    m = read_marker(cfg, now)
    if m:
        return "maintenance marker (%s)" % m.get("reason", "set")
    ch = state.get("version_changed_at")
    if ch and now - ch < VERSION_GRACE_S:
        return "version-change grace (%s)" % state.get("version_changed_what", "version")
    if obs["memory_fit"] and cond in ("slots_stuck", "slots_recovery", "no_jobs"):
        return "model/job not fitting in free memory"
    if cond == "slots_recovery" and any(c in state.get("conds", {}) and state["conds"][c].get("active")
                                        for c in FIX_CONDS):
        return "handled by the auto-fix alerts"
    return None


def track_versions(obs, state, now):
    old = state.get("versions")
    cur = obs["versions"]
    if old is not None:
        changed = [k for k in cur if cur[k] and old.get(k) and cur[k] != old[k]]
        if changed:
            state["version_changed_at"] = now
            state["version_changed_what"] = ",".join(changed)
    state["versions"] = {k: (cur[k] or (old or {}).get(k, "")) for k in cur}


# ---------------------------------------------------------------- tier 3: the slot fix
class RealOps:
    """The only place that touches processes. Tests replace it."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.job = "gui/%d/io.darkbloom.sandbox.agent" % os.getuid()

    def agent_pid(self):
        r = subprocess.run(["pgrep", "-f", "^%s agent " % self.cfg.binary], capture_output=True, text=True, timeout=15)
        return (r.stdout.split() or [None])[0]

    def term_agent(self):
        subprocess.run(["pkill", "-TERM", "-f", "^%s agent " % self.cfg.binary], capture_output=True, timeout=15)

    def kickstart(self):
        subprocess.run(["launchctl", "kickstart", self.job], capture_output=True, timeout=30)

    def sleep(self, s):
        time.sleep(s)


def slot_dir(profile, slot):
    return ("macos-slot-%d" if profile.startswith("macos") else "slot-%d") % slot


def run_recovery(cfg, obs, ops, now, wait_term=30, wait_back=60, step=0.2):
    """Port of sandbox-slot-recover.sh. Returns (ok, message). Preconditions are re-checked here.

    The agent's LaunchAgent (KeepAlive, a /bin/sh wrapper that `open -W`s the app) relaunches it about a second after
    it exits, so the old pid is watched at `step` intervals and the snapshots are moved the moment it is gone. If a new
    agent is already up by then, nothing is moved (moving files under a running agent is what this must never do)."""
    st = _load(cfg.status)
    mt = _mtime(cfg.status)
    if not isinstance(st, dict) or mt is None or now - mt > FIX_STATUS_MAX_AGE_S:
        return False, "ABORT: status.json missing or stale"
    if st.get("jobs_running") != 0:
        return False, "ABORT: a job is running or jobs_running unreadable"
    pid = ops.agent_pid()
    if not pid:
        return False, "ABORT: no agent running"
    ops.term_agent()
    gone = False
    for _ in range(max(1, int(wait_term / step))):
        cur = ops.agent_pid()
        if not cur:
            gone = True
            break
        if cur != pid:
            return False, ("ABORT: launchd restarted the agent before the snapshots could be moved "
                           "(files untouched; agent now runs as a new process)")
        ops.sleep(step)
    if not gone:
        return False, "ABORT: agent still running %d s after TERM (files untouched)" % wait_term
    bk = cfg.backup_root / ("recovery-backup-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + "-auto")
    moved = []
    for prof, slot in obs["log"]["recovery"]:
        d = slot_dir(prof, slot)
        src = cfg.controller / d / "runtime.snapshot"
        if src.is_file():
            (bk / d).mkdir(parents=True, exist_ok=True)
            os.replace(src, bk / d / "runtime.snapshot")
            moved.append(d)
    ops.kickstart()
    new = None
    for _ in range(wait_back):
        new = ops.agent_pid()
        if new and new != pid:
            break
        new = None
        ops.sleep(1)
    if not new:
        return False, "ERROR: agent did not come back within %d s (moved %s to %s)" % (wait_back, moved, bk)
    return True, "agent restarted, moved %s to %s" % (moved, bk)


# ---------------------------------------------------------------- the tick
def tick(cfg, now=None, dry_run=False, notify=None, record=None, ops=None, alive=pid_alive, out=print):
    now = time.time() if now is None else now
    state = _load(cfg.state, {}) or {}
    if not isinstance(state, dict):
        state = {}
    first = "salt" not in state
    if first:
        state["salt"] = secrets.token_hex(16)
    state.setdefault("conds", {})
    state.setdefault("fixes", [])
    obs = observe(cfg, now, alive)
    track_versions(obs, state, now)
    act = evaluate(obs, state, now, cfg)
    decisions = []
    D = lambda s: (decisions.append(s), out(s))

    # --- tier 3 ------------------------------------------------------------------------------------------------
    state["fixes"] = [t for t in state["fixes"] if now - t < FIX_WINDOW_S]
    st = obs["status"]
    fresh_fix = st is not None and obs["status_age"] is not None and obs["status_age"] <= FIX_STATUS_MAX_AGE_S
    recov = "slots_recovery" in act
    if recov:
        why_not = None
        if not fresh_fix:
            why_not = "status.json not fresh enough (<=%d s)" % FIX_STATUS_MAX_AGE_S
        elif st.get("jobs_running") != 0:
            why_not = "jobs_running is not 0"
        elif obs["agent_alive"] is False:
            why_not = "agent not running"
        elif len(state["fixes"]) >= FIX_MAX_PER_DAY:
            why_not = "daily limit (%d) reached" % FIX_MAX_PER_DAY
            act["autofix_limit"] = why_not
        elif read_marker(cfg, now):
            why_not = "maintenance marker set (%s)" % read_marker(cfg, now).get("reason", "set")
        elif state.get("version_changed_at") and now - state["version_changed_at"] < VERSION_GRACE_S:
            why_not = "version-change grace"
        elif state["fixes"] and now - state["fixes"][-1] < FIX_COOLDOWN_S:
            why_not = "last try was %d min ago (wait %d min between tries)" % ((now - state["fixes"][-1]) // 60,
                                                                               FIX_COOLDOWN_S // 60)
        if why_not:
            D("autofix: refused: " + why_not)
        elif dry_run:
            D("autofix: would run the slot recovery now (%d/%d tries in 24 h)" % (len(state["fixes"]), FIX_MAX_PER_DAY))
        else:
            state["fixes"].append(now)
            set_marker(cfg, FIX_MARKER_S, "slot auto-fix", "autofix", now)
            _save(cfg, state)                      # the attempt counts even if we crash mid-way
            ok, msg = run_recovery(cfg, obs, ops or RealOps(cfg), now)
            D("autofix: " + msg)
            (record or _record)("sandbox-autofix", "sandbox-watch", ok=ok, detail=msg, tries_24h=len(state["fixes"]))
            if ok:
                set_marker(cfg, FIX_TAIL_MARKER_S, "slot auto-fix settling", "autofix", now)
            else:
                clear_marker(cfg, "autofix")       # the failure page must not be held by our own marker
                state["autofix_failed"] = True
    if state.get("autofix_failed"):
        if recov:
            act["autofix_failed"] = "the automatic slot recovery failed; slots still need recovery"
        else:
            state.pop("autofix_failed", None)
    if "autofix_limit" in act and "autofix_failed" in act:
        act.pop("autofix_limit")

    # --- paging ------------------------------------------------------------------------------------------------
    pager = notify or _notify
    rec = record or _record
    conds = state["conds"]
    for c in sorted(set(act) | set(conds)):
        cs = conds.get(c, {})
        if c in act:
            why = suppression(c, obs, state, now, cfg)
            if why:
                key = "%s|%s" % (c, why.split(" (")[0])
                D("%s: ACTIVE, suppressed: %s [%s]" % (c, why, act[c]))
                if cs.get("suppress_logged") != key:
                    if not dry_run:
                        rec("sandbox-alert-suppressed", "sandbox-watch", condition=c, reason=why, detail=act[c])
                    cs["suppress_logged"] = key
                cs["active"] = True
                conds[c] = cs
                continue
            cs.pop("suppress_logged", None)
            due = (not cs.get("paged_at")) or now - cs.get("last_page", 0) >= REPEAT_PAGE_S
            cs["active"] = True
            if cs.get("since") is None:
                cs["since"] = now
            if due:
                D("%s: ACTIVE, page%s [%s]" % (c, " (repeat)" if cs.get("paged_at") else "", act[c]))
                if not dry_run:
                    pager("Sandbox: " + TITLES[c], act[c] + ". Check ~/DarkbloomSandbox/agent/status.json and agent.log.")
                    rec("sandbox-alert", "sandbox-watch", condition=c, detail=act[c])
                    cs["paged_at"] = cs.get("paged_at") or now
                    cs["last_page"] = now
            else:
                D("%s: ACTIVE, already paged (next repeat in %d min)" % (c, (REPEAT_PAGE_S - (now - cs["last_page"])) // 60))
            conds[c] = cs
        else:
            if cs.get("paged_at"):
                if read_marker(cfg, now):
                    D("%s: cleared, cleared-note held (maintenance marker)" % c)
                    continue
                D("%s: CLEARED, send cleared note" % c)
                if not dry_run:
                    pager("Sandbox: cleared", TITLES.get(c, c) + " is clear again.")
                    rec("sandbox-alert-cleared", "sandbox-watch", condition=c)
            else:
                D("%s: inactive" % c) if c in conds else None
            if not dry_run:
                conds.pop(c, None)
    if not act:
        D("all clear (%d conditions checked)" % len(TITLES))
    if first and not dry_run:
        D("first run: baselines stored (host hash, versions)")
    if not dry_run:
        _save(cfg, state)
    return decisions


def _save(cfg, state):
    cfg.state.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg.state)


def _notify(title, message):
    try:
        return _load_module("notify_owner_sw", "notify-owner.py").notify(title, message)
    except Exception:
        return "none"


def _record(kind, component, **fields):
    try:
        return _load_module("infra_incidents_sw", "infra_incidents.py").record(kind, component, **fields)
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sandbox-dir")
    ap.add_argument("--darkbloom-dir")
    ap.add_argument("--state")
    ap.add_argument("--marker")
    ap.add_argument("--set-maintenance", type=int, metavar="SECONDS")
    ap.add_argument("--clear-maintenance", action="store_true")
    ap.add_argument("--owner", default="manual")
    ap.add_argument("reason", nargs="?", default="maintenance")
    a = ap.parse_args(argv)
    cfg = Cfg(a.sandbox_dir, a.darkbloom_dir, a.state, a.marker)
    if a.set_maintenance:
        if a.dry_run:
            print("dry-run: would set the maintenance marker for %d s" % a.set_maintenance)
        else:
            set_marker(cfg, a.set_maintenance, a.reason, a.owner)
        return 0
    if a.clear_maintenance:
        if not a.dry_run:
            clear_marker(cfg)
        return 0
    if a.dry_run:
        out = print
    else:   # launchd run every 60 s: timestamped, and no "all clear" line, so the log only grows when something happens
        def out(s):
            if not s.startswith("all clear"):
                print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), s, flush=True)
    tick(cfg, dry_run=a.dry_run, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
