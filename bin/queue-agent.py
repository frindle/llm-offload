#!/usr/bin/env python3
"""queue-agent -- the small Mac status service for the queue that runs in Docker on Unraid.

Serves what only the Mac knows (Darkbloom add-on hold state, screen lock, memory, lane_plan's vendor-file
facts), runs the Mac notify path on request, and (when asked to) mirrors queue events and escalations back to
the Mac files the Claude Code hooks read. Stdlib only. Details, sources of truth and the container-side
routes the mirror needs: queue-agent.md.

  GET  /health           open, no secrets
  GET  /v1/hold          Darkbloom add-on hold: the queue's own darkbloom_view() plus inputs and ages
  GET  /v1/mac           screen locked, memory (vm_stat/sysctl), load average
  GET  /v1/lane-plan     lane_plan's vendor-file and pid facts (no keys)
  POST /v1/notify        notify-operator.py with validated, rate-limited input
  POST /v1/bloomgauge-tick  run the queue's BloomGauge control step here (the app is on this Mac's loopback)
  GET  /v1/bloomgauge-tick  that step's mode, last run and alerts not yet taken

Everything but /health needs `Authorization: Bearer <token>`. Config: TOML at $QUEUE_CONFIG or
~/.config/ollama-queue/config.toml, table [queue_agent]: bind, port, mirror_url, mirror_interval_s,
mirror_token_file, notify_per_hour. Token: ~/.config/ollama-queue/agent-token (0600).

Never run as a test server on the Mac. Tests: test_queue_agent.py (claude-sandbox).
"""
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(os.environ.get("QUEUE_AGENT_CODE_DIR") or Path(__file__).resolve().parent)
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 7691
MAX_BODY = 8192
NOTIFY_PER_HOUR = 30
TITLE_MAX = 120
BODY_MAX = 1000
URL_MAX = 300
KEY_MAX = 120
MIRROR_DEFAULT_INTERVAL_S = 15.0
MIRROR_EVENT_BATCH = 200
EVENTS_MAX_BYTES = 1_000_000      # same rotation size as queue-event-log.py:30 (QEL_MAX_BYTES)
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _now():
    return time.time()


# ------------------------------------------------------------------ config and token
def config_path():
    return Path(os.environ.get("QUEUE_CONFIG") or Path.home() / ".config" / "ollama-queue" / "config.toml")


def token_path():
    return Path(os.environ.get("QUEUE_AGENT_TOKEN_FILE") or Path.home() / ".config" / "ollama-queue" / "agent-token")


def load_config(path=None):
    """The [queue_agent] table as a dict (nested table, or flat 'queue_agent.x' keys). {} when absent/unreadable."""
    p = Path(path) if path else config_path()
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    sub = data.get("queue_agent")
    if isinstance(sub, dict):
        out.update(sub)
    for k, v in data.items():
        if k.startswith("queue_agent.") and not isinstance(v, dict):
            out[k.split(".", 1)[1]] = v
    return out


def read_token(path=None):
    try:
        return Path(path or token_path()).read_text().strip() or None
    except OSError:
        return None


def is_loopback(bind):
    return str(bind) in LOOPBACK


def check_startup(cfg, token):
    """-> error string or None. No token is fine only on loopback."""
    bind = str(cfg.get("bind") or DEFAULT_BIND)
    if not token and not is_loopback(bind):
        return f"no token at {token_path()}: refusing to bind {bind} without one"
    return None


def auth_ok(header, token):
    """Constant-time Bearer check. No token configured (loopback only) = everything passes."""
    if not token:
        return True
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())


# ------------------------------------------------------------------ lazy imports of the queue's own code
_mods = {}
_mods_lock = threading.Lock()


def _load_module(key, filename, modname):
    """Import HERE/<filename> once by path (the queue's API does the same for ollama-queue.py). Raises on failure."""
    with _mods_lock:
        if key in _mods:
            m = _mods[key]
            if isinstance(m, Exception):
                raise m
            return m
        try:
            path = HERE / filename
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            spec = importlib.util.spec_from_file_location(modname, str(path))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        except Exception as e:  # noqa: BLE001
            _mods[key] = RuntimeError(f"cannot load {filename}: {type(e).__name__}")
            print(f"[queue-agent] cannot load {filename}: {e!r}", file=sys.stderr)
            raise _mods[key]
        _mods[key] = m
        return m


def queue_mod():
    return _load_module("queue", "ollama-queue.py", "ollama_queue_lib")


def cpu_lane_mod():
    return _load_module("cpu_lane", "cpu_lane.py", "cpu_lane")


def infra_mod():
    return _load_module("infra", "infra_incidents.py", "infra_incidents")


def lane_plan_mod():
    return _load_module("lane_plan", "lane_plan.py", "lane_plan")


def notify_mod():
    return _load_module("notify", "notify-operator.py", "notify_operator")


# ------------------------------------------------------------------ file facts
def file_fact(path, now=None):
    """{"path", "exists", "mtime", "age_s", "size"} for one input file. Never raises, never reads contents."""
    now = _now() if now is None else now
    p = Path(path)
    try:
        st = p.stat()
        return {"path": str(p), "exists": True, "mtime": round(st.st_mtime, 3),
                "age_s": round(max(0.0, now - st.st_mtime), 1), "size": st.st_size}
    except OSError:
        return {"path": str(p), "exists": False, "mtime": None, "age_s": None, "size": None}


# ------------------------------------------------------------------ /v1/hold
class HoldTracker:
    """hold_since for a view. The queue daemon keeps this in its own _DB_TRANS (ollama-queue.py:6177-6185) because
    it primes the view every tick; this agent is asked on demand, so it keeps the same transition itself: set on the
    first held view, cleared on the first readable not-held one."""

    def __init__(self):
        self.since = None
        self.lock = threading.Lock()

    def apply(self, view, now):
        with self.lock:
            if view.get("hold"):
                if self.since is None:
                    self.since = now
                view["hold_since"] = self.since
            elif not view.get("unreadable"):
                self.since = None


HOLD = HoldTracker()
_hold_call_lock = threading.Lock()


def hold_payload(model=None, now=None):
    """The Darkbloom hold state exactly as the queue computes it: q.darkbloom_view() (ollama-queue.py:6045), the
    CPU lane's model_hold() (cpu_lane.py:322-340) and, when `model` is given, darkbloom_hold_wait() (ollama-queue.py:
    6087). Policy stays fail-open: an unreadable log is not held. Raises only when the queue module cannot load."""
    q = queue_mod()
    now = _now() if now is None else now
    view = dict(q.darkbloom_view(now))
    if view.get("cause") == "model-cache-unreadable":
        try:
            view["locked"] = infra_mod().screen_locked()
        except Exception:  # noqa: BLE001
            view["locked"] = None
    HOLD.apply(view, now)
    held, cause = bool(view.get("hold")), str(view.get("cause") or "")[:120] if view.get("hold") else ""
    try:  # the CPU lane's own answer, from the same view (no second read of the log)
        cl = cpu_lane_mod()
        with _hold_call_lock:
            saved = cl.HOLD_VIEW
            cl.HOLD_VIEW = lambda: view
            try:
                held, cause = cl.model_hold(fresh=True)
            finally:
                cl.HOLD_VIEW = saved
    except Exception:  # noqa: BLE001
        pass
    out = {
        "generated_at": now,
        "policy": "fail-open",
        "hold": held,
        "cause": cause,
        "view": view,
        "sandbox_hold_wait_enabled": bool(q.SANDBOX_HOLD_WAIT),
        "inputs": {
            "provider_log": dict(file_fact(q.DARKBLOOM_PROVIDER_LOG, now), tail_bytes=q.DB_LOG_TAIL_BYTES),
            "sandbox_slots_log": file_fact(q.DARKBLOOM_SLOTS_LOG, now),
        },
        "freshness_s": {
            "hold_stale": q.DB_HOLD_STALE_S,       # ollama-queue.py:5942 a live hold renews its lease in the log
            "view_fresh": q.DB_VIEW_FRESH_S,       # :5943 the launch loop trusts a view at most this old
            "slots_fresh": q.DB_SLOTS_FRESH_S,     # :5946 sandbox-slots.log last line valid this long
            "hold_ttl_cpu_lane": getattr(_mods.get("cpu_lane"), "HOLD_TTL_S", None),
        },
    }
    if model:
        try:
            out["hold_wait"] = q.darkbloom_hold_wait(str(model)[:120], now, view=view)
        except Exception:  # noqa: BLE001
            out["hold_wait"] = None
    return out


# ------------------------------------------------------------------ /v1/mac
_VM_PAGE_RE = re.compile(r"page size of (\d+) bytes")


def parse_vm_stat(vm_stat_text, page_size, total_bytes):
    """(used_gb, total_gb) from vm_stat text, same arithmetic as ollama-queue-api.py:5097-5122 (_parse_vm_stat):
    used = total - (free + inactive + speculative + purgeable) pages. (None, None) when unparseable."""
    rx = re.compile(r'^"?([A-Za-z][A-Za-z0-9 ()"/-]*?)"?:\s+(\d+)\.?\s*$')
    pages = {}
    for line in str(vm_stat_text).splitlines():
        m = rx.match(line.strip())
        if m:
            pages[m.group(1).strip().lower()] = int(m.group(2))
    if not pages or page_size <= 0 or total_bytes <= 0:
        return (None, None), pages
    reclaim = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages speculative", "pages purgeable"))
    used = min(max(0, total_bytes - reclaim * page_size), total_bytes)
    return (round(used / 1e9, 1), round(total_bytes / 1e9, 1)), pages


def _run(cmd, timeout=3):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False).stdout


def memory_facts(run=_run):
    """{"used_gb", "total_gb", "page_size", "pages": {...}} from `sysctl -n hw.memsize` + `vm_stat`
    (ollama-queue-api.py:5125-5138). All None on any failure (Linux, no vm_stat)."""
    try:
        total = int(run(["sysctl", "-n", "hw.memsize"]).strip())
        out = run(["vm_stat"])
        m = _VM_PAGE_RE.search(out)
        ps = int(m.group(1)) if m else 4096
        (used, tot), pages = parse_vm_stat(out, ps, total)
        keep = {k: v for k, v in pages.items() if k in ("pages free", "pages active", "pages inactive",
                                                          "pages speculative", "pages purgeable", "pages wired down")}
        return {"used_gb": used, "total_gb": tot, "page_size": ps, "pages": keep}
    except Exception:  # noqa: BLE001
        return {"used_gb": None, "total_gb": None, "page_size": None, "pages": {}}


def mac_payload(now=None, run=_run):
    now = _now() if now is None else now
    try:
        locked = infra_mod().screen_locked()      # ioreg, cached 30 s (infra_incidents.py:90-118)
    except Exception:  # noqa: BLE001
        locked = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {"generated_at": now, "screen_locked": locked, "memory": memory_facts(run), "load_avg": load}


# ------------------------------------------------------------------ /v1/lane-plan
def _json_file(path):
    try:
        o = json.loads(Path(path).read_text())
        return o if isinstance(o, dict) else None
    except (OSError, ValueError):
        return None


def lane_plan_payload(now=None, sources=None, pid_alive=None):
    """The facts lane_plan.take_snapshot reads (lane_plan.py:183-190, 328-330, 345-460), WITHOUT local.json's api_key:
    file facts (age/size), the parsed non-secret fields, the pid and whether it is alive, and the snapshot the same
    function builds (its public() form never holds the key)."""
    lp = lane_plan_mod()
    now = _now() if now is None else now
    src = sources or lp.Sources()
    alive = pid_alive or lp._pid_alive
    names = {"local_json": src.local_json, "daemon_state": src.daemon_state, "loaded_models": src.loaded_models,
             "provider_pid": src.provider_pid, "provider_toml": src.provider_toml, "hosts_json": src.hosts_json,
             "bonsai_host": src.bonsai_host}
    files = {k: file_fact(v, now) for k, v in names.items()}
    loc = _json_file(src.local_json) or {}
    ds = _json_file(src.daemon_state) or {}
    pi = ds.get("process_identity") if isinstance(ds.get("process_identity"), dict) else {}
    try:
        pid = int(Path(src.provider_pid).read_text().strip())
    except (OSError, ValueError):
        pid = None
    try:
        toml_txt = Path(src.provider_toml).read_text()
    except OSError:
        toml_txt = ""
    hosts = {}
    for name, h in ((_json_file(src.hosts_json) or {}).get("hosts") or {}).items():
        if isinstance(h, dict) and lp.norm_url(h.get("url")):
            hosts[str(name)] = {"url": lp.norm_url(h["url"]), "usable_bytes": h.get("usable_bytes")}
    try:
        bonsai = lp.norm_url(Path(src.bonsai_host).read_text().strip()) or None
    except OSError:
        bonsai = None
    facts = {
        "local_json": {"base_url": lp.norm_url(loc.get("base_url")) or None, "has_api_key": bool(loc.get("api_key")),
                       "pid": loc.get("pid"), "version": loc.get("version"), "updated_at": loc.get("updated_at")},
        "daemon_state": {"pid": pi.get("pid"), "start_time_micros": pi.get("start_time_micros"),
                         "version": ds.get("version"),
                         "advertised_models": [str(x) for x in ds.get("advertised_models") or [] if x]
                         if isinstance(ds.get("advertised_models"), list) else []},
        "loaded_models": _json_file(src.loaded_models),
        "provider_toml_served": lp.served_from_texts(toml_txt, None),
        "provider_pid": {"pid": pid, "alive": (alive(pid) if pid else None)},
        "hosts": hosts,
        "bonsai_url": bonsai,
    }
    try:
        snap = lp.take_snapshot(sources=src, pid_alive=alive).public()
    except Exception:  # noqa: BLE001
        snap = None
    return {"generated_at": now, "files": files, "facts": facts, "snapshot": snap}


# ------------------------------------------------------------------ /v1/darkbloom-status
DARKBLOOM_STATUS_TTL_S = 10.0
DARKBLOOM_STATUS_CLI_TIMEOUT_S = 4
_status_cache = {"at": 0.0, "text": ""}
_status_lock = threading.Lock()


def darkbloom_status_payload(now=None, run=None):
    """Raw `darkbloom status` text (the queue parses it itself: parse_darkbloom_status), run through the queue's own
    CLI finder with a 4 s timeout (under the container client's 5 s) and cached 10 s. "text" is "" when the CLI is missing, slow or fails, which is
    what the queue treats as "CLI failed". The text holds no key."""
    now = _now() if now is None else now
    with _status_lock:
        if now - _status_cache["at"] >= DARKBLOOM_STATUS_TTL_S:
            try:
                # 4 s, not the queue's 8 s: the container's client gives up after 5 s, so a slower CLI answer would
                # arrive after nobody is listening while later requests wait on this lock
                txt = (run or (lambda: queue_mod()._darkbloom_status_cli(timeout=DARKBLOOM_STATUS_CLI_TIMEOUT_S)))()
            except Exception:  # noqa: BLE001
                txt = ""
            _status_cache.update(at=now, text=str(txt or ""))
        return {"generated_at": now, "text": _status_cache["text"], "cached_s": round(now - _status_cache["at"], 1)}


# ------------------------------------------------------------------ /v1/darkbloom-model-configs
MODEL_CONFIG_MAX_BYTES = 262144
MODEL_CONFIGS_MAX = 64


def darkbloom_model_configs_payload(now=None, root=None):
    """config.json of each model in Darkbloom's HF cache, keyed by its cache folder name (models--<id>), the way the
    queue looks them up (_darkbloom_model_config): the container sizes Darkbloom windows and second slots from them.
    First readable snapshot per model, files over MODEL_CONFIG_MAX_BYTES skipped, at most MODEL_CONFIGS_MAX models.
    Model metadata only (no weights, no keys). Read-only; {} when the cache is not configured or unreadable."""
    now = _now() if now is None else now
    if root is None:
        root = queue_mod()._darkbloom_cache_dir()
    out = {}
    try:
        dirs = sorted(Path(root).iterdir()) if root is not None else []
    except OSError:
        dirs = []
    for d in dirs:
        if len(out) >= MODEL_CONFIGS_MAX:
            break
        try:
            if not (d.name.lower().startswith("models--") and d.is_dir() and (d / "snapshots").is_dir()):
                continue
            cfgs = sorted((d / "snapshots").glob("*/config.json"))
        except OSError:
            continue
        for cfg in cfgs:
            try:
                if cfg.stat().st_size > MODEL_CONFIG_MAX_BYTES:
                    continue
                v = json.loads(cfg.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(v, dict):
                out[d.name] = v
                break
    return {"generated_at": now, "configs": out}


# ------------------------------------------------------------------ /v1/bloomgauge-ledger
LEDGER_TAIL_BYTES = 262144


def bloomgauge_ledger_payload(now=None, cfg=None, tail_bytes=LEDGER_TAIL_BYTES):
    """What escalation_index_janitor reads to close BloomGauge control alerts: the control mode and the tail of the
    bloomgauge_control ledger (bloomgauge-control.jsonl). The ledger is written on the Mac (the control tick and the
    lease guard talk to BloomGauge there). rows: the raw JSON lines of the last `tail_bytes`, without a cut first
    line. ledger: "ok" | "missing" | "unreadable". Read-only."""
    now = _now() if now is None else now
    if cfg is None:
        cfg = _load_module("bgc", "bloomgauge_control.py", "bloomgauge_control").config()
    path = cfg.get("ledger_path")
    rows, state = [], "missing"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if size > tail_bytes and lines:
            lines = lines[1:]
        rows, state = [ln for ln in lines if ln.strip()], "ok"
    except FileNotFoundError:
        pass
    except OSError:
        state = "unreadable"
    return {"generated_at": now, "mode": str(cfg.get("mode") or "off"), "ledger": state, "rows": rows}


# ------------------------------------------------------------------ /v1/bloomgauge-tick
# The queue's BloomGauge control step (bloomgauge_tick.tick) talks to the BloomGauge app on 127.0.0.1, so for a queue
# that runs elsewhere it runs here: the queue POSTs {busy, why, target, ack, epoch} every few seconds; this service
# keeps bloomgauge_tick's own throttle (one step per tick_s, never two at once), keeps the alerts the step raises
# until the queue acks them (it writes the ESCALATIONS.md rows, which the mirror brings back) and sends each one
# through the Mac notify path at once (the container has no desktop). Its mode (bloomgauge_control.config: env
# QUEUE_BLOOMGAUGE_CONTROL over the mode file) is the authority, also for /v1/bloomgauge-ledger.
BG_ALERTS_MAX = 50
BG_WHY_MAX = 300
BG_MSG_MAX = 600
_BG_TARGET_RE = re.compile(r"^[A-Za-z0-9._/:@+-]{1,200}$")


def validate_bg_work(body):
    """-> (work, ack, epoch, None) or (None, None, None, error text). why is made plain (control characters to
    spaces, at most BG_WHY_MAX); target is a model id or null."""
    if not isinstance(body, dict):
        return None, None, None, "body must be a JSON object"
    busy, why, target = body.get("busy"), body.get("why", ""), body.get("target")
    if not isinstance(busy, bool):
        return None, None, None, "busy must be true or false"
    if not isinstance(why, str):
        return None, None, None, "why must be text"
    if target is not None and (not isinstance(target, str) or not _BG_TARGET_RE.match(target)):
        return None, None, None, "target must be a model id or null"
    ack = body.get("ack", 0)
    if isinstance(ack, bool) or not isinstance(ack, int) or ack < 0:
        return None, None, None, "ack must be a whole number >= 0"
    epoch = body.get("epoch")
    if epoch is not None and (not isinstance(epoch, str) or len(epoch) > 64):
        return None, None, None, "epoch must be short text or null"
    why = " ".join(_CTRL_RE.sub(" ", why).split())[:BG_WHY_MAX]
    return {"busy": busy, "why": why, "target": target}, ack, epoch, None


class BloomTick:
    """Runs bloomgauge_tick for a queue that runs elsewhere. bgt/ctl: the bloomgauge_tick / bloomgauge_control
    modules (loaded from HERE when None); notify: do_notify's notify function (tests)."""

    def __init__(self, bgt=None, ctl=None, notify=None, clock=time.time):
        self._bgt, self._ctl, self.notify, self.clock = bgt, ctl, notify, clock
        self.epoch = f"{os.getpid()}-{int(time.time() * 1000)}"
        self.lock, self.run_lock = threading.Lock(), threading.Lock()
        self.alerts, self.seq = [], 0

    def bgt(self):
        return self._bgt or _load_module("bgt", "bloomgauge_tick.py", "bloomgauge_tick")

    def ctl(self):
        return self._ctl or _load_module("bgc", "bloomgauge_control.py", "bloomgauge_control")

    def deliver(self, key, msg):
        """bloomgauge_tick's _STATE['deliver']: keep the alert for the queue, notify on the Mac now. Never raises."""
        key = re.sub(r"[^\w.:/@+-]", "-", str(key or "bloomgauge-control"))[:KEY_MAX]
        msg = " ".join(_CTRL_RE.sub(" ", str(msg)).split())[:BG_MSG_MAX]
        with self.lock:
            self.seq += 1
            self.alerts.append({"seq": self.seq, "key": key, "msg": msg, "at": _now()})
            del self.alerts[:-BG_ALERTS_MAX]
        print(f"[queue-agent] !!! bloomgauge-control: {msg}", file=sys.stderr)
        try:
            clean, bad = validate_notify({"title": "BloomGauge control", "body": f"{key}: {msg}"[:BODY_MAX],
                                          "dedupe_key": key, "dedupe_s": 3600})
            if not bad:
                do_notify(clean, self.notify)
        except Exception as e:  # noqa: BLE001
            print(f"[queue-agent] bloomgauge-control notify failed: {e!r}", file=sys.stderr)

    def _pending(self):
        with self.lock:
            return [dict(a) for a in self.alerts]

    def status(self, mode=None, wanted=None, started=None):
        bgt = self.bgt()
        if mode is None:
            ctl = self.ctl()
            cfg = ctl.config()
            mode = str(cfg.get("mode") or "off")
            wanted = bool(bgt.wanted(cfg, ctl))
        st = getattr(bgt, "_STATE", None)
        st = st if isinstance(st, dict) else {}
        th = st.get("thread")
        return {"generated_at": _now(), "epoch": self.epoch, "mode": mode, "wanted": wanted, "started": started,
                "running": bool(th is not None and th.is_alive()), "last_step_at": st.get("last") or None,
                "alerts": self._pending()}

    def handle(self, body, now=None):
        """POST body -> (answer, None) or (None, error text)."""
        work, ack, epoch, bad = validate_bg_work(body)
        if bad:
            return None, bad
        if epoch == self.epoch and ack:
            with self.lock:
                self.alerts = [a for a in self.alerts if a["seq"] > ack]
        ctl, bgt = self.ctl(), self.bgt()
        cfg = ctl.config()
        mode = str(cfg.get("mode") or "off")
        wanted = bool(bgt.wanted(cfg, ctl))
        started = False
        if wanted:
            with self.run_lock:
                st = getattr(bgt, "_STATE", None)
                if isinstance(st, dict):
                    st["deliver"] = self.deliver
                # owner_pid None: this service outlives the queue, so the lease must not name its pid (the
                # stale-lease guard then goes by the heartbeat, which stops when the queue stops posting).
                started = bool(bgt.run_async(work, self.clock() if now is None else now, dict(cfg, owner_pid=None)))
            st = getattr(bgt, "_STATE", None)
            left = st.pop("alert", None) if isinstance(st, dict) else None
            if isinstance(left, dict) and left.get("msg"):
                self.deliver(left.get("key"), left["msg"])
        return self.status(mode=mode, wanted=wanted, started=started), None


# ------------------------------------------------------------------ /v1/notify
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")      # control characters except \n and \t


class RateLimiter:
    """At most `limit` hits per sliding `window_s`."""

    def __init__(self, limit=NOTIFY_PER_HOUR, window_s=3600.0):
        self.limit, self.window_s, self.hits, self.lock = limit, window_s, [], threading.Lock()

    def take(self, now=None):
        """-> (allowed, retry_after_s)."""
        now = time.monotonic() if now is None else now
        with self.lock:
            self.hits = [t for t in self.hits if now - t < self.window_s]
            if len(self.hits) >= self.limit:
                return False, max(1, int(self.window_s - (now - self.hits[0])) + 1)
            self.hits.append(now)
            return True, 0


def validate_notify(body):
    """-> (clean dict, None) or (None, error text). title/body are plain text with length caps; nothing here
    reaches a shell (notify-operator runs osascript with an argv list and escapes its own strings)."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    title, text = body.get("title"), body.get("body", body.get("message"))
    if not isinstance(title, str) or not title.strip():
        return None, "title is required"
    if not isinstance(text, str) or not text.strip():
        return None, "body is required"
    if len(title) > TITLE_MAX:
        return None, f"title longer than {TITLE_MAX}"
    if len(text) > BODY_MAX:
        return None, f"body longer than {BODY_MAX}"
    if _CTRL_RE.search(title) or "\n" in title or _CTRL_RE.search(text):
        return None, "control characters not allowed"
    url = body.get("url")
    if url is not None:
        if not isinstance(url, str) or len(url) > URL_MAX or not re.match(r"^https?://[^\s]+$", url):
            return None, "url must be an http(s) URL"
    key = body.get("dedupe_key")
    if key is not None:
        if not isinstance(key, str) or not key or len(key) > KEY_MAX or not re.match(r"^[\w.:/@+-]+$", key):
            return None, "dedupe_key must be 1-120 plain characters"
    secs = body.get("dedupe_s", 3600)
    if isinstance(secs, bool) or not isinstance(secs, int) or not 1 <= secs <= 7 * 86400:
        return None, "dedupe_s must be 1..604800"
    return {"title": title.strip(), "body": text.strip(), "url": url, "dedupe_key": key, "dedupe_s": secs}, None


def do_notify(clean, notify=None):
    """Run the existing notify path (notify-operator.py notify()): Pushover or desktop, log line, dedupe. -> channel."""
    fn = notify or notify_mod().notify
    return fn(clean["title"], clean["body"], clean["url"], clean["dedupe_key"], clean["dedupe_s"])


# ------------------------------------------------------------------ mirror (queue events and escalations -> Mac files)
def events_dir():
    return Path(os.environ.get("QEL_DIR") or Path.home() / ".ollama-dispatch")


def escalations_dir():
    return events_dir() / "escalations"


ESCALATION_FILES = ("ESCALATIONS.md", "READY-TO-LAND.md")


def last_local_seq(d):
    """Highest seq in queue-events.jsonl (and .1), as queue-event-log.py:last_seq() reads it."""
    best = 0
    for fn in ("queue-events.jsonl", "queue-events.jsonl.1"):
        try:
            with open(Path(d) / fn, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                for ln in f.read().splitlines():
                    try:
                        best = max(best, int(json.loads(ln).get("seq") or 0))
                    except Exception:  # noqa: BLE001
                        pass
        except OSError:
            pass
    return best


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def apply_events(d, events, max_bytes=EVENTS_MAX_BYTES):
    """Append events with seq above the local last seq to <d>/queue-events.jsonl, same line format and rotation as
    queue-event-log.py:345-362. Returns how many were written."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    have = last_local_seq(d)
    fresh = []
    for e in sorted((e for e in events if isinstance(e, dict)), key=lambda e: int(e.get("seq") or 0)):
        try:
            if int(e.get("seq")) > have:
                fresh.append(e)
                have = int(e["seq"])
        except (TypeError, ValueError):
            continue
    if not fresh:
        return 0
    lp = d / "queue-events.jsonl"
    try:
        if lp.stat().st_size > max_bytes:
            os.replace(lp, str(lp) + ".1")
    except OSError:
        pass
    lead = ""
    try:
        with open(lp, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                lead = "\n"
    except OSError:
        pass
    with open(lp, "a") as f:
        f.write(lead + "".join(json.dumps(e) + "\n" for e in fresh))
    return len(fresh)


def apply_escalations(d, files):
    """Write ESCALATIONS.md / READY-TO-LAND.md from {name: text|None}. A missing (None) file is left alone.
    Returns the names written. Only the two names the hooks read are accepted."""
    wrote = []
    for name in ESCALATION_FILES:
        text = (files or {}).get(name)
        if not isinstance(text, str):
            continue
        p = Path(d) / name
        try:
            if p.read_text() == text:
                continue
        except OSError:
            pass
        _write_atomic(p, text)
        wrote.append(name)
    return wrote


def apply_failed_snapshot(d, snap):
    """queue-events.failed.json (queue-event-log.py:364), the file queue-events-deliver.py:365 reads."""
    if isinstance(snap, dict) and isinstance(snap.get("failed"), list):
        _write_atomic(Path(d) / "queue-events.failed.json", json.dumps(snap))
        return True
    return False


def _http_json(url, token, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4_000_000))


class Mirror:
    """Pull loop. Routes it calls on the container API are defined in queue-agent.md (section Mirror routes)."""

    def __init__(self, base_url, token=None, interval_s=MIRROR_DEFAULT_INTERVAL_S, fetch=_http_json,
                 edir=None, esc_dir=None):
        self.base = str(base_url).rstrip("/")
        self.token, self.interval_s, self.fetch = token, interval_s, fetch
        self.edir = Path(edir) if edir else events_dir()
        self.esc_dir = Path(esc_dir) if esc_dir else self.edir / "escalations"
        self.stop = threading.Event()
        self.status = {"last_ok": None, "last_error": None, "events_written": 0}

    def once(self):
        """One pull. Raises on a transport error; the loop catches it."""
        after = last_local_seq(self.edir)
        ev = self.fetch(f"{self.base}/api/mirror/events?after={after}&limit={MIRROR_EVENT_BATCH}", self.token)
        n = apply_events(self.edir, ev.get("events") or [])
        while n and len(ev.get("events") or []) >= MIRROR_EVENT_BATCH:      # more waiting: keep paging
            ev = self.fetch(f"{self.base}/api/mirror/events?after={last_local_seq(self.edir)}"
                            f"&limit={MIRROR_EVENT_BATCH}", self.token)
            n += apply_events(self.edir, ev.get("events") or [])
        apply_failed_snapshot(self.edir, ev.get("failed"))
        esc = self.fetch(f"{self.base}/api/mirror/escalations", self.token)
        apply_escalations(self.esc_dir, esc.get("files") or {})
        self.status.update(last_ok=_now(), last_error=None, events_written=self.status["events_written"] + n)
        return n

    def run(self):
        while not self.stop.is_set():
            try:
                self.once()
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = f"{type(e).__name__}"
                print(f"[queue-agent] mirror pull failed: {e!r}", file=sys.stderr)
            self.stop.wait(self.interval_s)


# ------------------------------------------------------------------ HTTP
class Ctx:
    """What the handler needs. Tests build their own."""

    def __init__(self, token=None, notify_limiter=None, notify=None, mirror=None, hold=None, mac=None, lane=None,
                 dbstatus=None, ledger=None, bgtick=None, dbconfigs=None):
        self.token = token
        self.limiter = notify_limiter or RateLimiter()
        self.notify = notify
        self.mirror = mirror
        self.hold = hold or hold_payload
        self.mac = mac or mac_payload
        self.lane = lane or lane_plan_payload
        self.dbstatus = dbstatus or darkbloom_status_payload
        self.dbconfigs = dbconfigs or darkbloom_model_configs_payload
        self.ledger = ledger or bloomgauge_ledger_payload
        self.bgtick = bgtick or BloomTick(notify=notify)


def make_handler(ctx):
    class H(BaseHTTPRequestHandler):
        server_version = "queue-agent"
        sys_version = ""

        def log_message(self, fmt, *args):        # no request lines: paths carry no secrets, but keep logs small
            pass

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg, headers=None):
            self._send(code, {"error": msg, "status": code, "generated_at": _now()}, headers)

        def _authed(self):
            if auth_ok(self.headers.get("Authorization"), ctx.token):
                return True
            self._err(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
            return False

        def do_GET(self):
            u = urlsplit(self.path)
            try:
                if u.path == "/health":
                    return self._send(200, {"ok": True, "service": "queue-agent", "generated_at": _now(),
                                            "mirror": (ctx.mirror.status if ctx.mirror else None)})
                if not self._authed():
                    return
                if u.path == "/v1/hold":
                    q = parse_qs(u.query)
                    return self._send(200, ctx.hold(model=(q.get("model") or [None])[0]))
                if u.path == "/v1/mac":
                    return self._send(200, ctx.mac())
                if u.path == "/v1/lane-plan":
                    return self._send(200, ctx.lane())
                if u.path == "/v1/darkbloom-status":
                    return self._send(200, ctx.dbstatus())
                if u.path == "/v1/darkbloom-model-configs":
                    return self._send(200, ctx.dbconfigs())
                if u.path == "/v1/bloomgauge-ledger":
                    return self._send(200, ctx.ledger())
                if u.path == "/v1/bloomgauge-tick":
                    return self._send(200, ctx.bgtick.status())
                return self._err(404, "not found")
            except Exception as e:  # noqa: BLE001
                print(f"[queue-agent] GET {u.path} failed: {e!r}", file=sys.stderr)
                return self._err(503, "source unavailable")

        def do_POST(self):
            u = urlsplit(self.path)
            try:
                if not self._authed():
                    return
                if u.path not in ("/v1/notify", "/v1/bloomgauge-tick"):
                    return self._err(404, "not found")
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._err(400, "bad Content-Length")
                if n <= 0 or n > MAX_BODY:
                    return self._err(413 if n > MAX_BODY else 400, "body size")
                try:
                    body = json.loads(self.rfile.read(n))
                except ValueError:
                    return self._err(400, "body is not JSON")
                if u.path == "/v1/bloomgauge-tick":
                    ans, bad = ctx.bgtick.handle(body)
                    return self._err(400, bad) if bad else self._send(200, ans)
                clean, bad = validate_notify(body)
                if bad:
                    return self._err(400, bad)
                ok, retry = ctx.limiter.take()
                if not ok:
                    return self._err(429, "rate limit", {"Retry-After": retry})
                channel = do_notify(clean, ctx.notify)
                return self._send(200, {"ok": True, "channel": channel, "generated_at": _now()})
            except Exception as e:  # noqa: BLE001
                print(f"[queue-agent] POST {u.path} failed: {e!r}", file=sys.stderr)
                return self._err(503, "notify failed" if u.path == "/v1/notify" else "control step failed")

    return H


def build_server(cfg, token, **ctx_kw):
    bind = str(cfg.get("bind") or DEFAULT_BIND)
    port = int(cfg["port"]) if cfg.get("port") is not None else DEFAULT_PORT
    limit = int(cfg.get("notify_per_hour") or NOTIFY_PER_HOUR)
    ctx = Ctx(token=token, notify_limiter=RateLimiter(limit), **ctx_kw)
    srv = ThreadingHTTPServer((bind, port), make_handler(ctx))
    srv.daemon_threads = True
    return srv, ctx


def main():
    # This service IS the Mac side. With QUEUE_AGENT_URL inherited, the queue code it loads would ask the agent (itself)
    # for the vendor files instead of reading them, and /v1/darkbloom-status would wait on its own lock.
    os.environ.pop("QUEUE_AGENT_URL", None)
    cfg = load_config()
    token = read_token()
    bad = check_startup(cfg, token)
    if bad:
        print(f"[queue-agent] {bad}", file=sys.stderr)
        return 2
    mirror = None
    if cfg.get("mirror_url"):
        mt = read_token(cfg.get("mirror_token_file") or Path.home() / ".config" / "ollama-queue" / "queue-api-token")
        mirror = Mirror(cfg["mirror_url"], mt, float(cfg.get("mirror_interval_s") or MIRROR_DEFAULT_INTERVAL_S))
        threading.Thread(target=mirror.run, name="mirror", daemon=True).start()
    srv, _ = build_server(cfg, token, mirror=mirror)
    print(f"[queue-agent] listening on {srv.server_address[0]}:{srv.server_address[1]}"
          f" (mirror {'on' if mirror else 'off'})", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
