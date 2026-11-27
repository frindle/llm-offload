#!/usr/bin/env python3
"""darkbloom-catalog-watch.py -- 6-hourly Darkbloom catalog watcher (plan: ~/Desktop/Claude Plans/Darkbloom
Catalog Watch Plan.md; owner decisions 2026-11-26 override its approval phase).

  darkbloom-catalog-watch.py             # dry run (default): reads only, prints what it would do, writes nothing
  darkbloom-catalog-watch.py --apply     # acts

Rules (owner, 2026-11-26):
  * A model that is NEW in the coordinator catalog (`darkbloom models catalog --json`) is downloaded
    (`darkbloom models download <id>`: fetches weights only; it never runs `start`, so the served set and
    the provider are untouched) and enabled in BloomGauge (`bloomctl model add <id>`, the update-plan path).
  * Every downloaded model (`darkbloom models list --all --json`) is always enabled in BloomGauge, including
    models already on disk today. "Enabled" is only plan membership: BloomGauge's picker, the lease, the cold
    gate and the 900 s restart floor still decide what runs. This script never starts, stops, restarts,
    switches or evicts anything, never deletes weights, never touches auto-update, never reads local.json.
  * Tombstone: a model that was in the BloomGauge plan on the last run and is gone now, by a change this
    script did not make (it never removes anything), is recorded `owner_removed` and never added or enabled
    again. It reappearing in the plan (a manual add) clears the tombstone. The served set (provider.toml
    enabled_models) is rewritten by the manager all day, so it is not used as an owner signal.
  * Debounce: a model counts only after it is seen in two consecutive runs. The first run seeds the manifest
    and acts on nothing. Fetch errors or an empty catalog never overwrite the manifest.
  * Nothing is done while the queue lease is HELD/DRAIN/HANDBACK or a queue job is running (deferred).
  * Disk guard: download only if free >= weights * DISK_FACTOR + DISK_FLOOR_GB on the cache volume.
  * A failed add is `blocked`, alerted once per failure, retried at most daily.
  * Alerts (apply mode): once per new model, per add, per failure, through ollama-queue's _control_alert
    (ESCALATIONS.md row + desktop notification) via bloomgauge_lease_guard.queue_alert.

Env overrides (tests): DBWATCH_MANIFEST, DBWATCH_LOCK, DBWATCH_CATALOG_CMD, DBWATCH_LIST_CMD,
DBWATCH_DOWNLOAD_CMD (model id appended), DBWATCH_BLOOMCTL (command prefix; "--json" and the subcommand are
appended), DBWATCH_FREE_GB, DBWATCH_CACHE_DIR, DBWATCH_LEASE_FILE, DBWATCH_QUEUE_STATE, DBWATCH_NOTIFY_LOG
(append alerts there as JSON lines instead of delivering), DBWATCH_TOML, DBWATCH_NOW, DBWATCH_DISK_FLOOR_GB,
DBWATCH_RETRY_S.
"""
import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
E = os.environ.get
HERE = str(Path(__file__).resolve().parent)

DISK_FACTOR = 1.3                                   # free space must cover weights * this ...
DISK_FLOOR_GB = float(E("DBWATCH_DISK_FLOOR_GB", "50"))   # ... plus this many GB
CONFIRM_RUNS = 2                                    # consecutive runs a model must be seen
RETRY_S = float(E("DBWATCH_RETRY_S", "86400"))      # a blocked model is retried at most this often
DOWNLOAD_TIMEOUT_S = 4 * 3600
CMD_TIMEOUT_S = 120
DEFAULT_CACHE = "/Volumes/NVMe-Models/hf-cache"

MANIFEST = Path(E("DBWATCH_MANIFEST", str(HOME / ".darkbloom/catalog-manifest.json")))
LOCK = Path(E("DBWATCH_LOCK", str(HOME / ".darkbloom/catalog-watch.lock")))
DARKBLOOM = str(HOME / ".darkbloom/bin/darkbloom")
TOML = Path(E("DBWATCH_TOML", str(HOME / ".config/darkbloom/provider.toml")))
BG_DIRS = [HOME / "Library/Application Support" / n for n in ("Bloom Dashboard Beta", "Bloom Dashboard", "BloomGauge Beta")]
BLOOMCTL_CANDIDATES = [
    HOME / "Desktop/GitHub Projects.nosync/bloomgauge-wt-modeladd/native/bloomctl.py",   # model add/set (PR bloomctl-model-add)
    HOME / "Desktop/GitHub Projects.nosync/bloomgauge/native/bloomctl.py",               # status / model list only
]


class Abort(Exception):
    """A read failed; nothing is written."""


def now_ts():
    return float(E("DBWATCH_NOW") or time.time())


def say(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------------------------- IO

def run_cmd(argv, timeout=CMD_TIMEOUT_S):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def cmd_for(env_name, default):
    raw = E(env_name)
    return shlex.split(raw) if raw else list(default)


def fetch_catalog():
    """-> {id: {size_gb, min_ram_gb, hf}} of active catalog models. Raises Abort on error or empty."""
    argv = cmd_for("DBWATCH_CATALOG_CMD", [DARKBLOOM, "models", "catalog", "--json"])
    try:
        rc, out, err = run_cmd(argv)
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise Abort("catalog fetch failed: %s" % type(e).__name__)
    if rc != 0 or not isinstance(data, list):
        raise Abort("catalog fetch failed (exit %s)" % rc)
    cat = {}
    for m in data:
        if not isinstance(m, dict) or not m.get("id") or m.get("active") is False:
            continue
        size = m.get("size_gb")
        if size is None and m.get("total_size_bytes"):
            size = m["total_size_bytes"] / 1e9
        hf = (m.get("hugging_face_artifact") or {}).get("repo_id")
        cat[m["id"]] = {"size_gb": size, "min_ram_gb": m.get("min_ram_gb"), "hf": hf}
    if not cat:
        raise Abort("catalog is empty")
    return cat


def fetch_local():
    """-> ({id: size_gb}, cache_dir) of weights on disk. Raises Abort on error or empty."""
    argv = cmd_for("DBWATCH_LIST_CMD", [DARKBLOOM, "models", "list", "--all", "--json"])
    try:
        rc, out, err = run_cmd(argv)
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise Abort("local model list failed: %s" % type(e).__name__)
    rows = data.get("models") if isinstance(data, dict) else None
    if rc != 0 or not isinstance(rows, list) or not rows:
        raise Abort("local model list failed or empty (exit %s)" % rc)
    local = {r["id"]: (r.get("size_bytes") or 0) / 1e9 for r in rows if isinstance(r, dict) and r.get("id")}
    return local, data.get("cacheDirectory") or DEFAULT_CACHE


def bloomctl_base():
    raw = E("DBWATCH_BLOOMCTL")
    if raw:
        return shlex.split(raw)
    for path in BLOOMCTL_CANDIDATES:
        if path.exists():
            base = ["/usr/bin/python3", "-B", str(path)]
            for d in BG_DIRS:
                if d.is_dir():
                    return base + ["--data-dir", str(d)]
            return base
    return None


def bloomctl(*args):
    """Run bloomctl. -> (rc, parsed JSON or None, short error text). The token is never read here."""
    base = bloomctl_base()
    if base is None:
        return 3, None, "bloomctl.py not found"
    try:
        rc, out, err = run_cmd(base + ["--json"] + list(args))
    except (OSError, subprocess.SubprocessError) as e:
        return 3, None, "bloomctl did not run: %s" % type(e).__name__
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        data = None
    return rc, data, " ".join(err.split())[:200]


def read_bloom():
    """-> {'plan': [ids], 'available': {id: bool}}. Raises Abort when BloomGauge cannot be read."""
    rc, status, err = bloomctl("status")
    plan = ((status or {}).get("plan") or {}).get("models") if isinstance(status, dict) else None
    if rc != 0 or not isinstance(plan, list):
        raise Abort("BloomGauge status unreadable (exit %s) %s" % (rc, err))
    rc, ml, err = bloomctl("model", "list")
    rows = (ml or {}).get("models") if isinstance(ml, dict) else None
    if rc != 0 or not isinstance(rows, list):
        raise Abort("BloomGauge model list unreadable (exit %s) %s" % (rc, err))
    return {"plan": [str(p) for p in plan], "available": {r.get("id"): bool(r.get("available")) for r in rows}}


def lease_state():
    """-> 'HELD' | 'DRAIN' | 'HANDBACK' | None. A corrupt lease file counts as DRAIN (as the control does)."""
    path = Path(E("DBWATCH_LEASE_FILE") or (Path(E("QUEUE_BG_HOME", str(HOME / ".ollama-dispatch"))) / "bloomgauge-lease.json"))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "DRAIN"
    state = data.get("state") if isinstance(data, dict) else None
    return state if state in ("HELD", "DRAIN", "HANDBACK") else "DRAIN"


def queue_running():
    """-> number of running queue jobs; an unreadable state counts as busy (like the apply script)."""
    path = Path(E("DBWATCH_QUEUE_STATE") or (HOME / "bin/ollama-queue-state.json"))
    try:
        jobs = json.loads(path.read_text()).get("jobs") or []
    except (OSError, ValueError, AttributeError):
        return 1
    return sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "running")


def free_gb(cache_dir):
    if E("DBWATCH_FREE_GB") is not None:
        return float(E("DBWATCH_FREE_GB"))
    try:
        return shutil.disk_usage(cache_dir).free / 1e9
    except OSError:
        return None   # cache volume not mounted


def toml_served():
    try:
        return TOML.read_text()
    except OSError:
        return None


def notify(key, msg, apply):
    """One alert through the queue's deliver path (ESCALATIONS.md + desktop notification)."""
    if not apply:
        say("  [dry-run alert] %s: %s" % (key, msg))
        return
    log = E("DBWATCH_NOTIFY_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"key": key, "msg": msg}) + "\n")
        return
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import bloomgauge_lease_guard
        bloomgauge_lease_guard.queue_alert(key, msg)
    except Exception as e:
        say("alert not delivered (%s): %s %s" % (type(e).__name__, key, msg))


# ------------------------------------------------------------------------------------------ manifest

def load_manifest():
    try:
        data = json.loads(MANIFEST.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Abort("manifest unreadable; not overwriting %s" % MANIFEST)
    if not isinstance(data, dict) or not data.get("seeded_at"):
        raise Abort("manifest malformed; not overwriting %s" % MANIFEST)
    return data


def save_manifest(man):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_name(MANIFEST.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(man, indent=1, sort_keys=True))
    os.replace(tmp, MANIFEST)


def entry(now, info=None):
    info = info or {}
    return {"first_seen": now, "last_seen": now, "hf": info.get("hf"), "min_ram_gb": info.get("min_ram_gb"),
            "size_gb": info.get("size_gb")}


# --------------------------------------------------------------------------------------------- logic

def step(man, obs, now):
    """PURE. One run's bookkeeping from the observations. Mutates and returns (man, todo).
    todo: list of {id, kind: 'new'|'enable', note_new: bool} the executor should try. Seeding (man None)
    acts on nothing."""
    cat, local, plan = obs["catalog"], obs["local"], obs["plan"]
    known_ids = set(cat) | set(local)
    in_plan = set(plan)
    if man is None:
        man = {"version": 1, "seeded_at": now, "plan_last": list(plan), "models": {}, "candidates": {}}
        for mid in known_ids:
            man["models"][mid] = entry(now, cat.get(mid))
        for mid in local:   # already on disk and not enabled: counted once now, acts on the next run
            if mid not in in_plan:
                man["candidates"][mid] = {"count": 1, "kind": "enable", "first_seen": now}
        return man, []
    models, cands = man.setdefault("models", {}), man.setdefault("candidates", {})
    for mid, info in cat.items():
        if mid in models:
            models[mid].update(last_seen=now, hf=info.get("hf"), min_ram_gb=info.get("min_ram_gb"), size_gb=info.get("size_gb"))
    # tombstones: gone from the plan since last run (the script never removes anything) -> owner removed it
    for mid in set(man.get("plan_last") or []) - in_plan:
        if mid in known_ids or mid in models:
            m = models.setdefault(mid, entry(now, cat.get(mid)))
            m["owner_removed"] = now
            m.pop("blocked", None)
            m.pop("wanted", None)
            cands.pop(mid, None)
    # a manual re-add (back in the plan; the script never adds a tombstoned model) clears the tombstone
    for mid, m in models.items():
        if m.get("owner_removed") and mid in in_plan:
            m.pop("owner_removed", None)
    man["plan_last"] = list(plan)
    # candidates: catalog models not in the manifest (new) and downloaded models not in the plan (enable)
    want = {}
    for mid in cat:
        if mid not in models:
            want[mid] = "new"
    for mid in local:
        if mid not in in_plan and not (models.get(mid) or {}).get("owner_removed"):
            want[mid] = "new" if mid not in models else "enable"
    for mid, m in models.items():   # confirmed earlier, not done yet (blocked, deferred, download failed): keep trying
        if m.get("wanted") and mid in cat and mid not in in_plan and not m.get("owner_removed"):
            want.setdefault(mid, "enable")
            cands.setdefault(mid, {"count": CONFIRM_RUNS - 1, "kind": "enable", "first_seen": m["first_seen"]})
    for mid in list(cands):
        if mid not in want:
            del cands[mid]     # a one-run blip
    todo = []
    for mid, kind in want.items():
        c = cands.setdefault(mid, {"count": 0, "kind": kind, "first_seen": now})
        c["count"] += 1
        c["kind"] = kind
        if c["count"] < CONFIRM_RUNS:
            continue
        m = models.get(mid)
        if kind == "new" and m is None:
            m = models[mid] = entry(now, cat.get(mid))
            m["is_new"] = True
        if m is not None:
            m["wanted"] = True
        b = (m or {}).get("blocked")
        if b and now - b.get("last_try", 0) < RETRY_S:
            continue
        todo.append({"id": mid, "kind": kind, "note_new": bool((m or {}).get("is_new")) and not (m or {}).get("notified_new")})
    return man, todo


def gate(obs):
    """-> a reason string while nothing may be done, else None."""
    if obs["lease"]:
        return "lease %s" % obs["lease"]
    if obs["queue_running"]:
        return "%d queue job(s) running" % obs["queue_running"]
    return None


def execute(man, todo, obs, apply, now):
    """Run each todo item. -> list of result dicts. Mutates man (apply mode only)."""
    results = []
    models = man["models"]
    why = gate(obs)
    for item in todo:
        mid = item["id"]
        info = obs["catalog"].get(mid) or {}
        size = info.get("size_gb") or obs["local"].get(mid)
        res = {"id": mid, "kind": item["kind"], "size_gb": round(size, 1) if size else None}
        results.append(res)
        if why:
            res["outcome"] = "deferred (%s)" % why
            continue
        if item["note_new"]:
            res["notified_new"] = True
            notify("darkbloom-catalog-new-" + mid, "new Darkbloom catalog model %s (%s GB, needs %s GB RAM). The watcher "
                   "is downloading and enabling it in BloomGauge; the picker still decides if it runs." %
                   (mid, res["size_gb"], info.get("min_ram_gb")), apply)
            if apply:
                models[mid]["notified_new"] = True
        steps = []
        if mid not in obs["local"]:
            free = free_gb(obs["cache_dir"])
            if free is None:
                res["outcome"] = "deferred (cache volume %s not mounted)" % obs["cache_dir"]
                continue
            if not size:
                fail(man, mid, res, "unknown weights size in the catalog", apply, now)
                continue
            need = size * DISK_FACTOR + DISK_FLOOR_GB
            if free < need:
                fail(man, mid, res, "disk too small on the cache volume: %.0f GB free, need %.0f GB (weights %.1f x %.1f + %d)" %
                     (free, need, size, DISK_FACTOR, DISK_FLOOR_GB), apply, now)
                continue
            steps.append("download")
        if not obs["available"].get(mid) and mid in obs["local"]:
            res["outcome"] = "deferred (BloomGauge does not list it as available yet)"
            continue
        steps.append("enable")
        res["steps"] = steps
        if not apply:
            res["outcome"] = "would " + " + ".join(steps)
            continue
        if "download" in steps:
            before = toml_served()
            try:
                rc, out, err = run_cmd(cmd_for("DBWATCH_DOWNLOAD_CMD", [DARKBLOOM, "models", "download"]) + [mid], DOWNLOAD_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as e:
                rc, err = 1, type(e).__name__
            if rc != 0:
                fail(man, mid, res, "download failed (exit %s) %s" % (rc, " ".join(err.split())[:160]), apply, now)
                continue
            if toml_served() != before:
                notify("darkbloom-catalog-served-changed", "provider.toml changed during the download of %s. The watcher "
                       "never touches the served set; check `darkbloom status`." % mid, apply)
            try:
                obs["local"], _ = fetch_local()
            except Abort:
                pass
            if mid not in obs["local"]:
                fail(man, mid, res, "download finished but the model is not in `darkbloom models list`", apply, now)
                continue
            try:
                obs["available"] = read_bloom()["available"]
            except Abort:
                pass
            if not obs["available"].get(mid):
                res["outcome"] = "downloaded; deferred (BloomGauge does not list it as available yet)"
                continue
        rc, out, err = bloomctl("model", "add", mid)
        if rc != 0:
            fail(man, mid, res, "bloomctl model add exit %s: %s" % (rc, err or "no error text"), apply, now)
            continue
        res["outcome"] = "enabled"
        m = models[mid]
        m.pop("blocked", None)
        m.pop("wanted", None)
        m["enabled_by_watcher"] = now
        man["candidates"].pop(mid, None)
        notify("darkbloom-catalog-added-" + mid, "Darkbloom model %s is on disk and enabled in BloomGauge (plan list). Undo: "
               "bloomctl model remove %s; the watcher then never re-adds it until you add it back by hand." % (mid, mid), apply)
    return results


def fail(man, mid, res, error, apply, now):
    res["outcome"] = "blocked: " + error
    if not apply:
        return
    m = man["models"].setdefault(mid, entry(now))
    b = m.get("blocked") or {"since": now, "tries": 0}
    b.update(last_try=now, error=error, tries=b["tries"] + 1)
    m["blocked"] = b
    notify("darkbloom-catalog-failed-" + mid, "adding Darkbloom model %s failed: %s. It is marked blocked and retried at most daily. "
           "Nothing was served, restarted or deleted." % (mid, error), apply)


def observe():
    catalog = fetch_catalog()
    local, cache_dir = fetch_local()
    cache_dir = E("DBWATCH_CACHE_DIR") or cache_dir
    bg = read_bloom()
    return {"catalog": catalog, "local": local, "cache_dir": cache_dir, "plan": bg["plan"], "available": bg["available"],
            "lease": lease_state(), "queue_running": queue_running()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: read only, print the plan")
    mode.add_argument("--apply", action="store_true", help="download, enable, write the manifest, alert")
    args = ap.parse_args(argv)
    apply = bool(args.apply)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("another run holds the lock; exiting")
        return 0
    now = now_ts()
    say("darkbloom-catalog-watch %s" % ("APPLY" if apply else "DRY-RUN"))
    try:
        man = load_manifest()
        obs = observe()
    except Abort as e:
        say("ABORT (manifest untouched): %s" % e)
        return 3
    first = man is None
    man, todo = step(json.loads(json.dumps(man)) if man else None, obs, now)
    say("catalog %d, on disk %d, BloomGauge plan %d, lease %s, queue jobs running %d, free %s GB" % (
        len(obs["catalog"]), len(obs["local"]), len(obs["plan"]), obs["lease"] or "none", obs["queue_running"],
        "?" if free_gb(obs["cache_dir"]) is None else "%.0f" % free_gb(obs["cache_dir"])))
    if first:
        say("FIRST RUN: %s the manifest with %d models; acting on nothing." % ("seeding" if apply else "would seed", len(man["models"])))
        if not apply:   # project the next run so the dry run shows what it will do
            _, todo = step(json.loads(json.dumps(man)), obs, now)
            say("projected next run (if nothing changes):")
    removed = sorted(m for m, v in man["models"].items() if v.get("owner_removed"))
    if removed:
        say("owner_removed (never re-added): %s" % ", ".join(removed))
    results = execute(man, todo, obs, apply, now)
    for r in results:
        say("  %s [%s]%s -> %s" % (r["id"], r["kind"], " %s GB" % r["size_gb"] if r["size_gb"] else "", r["outcome"]))
    if not results:
        pending = {k: v["count"] for k, v in man["candidates"].items()}
        say("nothing to do%s" % ("; pending debounce: %s" % json.dumps(pending, sort_keys=True) if pending else ""))
    if apply:
        man["updated"] = now
        try:
            obs["plan"] = read_bloom()["plan"]   # what the plan is after our own changes, so they never look like owner edits
            man["plan_last"] = list(obs["plan"])
        except Abort:
            pass
        save_manifest(man)
    return 0


if __name__ == "__main__":
    sys.exit(main())
