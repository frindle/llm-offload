#!/usr/bin/env python3
"""bloom_control: event-driven hand-off of the Darkbloom provider between the ollama queue
and BloomGauge (earnings). Replaces the 10-minute idle timer of bloom-idle-switch.py.

  hold_for_queue()    the queue has work. BloomGauge -> Manual (so it cannot switch models
                      away), Darkbloom auto-update -> off, provider.toml -> max_model_slots=2 /
                      idle_timeout_mins=0 / preload_models=pair, `darkbloom start --model A
                      --model B --idle-timeout 0 --local-endpoint --timeout <drain>` (skipped when
                      the pair is already served), then wait for /health + /v1/models +
                      loaded-models.json to agree on exactly the pair.
  release_to_bloom()  the queue is done. BloomGauge release-pin + set-automatic enabled:true,
                      Darkbloom auto-update restored if hold_for_queue turned it off.

  GAUGE HAND-OFF (2026-10-29, BloomGauge on, BLOOMCTL_GAUGE_HANDOFF!=0): BloomGauge's settings are
  read-only to programs (its POSTs need the app's session), so they are left exactly as the user has
  them and never POSTed. The hold moves Darkbloom to the queue model with ONE `darkbloom switch`
  (long --timeout; restart only as the fallback when the daemon is not draining) WITHOUT provider.toml
  pins; the release picks no model: it puts BloomGauge's saved enabled-model filter back (if it is
  still the queue's), clears a preload pin of only queue models, and reports BloomGauge's mode as a
  GET shows it. Never `--autopilot` (it enrolled Darkbloom Autopilot: every `switch` refused rc=64).

  DRAIN WATCH (2026-10-29): `darkbloom status` is read before any switch/start. While the daemon is
  draining (in-flight requests, a timed-out switch with admission closed, another operation) nothing
  is stacked: state phase=draining + drain{since, elapsed_s, unfinished, switch, selection, ...}, a
  log line per poll, an ALERT once admission stays closed past drain_alert_s; a timed-out switch is
  reconciled by repeating `darkbloom switch` to its Selection once the requests are done. Never
  --force, never stop/restart while draining. Release waits out a drain the same way.
  get_state()         read-only snapshot (never mutates).

Both mutators are idempotent (already in the wanted state -> no-op), serialised by one flock so
concurrent callers never double-switch (the second caller waits, re-reads, and no-ops), and have
bounded timeouts. Transitions are driven by the queue daemon (ollama-queue.py bloom_queue_sync);
there is NO timer here.

BloomGauge's control API (POST /api/optimizer/control, X-Bloom-Action: optimizer; actions
set-automatic{enabled}, release-pin; every call needs the controlVersion of a fresh
GET /api/optimizer/control) is UNDOCUMENTED and may change when the app auto-updates. Every
response is shape-checked (controlVersion str, automatic.mode in {on, manual}); on any mismatch
the Bloom half is skipped (DEGRADED: `darkbloom start` only), state.degraded is set and the
queue daemon prints a loud alert. The queue is never blocked by a Bloom problem.

Kill switch (integration OFF -> every function is a no-op returning {"disabled": True}):
  env BLOOM_CONTROL=0, or the file ~/.ollama-dispatch/bloom-control.disabled.
State: ~/.ollama-dispatch/bloom-control-state.json (heartbeat-stamped; ollama-queue.py's
bloom_idle_hold makes launches WAIT as infra-wait while phase == "switching").
Log:   ~/.ollama-dispatch/bloom-control.log (secrets scrubbed).
NO-SWITCH mode (2026-10-13, the owner: "I don't want to switch models"): when the provider already
ADVERTISES the pair (both queue models in /v1/models) this module NEVER runs `darkbloom
start/stop/switch/restart` and never edits provider.toml, even if the pair is not resident: the
provider owns residency (max_model_slots, autopilot, network traffic). hold_for_queue then only
parks BloomGauge on Manual, and noswitch_ensure() READS ~/.darkbloom/daemon-state.json +
loaded-models.json, and sends the bounded 1-token warm request so the PROVIDER evicts an idle slot
and loads the model; when it refuses for capacity the queue parks the job in INFRA WAIT naming the
blocker (noswitch_gate_reason) and re-checks with bounded backoff. A model NOT advertised keeps the
restart-based hold. Kill switch: env BLOOM_NOSWITCH=0 (today's restart behaviour), plus the
BLOOM_CONTROL=0 / bloom-control.disabled switches above (which disable everything).
BLOOMGAUGE IS OPTIONAL (2026-10-13, the owner: "forget BloomGauge ... we're just going to use autopilot"): the
BloomGauge app/control API (127.0.0.1:8765) is gone. Switch (bloomgauge_on): env BLOOM_GAUGE=1/0 wins, else
file ~/.ollama-dispatch/bloomgauge.off (off) / bloomgauge.on (on), DEFAULT OFF. Off = every BloomGauge call
(view, set-automatic, release-pin) is skipped: no Manual park, no `degraded`, no `release-pending`, no retry
loop, no WARN spam; the Darkbloom auto-update toggle (which only existed to stop it restarting the provider
mid-hold) is not touched either (a stale `autoupdate_was_on` from an earlier on-mode hold is still restored).
hold_for_queue still does all provider-side work (no-switch warm, restart-based hold for a non-advertised
pair, memory gates); release_to_bloom is a clean noop why='bloomgauge off'. Re-enable: BLOOM_GAUGE=1 or
`touch ~/.ollama-dispatch/bloomgauge.on` (rm bloomgauge.off).
Env for tests: BLOOMCTL_DAEMON_JSON, BLOOMCTL_NS_*, BLOOMCTL_HOME, BLOOMCTL_BLOOM_URL, BLOOMCTL_DARKBLOOM_BIN, BLOOMCTL_PROVIDER_TOML,
BLOOMCTL_LOCAL_JSON, BLOOMCTL_LOADED_JSON, BLOOMCTL_PAIR (a,b), BLOOMCTL_DRAIN_S, BLOOMCTL_WAIT_S,
BLOOMCTL_POLL_S, BLOOMCTL_FORCE=1 (darkbloom start --force), BLOOMCTL_DRY_RUN=1.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 2026-10-16 (the owner): 35B only. The 9B is unused for offload and, while serving, blocked the 35B load (needs 30.3 GB).
DEFAULT_PAIR = ["qwen3.6-35b-a3b-vl-mtp-mxfp8"]
_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{28,}")


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_cfg():
    home = Path(_env("BLOOMCTL_HOME", str(Path.home() / ".ollama-dispatch")))
    pair = [m for m in _env("BLOOMCTL_PAIR", ",".join(DEFAULT_PAIR)).split(",") if m]
    return {
        "home": home,
        "state": home / "bloom-control-state.json",
        "log": home / "bloom-control.log",
        "lock": home / "bloom-control.lock",
        "disabled_file": home / "bloom-control.disabled",
        "gauge_off_file": home / "bloomgauge.off",
        "gauge_on_file": home / "bloomgauge.on",
        "bloom": _env("BLOOMCTL_BLOOM_URL", "http://127.0.0.1:8765").rstrip("/"),
        "bin": _env("BLOOMCTL_DARKBLOOM_BIN") or shutil.which("darkbloom")
               or str(Path.home() / ".darkbloom/bin/darkbloom"),
        "toml": Path(_env("BLOOMCTL_PROVIDER_TOML", str(Path.home() / ".config/darkbloom/provider.toml"))),
        "local_json": Path(_env("BLOOMCTL_LOCAL_JSON", str(Path.home() / ".darkbloom/local.json"))),
        "loaded_json": Path(_env("BLOOMCTL_LOADED_JSON", str(Path.home() / ".darkbloom/loaded-models.json"))),
        "pair": pair,
        "drain_s": int(float(_env("BLOOMCTL_DRAIN_S", 60))),   # darkbloom start --timeout
        "wait_s": float(_env("BLOOMCTL_WAIT_S", 900)),         # pair must be served within this
        "poll_s": float(_env("BLOOMCTL_POLL_S", 3)),
        "lock_wait_s": float(_env("BLOOMCTL_LOCK_WAIT", 1200)),
        "primary": _env("BLOOMCTL_PRIMARY") or pair[-1],      # the model that must be served when the pair cannot fit
        "fallback_s": float(_env("BLOOMCTL_FALLBACK_S", 150)),  # pair infeasible this long -> serve the primary alone
        "restart_min_s": float(_env("BLOOMCTL_RESTART_MIN_S", 900)),  # no second `darkbloom start` inside this when memory-infeasible
        "partial_retry_s": float(_env("BLOOMCTL_PARTIAL_RETRY_S", 900)),   # re-check whether the full pair fits now
        "small_need_gb": float(_env("BLOOMCTL_SMALL_NEED_GB", 8.5)),       # usable GB needed to add the small model back
        "probe_s": float(_env("BLOOMCTL_PROBE_S", 20)),        # first memory-feasibility probe while waiting for the pair
        "force": _env("BLOOMCTL_FORCE") == "1",
        "dry_run": _env("BLOOMCTL_DRY_RUN") == "1",
        "http_timeout": float(_env("BLOOMCTL_HTTP_TIMEOUT", 8)),
        "warm_s": float(_env("BLOOMCTL_WARM_S", 300)),         # budget to get each pair model answering a 1-token request
        "warm_req_s": float(_env("BLOOMCTL_WARM_REQ_S", 180)), # one warm request may block this long (a cold load)
        # NO-SWITCH mode (2026-10-13): the provider advertises the pair; the queue only reads state + warms.
        "daemon_json": Path(_env("BLOOMCTL_DAEMON_JSON", str(Path.home() / ".darkbloom/daemon-state.json"))),
        "ns_idle_s": float(_env("BLOOMCTL_NS_IDLE_S", 30)),            # resident idle >= this -> the provider may evict it
        "ns_state_max_age_s": float(_env("BLOOMCTL_NS_STATE_MAX_AGE_S", 30)),   # older daemon-state.json = unknown
        "ns_probe_s": float(_env("BLOOMCTL_NS_PROBE_S", 120)),         # blocked: still send one warm probe this often
        "ns_backoff_base_s": float(_env("BLOOMCTL_NS_BACKOFF_BASE_S", 10)),   # refused warm: retry after base*2^n ...
        "ns_backoff_max_s": float(_env("BLOOMCTL_NS_BACKOFF_MAX_S", 120)),    # ... capped here
        "ns_warn_s": float(_env("QUEUE_BLOOM_WAIT_WARN_S", 600)),      # waited this long for its model -> dashboard warning
        "ns_est_ttl_s": float(_env("BLOOMCTL_NS_EST_TTL_S", 1800)),    # cache of `darkbloom models list --all --json`
        # PAIR-RESTART mode (2026-10-13, default): hold = enabled/preload = the pair only (Gemma off) + one
        # `darkbloom start`; release = the full restore set (Gemma back, primary first) + one start, verified.
        "restore": [m for m in _env("BLOOMCTL_RESTORE_MODELS", ",".join(
            [DEFAULT_PAIR[0], "Qwen3.5-9B", "gemma-4-26b-qat-4bit"])).split(",") if m],
        "exclude_models": [x.strip().lower() for x in _env("BLOOMCTL_EXCLUDE_MODELS", "gpt-oss-20b").split(",") if x.strip()],
        "start_extra": _env("BLOOMCTL_START_EXTRA", "--local-endpoint --port 8000 --bind 127.0.0.1").split(),
        "restart_after_s": float(_env("BLOOM_PAIR_RESTART_AFTER_S", 45)),   # bounded no-switch attempt before any restart
        # memory pressure / purge (2026-10-13): `sudo -n /usr/sbin/purge` (NOPASSWD rule needed), rate-limited, never kills anything
        "purge_cmd": _env("BLOOMCTL_PURGE_CMD", "sudo -n /usr/sbin/purge").split(),
        "purge_min_s": float(_env("BLOOMCTL_PURGE_MIN_S", 600)),       # at most one purge per this many seconds
        "purge_settle_s": float(_env("BLOOMCTL_PURGE_SETTLE_S", 20)),  # wait this long for the provider to re-publish capacity
        # UNLOAD idle residents (2026-10-22): the provider never evicts an always-ready model for a cold load and
        # `darkbloom switch` is refused while Autopilot is enrolled, so the only supported unload is the hold's
        # graceful `darkbloom start --model <primary>` (never --force). A restart also frees file-backed pages the
        # provider does not credit as usable (observed: 35B loads at 25-26 GB "usable"), hence the slack.
        "unload_slack_gb": float(_env("BLOOMCTL_UNLOAD_SLACK_GB", 8)),
        "unload_wait_s": float(_env("BLOOMCTL_UNLOAD_WAIT_S", 240)),   # wait this long for 0 unfinished requests
        "unload_quiet_s": float(_env("BLOOMCTL_UNLOAD_QUIET_S", 60)),   # ...held continuously for this long
        # a provider start already in progress (hashing/preloading, /health not ok yet): never start it again
        "pid_file": Path(_env("BLOOMCTL_PROVIDER_PID", str(Path.home() / ".darkbloom/provider.pid"))),
        "start_grace_s": float(_env("BLOOMCTL_START_GRACE_S", 480)),     # provider process younger than this + /health not ok = starting
        "start_wait_s": float(_env("BLOOMCTL_START_WAIT_S", 480)),       # wait this long for it to finish before giving up (still no restart)
        # WARM-UP (2026-10-13, d-inference #1238): a restarted/switched provider can sit routable but get no network
        # work (coordinator prefers 'Feasible' over 'Unknown'; evidence goes stale after ~2 min idle). A few small
        # loopback requests seed it. BLOOM_WARMUP_N=0 disables everything.
        "warmup_n": int(float(_env("BLOOM_WARMUP_N", 3))),                  # requests per resident model per batch (0 = off)
        "warmup_flat_s": float(_env("BLOOM_WARMUP_FLAT_S", 300)),           # requests_served flat this long -> one more batch
        "warmup_min_gap_s": float(_env("BLOOM_WARMUP_MIN_GAP_S", 600)),     # at most one idle batch per this
        "warmup_space_s": float(_env("BLOOM_WARMUP_SPACE_S", 4)),           # spacing between warm-up requests
        "warmup_tokens": int(float(_env("BLOOM_WARMUP_TOKENS", 64))),
        "warmup_state": home / "bloom-warmup-state.json",
        # SESSION-AWARE set (2026-10-15, plan sleepy-dijkstra): pair while working, +Gemma after idle_s of no Claude activity
        "activity_file": Path(_env("BLOOMCTL_ACTIVITY_FILE", str(home / "claude-active"))),
        "idle_s": float(_env("BLOOMCTL_IDLE_S", 1800)),                  # no activity + no queue work this long -> idle set
        "switch_min_gap_s": float(_env("BLOOMCTL_SWITCH_MIN_GAP_S", 600)),   # at most one idle-driven set change per this
        "debounce_s": float(_env("BLOOMCTL_DEBOUNCE_S", 60)),            # idle->pair needs activity pings spanning >= this (a stray ping never flaps)
        "switch_wait_s": float(_env("BLOOMCTL_SWITCH_WAIT_S", 120)),
        "switch_timeout_s": int(float(_env("BLOOMCTL_SWITCH_TIMEOUT_S", 1800))),   # `darkbloom switch --timeout` (drain deadline)
        "drain_poll_s": float(_env("BLOOMCTL_DRAIN_POLL_S", 60)),       # `darkbloom status` poll while a switch drains
        "drain_alert_s": float(_env("BLOOMCTL_DRAIN_ALERT_S", 1800)),   # admission closed this long -> ALERT (a human decides)     # `darkbloom switch` must show the set within this, else restart
        "restore_wait_s": float(_env("BLOOMCTL_RESTORE_WAIT_S", 300)),          # restored models must be served within this
        "restore_backoff_base_s": float(_env("BLOOMCTL_RESTORE_BACKOFF_BASE_S", 60)),   # failed restore: retry after base*2^n ...
        "restore_backoff_max_s": float(_env("BLOOMCTL_RESTORE_BACKOFF_MAX_S", 900)),    # ... capped here
    }


def pair_restart_mode(cfg=None):
    """True (default) when the queue HOLDS the provider on exactly the pair (Gemma off, one restart) and
    RESTORES the full set at queue-done. Kill switch: env BLOOM_PAIR_RESTART=0 -> the NO-SWITCH behaviour
    (the queue never touches provider.toml / restarts; see noswitch_mode)."""
    cfg = cfg or load_cfg()
    return _env("BLOOM_PAIR_RESTART", "1") != "0" and enabled(cfg)


def enabled(cfg=None):
    cfg = cfg or load_cfg()
    return _env("BLOOM_CONTROL", "1") != "0" and not cfg["disabled_file"].exists()


def bloomgauge_on(cfg=None):
    """True only when BloomGauge is explicitly enabled (env BLOOM_GAUGE=1, or file bloomgauge.on and no
    bloomgauge.off). DEFAULT OFF: the app is gone (2026-10-13). env BLOOM_GAUGE=0 always wins."""
    cfg = cfg or load_cfg()
    e = _env("BLOOM_GAUGE")
    if e is not None:
        return e.strip().lower() not in ("0", "off", "false", "no")
    if cfg["gauge_off_file"].exists():
        return False
    return cfg["gauge_on_file"].exists()


def scrub(text, limit=300):
    return _SECRETISH.sub("[redacted]", str(text))[:limit].replace("\n", " ")


def log(cfg, msg, level="INFO"):
    line = "%s %-5s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, scrub(msg, 600))
    print(line, flush=True)
    try:
        cfg["log"].parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["log"], "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# Drift-guard WARN de-noising (2026-10-09 smoke): the queue re-verifies the hold every ~67s,
# and a DEGRADED hold (BloomGauge closed) re-logged the same WARN each time for hours. The
# guard's BEHAVIOUR is untouched; only the LOGGING of an unchanged condition is limited: log
# when the condition (key, signature) changes, plus a heartbeat at most every
# WARN_HEARTBEAT_S while it persists. `_LOG_SEEN` is per process (the queue daemon holds one).
WARN_HEARTBEAT_S = 600.0
_LOG_SEEN = {}


def log_on_change(cfg, key, sig, msg, level="WARN", now=None, heartbeat_s=None):
    """log() once per state change of `key` (signature `sig`) and at most every
    `heartbeat_s` while the same signature persists. -> True if a line was written."""
    now = time.time() if now is None else now
    hb = WARN_HEARTBEAT_S if heartbeat_s is None else heartbeat_s
    last = _LOG_SEEN.get(key)
    if last is not None and last[0] == sig and now - last[1] < hb:
        return False
    _LOG_SEEN[key] = (sig, now)
    log(cfg, msg + (" [still: heartbeat]" if last is not None and last[0] == sig else ""), level)
    return True


def clear_log_seen(key=None):
    """Forget a condition (it cleared) so its next occurrence logs immediately."""
    if key is None:
        _LOG_SEEN.clear()
    else:
        _LOG_SEEN.pop(key, None)


# ------------------------------------------------------------------ state file
def read_state(cfg):
    try:
        st = json.loads(cfg["state"].read_text())
        if isinstance(st, dict):
            return st
    except (OSError, ValueError):
        pass
    return {"mode": "bloom", "phase": "idle"}


def write_state(cfg, **upd):
    """Merge `upd` into the state file (atomic) and stamp the heartbeat."""
    if cfg["dry_run"]:
        return
    st = read_state(cfg)
    st.update(upd)
    st["heartbeat"] = time.time()
    cfg["state"].parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg["state"].with_name(cfg["state"].name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, cfg["state"])


# ------------------------------------------------------------------ BloomGauge API
class BloomDown(Exception):
    pass


class BloomShape(Exception):
    """The control API answered but not in the shape this module understands (API drift)."""


def _http(cfg, method, path, body=None, headers=None):
    req = urllib.request.Request(cfg["bloom"] + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=cfg["http_timeout"]) as r:
            raw = r.read() or b"{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload
    except ValueError as e:                    # non-JSON 200
        raise BloomShape("non-JSON response: %s" % scrub(e))
    except (urllib.error.URLError, OSError) as e:
        raise BloomDown(scrub(e))


def bloom_view(cfg):
    """-> {"control_version", "mode" ('on'|'manual'), "can_enable", "provider_running",
    "provider_version", "pinned", "serving"}. Raises BloomDown / BloomShape."""
    s, ctl = _http(cfg, "GET", "/api/optimizer/control")
    if s != 200:
        raise BloomShape("GET /api/optimizer/control HTTP %s" % s)
    auto = ctl.get("automatic") if isinstance(ctl, dict) else None
    if (not isinstance(ctl, dict) or not isinstance(ctl.get("controlVersion"), str)
            or not isinstance(auto, dict) or auto.get("mode") not in ("on", "manual")):
        raise BloomShape("control view lacks controlVersion/automatic.mode (BloomGauge API changed?)")
    mgr = ctl.get("manager") if isinstance(ctl.get("manager"), dict) else {}
    return {
        "control_version": ctl["controlVersion"],
        "mode": auto["mode"],
        "can_enable": bool(auto.get("canEnable")),
        "provider_running": bool(ctl.get("providerRunning")),
        "provider_version": ctl.get("providerVersion"),
        "pinned": bool(mgr.get("pinned")),
        "serving": ctl.get("currentModel"),
    }


def bloom_post(cfg, body):
    """POST a control action. -> (ok, why). Shape-checks the answer."""
    s, r = _http(cfg, "POST", "/api/optimizer/control", body,
                 {"X-Bloom-Action": "optimizer", "Content-Type": "application/json"})
    if not isinstance(r, dict):
        raise BloomShape("POST answer is not an object")
    err = r.get("error")
    if s == 200 and not err:
        if not isinstance(r.get("controlVersion"), str):
            raise BloomShape("POST answer lacks controlVersion")
        return True, "ok"
    return False, scrub(err or "HTTP %s" % s)


def bloom_set(cfg, action, **extra):
    """One control call with a fresh controlVersion. -> (ok, why)."""
    v = bloom_view(cfg)
    body = {"action": action, "requestId": str(uuid.uuid4()), "expectedControl": v["control_version"]}
    body.update(extra)
    if action == "set-automatic" and extra.get("enabled") and not v["provider_running"]:
        body["expectedProvider"] = v["provider_version"]
    if cfg["dry_run"]:
        log(cfg, "DRY-RUN: POST /api/optimizer/control %s" % json.dumps(
            {k: body[k] for k in body if k not in ("requestId", "expectedControl", "expectedProvider")}))
        return True, "dry-run"
    return bloom_post(cfg, body)


# ------------------------------------------------------------------ Darkbloom
def db_base_key(cfg):
    try:
        rec = json.loads(cfg["local_json"].read_text())
    except (OSError, ValueError):
        return None, None
    # local.json loses base_url when Darkbloom restarts (canary models this): fall back to the same
    # DARKBLOOM_BASE_URL override the rest of the pipeline honours before guessing port 8000.
    base = str(rec.get("base_url") or _env("DARKBLOOM_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return (base or "http://127.0.0.1:%s" % (rec.get("port") or 8000)), rec.get("api_key")


def db_health(cfg):
    base, _ = db_base_key(cfg)
    if not base:
        return False
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            return r.status == 200 and json.loads(r.read()).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def db_listed(cfg):
    base, key = db_base_key(cfg)
    if not base:
        return None
    try:
        req = urllib.request.Request(base + "/v1/models", headers={"Authorization": "Bearer " + str(key or "")})
        with urllib.request.urlopen(req, timeout=5) as r:
            return [str(m.get("id")) for m in json.loads(r.read()).get("data", [])]
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None


def toml_text(cfg):
    try:
        return cfg["toml"].read_text()
    except OSError:
        return ""


def toml_enabled(cfg):
    out = []
    for m in re.finditer(r"(?m)^\s*enabled_models\s*=\s*\[([^\]]*)\]", toml_text(cfg)):
        out += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    return out


def toml_auto_update(cfg):
    m = re.search(r"(?m)^\s*auto_update\s*=\s*(true|false)", toml_text(cfg))
    return None if not m else m.group(1) == "true"


def loaded_models(cfg):
    try:
        return [str(x) for x in (json.loads(cfg["loaded_json"].read_text()).get("models") or [])]
    except (OSError, ValueError, AttributeError):
        return []


# ------------------------------------------------------------------ memory feasibility (2026-10-12)
# `darkbloom status` is the only place the provider publishes its live load-gate arithmetic
# ("Live load memory: 25.2 GB usable now", "Cold load blocked (memory): M -- 23.8 GB model + 6.5 GB
# serving reserve = 30.3 GB needed ... Still 1.8-3.1 GB short after idle eviction", "Last
# model-load error: M: Insufficient memory (26.7 GB free, need 30.3 GB) ..."). The provider counts
# Mach free + INACTIVE pages minus max(memory_reserve_gb, physical - 90% cap) as usable; ACTIVE
# file-backed pages (the model files a restart's WeightHasher just re-read) are NOT credited
# (docs/architecture/scheduling.md "Shared-host memory admission"; sudo purge raised usable 21.6 ->
# 34.9 GB live 2026-10-12). A cold load that cannot fit is refused (HTTP 429 "Provider capacity is
# temporarily unavailable") until memory frees -- restarting the provider cannot help.
_GB = r"([\d.]+)(?:\s*-\s*([\d.]+))?\s*GB"


def parse_status_memory(text):
    """PURE. `darkbloom status` text -> {"usable": (lo, hi)|None, "warm": [ids] | None,
    "blocked": {model: {"needed", "short": (lo, hi)|None}}, "last_error": {"model", "free", "need"}|None}.
    Unknown/odd text yields None/empty parts, never an exception."""
    out = {"usable": None, "warm": None, "blocked": {}, "last_error": None}
    text = str(text or "")
    m = re.search(r"Live load memory:\s*" + _GB + r"\s*usable", text)
    if m:
        lo = float(m.group(1))
        out["usable"] = (lo, float(m.group(2)) if m.group(2) else lo)
    m = re.search(r"(?m)^Warm models:\s*(.*)$", text)
    if m:
        w = m.group(1).strip()
        out["warm"] = [] if (not w or w.lower().startswith("none")) else [x.strip() for x in w.split(",") if x.strip()]
    for m in re.finditer(r"Cold load blocked \(memory\):\s*([^\s:]+)\s*[\u2014\u2013-]+\s*([\d.]+)\s*GB model \+ "
                         r"([\d.]+)\s*GB serving reserve = ([\d.]+)\s*GB needed([^\n]*)", text):
        sh = re.search(r"Still\s*" + _GB + r"\s*short", m.group(5))
        short = None
        if sh:
            short = (float(sh.group(1)), float(sh.group(2)) if sh.group(2) else float(sh.group(1)))
        out["blocked"][m.group(1).lower()] = {"needed": float(m.group(4)), "short": short}
    m = re.search(r"Last model-load error:\s*([^\s:]+):\s*Insufficient memory \(([\d.]+)\s*GB free, need ([\d.]+)\s*GB\)", text)
    if m:
        out["last_error"] = {"model": m.group(1).lower(), "free": float(m.group(2)), "need": float(m.group(3))}
    return out


def infeasible_for(info, model):
    """PURE. None when `model` can load (or the status gives no verdict: 'temporarily busy', odd
    text). Else {"needed", "usable", "short", "reason"} for a model that is NOT warm and whose
    cold load the provider says cannot fit: a 'Cold load blocked (memory)' line for it, or its
    latest 'Insufficient memory (X free, need Y)' error while usable is still below Y."""
    if not info:
        return None
    low = str(model or "").lower()
    if low in {str(w).lower() for w in (info.get("warm") or [])}:
        return None
    usable = info.get("usable")
    needed = short = None
    b = (info.get("blocked") or {}).get(low)
    if b:
        needed = b["needed"]
        short = b["short"]
    else:
        e = info.get("last_error")
        if e and e["model"] == low and usable is not None and usable[1] < e["need"]:
            needed = e["need"]
    if needed is None:
        return None
    if usable is None:
        return None
    if short is None:
        short = (max(0.0, needed - usable[1]), max(0.0, needed - usable[0]))
    have = ("%.1f" % usable[0]) if usable[0] == usable[1] else ("%.1f-%.1f" % usable)
    sh = ("%.1f" % short[0]) if short[0] == short[1] else ("%.1f-%.1f" % short)
    return {"needed": needed, "usable": usable, "short": short, "model": low,
            "reason": "needs %.1f GB, have %s GB usable (%s GB short)" % (needed, have, sh)}


def memory_feasibility(cfg=None, run=None, timeout=15):
    """`darkbloom status` -> parse_status_memory dict, or None when it cannot be read (provider
    down, timeout). `run` is a test seam returning the status text. Never raises."""
    try:
        if run is not None:
            return parse_status_memory(run())
        cfg = cfg or load_cfg()
        p = subprocess.run([cfg["bin"], "status"], capture_output=True, text=True, timeout=timeout)
        return parse_status_memory(p.stdout) if p.returncode == 0 else None
    except Exception:
        return None


def cold_infeasible(model, cfg=None, run=None, loaded=None):
    """None, or the infeasible_for() dict, for `model` when it is not resident (not in
    loaded-models.json) and the provider's own arithmetic says its cold load cannot fit.
    A resident model costs nothing here (no subprocess): the healthy case is unchanged."""
    try:
        cfg = cfg or load_cfg()
        ld = loaded_models(cfg) if loaded is None else loaded
        if str(model or "").lower() in {str(x).lower() for x in ld}:
            return None
        inf = infeasible_for(memory_feasibility(cfg, run=run), model)
        if inf and run is None and maybe_purge(cfg):
            # file cache counted against "usable"; one purge often frees the shortfall -- re-ask the provider
            inf = infeasible_for(memory_feasibility(cfg), model)
        return inf
    except Exception:
        return None


def unfinished_requests(cfg=None, run=None, timeout=15):
    """`darkbloom status` 'Model switch: serving; N unfinished request(s)' -> N, or None when the line is
    absent/unreadable (provider not running: nothing to drain). `run` is a test seam. Never raises."""
    try:
        if run is not None:
            text = run()
        else:
            cfg = cfg or load_cfg()
            p = subprocess.run([cfg["bin"], "status"], capture_output=True, text=True, timeout=timeout)
            text = p.stdout if p.returncode == 0 else ""
        m = re.search(r"(?m)^Model switch:[^\n]*?(\d+)\s+unfinished request", str(text or ""))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def parse_status_ops(text):
    """PURE. `darkbloom status` (0.10.4) -> operational state: running, draining ('Not serving: draining'),
    switch ('Model switch: <state>'), timed_out, unfinished, selection ('Selection: <model>'), filter ('Enabled
    model filter'), outside_filter ('Serving outside the enabled model filter'), admission_closed, other_op
    ('... draining for another operation'), blocked (no new switch/start may be issued), reconcile (a timed-out
    switch with no request left: repeat `darkbloom switch` to reopen admission)."""
    t = str(text or "")

    def one(pat):
        m = re.search(pat, t, re.M)
        return m.group(1).strip() if m else None
    ns = one(r"^Not serving:\s*(.*)$")
    sw = one(r"^Model switch:\s*([A-Za-z]+)")
    m = re.search(r"(?m)^(?:Model switch|Not serving):[^\n]*?(\d+)\s+unfinished request", t)
    n = int(m.group(1)) if m else None
    out = one(r"^Serving outside the enabled model filter:\s*([^(\n]*)")
    flt = one(r"^Enabled model filter:\s*(.*)$")
    draining = bool(ns and ns.lower().startswith("draining"))
    timed_out = (sw or "").lower() == "timedout"
    other = bool(re.search(r"draining for another operation|Provider is (?:starting|updating)", t))
    closed = draining or "Admission remains closed" in t
    return {"running": bool(re.search(r"(?m)^Daemon: running", t)), "draining": draining, "not_serving": ns,
            "switch": sw, "timed_out": timed_out, "unfinished": n, "selection": one(r"^\s*Selection:\s*(\S+)"),
            "filter": [x.strip() for x in flt.split(",") if x.strip()] if flt else [],
            "outside_filter": [x.strip() for x in out.split(",") if x.strip()] if out else [],
            "admission_closed": closed, "other_op": other, "blocked": draining or timed_out or other,
            "reconcile": timed_out and not n}


def provider_ops(cfg, timeout=20):
    """`darkbloom status` -> parse_status_ops dict, or None when it cannot be read. Read-only. Never raises."""
    try:
        p = subprocess.run([cfg["bin"], "status"], capture_output=True, text=True, timeout=timeout)
        return parse_status_ops(p.stdout + "\n" + p.stderr)
    except Exception:
        return None


def _note_drain(cfg, ops, ctx):
    """Record + log one drain poll (phase draining, in-flight count, elapsed); ALERT once past drain_alert_s."""
    st = read_state(cfg)
    d = st.get("drain") if isinstance(st.get("drain"), dict) else {}
    now = time.time()
    since = float(d.get("since") or now)
    el = now - since
    d2 = {"since": since, "checked_at": now, "elapsed_s": round(el), "unfinished": ops.get("unfinished"),
          "switch": ops.get("switch"), "selection": ops.get("selection"), "not_serving": ops.get("not_serving"),
          "outside_filter": ops.get("outside_filter"), "admission_closed": bool(ops.get("admission_closed")),
          "alert": bool(d.get("alert"))}
    log(cfg, "DRAIN: %s -- %s; %s unfinished request(s); switch=%s; selection=%s; admission %s; %ds so far" % (
        ctx, ops.get("not_serving") or ("another operation" if ops.get("other_op") else "busy"), ops.get("unfinished"),
        ops.get("switch"), ops.get("selection"), "CLOSED" if d2["admission_closed"] else "open", el), "WARN")
    if d2["admission_closed"] and el >= cfg["drain_alert_s"] and not d2["alert"]:
        d2["alert"] = True
        log(cfg, "ALERT: Darkbloom admission closed for %d min (%s unfinished, switch=%s): a human decides -- wait, "
                 "`darkbloom switch` to reconcile, or `darkbloom restart`. bloom_control will not force it." % (
                     el // 60, ops.get("unfinished"), ops.get("switch")), "ERROR")
    write_state(cfg, mode="queue", phase="draining", drain=d2,
                last_error="darkbloom draining (%s unfinished, switch=%s, %ds)%s" % (
                    ops.get("unfinished"), ops.get("switch"), el, " ALERT" if d2["alert"] else ""))
    return d2


def _clear_drain(cfg):
    d = read_state(cfg).get("drain")
    if d:
        log(cfg, "DRAIN: over after %ds; admission open" % (time.time() - float((d or {}).get("since") or time.time())))
        write_state(cfg, drain=None)


def _drain_result(cfg, ops, ctx):
    d = _note_drain(cfg, ops, ctx)
    return {"ok": False, "noop": False, "draining": True,
            "why": "darkbloom draining: %s unfinished, switch=%s, %ss%s" % (
                d["unfinished"], d["switch"], d["elapsed_s"], " (ALERT)" if d["alert"] else "")}


def _drain_gate(cfg, ctx):
    """Before a hold/release touches Darkbloom: None = go ahead; else the draining result (nothing issued). A timed-out
    switch whose requests are done is reconciled with ONE repeated `darkbloom switch` to its Selection."""
    if cfg["dry_run"]:
        return None
    ops = provider_ops(cfg)
    if ops is None or not ops["blocked"]:
        _clear_drain(cfg)
        return None
    if ops["reconcile"]:
        tgt = [ops["selection"]] if ops.get("selection") else list(cfg["pair"])
        log(cfg, "DRAIN: timed-out switch has no unfinished request left: repeating `darkbloom switch` (%s) to "
                 "reconcile" % ", ".join(tgt), "WARN")
        rc, tail = _switch_cli(cfg, tgt, "(reconcile)")
        ops2 = provider_ops(cfg)
        if rc == 0 and (ops2 is None or not ops2["blocked"]):
            _clear_drain(cfg)
            return None
        log(cfg, "DRAIN: reconcile switch rc=%s %s" % (rc, tail), "WARN")
        ops = ops2 or ops
    return _drain_result(cfg, ops, ctx)


def _switch_cli(cfg, models, why):
    """ONE `darkbloom switch` with the long drain deadline; `darkbloom status` is polled every drain_poll_s while it
    runs (state + a log line per poll when it drains). -> (rc, scrubbed tail)."""
    T = int(cfg["switch_timeout_s"])
    args = ["switch"] + [a for m in models for a in ("--model", m)] + ["--timeout", str(T)]
    if cfg["dry_run"]:
        return run_cli(cfg, args, T + 120)
    ops = provider_ops(cfg)
    log(cfg, "SET: darkbloom switch (%s) %s, drain timeout %ds, %s request(s) in flight" % (
        ", ".join(models), why, T, None if ops is None else ops.get("unfinished")))
    try:
        p = subprocess.Popen([cfg["bin"]] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return 127, scrub(e)
    t0 = time.monotonic()
    nxt = t0 + cfg["drain_poll_s"]
    while p.poll() is None:
        if time.monotonic() - t0 > T + 120:
            p.kill()
            p.wait()
            return 124, "switch CLI still running after %ss" % (T + 120)
        if time.monotonic() >= nxt:
            nxt = time.monotonic() + cfg["drain_poll_s"]
            o = provider_ops(cfg)
            if o and o["blocked"]:
                _note_drain(cfg, o, "switch to %s in progress" % ", ".join(models))
            else:
                _beat(cfg)
        time.sleep(min(1.0, max(cfg["poll_s"], 0.05)))
    out = p.stdout.read() if p.stdout else ""
    return p.returncode, scrub((out or "").strip()[-300:])


def wait_no_unfinished(cfg, why, run=None):
    """Before any start that unloads resident models: wait up to unload_wait_s for 0 unfinished requests
    held CONTINUOUSLY for unload_quiet_s (Darkbloom serves network jobs of its own; one idle sample is a race).
    -> (ok, n). Never unloads a busy model: the caller waits (INFRA WAIT) and retries on its next hold."""
    deadline = time.monotonic() + cfg["unload_wait_s"]
    quiet_since = None
    while True:
        n = unfinished_requests(cfg, run=run)
        now = time.monotonic()
        if n is None:
            return True, n                          # daemon not running / status unreadable: nothing in flight to protect
        if n:
            quiet_since = None
        else:
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= cfg.get("unload_quiet_s", 0):
                return True, n
        if now >= deadline:
            log_on_change(cfg, "unload-busy", str(n),
                          "UNLOAD: %s unfinished request(s) / no %ds quiet window within %ds (%s): NOT restarting, will retry" % (
                              n, int(cfg.get("unload_quiet_s", 0)), int(cfg["unload_wait_s"]), why), "WARN")
            return False, n
        _beat(cfg)
        time.sleep(max(cfg["poll_s"], 0.05))


_LAST_PURGE = [0.0]


def maybe_purge(cfg=None):
    """True when `sudo -n /usr/sbin/purge` ran (rc 0). Rate-limited (BLOOMCTL_PURGE_MIN_S, default 120);
    BLOOMCTL_PURGE=0 disables; needs a passwordless sudoers entry for purge. Never raises."""
    try:
        if str(_env("BLOOMCTL_PURGE", "1")) == "0":
            return False
        now = time.time()
        if now - _LAST_PURGE[0] < float(_env("BLOOMCTL_PURGE_MIN_S", 120)):
            return False
        _LAST_PURGE[0] = now
        p = subprocess.run(["sudo", "-n", "/usr/sbin/purge"], capture_output=True, text=True, timeout=60)
        if cfg:
            log(cfg, "model switch/cold load: sudo purge rc=%s" % p.returncode, "INFO" if p.returncode == 0 else "WARN")
        if p.returncode == 0:
            time.sleep(3)
        return p.returncode == 0
    except Exception:
        return False


def same_models(a, b):
    return {str(x).lower() for x in a} == {str(x).lower() for x in b}


def pair_ready(cfg, models=None):
    """(ok, why): /health, /v1/models and loaded-models.json all include the pair (or `models`). Extra served
    models (darkbloom start --all, Gemma) are allowed: they never make it "not ready" (no toml rewrite, no restart)."""
    pair = models or cfg["pair"]
    if not db_health(cfg):
        return False, "/health not ok"
    listed = db_listed(cfg)
    if listed is None or not {m.lower() for m in pair} <= {m.lower() for m in listed}:
        return False, "/v1/models=%s" % listed
    if not {m.lower() for m in pair} <= {m.lower() for m in loaded_models(cfg)}:
        return False, "loaded-models.json=%s" % loaded_models(cfg)
    return True, "ok"


def set_ready(cfg, models):
    """(ok, why): /health ok and /v1/models lists every one of `models` (the restore check)."""
    if not db_health(cfg):
        return False, "/health not ok"
    listed = db_listed(cfg)
    if listed is None or not {m.lower() for m in models} <= {m.lower() for m in listed}:
        return False, "/v1/models=%s" % listed
    return True, "ok"


def desired_toml(text, pair, enabled=None):
    """PURE. provider.toml text with [backend] max_model_slots=2, idle_timeout_mins=0 and
    preload_models=pair (canonical-case ids); enabled_models=`enabled` when given (pair-restart mode).
    Returns (new_text, [changes])."""
    changes = []

    def setkey(src, key, new_line):
        pat = re.compile(r"(?m)^(\s*)%s\s*=\s*(?:\[[^\]]*\].*|.*)$" % re.escape(key))
        m = pat.search(src)
        if m:
            if m.group(0).strip() != new_line:
                changes.append("%s: %s -> %s" % (key, m.group(0).strip(), new_line))
                return pat.sub(lambda mm: mm.group(1) + new_line, src, count=1)
            return src
        changes.append("%s: (absent) -> %s" % (key, new_line))
        return re.sub(r"(?m)^(\[backend\]\s*\n)", lambda mm: mm.group(1) + new_line + "\n", src, count=1)

    text = setkey(text, "max_model_slots", "max_model_slots = 2")
    text = setkey(text, "idle_timeout_mins", "idle_timeout_mins = 0")
    text = setkey(text, "preload_models", "preload_models = [ %s ]" % ", ".join("'%s'" % m for m in pair))
    if enabled:
        text = setkey(text, "enabled_models", "enabled_models = [ %s ]" % ", ".join("'%s'" % m for m in enabled))
    return text, changes


def _write_toml(cfg, new, changes):
    """Back up provider.toml, then atomic replace. -> True on success. Never raises."""
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        why = "provider.toml edit refused: new text is not valid TOML (%s)" % scrub(e)
        log(cfg, why, "WARN")
        write_state(cfg, last_error=why)
        return False
    bak = cfg["toml"].with_name(cfg["toml"].name + ".bak-bloomctl-%d" % int(time.time()))
    try:
        shutil.copy2(cfg["toml"], bak)
        tmp = cfg["toml"].with_name(cfg["toml"].name + ".tmp-bloomctl")
        tmp.write_text(new)
        os.replace(tmp, cfg["toml"])
        log(cfg, "provider.toml: " + "; ".join(changes))
        return True
    except OSError as e:
        log(cfg, "provider.toml edit failed: %s" % scrub(e), "WARN")
        return False


def needed_models(cfg):
    """Models the queue needs served right now (queue sets cfg["needed"]); default: the primary (35B)."""
    n = [m for m in (cfg.get("needed") or []) if m]
    return n or [cfg["primary"]]


def _try_noswitch_first(cfg, why):
    """Pair-restart mode, before any restart: for up to restart_after_s send fast, jittered 1-token warms so
    the PROVIDER loads the needed model if it can (a 429 is harmless; Gemma is never requested). -> (ready, why).
    No darkbloom CLI, no toml edit here."""
    import random
    needed = needed_models(cfg)
    if not db_health(cfg) or db_listed(cfg) is None or \
            not {m.lower() for m in needed} <= {m.lower() for m in (db_listed(cfg) or [])}:
        return False, why                        # not even advertised: nothing a request can do
    maybe_purge(cfg)                             # switch: free file cache before the provider tries to load
    deadline = time.monotonic() + cfg["restart_after_s"]
    log(cfg, "HOLD: %s not resident (%s): trying the provider (no restart) for up to %ds" % (
        ", ".join(needed), why, cfg["restart_after_s"]))
    while True:
        res = {x.lower() for x in loaded_models(cfg)}
        for m in needed:
            if m.lower() not in res:
                st, w = _warm_one(cfg, m)
                if st == "retry":
                    log_on_change(cfg, "pr-try-" + m, w, "HOLD: provider refused %s (%s); retrying" % (m, w), "INFO")
        ready, why = pair_ready(cfg, needed)
        if ready:
            log(cfg, "HOLD: %s served by the provider without a restart" % ", ".join(needed))
            return True, why
        if time.monotonic() >= deadline:
            log(cfg, "HOLD: %s still not resident after %ds (%s): falling back to a pair-only restart" % (
                ", ".join(needed), cfg["restart_after_s"], why), "WARN")
            return False, why
        _beat(cfg)
        time.sleep(max(0.05, random.uniform(0.5, 1.5) * max(cfg["poll_s"], 0.1)))


def _etime_s(pid):
    """Process age in seconds via `ps -o etime=` ([[dd-]hh:]mm:ss), or None."""
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
        if not out:
            return None
        days = 0
        if "-" in out:
            d, out = out.split("-", 1)
            days = int(d)
        parts = [int(x) for x in out.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return None


def provider_start_in_progress(cfg, now=None):
    """(bool, why). True while Darkbloom is mid-start -- /health not ok (or startup preload pending) and the
    provider process is younger than start_grace_s (provider.pid + ps etime; daemon-state started_at), or WE
    issued a `darkbloom start` within the grace and /health is still not ok. Hold must WAIT then: a second
    `darkbloom start` kills the first mid-preload and doubles the downtime (2026-10-13 16:35). Never raises."""
    now = time.time() if now is None else now
    try:
        grace = cfg["start_grace_s"]
        healthy = db_health(cfg)
        ds = _read_json(cfg["daemon_json"])
        pending = bool(isinstance(ds, dict) and ds.get("startup_preload_pending_models"))
        if healthy and not pending:
            return False, "healthy"
        age = None
        try:
            pid = int(Path(cfg["pid_file"]).read_text().strip())
            os.kill(pid, 0)
            age = _etime_s(pid)
        except (OSError, ValueError):
            age = None
        if age is None and isinstance(ds, dict) and ds.get("started_at"):
            try:
                a = now - float(ds["started_at"])
                age = a if 0 <= a and now - float(ds.get("written_at") or 0) <= cfg["ns_state_max_age_s"] else None
            except (TypeError, ValueError):
                age = None
        if age is not None and age < grace:
            return True, "provider process up %ds (< %ds start grace), /health %s%s" % (
                age, grace, "ok" if healthy else "not ok", ", preload pending" if pending else "")
        last = float(read_state(cfg).get("last_start_at") or 0)
        if not healthy and 0 < now - last < grace:
            return True, "our own darkbloom start %ds ago and /health still not ok" % (now - last)
        return False, "no start in progress"
    except Exception:
        return False, "unknown"


def _wait_for_start(cfg, ready_fn):
    """Wait (bounded) for an in-progress provider start to finish. -> (ready, why, still_starting)."""
    deadline = time.monotonic() + cfg["start_wait_s"]
    first = True
    while True:
        ready, why = ready_fn()
        if ready:
            return True, why, False
        busy, bwhy = provider_start_in_progress(cfg)
        if not busy:
            return False, why, False
        if first:
            log(cfg, "HOLD: a provider start is already in progress (%s): waiting, NOT starting it again" % bwhy)
            first = False
        if time.monotonic() >= deadline:
            return False, why, True
        _beat(cfg)
        time.sleep(cfg["poll_s"])


def purge_enabled(cfg=None):
    """Env BLOOM_PURGE=0 turns the purge step off (the memory check still runs and reports)."""
    return _env("BLOOM_PURGE", "1") != "0"


def ensure_memory(cfg, model, run_purge=None, now=None):
    """Memory gate for loading `model`. -> (ok, reason|None, info). ok when the provider's live usable GB
    already covers need (estimate + headroom), the figures are unknown (fail open), or a rate-limited
    `purge` (sudo -n /usr/sbin/purge) lifted it there. Otherwise ok=False and reason reads like
    'waiting on memory: need 30.3 GB, have 22.1 GB usable (8.2 GB short); purge ...'. Never kills or quits
    anything; a purge is logged with before/after GB and recorded in state["purge"]. Never raises."""
    now = time.time() if now is None else now
    try:
        snap = provider_snapshot(cfg)
        need = model_need_gb(cfg, model, snap)
        usable = snap.get("usable")
        if str(model).lower() in {x.lower() for x in snap.get("resident") or []} or need is None \
                or usable is None or not snap.get("ok"):
            return True, None, {"need": need, "usable": usable}
        if usable >= need:
            return True, None, {"need": need, "usable": usable}
        note = ""
        rec = dict(read_state(cfg).get("purge") or {})
        if not purge_enabled(cfg):
            note = "; purge disabled (BLOOM_PURGE=0)"
        elif now - float(rec.get("at") or 0) < cfg["purge_min_s"]:
            note = "; purge ran %ds ago (%s) and is rate-limited to one per %ds" % (
                now - float(rec.get("at") or 0), rec.get("why") or "no effect", cfg["purge_min_s"])
        else:
            t0 = time.time()
            if run_purge is not None:
                rc, tail = run_purge()
            elif cfg["dry_run"]:
                log(cfg, "DRY-RUN: " + " ".join(cfg["purge_cmd"]))
                rc, tail = 0, "dry-run"
            else:
                try:
                    p = subprocess.run(cfg["purge_cmd"], capture_output=True, text=True, timeout=90)
                    rc, tail = p.returncode, scrub((p.stdout + p.stderr).strip()[-200:])
                except subprocess.TimeoutExpired:
                    rc, tail = 124, "timed out"
                except OSError as e:
                    rc, tail = 127, scrub(e)
            after = usable
            if rc == 0:
                end = time.monotonic() + cfg["purge_settle_s"]
                while time.monotonic() < end:        # wait for a capacity figure newer than the purge
                    s2 = provider_snapshot(cfg)
                    if s2.get("usable") is not None and (s2.get("age_s") is not None and
                                                         time.time() - s2["age_s"] > t0):
                        after = s2["usable"]
                        break
                    time.sleep(min(1.0, max(0.05, cfg["poll_s"])))
                else:
                    s2 = provider_snapshot(cfg)
                    after = s2["usable"] if s2.get("usable") is not None else usable
                why = "%.1f -> %.1f GB usable" % (usable, after)
                log(cfg, "PURGE: ran %s: %s (need %.1f GB for %s)" % (" ".join(cfg["purge_cmd"]), why, need, model))
                usable = after
            else:
                why = "unavailable (rc=%s %s)" % (rc, tail)
                log(cfg, "PURGE unavailable rc=%s %s -- needs a sudoers NOPASSWD entry for /usr/sbin/purge" % (rc, tail), "WARN")
                note = "; purge unavailable (needs sudoers NOPASSWD /usr/sbin/purge)"
            write_state(cfg, purge={"at": now, "rc": rc, "before": round(float(snap["usable"]), 1),
                                    "after": round(float(usable), 1), "why": why})
            if rc == 0:
                note = "; purge ran: " + why
            if usable >= need:
                return True, None, {"need": need, "usable": usable, "purged": True}
        short = need - usable
        return False, ("waiting on memory: need %.1f GB, have %.1f GB usable (%.1f GB short)%s; "
                       "other apps hold the rest, nothing is closed or killed" % (need, usable, short, note)), {
            "need": need, "usable": usable, "short": short}
    except Exception as e:
        log(cfg, "ensure_memory error: %r" % e, "WARN")
        return True, None, {}


def _memory_infeasible(cfg, model, reason, info):
    _note_infeasible(cfg, {"model": model, "reason": reason, "needed": info.get("need") or 0.0,
                           "usable": (info.get("usable") or 0.0, info.get("usable") or 0.0)}, model)


def hold_models(cfg):
    """The models a pair-restart hold serves: exactly the pair, primary first."""
    pair = list(cfg["pair"])
    return sorted(pair, key=lambda m: m.lower() != str(cfg["primary"]).lower())


def run_cli(cfg, args, timeout):
    """Run the darkbloom CLI. -> (rc, scrubbed tail). Never raises."""
    cmd = [cfg["bin"]] + list(args)
    if cfg["dry_run"]:
        log(cfg, "DRY-RUN: " + " ".join([Path(cfg["bin"]).name] + list(args)))
        return 0, "dry-run"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, scrub((p.stdout + p.stderr).strip()[-300:])
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except OSError as e:
        return 127, scrub(e)


# ------------------------------------------------------------------ get_state
def get_state(cfg=None):
    """Read-only snapshot. Never mutates anything."""
    cfg = cfg or load_cfg()
    st = read_state(cfg)
    out = {"enabled": enabled(cfg), "bloomgauge": bloomgauge_on(cfg),
           "control": {k: st.get(k) for k in ("mode", "phase", "degraded", "last_error", "since")},
           "bloom": None, "bloom_error": None}
    if out["bloomgauge"]:
        try:
            out["bloom"] = bloom_view(cfg)
        except BloomDown as e:
            out["bloom_error"] = "down: %s" % e
        except BloomShape as e:
            out["bloom_error"] = "shape: %s" % e
    ok, why = pair_ready(cfg)
    out.update(pair_ready=ok, pair_why=why, auto_update=toml_auto_update(cfg))
    return out


# ------------------------------------------------------------------ locking
class _Lock:
    """One flock for every mutator. Waits (bounded) so a concurrent caller re-reads state
    after the first finishes and no-ops instead of double-switching."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.fh = None

    def __enter__(self):
        c = self.cfg
        c["lock"].parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(c["lock"], "w")
        end = time.monotonic() + c["lock_wait_s"]
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > end:
                    self.fh.close()
                    raise TimeoutError("bloom-control lock busy")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _beat(cfg, **kw):
    write_state(cfg, **kw)


def _note_infeasible(cfg, inf, model=None):
    """Record WHY a model cannot load so the queue can show 'needs X GB, have Y GB usable (Z short)'
    on the INFRA WAIT lane state (and so a launch does not start a worker that would only 429)."""
    write_state(cfg, infeasible={"model": model or inf["model"], "reason": inf["reason"],
                                 "needed": inf["needed"], "usable": list(inf["usable"]), "at": time.time()})
    log_on_change(cfg, "infeasible", inf["reason"],
                  "MEMORY INFEASIBLE for %s: %s" % (model or inf["model"], inf["reason"]))


def _clear_infeasible(cfg):
    if read_state(cfg).get("infeasible"):
        write_state(cfg, infeasible=None)
    clear_log_seen("infeasible")


def _partial_held(cfg, st):
    """True while the provider serves the primary ALONE by our own memory fallback (phase ready,
    partial set) and everything the partial names is still loaded + listed + healthy."""
    part = st.get("partial")
    if not part or st.get("mode") != "queue" or st.get("phase") != "ready":
        return False
    low = {m.lower() for m in part}
    return (db_health(cfg) and low <= {m.lower() for m in loaded_models(cfg)}
            and low <= {m.lower() for m in (db_listed(cfg) or [])})


def _partial_noop(cfg, st):
    """Partial hold is steady state: never restart the provider to chase the pair. Once per
    partial_retry_s re-read the provider's memory; add the small model back (one start) only when
    the figure says it now fits (-> None: fall through to the normal pair hold)."""
    now = time.time()
    if now - float(st.get("partial_checked_at") or st.get("partial_since") or 0) >= cfg["partial_retry_s"]:
        write_state(cfg, partial_checked_at=now)
        info = memory_feasibility(cfg)
        use = (info or {}).get("usable")
        if use is not None and use[0] >= cfg["small_need_gb"]:
            log(cfg, "PARTIAL: %.1f GB usable now (>= %.1f for the small model): retrying the full pair"
                % (use[0], cfg["small_need_gb"]))
            write_state(cfg, partial=None, partial_since=None, partial_checked_at=None, infeasible=None,
                        last_start_at=0)
            return None
    return {"ok": True, "noop": True, "degraded": True,
            "why": "partial hold (%s only; %s)" % (",".join(st.get("partial") or []),
                                                    (st.get("infeasible") or {}).get("reason", "memory"))}


def _serve_primary_alone(cfg, inf, degraded, notes):
    """The pair cannot fit and has not for fallback_s: restart the provider on the PRIMARY model
    only (queue work needing it proceeds; jobs needing the other wait with the explicit memory
    reason). One attempt per restart_min_s. -> hold result dict, or None to keep waiting."""
    st = read_state(cfg)
    if time.time() - float(st.get("last_start_at") or 0) < cfg["restart_min_s"] and st.get("partial_tried"):
        return None
    args = ["start", "--model", cfg["primary"], "--idle-timeout", "0", "--local-endpoint",
            "--timeout", str(cfg["drain_s"])]
    if cfg["force"]:
        args.append("--force")
    if not cfg["dry_run"] and not wait_no_unfinished(cfg, "memory fallback")[0]:
        return None                                 # never unload a model with unfinished requests: keep waiting
    log(cfg, "MEMORY FALLBACK: pair infeasible (%s) -- serving %s ALONE (the other model waits)"
        % (inf["reason"], cfg["primary"]), "WARN")
    write_state(cfg, last_start_at=time.time(), partial_tried=True)
    rc, tail = run_cli(cfg, args, cfg["drain_s"] + 300)
    log(cfg, "darkbloom start (primary only) rc=%s %s" % (rc, tail), "INFO" if rc == 0 else "ERROR")
    if rc != 0:
        return None
    deadline = time.monotonic() + cfg["wait_s"]
    low = cfg["primary"].lower()
    while time.monotonic() < deadline:
        if db_health(cfg) and low in {m.lower() for m in loaded_models(cfg)} \
                and low in {m.lower() for m in (db_listed(cfg) or [])}:
            others = [m for m in cfg["pair"] if m.lower() != low]
            write_state(cfg, mode="queue", phase="ready", degraded=True, partial=[cfg["primary"]],
                        partial_since=time.time(), partial_checked_at=time.time(),
                        infeasible={"model": ",".join(others), "reason": inf["reason"], "needed": inf["needed"],
                                    "usable": list(inf["usable"]), "at": time.time()},
                        last_error="partial: %s only (memory: %s)" % (cfg["primary"], inf["reason"]))
            log(cfg, "HOLD: PARTIAL -- %s served alone (memory: %s); %s wait" % (
                cfg["primary"], inf["reason"], ",".join(others)), "WARN")
            return {"ok": True, "noop": False, "degraded": True, "partial": True,
                    "why": "partial: %s only (%s)" % (cfg["primary"], inf["reason"]), "notes": notes}
        _beat(cfg)
        time.sleep(cfg["poll_s"])
    write_state(cfg, mode="queue", phase="failed",
                last_error="primary %s not served after the fallback start (%s)" % (cfg["primary"], inf["reason"]))
    return {"ok": False, "noop": False, "degraded": degraded,
            "why": "primary alone not served: %s" % inf["reason"]}


# ------------------------------------------------------------------ warm-before-first-job (2026-10-12)
# /v1/models + loaded-models.json can list a model that still answers HTTP 429 "Provider capacity is
# temporarily unavailable" to its FIRST request while memory frees (seen 2026-10-12: the 35B failed the
# first job, then served). hold_for_queue therefore sends one 1-token request per pair model BEFORE it
# reports phase=ready (launches keep waiting as infra-wait while phase=switching), retrying 429/503/
# timeouts for warm_s. A model the provider's own gate says cannot fit is skipped (nothing to wait for);
# anything else odd (404/501/auth) is treated as "cannot tell" and never fails or blocks the hold.
def _warm_one(cfg, model, max_tokens=1):
    """One small chat request (1 token by default; the post-load warm-ups use 64). -> (status, why): 'ok',
    'retry' (429/503/timeout), 'unknown'."""
    base, key = db_base_key(cfg)
    if not base:
        return "unknown", "no endpoint"
    body = json.dumps({"model": model, "max_tokens": int(max_tokens), "temperature": 0, "stream": False,
                       "messages": [{"role": "user", "content": "ok" if int(max_tokens) <= 1 else "Say hello in one short sentence."}],
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST", headers={
        "Authorization": "Bearer " + str(key or ""), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["warm_req_s"]) as r:
            return ("ok", "200") if r.status == 200 else ("unknown", "HTTP %s" % r.status)
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            return "retry", "HTTP %s" % e.code
        return "unknown", "HTTP %s" % e.code
    except (urllib.error.URLError, OSError) as e:
        return "retry", scrub(e)
    except ValueError as e:
        return "unknown", scrub(e)


def warm_pair(cfg, sleep=time.sleep):
    """Make each served pair model answer one request. -> {model: 'ok'|'infeasible'|'unknown'|'timeout'}.
    Never raises, never fails the hold."""
    out = {}
    served = {m.lower() for m in (db_listed(cfg) or [])}
    order = [cfg["primary"]] + [m for m in cfg["pair"] if m.lower() != cfg["primary"].lower()]
    for m in order:
        if m.lower() not in served:
            continue
        if m.lower() not in {x.lower() for x in loaded_models(cfg)}:
            maybe_purge(cfg)                     # switch: model not resident, free file cache first
        deadline = time.monotonic() + cfg["warm_s"]
        while True:
            try:
                st, why = _warm_one(cfg, m)
            except Exception as e:
                st, why = "unknown", scrub(repr(e))
            if st == "ok":
                out[m] = "ok"
                break
            if st == "unknown":
                out[m] = "unknown"
                log(cfg, "warm %s: cannot tell (%s); not blocking" % (m, why))
                break
            inf = cold_infeasible(m, cfg)
            if inf:
                out[m] = "infeasible"
                _note_infeasible(cfg, inf, m)
                break
            if time.monotonic() >= deadline:
                out[m] = "timeout"
                log(cfg, "warm %s: still %s after %ds; continuing" % (m, why, cfg["warm_s"]), "WARN")
                break
            log_on_change(cfg, "warm-" + m, why, "warm %s: %s (capacity not free yet); retrying" % (m, why), "INFO")
            _beat(cfg)
            sleep(cfg["poll_s"])
        clear_log_seen("warm-" + m)
    return out


# ------------------------------------------------------------------ WARM-UP requests (2026-10-13)
# d-inference #1238: a freshly restarted/switched provider can be routable yet receive no network work, because the
# coordinator prefers 'Feasible' candidates over 'Unknown' and a provider's evidence goes stale after ~2 min idle.
# A few small requests to our OWN loopback endpoint (same authenticated helper as the readiness warm, _warm_one)
# seed it. Two triggers: (1) right after hold_for_queue restarted the provider and the models are resident;
# (2) while the queue holds, daemon-state stats.requests_served flat for warmup_flat_s with the provider healthy
# -> one more batch, at most once per warmup_min_gap_s. Never while a start/switch is in progress, never for a
# model with a queue job in flight (no extra GPU contention). FAIL-SOFT: nothing here raises or blocks the queue.
# BLOOM_WARMUP_N=0 is the kill switch.
def requests_served(cfg):
    """daemon-state.json stats.requests_served (the provider's own served-request counter), or None."""
    ds = _read_json(cfg["daemon_json"])
    try:
        v = ((ds or {}).get("stats") or {}).get("requests_served")
        return None if v is None or isinstance(v, bool) else int(v)
    except (AttributeError, TypeError, ValueError):
        return None


def _warmup_send(cfg, model):
    return _warm_one(cfg, model, max_tokens=cfg["warmup_tokens"])


def warmup_batch(cfg, models, why, sender=None, sleep=time.sleep):
    """N small requests per model, spaced warmup_space_s apart. -> {model: [status,...]}. Never raises."""
    out = {}
    try:
        n = int(cfg.get("warmup_n") or 0)
        if n <= 0:
            return out
        sender = sender or _warmup_send
        order = list(dict.fromkeys(models or []))
        for mi, m in enumerate(order):
            out[m] = []
            for i in range(n):
                try:
                    st, w = sender(cfg, m)
                except Exception as e:
                    st, w = "unknown", scrub(repr(e))
                out[m].append(st)
                log(cfg, "WARMUP (%s): %s request %d/%d -> %s (%s)" % (why, m, i + 1, n, st, w))
                if not (mi == len(order) - 1 and i == n - 1):
                    try:
                        sleep(cfg["warmup_space_s"])
                    except Exception:
                        pass
    except Exception as e:
        try:
            log(cfg, "WARMUP (%s) aborted: %s" % (why, scrub(repr(e))), "WARN")
        except Exception:
            pass
    return out


def _resident_for_warmup(cfg):
    snap = provider_snapshot(cfg)
    res = snap.get("resident") or [] if snap.get("ok") else loaded_models(cfg)
    return [str(m) for m in res]


def warmup_after_restart(cfg, sender=None, sleep=time.sleep):
    """hold_for_queue just restarted the provider and the models are resident: seed it. Never raises."""
    try:
        if cfg.get("warmup_n", 0) <= 0 or cfg["dry_run"]:
            return {}
        res = _resident_for_warmup(cfg)
        out = warmup_batch(cfg, res, "after provider restart", sender, sleep)
        _warmup_save(cfg, last_batch_at=time.time(), rs=requests_served(cfg), rs_at=time.time())
        return out
    except Exception:
        return {}


def _warmup_load(cfg):
    d = _read_json(cfg["warmup_state"])
    return d if isinstance(d, dict) else {}


def _warmup_save(cfg, **upd):
    try:
        d = _warmup_load(cfg)
        d.update(upd)
        tmp = cfg["warmup_state"].with_name(cfg["warmup_state"].name + ".tmp-%d" % os.getpid())
        cfg["warmup_state"].parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(d))
        os.replace(tmp, cfg["warmup_state"])
    except Exception:
        pass


def warmup_tick(cfg=None, busy_models=(), now=None, sender=None, sleep=time.sleep):
    """Called periodically by the queue (background thread) while it holds Darkbloom. If requests_served has been
    flat for warmup_flat_s, the provider is up/healthy and not mid-start, and the last idle batch is older than
    warmup_min_gap_s, send one batch to every resident model that has NO queue job in flight. -> 'sent'|reason|None.
    Never raises."""
    try:
        cfg = cfg or load_cfg()
        if cfg.get("warmup_n", 0) <= 0 or cfg["dry_run"] or not enabled(cfg):
            return None
        now = time.time() if now is None else now
        st = read_state(cfg)
        if st.get("mode") != "queue" or st.get("phase") != "ready":
            return "not-holding"
        if provider_start_in_progress(cfg, now)[0]:
            return "start-in-progress"
        if not db_health(cfg) or not provider_snapshot(cfg, now).get("ok"):
            return "provider-not-healthy"
        rs = requests_served(cfg)
        if rs is None:
            return "no-counter"
        w = _warmup_load(cfg)
        if w.get("rs") != rs or not w.get("rs_at"):
            _warmup_save(cfg, rs=rs, rs_at=now)
            return "counter-moved"
        if now - float(w["rs_at"]) < cfg["warmup_flat_s"]:
            return "flat-not-long-enough"
        if now - float(w.get("last_batch_at") or 0) < cfg["warmup_min_gap_s"]:
            return "rate-limited"
        busy = {str(m).lower() for m in (busy_models or [])}
        models = [m for m in _resident_for_warmup(cfg) if m.lower() not in busy]
        if not models:
            return "in-flight"
        # reserve the slot BEFORE sending so a slow batch can never be re-entered
        _warmup_save(cfg, last_batch_at=now)
        log(cfg, "WARMUP (idle): requests_served flat at %s for %ds; seeding %s" % (rs, now - float(w["rs_at"]), ", ".join(models)))
        warmup_batch(cfg, models, "idle", sender, sleep)
        _warmup_save(cfg, rs=requests_served(cfg), rs_at=now)      # our own requests must not read as network work
        return "sent"
    except Exception as e:
        try:
            log(cfg or {"log": Path("/dev/null")}, "WARMUP tick error: %s" % scrub(repr(e)), "WARN")
        except Exception:
            pass
        return None



# ------------------------------------------------------------------ NO-SWITCH mode (2026-10-13)
# The provider advertises the pair (and maybe more: Gemma for network demand) with max_model_slots=2 and
# NOTHING pinned. At most two are resident; the 35B (30.3 GB to load = 23.8 weights + 6.5 headroom) and
# Gemma QAT (17.4 + 6.5) do not fit together. In unified mode a local request for an advertised-but-cold
# model loads it if it fits, evicting an IDLE slot if needed, else it is REFUSED (HTTP 429 "Provider
# capacity is temporarily unavailable"), not queued; there is no local-vs-network priority. The owner does not
# want the queue to switch/restart models, so the queue's only levers are WAIT and RETRY:
#   provider_snapshot()  reads daemon-state.json (~1/s) + loaded-models.json -- no CLI, no HTTP
#   model_verdict()      resident | fits | evict-idle | unknown | transition | blocked | not-advertised
#   noswitch_ensure()    background: one bounded 1-token warm per non-resident needed model (the provider
#                        itself evicts/loads); capacity refusal -> bounded backoff, record, never a failure
#   noswitch_gate_reason()  cheap, for the queue's launch loop: None = may launch now
# need_gb = darkbloom's own estimate + load_headroom_gb (23.8 + 6.5 = 30.3 for the 35B: the provider's own
# "need 30.3 GB" message). The brief's "estimate*1.2" would give 35.0 and contradicts that figure; it is
# not used. Estimates come from `darkbloom models list --all --json` (refreshed off the hot path by
# noswitch_ensure), with this fallback table (same source, 2026-10-13) when the CLI is unavailable.
NS_FALLBACK_EST_GB = {
    "qwen3.5-9b": 6.8,
    "qwen3.6-35b-a3b-vl-mtp-mxfp8": 23.8,
    "gemma-4-26b-qat-4bit": 17.4,
    "gemma-4-26b-8bit": 31.2,
    "qwen3.5-35b-a3b": 23.3,
    "nvidia-nemotron-3.5-lightning": 21.3,
    "ternary-bonsai-2-27b": 9.6,
    "eigenlabs/ternary-bonsai-2-9b-mlx-2bit": 3.9,
}
NS_DEFAULT_HEADROOM_GB = 6.5
_NS_EST_CACHE = {"at": 0.0, "est": None}


def noswitch_mode(cfg=None):
    """True when NO-SWITCH handling is on (BLOOM_NOSWITCH != 0 and the integration itself is enabled)."""
    cfg = cfg or load_cfg()
    return _env("BLOOM_NOSWITCH", "1") != "0" and _env("BLOOM_PAIR_RESTART", "1") == "0" and enabled(cfg)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def model_estimates(cfg, refresh=False, run=None, now=None):
    """{lower model id: estimated GB}. Cached ns_est_ttl_s; the CLI (`darkbloom models list --all --json`,
    read-only) runs ONLY when refresh=True (background caller), never on the launch hot path. Falls back
    to NS_FALLBACK_EST_GB. Never raises."""
    now = time.time() if now is None else now
    c = _NS_EST_CACHE
    if refresh and now - c["at"] >= cfg["ns_est_ttl_s"]:
        try:
            if run is not None:
                txt = run()
            else:
                p = subprocess.run([cfg["bin"], "models", "list", "--all", "--json"],
                                   capture_output=True, text=True, timeout=25)
                txt = p.stdout if p.returncode == 0 else ""
            est = {str(m["id"]).lower(): float(m["estimated_memory_gb"])
                   for m in json.loads(txt).get("models", []) if m.get("id") and m.get("estimated_memory_gb")}
            if est:
                c["est"], c["at"] = est, now
        except Exception:
            c["at"] = now - cfg["ns_est_ttl_s"] + 60      # CLI unavailable: retry in a minute, meanwhile fallback
    out = dict(NS_FALLBACK_EST_GB)
    out.update(c["est"] or {})
    return out


def provider_snapshot(cfg, now=None):
    """Read-only view of the provider's own live state files. -> {"ok", "age_s", "resident" [ids],
    "advertised" [ids], "usable" GB|None, "headroom", "transition", "slots", "residents"
    {lower id: {"id","idle_s"|None,"gb"|None}}, "load_error"}. ok=False when daemon-state.json is
    missing/stale/unparseable (callers treat that as UNKNOWN and fail open)."""
    now = time.time() if now is None else now
    ds = _read_json(cfg["daemon_json"])
    ld = _read_json(cfg["loaded_json"])
    snap = {"ok": False, "age_s": None, "resident": [], "advertised": [], "usable": None,
            "headroom": NS_DEFAULT_HEADROOM_GB, "transition": False, "slots": 2, "residents": {}, "load_error": None}
    if isinstance(ds, dict):
        try:
            snap["age_s"] = now - float(ds.get("written_at") or 0)
        except (TypeError, ValueError):
            snap["age_s"] = None
        snap["ok"] = snap["age_s"] is not None and 0 <= snap["age_s"] <= cfg["ns_state_max_age_s"]
        cap = ds.get("capacity") if isinstance(ds.get("capacity"), dict) else {}
        for k, key in (("usable", "load_usable_gb"), ("headroom", "load_headroom_gb")):
            try:
                if cap.get(key) is not None:
                    snap[k] = float(cap[key])
            except (TypeError, ValueError):
                pass
        snap["transition"] = bool(ds.get("load_transition_active") or cap.get("load_transition_active"))
        snap["advertised"] = [str(x) for x in (ds.get("advertised_models") or [])]
        ap = ds.get("autopilot_state") if isinstance(ds.get("autopilot_state"), dict) else {}
        try:
            snap["slots"] = int(ap.get("max_model_slots") or 0) or int(
                (re.search(r"(?m)^\s*max_model_slots\s*=\s*(\d+)", toml_text(cfg)) or [0, 2])[1])
        except (TypeError, ValueError):
            snap["slots"] = 2
        for r in ap.get("resident_models") or []:
            if isinstance(r, dict) and r.get("model_id"):
                def _f(v):
                    try:
                        return None if v is None else float(v)
                    except (TypeError, ValueError):
                        return None
                snap["residents"][str(r["model_id"]).lower()] = {
                    "id": str(r["model_id"]), "idle_s": _f(r.get("idle_seconds")),
                    "gb": _f(r.get("resident_gb")) or _f(r.get("weights_gb")), "weights_gb": _f(r.get("weights_gb"))}
        snap["load_error"] = ds.get("last_model_load_error") if isinstance(ds.get("last_model_load_error"), dict) else None
        res = ld.get("models") if isinstance(ld, dict) else None
        if not isinstance(res, list):
            res = ds.get("warm_models") or []
        snap["resident"] = [str(x) for x in res]
    elif isinstance(ld, dict) and isinstance(ld.get("models"), list):
        snap["resident"] = [str(x) for x in ld["models"]]
    return snap


def model_need_gb(cfg, model, snap, est=None):
    """GB the provider needs free to load `model` (estimate + load_headroom_gb), or None if unknown."""
    e = (est or model_estimates(cfg)).get(str(model).lower())
    if e is None:
        return None
    return e + float(snap.get("headroom") or NS_DEFAULT_HEADROOM_GB)


def _fmt_blockers(items):
    out = []
    for r in items:
        a = r.get("idle_s")
        out.append("%s (serving, last active %ds ago)" % (r["id"], a) if a is not None else "%s (serving)" % r["id"])
    return ", ".join(out)


def model_verdict(cfg, model, snap=None, est=None):
    """PURE given the snapshot. -> {"state", "reason", "need", "usable", "blockers" [ids]} where state is
      resident     already loaded                                   -> nothing to do
      fits         a slot is free and load_usable_gb >= need         -> a warm request loads it
      evict-idle   an IDLE resident can be evicted to make room      -> a warm request makes the provider evict it
      unknown      state unreadable / no idle signal / no estimate   -> let the warm request decide
      transition   load_transition_active (provider is mid-load)     -> wait a moment
      blocked      only ACTIVELY SERVING residents stand in the way  -> INFRA WAIT naming them
      not-advertised  the provider does not list it                  -> old (restart-based) path
    Never raises. We never evict anything ourselves; only the provider loads/evicts."""
    snap = snap if snap is not None else provider_snapshot(cfg)
    low = str(model or "").lower()
    base = {"state": "unknown", "reason": None, "need": None, "usable": snap.get("usable"), "blockers": []}
    try:
        if low in {x.lower() for x in snap.get("resident") or []}:
            return dict(base, state="resident")
        adv = {x.lower() for x in snap.get("advertised") or []}
        if snap.get("ok") and adv and low not in adv:
            return dict(base, state="not-advertised", reason="%s is not advertised by the provider" % model)
        if not snap.get("ok"):
            return dict(base, reason="provider state files unreadable/stale")
        if snap.get("transition"):
            return dict(base, state="transition", reason="provider is mid-load (load_transition_active)")
        need = model_need_gb(cfg, model, snap, est)
        base["need"] = need
        usable = snap.get("usable")
        if need is None or usable is None:
            return dict(base, reason="no memory figures for %s" % model)
        residents = [snap["residents"].get(x.lower()) or {"id": x, "idle_s": None, "gb": None}
                     for x in snap.get("resident") or []]
        free_slot = len(residents) < int(snap.get("slots") or 2)
        if free_slot and usable >= need:
            return dict(base, state="fits")
        idle = [r for r in residents if r["idle_s"] is not None and r["idle_s"] >= cfg["ns_idle_s"]]
        busy = [r for r in residents if r["idle_s"] is not None and r["idle_s"] < cfg["ns_idle_s"]]
        unk = [r for r in residents if r["idle_s"] is None]
        gain = sum((r["gb"] if r["gb"] is not None else model_estimates(cfg).get(r["id"].lower(), 0.0)) for r in idle)
        if idle and usable + gain >= need:
            return dict(base, state="evict-idle", blockers=[r["id"] for r in idle],
                        reason="idle %s can be evicted by the provider" % ", ".join(r["id"] for r in idle))
        if unk:
            return dict(base, reason="no idle signal for %s" % ", ".join(r["id"] for r in unk))
        why = "%s blocked" % model
        if busy or idle:
            why += " by %s" % _fmt_blockers(busy or idle)
        else:
            why += " (memory held outside Darkbloom)"
        if not free_slot and not idle:
            why += ": all %d model slots in use" % int(snap.get("slots") or 2)
        why += ": needs %.1f GB, load_usable %.1f GB" % (need, usable)
        return dict(base, state="blocked", reason=why, blockers=[r["id"] for r in (busy or idle)])
    except Exception as e:
        return dict(base, reason="verdict error: %s" % scrub(repr(e)))


def noswitch_blocked_reason(model, cfg=None, snap=None):
    """Banner/dashboard reason when `model` cannot get a slot right now: blocked by actively-serving
    residents (named), or the provider keeps refusing the warm request despite a loadable-looking state.
    Else None. Pure file reads; no heartbeat requirement. Never raises."""
    try:
        cfg = cfg or load_cfg()
        if not noswitch_mode(cfg):
            return None
        v = model_verdict(cfg, model, snap)
        if v["state"] == "blocked":
            return v["reason"]
        if v["state"] in ("resident", "not-advertised"):
            return None
        rec = (read_state(cfg).get("noswitch") or {}).get(str(model).lower()) or {}
        if rec.get("last_warm") == "retry" and int(rec.get("attempts") or 0) > 0:
            return "%s refused by the provider %d time(s) (%s); state says %s" % (
                model, int(rec["attempts"]), rec.get("last_why") or "capacity", v["state"])
        return None
    except Exception:
        return None


def _ns_sync_alive(cfg, st=None, now=None):
    st = st if st is not None else read_state(cfg)
    now = time.time() if now is None else now
    try:
        return now - float(st.get("noswitch_hb") or 0) <= cfg["warm_req_s"] + 120
    except (TypeError, ValueError):
        return False


def noswitch_gate_reason(model, cfg=None, snap=None, now=None):
    """Why a launch (or the resume of a capacity-paused job) on Darkbloom `model` must WAIT, else None.
    Cheap (file reads only). Resident / not advertised / state unreadable -> None. A model that is
    loadable only by the provider's own eviction waits while the background warm (noswitch_ensure) is
    alive -- and fails OPEN (None) if that thread is dead, so a dead sync can never freeze the queue."""
    try:
        cfg = cfg or load_cfg()
        if not noswitch_mode(cfg):
            return None
        v = model_verdict(cfg, model, snap)
        if v["state"] in ("resident", "not-advertised"):
            return None
        if v["state"] == "unknown" and v["reason"] and "unreadable" in v["reason"]:
            return None
        if not _ns_sync_alive(cfg, now=now):
            return None
        rec = (read_state(cfg).get("noswitch") or {}).get(str(model).lower()) or {}
        if v["state"] == "blocked":
            return v["reason"] + " [provider-managed: retrying, never switching models]"
        extra = ""
        if rec.get("last_why"):
            extra = " (provider answered: %s)" % rec["last_why"]
        return "warming %s: %s%s" % (model, v["reason"] or "the provider loads it on request", extra)
    except Exception:
        return None


def noswitch_ensure(cfg, models, now=None, warm_one=None, sleep=None):
    """Background step (queue thread). For each needed Darkbloom model that is not resident: judge it,
    and (when the verdict allows, rate-limited by bounded backoff) send ONE 1-token warm request so the
    PROVIDER evicts an idle slot and loads it. Never runs the darkbloom CLI to start/stop/switch, never
    edits provider.toml, never fails a job. Records per-model state in bloom-control-state.json
    ("noswitch": {model: {...since, attempts, next_warm_at, reason}}, "noswitch_hb") for the queue gate
    and the dashboard escalation. -> {model: verdict-state or 'warm-ok'|'warm-refused'|...}."""
    now = time.time() if now is None else now
    warm_one = warm_one or _warm_one
    out = {}
    if not provider_snapshot(cfg).get("ok"):
        return out          # no live provider state (sandbox / daemon down / stale): nothing to read, never warm blind
    est = model_estimates(cfg, refresh=True)
    st = read_state(cfg)
    ns = {k: dict(v) for k, v in (st.get("noswitch") or {}).items() if isinstance(v, dict)}
    write_state(cfg, noswitch_hb=now)
    for m in dict.fromkeys(models or []):
        key = m.lower()
        snap = provider_snapshot(cfg)          # real clock: freshness of the provider's files; `now` only paces warms
        v = model_verdict(cfg, m, snap, est)
        s = v["state"]
        rec = ns.get(key) or {}
        if s in ("resident", "not-advertised"):
            if rec:
                log(cfg, "NOSWITCH: %s %s after %ds wait" % (m, "resident" if s == "resident" else "not advertised",
                                                              now - float(rec.get("since") or now)))
                clear_log_seen("ns-block-" + key)
                clear_log_seen("ns-warn-" + key)
            ns.pop(key, None)
            out[m] = s
            continue
        rec.setdefault("since", now)
        rec.update(state=s, reason=v["reason"], blockers=v["blockers"], need=v["need"], usable=v["usable"],
                   model=m, at=now)
        rec.setdefault("attempts", 0)
        due = now >= float(rec.get("next_warm_at") or 0)
        warm = False
        if s in ("fits", "evict-idle", "unknown"):
            warm = due
        elif s == "blocked":
            warm = now - float(rec.get("last_warm_at") or 0) >= cfg["ns_probe_s"] and due
            log_on_change(cfg, "ns-block-" + key, v["reason"], "NOSWITCH: " + str(v["reason"]), "INFO")
            if now - float(rec["since"]) >= cfg["ns_warn_s"]:
                log_on_change(cfg, "ns-warn-" + key, "w", "NOSWITCH ESCALATION: %s waiting %ds for its model: %s"
                              % (m, now - float(rec["since"]), v["reason"]), "WARN", heartbeat_s=1800)
        elif s == "transition":
            rec["next_warm_at"] = now + 5
        out[m] = s
        if warm:
            rec["last_warm_at"] = now
            try:
                res, why = warm_one(cfg, m)
            except Exception as e:
                res, why = "unknown", scrub(repr(e))
            rec["last_warm"], rec["last_why"] = res, why
            if res == "ok":
                out[m] = "warm-ok"
                rec["attempts"] = 0
                rec["next_warm_at"] = 0
                if m.lower() in {x.lower() for x in provider_snapshot(cfg).get("resident") or []}:
                    log(cfg, "NOSWITCH: %s loaded by the provider after %ds" % (m, now - float(rec["since"])))
                    ns.pop(key, None)
                    continue
            elif res == "retry":
                out[m] = "warm-refused"
                rec["attempts"] = int(rec["attempts"]) + 1
                back = min(cfg["ns_backoff_max_s"], cfg["ns_backoff_base_s"] * (2 ** (rec["attempts"] - 1)))
                rec["next_warm_at"] = now + back
                log_on_change(cfg, "ns-refused-" + key, "%s/%s" % (s, why),
                              "NOSWITCH: provider refused %s (%s); verdict=%s; retry in %ds (never switching models)"
                              % (m, why, s, back), "INFO")
            else:
                out[m] = "warm-unknown"
                rec["next_warm_at"] = now + cfg["ns_probe_s"]
        ns[key] = rec
    for key in [k for k in ns if k not in {m.lower() for m in (models or [])}]:
        ns.pop(key, None)          # nothing needs it any more
    write_state(cfg, noswitch=ns, noswitch_hb=time.time())
    return out


def noswitch_active(cfg):
    """True when hold_for_queue must use the no-switch path: mode on, provider healthy and its
    /v1/models ALREADY advertises every pair model."""
    if not noswitch_mode(cfg) or not db_health(cfg):
        return False
    listed = db_listed(cfg)
    return listed is not None and {m.lower() for m in cfg["pair"]} <= {m.lower() for m in listed}


# ------------------------------------------------------------------ hold_for_queue
# ------------------------------------------------------------------ session-aware set
def idle_autopilot_enabled():
    """Kill switch: env BLOOM_IDLE_AUTOPILOT=0 -> the idle set is the old restore list (pair + Gemma)."""
    return _env("BLOOM_IDLE_AUTOPILOT", "1") != "0"


def _excluded(cfg, model):
    low = str(model).lower()
    return "gpt-oss" in low or any(x and x in low for x in cfg.get("exclude_models", []))


def toml_selected(cfg):
    """Autopilot selected_models from provider.toml ([] when absent/unparseable)."""
    out = []
    for m in re.finditer(r"(?m)^\s*selected_models\s*=\s*\[([^\]]*)\]", toml_text(cfg)):
        out += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    return out


def idle_models(cfg):
    """The set served while IDLE: Autopilot's selected_models minus the denylist (gpt-oss), primary then
    Qwen3.5-9B first. Falls back to cfg["restore"] (old pair+Gemma) when disabled/unparseable/empty. The
    denylist also filters the fallback. The hold (pair) path never uses this."""
    base = [m for m in cfg["restore"] if not _excluded(cfg, m)]
    if not idle_autopilot_enabled():
        return base
    sel = toml_selected(cfg)
    bad = [m for m in sel if _excluded(cfg, m)]
    if bad:
        log_on_change(cfg, "idle-set-denylisted", tuple(bad),
                      "idle set: provider.toml selected_models contains %s; excluded from the idle set" % ", ".join(bad))
    seen, keep = set(), []
    for m in sel:
        if not _excluded(cfg, m) and m.lower() not in seen:
            seen.add(m.lower())
            keep.append(m)
    if not keep:
        return base
    order = []
    for p in (cfg["restore"][0] if cfg["restore"] else DEFAULT_PAIR[0], "Qwen3.5-9B"):
        order += [m for m in keep if m.lower() == p.lower() and m not in order]
    return order + [m for m in keep if m not in order]


def idle_gemma_enabled():
    """Kill switch: env BLOOM_IDLE_GEMMA=0 keeps the pre-2026-10-15 behaviour (Gemma back the moment the queue is done)."""
    return _env("BLOOM_IDLE_GEMMA", "1") != "0"


def gauge_handoff(cfg=None):
    """BloomGauge on and the gauge hand-off not disabled (BLOOMCTL_GAUGE_HANDOFF=0): queue work -> Manual + the queue
    model by switch; idle -> BloomGauge's own model + Automatic, with no queue pin/filter left behind."""
    return _env("BLOOMCTL_GAUGE_HANDOFF", "1") != "0" and bloomgauge_on(cfg)


def selection_members(sel):
    """BloomGauge selection -> model ids: 'm' -> [m]; '@combo:["a","b"]' -> [a, b]; anything else -> []."""
    if not isinstance(sel, str) or not sel:
        return []
    if not sel.startswith("@combo:"):
        return [sel]
    try:
        v = json.loads(sel[len("@combo:"):])
    except ValueError:
        return []
    return [m for m in v if isinstance(m, str) and m] if isinstance(v, list) else []


def toml_preload(cfg):
    try:
        return list(((tomllib.loads(toml_text(cfg)).get("backend") or {}).get("preload_models")) or [])
    except Exception:
        return []


def activity_mtime(cfg):
    """mtime of the Claude-activity beacon, or None (missing/unreadable). Never raises."""
    try:
        return Path(cfg["activity_file"]).stat().st_mtime
    except OSError:
        return None


def session_decision(cfg, st, now, work, running, act_mtime):
    """PURE. What the Darkbloom set should be, from activity + queue state.
    -> (target, why); target is "pair", "idle" (pair + Gemma) or None (leave as is).
    work = queue has lane work (blocks idle); running = a job is running now (never change the set under it).
    Queue work -> pair (exempt from the rate limit). Claude activity within idle_s -> pair. Neither for idle_s ->
    idle. Idle->pair for activity alone needs the beacon touched across >= debounce_s (hysteresis, state act_first);
    any activity-driven change is at most once per switch_min_gap_s (state last_switch_at).
    BLOOM_IDLE_GEMMA=0 -> (None, ...) (legacy)."""
    if not idle_gemma_enabled():
        return None, "BLOOM_IDLE_GEMMA=0"
    if gauge_handoff(cfg):      # BloomGauge owns idle: hand back as soon as the queue is done, Claude activity or not
        return ("pair", "queue work") if work else ("idle", "bloomgauge on: hand-off follows the queue only")
    current = "pair" if (st.get("mode") == "queue" or st.get("gemma_removed")) else "idle"
    if work:
        return "pair", "queue work"
    active = act_mtime is not None and 0 <= now - act_mtime < cfg["idle_s"]
    target = "pair" if active else "idle"
    if target == current:
        return None, "already %s" % current
    if running:
        return None, "a job is running: no set change"
    if target == "pair":
        first = float(st.get("act_first") or 0)
        if not first or act_mtime - first < cfg["debounce_s"]:
            return None, "debounce: activity not sustained yet"
    gap = now - float(st.get("last_switch_at") or 0)
    if gap < cfg["switch_min_gap_s"]:
        return None, "rate-limited (%ds since the last change)" % gap
    return target, ("claude active" if active else "idle >= %ds" % cfg["idle_s"])


def note_activity(cfg, st, act_mtime, now=None):
    """Track the first beacon touch of an activity burst (state act_first) for the debounce. A burst ends once the
    beacon is older than idle_s. Returns the (possibly updated) state dict; writes only on change."""
    now = time.time() if now is None else now
    if act_mtime is None or now - act_mtime >= cfg["idle_s"]:
        if st.get("act_first"):
            write_state(cfg, act_first=0)
            st = dict(st, act_first=0)
        return st
    first = float(st.get("act_first") or 0)
    if not first or act_mtime < first or now - first > 2 * cfg["idle_s"]:
        write_state(cfg, act_first=act_mtime)
        st = dict(st, act_first=act_mtime)
    return st


def _switch(cfg, models, ready_fn, why):
    """ONE `darkbloom switch` to `models`, then wait switch_wait_s for ready_fn.
    -> "served" | "busy" (the daemon drains / runs another operation: nothing may be stacked) | "failed"."""
    rc, tail = _switch_cli(cfg, models, why)
    if rc == 0:
        deadline = time.monotonic() + cfg["switch_wait_s"]
        while True:
            ok, _w = ready_fn()
            if ok:
                return "served"
            if time.monotonic() >= deadline:
                break
            time.sleep(cfg["poll_s"])
    ops = provider_ops(cfg)
    if ops is None or not ops["blocked"]:
        o2 = parse_status_ops(tail)
        ops = o2 if o2["blocked"] else ops
    if ops is not None and ops["blocked"]:
        log(cfg, "SET: switch rc=%s %s: daemon draining/busy -- NOT stacking another operation" % (rc, tail), "WARN")
        _note_drain(cfg, ops, "after switch rc=%s" % rc)
        return "busy"
    if rc == 0:
        log(cfg, "SET: switch not served within %ds: falling back to a restart" % cfg["switch_wait_s"], "WARN")
    else:
        log(cfg, "SET: switch failed rc=%s %s: falling back to a restart" % (rc, tail), "WARN")
    return "failed"


def set_models(cfg, models, start_args, ready_fn, why=""):
    """Change the served set: `darkbloom switch` first (no restart), then fall back to `darkbloom start` when the
    switch fails or `models` are not served within switch_wait_s. -> (rc, tail, how), how in {"switch","start"}.
    BLOOM_IDLE_GEMMA=0 or a dry run -> just the start (legacy)."""
    r = None
    if idle_gemma_enabled() and not cfg["dry_run"] and cfg.get("switch_first", True):
        r = _switch(cfg, models, ready_fn, why)
    elif gauge_handoff(cfg) and not cfg["dry_run"] and cfg.get("switch_first", True):
        r = _switch(cfg, models, ready_fn, why)
    if r == "served":
        return 0, "switched", "switch"
    if r == "busy":
        return 75, "darkbloom draining/busy: no restart stacked", "drain"
    if not cfg["dry_run"]:
        ops = provider_ops(cfg)
        if ops is not None and ops["blocked"]:      # never start/restart while draining
            _note_drain(cfg, ops, "before restart")
            return 75, "darkbloom draining/busy: no restart stacked", "drain"
    rc, tail = run_cli(cfg, start_args, cfg["drain_s"] + 300)
    return rc, tail, "start"


def hold_for_queue(cfg=None):
    """Make Darkbloom serve exactly the queue's pair, loaded, with BloomGauge on Manual.
    -> {"ok": bool, "noop": bool, "degraded": bool, "why": str}. Never raises."""
    cfg = cfg or load_cfg()
    if not enabled(cfg):
        return {"ok": True, "noop": True, "disabled": True, "why": "integration disabled"}
    try:
        with _Lock(cfg):
            return _hold_locked(cfg)
    except TimeoutError as e:
        return {"ok": False, "noop": False, "why": str(e)}
    except Exception as e:      # never take the caller down
        log(cfg, "hold_for_queue crashed: %r" % e, "ERROR")
        write_state(cfg, phase="failed", last_error=scrub(repr(e)))
        return {"ok": False, "noop": False, "why": scrub(repr(e))}


def _hold_locked(cfg):
    t0 = time.monotonic()
    degraded, notes = False, []
    view = None
    gauge = bloomgauge_on(cfg)
    if gauge:
        try:
            view = bloom_view(cfg)
        except (BloomDown, BloomShape) as e:
            degraded = True
            notes.append("bloom %s" % e)
            log_on_change(cfg, "bloom-unusable", str(e),
                          "BloomGauge unusable (%s): DEGRADED -- darkbloom start only" % e)
        else:
            clear_log_seen("bloom-unusable")
    ho = gauge_handoff(cfg)
    if ho:
        _g = _drain_gate(cfg, "hold")       # mid-drain: stack nothing; the queue re-polls every BLOOM_RETRY_S
        if _g is not None:
            return _g
        _st0 = read_state(cfg)
        _en0 = toml_enabled(cfg)
        if not _st0.get("filter_snapshot") and _en0 and not all(m.lower() in {x.lower() for x in cfg["pair"]} for m in _en0):
            # BloomGauge's saved model filter: `darkbloom switch` rewrites it; the release puts it back
            write_state(cfg, filter_snapshot=_en0)
            log(cfg, "HOLD: BloomGauge's enabled-model filter saved for the release: %s" % ", ".join(_en0))
    prm = pair_restart_mode(cfg)
    _st0 = read_state(cfg)
    # pair-restart mode: ready = the models the queue NEEDS (cfg["needed"], default the primary) are
    # resident; if not, a bounded no-switch attempt comes first and a restart is only the fallback.
    _req = needed_models(cfg) if prm and not _st0.get("partial") else None     # a partial fallback is judged on the FULL pair
    ready, why = pair_ready(cfg, _req)
    if not ready and not cfg["dry_run"] and provider_start_in_progress(cfg)[0]:
        ready, why, _still = _wait_for_start(cfg, lambda: pair_ready(cfg, _req))
        if not ready and _still:
            write_state(cfg, mode="queue", phase="failed",
                        last_error="provider start still in progress after %ds (%s); not restarting it" % (cfg["start_wait_s"], why))
            log(cfg, "HOLD: provider start still in progress after %ds (%s): not restarting it" % (cfg["start_wait_s"], why), "WARN")
            return {"ok": False, "noop": False, "degraded": degraded, "why": "provider start in progress: %s" % why}
    if not ready and _partial_held(cfg, _st0):
        _pn = _partial_noop(cfg, _st0)
        if _pn is not None:
            return _pn
        _st0 = read_state(cfg)
    manual = (not gauge) or ho or (view is not None and view["mode"] == "manual")   # no BloomGauge / read-only: nothing to park
    # NO-SWITCH (2026-10-13): the provider already ADVERTISES the pair -> never edit provider.toml / start /
    # stop / restart it, resident or not; residency is the provider's business (see noswitch_ensure).
    ns = (not ready) and noswitch_active(cfg)
    _en = hold_models(cfg) if prm else None
    if prm and _req is not None and not ready and not cfg["dry_run"]:
        _mok, _mwhy, _minfo = ensure_memory(cfg, cfg["primary"])
        _snap = provider_snapshot(cfg)
        _freeable = sum(max((_snap["residents"].get(x.lower()) or {}).get("gb") or 0.0,
                            (_snap["residents"].get(x.lower()) or {}).get("weights_gb") or 0.0)
                        for x in _snap.get("resident") or [] if x.lower() not in {n.lower() for n in needed_models(cfg)})
        # unloading idle residents (a graceful restart) also frees file-backed pages the provider does not credit
        _slack = cfg["unload_slack_gb"] if _freeable > 0 else 0.0
        if not _mok and (_minfo.get("usable") or 0) + _freeable + _slack < (_minfo.get("need") or 0):
            # even without the non-needed models it cannot fit: never restart for nothing -- INFRA WAIT with the reason
            _memory_infeasible(cfg, cfg["primary"], _mwhy, _minfo)
            write_state(cfg, mode="queue", phase="failed", last_error=_mwhy)
            log_on_change(cfg, "mem-wait", _mwhy, "HOLD: " + _mwhy, "WARN")
            return {"ok": False, "noop": False, "degraded": degraded, "why": _mwhy}
        clear_log_seen("mem-wait")
        if not _mok:
            log(cfg, "HOLD: memory short with %.1f GB held by non-needed models: unloading them (skipping the no-restart attempt)" % _freeable)
        else:
            ready, why = _try_noswitch_first(cfg, why)
    plan_edits = [] if (ready or ns) else desired_toml(toml_text(cfg), cfg["pair"], _en)[1]   # pair served: leave provider.toml alone
    if ho:
        plan_edits = []     # gauge hand-off: never pin/filter provider.toml (it outlives the hold); switch/start pick the model
    au_on = toml_auto_update(cfg) if gauge else None    # the auto-update park only guarded BloomGauge-era restarts
    if (ready or ns) and (manual or degraded) and not plan_edits and au_on is not True \
            and read_state(cfg).get("mode") == "queue" and read_state(cfg).get("phase") == "ready":
        return {"ok": True, "noop": True, "degraded": degraded,
                "why": "already held" if gauge else "bloomgauge off"}
    log(cfg, "HOLD: begin (pair ready=%s [%s], bloom=%s, toml edits=%d, auto_update=%s%s)" % (
        ready, why, "off" if not gauge else ("unreachable" if view is None else view["mode"]), len(plan_edits), au_on,
        ", NO-SWITCH: pair advertised, residency left to the provider" if ns else ""))
    _beat(cfg, mode="queue", phase="switching", since=time.time(), degraded=degraded, last_error=None)

    # 1) BloomGauge -> Manual (before touching the provider, so it cannot fight the switch); gauge hand-off: never
    #    (BloomGauge refuses programs' POSTs; it is left as the user has it)
    if view is not None and view["mode"] != "manual" and not ho:
        try:
            ok, w = bloom_set(cfg, "set-automatic", enabled=False)
            if ok and not cfg["dry_run"]:
                v2 = bloom_view(cfg)
                ok, w = (v2["mode"] == "manual"), "mode still %s after set-automatic" % v2["mode"]
            if not ok:
                degraded = True
                notes.append("set-automatic false refused: %s" % w)
                log_on_change(cfg, "set-automatic-refused", str(w),
                              "set-automatic enabled:false failed (%s): continuing, DEGRADED" % w)
        except (BloomDown, BloomShape) as e:
            degraded = True
            notes.append("manual: %s" % e)
            log_on_change(cfg, "set-automatic-down", str(e),
                          "set-automatic enabled:false: %s: continuing, DEGRADED" % e)

    # 2) Darkbloom auto-update off for the busy period (remember it was on)
    if au_on:
        rc, tail = run_cli(cfg, ["autoupdate", "disable"], 30)
        if rc == 0:
            _beat(cfg, autoupdate_was_on=True)
            log(cfg, "darkbloom auto-update disabled for the busy period")
        else:
            log(cfg, "darkbloom autoupdate disable failed rc=%s %s" % (rc, tail), "WARN")

    # 3) provider.toml + start (only when needed)
    if ns:
        write_state(cfg, mode="queue", phase="ready", degraded=degraded, last_error="; ".join(notes) or None,
                    bloomgauge=gauge, noswitch_since=time.time(), ready_at=time.time(), hold_seconds=round(time.monotonic() - t0, 1),
                    infeasible=None, partial=None, partial_since=None, partial_checked_at=None)
        log(cfg, "HOLD: NO-SWITCH -- pair advertised (%s); not resident: %s. Bloom %s; the provider loads/evicts, "
                 "the queue only warms + waits" % (", ".join(cfg["pair"]), why,
                                                   "off" if not gauge else ("unmanaged (DEGRADED)" if degraded else "Manual")))
        return {"ok": True, "noop": False, "degraded": degraded, "noswitch": True, "why": "held (no-switch)", "notes": notes}
    if plan_edits and not cfg["dry_run"]:
        new, changes = desired_toml(toml_text(cfg), cfg["pair"], _en)
        _write_toml(cfg, new, changes)
    elif plan_edits:
        log(cfg, "DRY-RUN: provider.toml edits: " + "; ".join(plan_edits))
    ready, why = pair_ready(cfg, _req)
    _skip_start = False
    _did_start = False
    if not ready and not cfg["dry_run"]:
        # A second `darkbloom start` cannot create memory: it re-hashes ~30 GB of weights through the
        # page cache (shrinking 'usable' further) and drops whatever is warm. When the provider already
        # says the cold load cannot fit, and we started it recently, wait/fall back instead.
        _inf = cold_infeasible(cfg["primary"], cfg)
        if _inf and time.time() - float(read_state(cfg).get("last_start_at") or 0) < cfg["restart_min_s"]:
            _skip_start = True
            _note_infeasible(cfg, _inf)
            log(cfg, "pair not served (%s) and %s: NOT restarting the provider again" % (why, _inf["reason"]))
    if not ready and not _skip_start:
        if prm:
            args = ["start"] + [a for m in hold_models(cfg) for a in ("--model", m)] + [
                "--idle-timeout", "0"] + list(cfg["start_extra"]) + ["--timeout", str(cfg["drain_s"])]
        else:
            args = ["start"] + [a for m in cfg["pair"] for a in ("--model", m)] + [
                "--idle-timeout", "0", "--local-endpoint", "--timeout", str(cfg["drain_s"])]
        if cfg["force"]:
            args.append("--force")
        if prm and not cfg["dry_run"] and not ho:
            # the start unloads every resident outside the hold set: never while a request is unfinished
            # (a drain that times out leaves the service draining) -- wait, else INFRA WAIT and retry next hold
            _drop = [x for x in loaded_models(cfg) if x.lower() not in {m.lower() for m in hold_models(cfg)}]
            _iok, _n = wait_no_unfinished(cfg, "unload %s" % (", ".join(_drop) or "nothing"))
            if not _iok:
                _bw = "waiting on %d unfinished request(s) before unloading %s for %s" % (
                    _n, ", ".join(_drop) or "the provider", cfg["primary"])
                write_state(cfg, mode="queue", phase="failed", last_error=_bw)
                return {"ok": False, "noop": False, "degraded": degraded, "why": _bw}
            clear_log_seen("unload-busy")
            if _drop:
                log(cfg, "UNLOAD: 0 unfinished requests; unloading idle %s via a graceful start on %s" % (
                    ", ".join(_drop), ", ".join(hold_models(cfg))))
        log(cfg, "pair not served (%s): darkbloom start (drain timeout %ss)" % (why, cfg["drain_s"]))
        _prev_start = read_state(cfg).get("last_start_at")
        write_state(cfg, last_start_at=time.time())
        if prm and not ho:
            write_state(cfg, gemma_removed=True)    # the restart serves the pair only: release must put Gemma back
        _how = "start"
        if prm:
            rc, tail, _how = set_models(cfg, hold_models(cfg), args, lambda: pair_ready(cfg), "(hold)")
            if _how == "drain":
                write_state(cfg, last_start_at=_prev_start)
                return dict(_drain_result(cfg, provider_ops(cfg) or parse_status_ops(tail), "hold"), degraded=degraded)
        else:
            rc, tail = run_cli(cfg, args, cfg["drain_s"] + 300)
        write_state(cfg, last_switch_at=time.time())
        log(cfg, "darkbloom %s rc=%s %s" % (_how, rc, tail),
            "INFO" if rc == 0 else "ERROR")
        _did_start = rc == 0
        if prm and rc == 0 and not cfg["dry_run"]:      # Gemma's memory is free now: measure (and purge if allowed) again
            _mok, _mwhy, _minfo = ensure_memory(cfg, cfg["primary"])
            if not _mok:
                _memory_infeasible(cfg, cfg["primary"], _mwhy, _minfo)
                log_on_change(cfg, "mem-wait", _mwhy, "HOLD: after the restart " + _mwhy, "WARN")
        if rc != 0 and not cfg["dry_run"]:
            # a failed start (e.g. drain incomplete) loaded nothing: do not let restart_min_s block the retry
            write_state(cfg, mode="queue", phase="failed", last_error="darkbloom start rc=%s %s" % (rc, tail),
                        last_start_at=_prev_start)
            return {"ok": False, "noop": False, "degraded": degraded, "why": "darkbloom start failed: %s" % tail}
    # 4) wait for the pair
    if cfg["dry_run"]:
        return {"ok": True, "noop": False, "degraded": degraded, "why": "dry-run", "notes": notes}
    deadline = time.monotonic() + cfg["wait_s"]
    t_wait = time.monotonic()
    next_probe = t_wait + cfg["probe_s"]
    while True:
        ready, why = pair_ready(cfg)
        if ready:
            break
        if time.monotonic() >= next_probe:
            next_probe = time.monotonic() + (30 if cfg["probe_s"] >= 1 else cfg["probe_s"])
            inf = cold_infeasible(cfg["primary"], cfg)
            if inf:
                _note_infeasible(cfg, inf)
                if time.monotonic() - t_wait >= cfg["fallback_s"]:
                    res = _serve_primary_alone(cfg, inf, degraded, notes)
                    if res is not None:
                        return res
            else:
                _clear_infeasible(cfg)
        if time.monotonic() > deadline:
            write_state(cfg, mode="queue", phase="failed", last_error="pair not served: %s" % why)
            log(cfg, "HOLD FAILED: pair not served within %ds (%s)" % (cfg["wait_s"], why), "ERROR")
            return {"ok": False, "noop": False, "degraded": degraded, "why": "pair not served: %s" % why}
        _beat(cfg)
        time.sleep(cfg["poll_s"])
    warmed = warm_pair(cfg)
    if warmed:
        log(cfg, "HOLD: warm check %s" % json.dumps(warmed, sort_keys=True))
    write_state(cfg, mode="queue", phase="ready", degraded=degraded, last_error="; ".join(notes) or None,
                bloomgauge=gauge, warmed=warmed, ready_at=time.time(), hold_seconds=round(time.monotonic() - t0, 1),
                infeasible=None, partial=None, partial_since=None, partial_checked_at=None)
    log(cfg, "HOLD: pair served and Bloom %s in %.1fs%s" % (
        "off" if not gauge else ("unmanaged (DEGRADED)" if degraded else
                                 "left as the user has it (read-only)" if ho else "Manual"), time.monotonic() - t0,
        " [DEGRADED: " + "; ".join(notes) + "]" if degraded else ""))
    if _did_start:
        warmup_after_restart(cfg)       # fail-soft, BLOOM_WARMUP_N=0 disables
    return {"ok": True, "noop": False, "degraded": degraded, "why": "held", "notes": notes}


# ------------------------------------------------------------------ release_to_bloom
def release_to_bloom(cfg=None):
    """Hand control back to BloomGauge (earnings) immediately. Idempotent.
    -> {"ok": bool, "noop": bool, "why": str}. Never raises. On a Bloom failure the state
    stays phase=release-pending so the caller can retry on its next event."""
    cfg = cfg or load_cfg()
    if not enabled(cfg):
        return {"ok": True, "noop": True, "disabled": True, "why": "integration disabled"}
    try:
        with _Lock(cfg):
            return _release_locked(cfg)
    except TimeoutError as e:
        return {"ok": False, "noop": False, "why": str(e)}
    except Exception as e:
        log(cfg, "release_to_bloom crashed: %r" % e, "ERROR")
        return {"ok": False, "noop": False, "why": scrub(repr(e))}


def _restore_full_set(cfg, st):
    """Pair-restart mode, queue-done: put the FULL model set back (Gemma on, primary first) with one
    provider.toml edit + one `darkbloom start`, then verify /health + /v1/models list them all. Idempotent
    (already restored -> no restart). -> (ok, why). A failure is retried with bounded exponential backoff
    (state restore_attempts / restore_next_at), logged WARN and recorded degraded: Gemma must never stay
    off silently. Never raises."""
    models = idle_models(cfg)
    # Autopilot may list only what fits in its slots, so readiness = the pair, not all advertised models.
    ready = [m for m in models if m.lower() in {x.lower() for x in cfg["pair"]}] or models if idle_autopilot_enabled() else models
    now = time.time()
    try:
        if float(st.get("restore_next_at") or 0) > now:
            return False, "restore backoff (%ds left)" % (float(st["restore_next_at"]) - now)
        en = {m.lower() for m in toml_enabled(cfg)}
        ok, why = set_ready(cfg, ready)
        if ok and {m.lower() for m in models} <= en and not cfg["dry_run"] and not st.get("gemma_removed"):
            return True, "already restored"
        why = "restore failed"
        new, changes = desired_toml(toml_text(cfg), cfg["pair"], models)
        if changes and not cfg["dry_run"] and not _write_toml(cfg, new, changes):
            raise OSError("provider.toml write failed")
        args = ["start"] + [a for m in models for a in ("--model", m)] + [
            "--idle-timeout", "0"] + list(cfg["start_extra"]) + ["--timeout", str(cfg["drain_s"])]
        log(cfg, "RESTORE: darkbloom start with the full set (%s)" % ", ".join(models))
        rc, tail, _how = set_models(cfg, models, args, lambda: set_ready(cfg, ready), "(restore)")
        write_state(cfg, last_switch_at=time.time())
        if rc != 0:
            raise RuntimeError("darkbloom start rc=%s %s" % (rc, tail))
        if cfg["dry_run"]:
            return True, "dry-run"
        deadline = time.monotonic() + cfg["restore_wait_s"]
        while True:
            ok, why = set_ready(cfg, ready)
            if ok:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("not served within %ds: %s" % (cfg["restore_wait_s"], why))
            time.sleep(cfg["poll_s"])
        write_state(cfg, restore_attempts=0, restore_next_at=0, gemma_removed=False, restored_at=time.time())
        log(cfg, "RESTORE: full set served again (%s)" % ", ".join(models))
        return True, "restored"
    except Exception as e:
        n = int(st.get("restore_attempts") or 0) + 1
        back = min(cfg["restore_backoff_max_s"], cfg["restore_backoff_base_s"] * (2 ** (n - 1)))
        err = "restore of %s failed (%s); attempt %d, retry in %ds" % (", ".join(models), scrub(e), n, back)
        write_state(cfg, restore_attempts=n, restore_next_at=time.time() + back)
        log(cfg, "RESTORE FAILED -- Gemma may be OFF: " + err, "WARN")
        return False, err


def _release_locked(cfg):
    st = read_state(cfg)
    ho = gauge_handoff(cfg)
    if ho:
        return _release_gauge(cfg, st)      # no pair-restart restore, no BloomGauge POST: BloomGauge picks the model
    elif pair_restart_mode(cfg) and st.get("gemma_removed"):
        ok, why = _restore_full_set(cfg, st)
        if not ok:
            write_state(cfg, mode="queue", phase="release-pending", degraded=True, last_error=why)
            return {"ok": False, "noop": False, "degraded": True, "why": why}
        st = read_state(cfg)
    if not bloomgauge_on(cfg):
        # No BloomGauge: nothing to hand back. Clear any stale queue/degraded/release-pending state
        # left by an on-mode run (idempotent, one write, no retry loop), restore a stale auto-update park.
        au = bool(st.get("autoupdate_was_on"))
        err = None
        if au:
            rc, tail = run_cli(cfg, ["autoupdate", "enable"], 30)
            if rc == 0:
                log(cfg, "darkbloom auto-update re-enabled (stale park from BloomGauge mode)")
                au = False
            else:
                err = "autoupdate enable failed"
                log_on_change(cfg, "autoupdate-enable", str(rc), "darkbloom autoupdate enable failed rc=%s %s" % (rc, tail))
        if err:
            write_state(cfg, mode="queue", phase="release-pending", last_error=err)
            return {"ok": False, "noop": False, "why": err}
        if (st.get("mode") == "queue" or st.get("phase") in ("release-pending", "releasing")
                or st.get("degraded") or st.get("last_error") or "autoupdate_was_on" in st and st["autoupdate_was_on"]):
            write_state(cfg, mode="bloom", phase="idle", last_error=None, degraded=False,
                        released_at=time.time(), autoupdate_was_on=False, bloomgauge=False)
            log(cfg, "RELEASE: BloomGauge off -- queue hold cleared (nothing to hand back)")
        return {"ok": True, "noop": True, "why": "bloomgauge off"}
    try:
        view = bloom_view(cfg)
    except (BloomDown, BloomShape) as e:
        view = None
        log(cfg, "release: BloomGauge unusable (%s)" % e, "WARN")
    au_was_on = bool(st.get("autoupdate_was_on"))
    bloom_ok = view is not None and view["mode"] == "on" and not view["pinned"]
    if st.get("mode") != "queue" and not au_was_on and (view is None or bloom_ok):
        return {"ok": True, "noop": True, "why": "already with BloomGauge"}
    log(cfg, "RELEASE: begin (bloom=%s pinned=%s auto_update_restore=%s)" % (
        "unreachable" if view is None else view["mode"], None if view is None else view["pinned"], au_was_on))
    _beat(cfg, phase="releasing")
    err = None
    if view is None:
        err = "BloomGauge unreachable"
    else:
        try:
            if view["pinned"]:
                ok, w = bloom_set(cfg, "release-pin")
                if not ok:
                    log(cfg, "release-pin refused (%s): continuing" % w, "WARN")
            v2 = bloom_view(cfg)
            if v2["mode"] != "on":
                ok, w = bloom_set(cfg, "set-automatic", enabled=True)
                if not ok:
                    err = "set-automatic true refused: %s" % w
                elif not cfg["dry_run"]:
                    v3 = bloom_view(cfg)
                    if v3["mode"] != "on":
                        err = "mode still %s after set-automatic true" % v3["mode"]
        except (BloomDown, BloomShape) as e:
            err = "bloom: %s" % e
    if au_was_on:
        rc, tail = run_cli(cfg, ["autoupdate", "enable"], 30)
        if rc == 0:
            _beat(cfg, autoupdate_was_on=False)
            log(cfg, "darkbloom auto-update re-enabled")
        else:
            log(cfg, "darkbloom autoupdate enable failed rc=%s %s" % (rc, tail), "WARN")
            err = err or "autoupdate enable failed"
    if err:
        write_state(cfg, mode="queue", phase="release-pending", last_error=err)
        log(cfg, "RELEASE incomplete (%s); will retry on the next queue event" % err, "WARN")
        return {"ok": False, "noop": False, "why": err}
    write_state(cfg, mode="bloom", phase="idle", last_error=None, released_at=time.time(), degraded=False)
    log(cfg, "RELEASE: BloomGauge is Automatic again")
    return {"ok": True, "noop": False, "why": "released"}


def _clear_queue_preload(cfg):
    """Drop a preload pin that lists only queue models (left by older holds): it would force the queue model on
    every provider start after the hand-back. -> note or None. Never raises."""
    pre = toml_preload(cfg)
    pair = {m.lower() for m in cfg["pair"]}
    if not pre or not all(m.lower() in pair for m in pre):
        return None
    if cfg["dry_run"]:
        return "dry-run: would clear preload_models %s" % pre
    text = toml_text(cfg)
    new = re.sub(r"(?m)^(\s*)preload_models\s*=\s*\[[^\]]*\].*$", lambda m: m.group(1) + "preload_models = []", text, count=1)
    if new != text and _write_toml(cfg, new, ["preload_models: %s -> [] (queue pin cleared)" % pre]):
        return "preload pin %s cleared" % pre
    return None


def _restore_filter(cfg, snap):
    """Put BloomGauge's saved enabled-model filter back, only while the saved filter is still the queue's own (a
    newer choice made during the hold wins). Saved file only: the running daemon is not touched. -> note or None."""
    cur = toml_enabled(cfg)
    pair = {m.lower() for m in cfg["pair"]}
    if not snap or not cur or not all(m.lower() in pair for m in cur) or cfg["dry_run"]:
        return None
    text = toml_text(cfg)
    line = "enabled_models = [ %s ]" % ", ".join("'%s'" % m for m in snap)
    new = re.sub(r"(?m)^(\s*)enabled_models\s*=\s*\[[^\]]*\].*$", lambda m: m.group(1) + line, text, count=1)
    if new != text and _write_toml(cfg, new, ["enabled_models: %s -> %s (BloomGauge's filter back)" % (cur, snap)]):
        return "BloomGauge's enabled-model filter back (%s)" % ", ".join(snap)
    return None


def _release_gauge(cfg, st):
    """Gauge hand-off release: wait out a drain, then pick NO model -- BloomGauge's saved filter back, the queue's
    preload pin gone, auto-update restored. BloomGauge is never POSTed; its mode is reported from a GET only."""
    au_was_on = bool(st.get("autoupdate_was_on"))
    if st.get("mode") != "queue" and not au_was_on and not st.get("filter_snapshot"):
        return {"ok": True, "noop": True, "why": "already with BloomGauge"}
    g = _drain_gate(cfg, "release")
    if g is not None:
        return g
    _beat(cfg, phase="releasing")
    for note in (_restore_filter(cfg, st.get("filter_snapshot")), _clear_queue_preload(cfg)):
        if note:
            log(cfg, "RELEASE: " + note)
    if au_was_on:
        rc, tail = run_cli(cfg, ["autoupdate", "enable"], 30)
        if rc != 0:
            log(cfg, "darkbloom autoupdate enable failed rc=%s %s" % (rc, tail), "WARN")
            write_state(cfg, mode="queue", phase="release-pending", last_error="autoupdate enable failed")
            return {"ok": False, "noop": False, "why": "autoupdate enable failed"}
        log(cfg, "darkbloom auto-update re-enabled")
    try:
        mode = bloom_view(cfg)["mode"]
        bloom = ("BloomGauge managing (Automatic on, confirmed by GET)" if mode == "on" else
                 "BloomGauge Manual (per GET): left as the user set it")
    except (BloomDown, BloomShape) as e:
        bloom = "BloomGauge mode unknown (GET failed: %s): not claimed" % e
    write_state(cfg, mode="bloom", phase="idle", last_error=None, released_at=time.time(), degraded=False,
                autoupdate_was_on=False, filter_snapshot=None, handback=None, gemma_removed=False, drain=None)
    log(cfg, "RELEASE: queue done; no model picked; " + bloom)
    return {"ok": True, "noop": False, "why": "released"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("state", "hold", "release"))
    ap.add_argument("--dry-run", action="store_true", help="log intended actions, change nothing")
    a = ap.parse_args(argv)
    if a.cmd != "state" and not a.dry_run and os.environ.get("DARKBLOOM_BY_OWNER") != "1":
        print("bloom_control.py %s: %s" % (a.cmd, "retired: BloomGauge runs Darkbloom and the queue's BloomGauge control picks models; a hand-run darkbloom/BloomGauge change fights it and spends BloomGauge's hourly change budget. The owner only: DARKBLOOM_BY_OWNER=1"), file=sys.stderr)
        return 2
    if a.dry_run:
        os.environ["BLOOMCTL_DRY_RUN"] = "1"
    cfg = load_cfg()
    if a.cmd == "state":
        print(json.dumps(get_state(cfg), indent=1, sort_keys=True, default=str))
        return 0
    r = hold_for_queue(cfg) if a.cmd == "hold" else release_to_bloom(cfg)
    print(json.dumps(r, sort_keys=True))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
