#!/usr/bin/env python3
"""darkbloom-keepwarm: keep the ollama queue's two Darkbloom models resident and warm.

WHY (research, 2026-10-06, read-only): Darkbloom (0.9.17) keeps at most `[backend]
max_model_slots` models loaded. A request (public fleet OR local) for an ENABLED model that is
not resident lazy-loads it and, when slots or memory are short, evicts the least-recently-used
IDLE model ("Evicted LRU model"). `idle_timeout_mins = 0` only stops idle UNLOADING, and
startup preload "will not evict" but also does not protect a model later. The only real
protections are: a model with an in-flight request, Autopilot pins (only while Autopilot
controls residency -- BloomGauge refuses to manage a provider enrolled in Autopilot), or a
serving set no larger than the slots. Nothing in provider.toml pins a model. So with a wider
enabled_models, an evicted queue model is a matter of time; this guard makes the damage small
and short:

  * RECENCY: every `touch_interval_s` it sends a 1-token local request to each queue model so
    both stay most-recently-used (a third-party model, not ours, is then the LRU victim).
  * RE-WARM: when a queue model is not resident (loaded-models.json / /v1/models / /health) it
    first touches the model that IS resident (so the load cannot evict it), then sends a
    1-token request to the missing one (a request is how Darkbloom loads on demand).
  * It NEVER restarts or reconfigures Darkbloom, never touches provider.toml, never acts while
    a queue job is running (that job's own requests keep its model active), rate-limits itself
    (max_rewarm_per_hour per model, min gap between attempts), alerts (log + macOS
    notification, deduped) on an eviction and on thrash, and logs every decision.
  * ~/.ollama-dispatch/darkbloom-keepwarm-state.json carries a heartbeat and `hold` (models
    being re-warmed). ollama-queue.py (darkbloom_warm_hold) makes a launch for a held model
    WAIT (infra wait) instead of cold-loading it concurrently with this guard. A heartbeat
    older than 5 min disables the hold.

Default OFF: ~/.ollama-dispatch/darkbloom-keepwarm.enabled must exist (else exit 0; the state
file is removed so the queue stops holding). One tick per 60 s (launchd StartInterval).

Config (optional, next to this script: darkbloom-keepwarm.json; env KEEPWARM_CONF): models,
touch_interval_s (600; 0 = never touch), rewarm_min_gap_s (120), max_rewarm_per_hour (6),
warm_timeout_s (240).
Env overrides (tests): KEEPWARM_HOME, KEEPWARM_ENABLED, KEEPWARM_STATE, KEEPWARM_LOG,
KEEPWARM_QUEUE_STATE, KEEPWARM_LOCAL_JSON, KEEPWARM_LOADED_JSON, KEEPWARM_NOW,
KEEPWARM_NO_NOTIFY.
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODELS = ["Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8"]
_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{28,}")


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_cfg():
    home = Path(_env("KEEPWARM_HOME", str(Path.home() / ".ollama-dispatch")))
    c = {
        "enabled": Path(_env("KEEPWARM_ENABLED", str(home / "darkbloom-keepwarm.enabled"))),
        "state": Path(_env("KEEPWARM_STATE", str(home / "darkbloom-keepwarm-state.json"))),
        "log": Path(_env("KEEPWARM_LOG", str(home / "darkbloom-keepwarm.log"))),
        "lock": Path(_env("KEEPWARM_LOCK", str(home / "darkbloom-keepwarm.lock"))),
        "conf": Path(_env("KEEPWARM_CONF", str(Path(__file__).resolve().with_name("darkbloom-keepwarm.json")))),
        "queue_state": Path(_env("KEEPWARM_QUEUE_STATE", str(Path.home() / "bin" / "ollama-queue-state.json"))),
        "local_json": Path(_env("KEEPWARM_LOCAL_JSON", str(Path.home() / ".darkbloom" / "local.json"))),
        "idle_state": Path(_env("KEEPWARM_BLOOM_IDLE_STATE", str(home / "bloom-idle-state.json"))),
        "loaded_json": Path(_env("KEEPWARM_LOADED_JSON", str(Path.home() / ".darkbloom" / "loaded-models.json"))),
        "models": list(DEFAULT_MODELS), "touch_interval_s": 600.0, "rewarm_min_gap_s": 120.0,
        "max_rewarm_per_hour": 6, "warm_timeout_s": 240.0, "alert_gap_s": 1800.0,
    }
    try:
        j = json.loads(c["conf"].read_text())
        for k in ("models", "touch_interval_s", "rewarm_min_gap_s", "max_rewarm_per_hour",
                  "warm_timeout_s", "alert_gap_s"):
            if k in j:
                c[k] = j[k]
    except (OSError, ValueError, TypeError):
        pass
    c["models"] = [str(m) for m in c["models"]]
    return c


def now_ts():
    v = os.environ.get("KEEPWARM_NOW")
    return float(v) if v else __import__("time").time()


def scrub(t, limit=300):
    return _SECRETISH.sub("[redacted]", str(t))[:limit].replace("\n", " ")


def log(cfg, msg, level="INFO"):
    line = "%s %-5s %s" % (datetime.datetime.fromtimestamp(now_ts()).strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    print(line, flush=True)
    try:
        cfg["log"].parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["log"], "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def alert(cfg, st, key, msg, now):
    """Log + deduped macOS notification. Never raises."""
    log(cfg, "ALERT %s" % msg, "WARN")
    last = st.setdefault("alerts", {})
    if now - last.get(key, 0) < cfg["alert_gap_s"]:
        return
    last[key] = now
    if os.environ.get("KEEPWARM_NO_NOTIFY"):
        return
    try:
        text = ("darkbloom-keepwarm: " + msg).replace('"', "'")[:180]
        subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "Darkbloom" sound name "Basso"' % text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def read_state(cfg):
    try:
        st = json.loads(cfg["state"].read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(cfg, st):
    st["heartbeat"] = now_ts()
    cfg["state"].parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg["state"].with_name(cfg["state"].name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, cfg["state"])


def running_job(cfg):
    """True when any queue job is running, or the queue state is unreadable (fail safe)."""
    for _ in range(3):
        try:
            d = json.loads(cfg["queue_state"].read_text())
            return any(isinstance(j, dict) and j.get("status") == "running" for j in d.get("jobs", []))
        except (OSError, ValueError, AttributeError):
            __import__("time").sleep(0.05)
    return True


def db_base_key(cfg):
    try:
        rec = json.loads(cfg["local_json"].read_text())
    except (OSError, ValueError):
        return None, None
    base = str(rec.get("base_url") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return (base or "http://127.0.0.1:%s" % (rec.get("port") or 8000)), rec.get("api_key")


def health(cfg):
    base, _ = db_base_key(cfg)
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            return r.status == 200 and json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def listed(cfg):
    base, key = db_base_key(cfg)
    try:
        req = urllib.request.Request(base + "/v1/models", headers={"Authorization": "Bearer " + str(key or "")})
        with urllib.request.urlopen(req, timeout=5) as r:
            return {str(m.get("id")).lower() for m in json.loads(r.read()).get("data", [])}
    except Exception:
        return None


def loaded(cfg):
    try:
        return {str(x).lower() for x in (json.loads(cfg["loaded_json"].read_text()).get("models") or [])}
    except (OSError, ValueError, AttributeError):
        return None


def residency(cfg):
    """-> (provider_ok, resident_lowercase_set). resident = loaded-models.json AND /v1/models."""
    if not health(cfg):
        return False, set()
    ld, ls = loaded(cfg), listed(cfg)
    if ld is None or ls is None:
        return False, set()
    return True, ld & ls


def ping(cfg, model):
    """One-token local request: Darkbloom loads a missing model on demand and marks it most
    recently used. -> (ok, detail). Never logs the key."""
    base, key = db_base_key(cfg)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1, "temperature": 0}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + str(key or "")})
    try:
        with urllib.request.urlopen(req, timeout=cfg["warm_timeout_s"]) as r:
            r.read()
            return r.status == 200, "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return False, "HTTP %s %s" % (e.code, scrub(detail, 120))
    except Exception as e:
        return False, scrub(e, 120)


def idle_switch_owns(cfg, now):
    """True while bloom-idle-switch.py has deliberately handed the choice to BloomGauge's optimizer
    ('away') or is restoring the pair: this guard must NOT fight it (the queue is idle, the optimizer
    may load anything; the queue's own infra-wait covers a restore). Needs a fresh heartbeat."""
    try:
        d = json.loads(cfg["idle_state"].read_text())
        return (d.get("state") in ("away", "restoring")
                and now - float(d.get("heartbeat") or 0) < 300)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def tick(cfg):
    now = now_ts()
    if not cfg["enabled"].exists():
        try:
            cfg["state"].unlink()                  # stop the queue holding on a dead guard's state
        except OSError:
            pass
        return 0
    st = read_state(cfg)
    if idle_switch_owns(cfg, now):
        if st.get("last_msg") != "idle-switch owns":
            log(cfg, "bloom-idle-switch is away/restoring: the optimizer owns the choice, not defending the pair")
        st.update(last_msg="idle-switch owns", hold=[], missing=[])
        write_state(cfg, st)
        return 0
    models = cfg["models"]
    low = {m: m.lower() for m in models}
    ok, resident = residency(cfg)
    if not ok:
        msg = "provider not reachable/ready (health, /v1/models or loaded-models.json): no action"
        if st.get("last_msg") != msg:
            log(cfg, msg, "WARN")
            st["last_msg"] = msg
        st["hold"] = []
        write_state(cfg, st)
        return 0
    st["last_msg"] = None
    missing = [m for m in models if low[m] not in resident]
    per = st.setdefault("models", {})
    for m in models:
        per.setdefault(m, {"attempts": [], "last_touch": 0})
        per[m]["resident"] = low[m] in resident
    busy = running_job(cfg)
    hold = []

    if missing:
        msg = "queue model(s) not resident: %s" % missing
        if per[missing[0]].get("was_resident", True):
            alert(cfg, st, "evicted:" + ",".join(missing), msg + " (evicted or unloaded)", now)
        for m in missing:
            per[m]["was_resident"] = False
        if busy:
            log(cfg, msg + "; a queue job is running -> not touching Darkbloom")
            hold = list(missing)
        else:
            order = [m for m in models if m not in missing] + missing   # touch residents FIRST
            for m in order:
                is_missing = m in missing
                if is_missing:
                    recent = [t for t in per[m]["attempts"] if t > now - 3600]
                    per[m]["attempts"] = recent
                    if recent and now - recent[-1] < cfg["rewarm_min_gap_s"]:
                        hold.append(m)
                        log(cfg, "%s: re-warm attempted %.0fs ago; waiting (min gap %ds)" % (m, now - recent[-1], cfg["rewarm_min_gap_s"]))
                        continue
                    if len(recent) >= cfg["max_rewarm_per_hour"]:
                        alert(cfg, st, "thrash:" + m, "%s keeps getting evicted (%d re-warms in the last hour); "
                              "giving up until the hour passes -- queue launches will cold-load" % (m, len(recent)), now)
                        continue
                    per[m]["attempts"].append(now)
                    hold.append(m)
                    st["hold"] = hold
                    write_state(cfg, st)           # the queue waits while the request is in flight
                    log(cfg, "RE-WARM %s: 1-token local request (residents touched first)" % m)
                    good, detail = ping(cfg, m)
                    ok2, res2 = residency(cfg)
                    if good and ok2 and low[m] in res2:
                        log(cfg, "RE-WARM %s: resident again (%s)" % (m, detail))
                        per[m].update(resident=True, was_resident=True)
                        hold.remove(m)
                    else:
                        log(cfg, "RE-WARM %s: NOT resident yet (%s); will retry" % (m, detail), "WARN")
                else:
                    good, detail = ping(cfg, m)
                    per[m]["last_touch"] = now
                    log(cfg, "touch %s before re-warm (keeps it most-recently-used): %s" % (m, detail))
    else:
        for m in models:
            per[m]["was_resident"] = True
        if busy:
            pass
        elif cfg["touch_interval_s"] > 0:
            for m in models:
                if now - per[m].get("last_touch", 0) >= cfg["touch_interval_s"]:
                    good, detail = ping(cfg, m)
                    per[m]["last_touch"] = now
                    log(cfg, "touch %s (recency): %s" % (m, detail), "INFO" if good else "WARN")
    st["hold"] = hold
    st["missing"] = [m for m in models if not per[m].get("resident")]
    write_state(cfg, st)
    return 0


def main():
    cfg = load_cfg()
    if "--status" in sys.argv:
        print(json.dumps(read_state(cfg), indent=1, sort_keys=True))
        print("enabled:", cfg["enabled"].exists())
        return 0
    cfg["lock"].parent.mkdir(parents=True, exist_ok=True)
    with open(cfg["lock"], "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            return tick(cfg)
        except Exception as e:
            log(cfg, "tick crashed: %r" % e, "ERROR")
            return 0


if __name__ == "__main__":
    sys.exit(main())
