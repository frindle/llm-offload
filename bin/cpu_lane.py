#!/usr/bin/env python3
"""cpu_lane.py -- queue side of the Unraid CPU runner (dispatch-cpu-runner).

A durable job store (sqlite WAL + payload blobs on disk under
~/.ollama-dispatch/cpu-jobs/) and the `/api/cpu/*` HTTP routes, mounted into the
dashboard API (ollama-queue-api.py).  Contract = dispatch-cpu-runner README /
reference/queue_server.py:

  producer: POST /api/cpu/jobs {spec,label,stage,bundle_id} -> 201 {id}
            PUT  /api/cpu/jobs/<id>/payload|patch|tools   POST .../ready
            GET  /api/cpu/jobs/<id>   DELETE /api/cpu/jobs/<id> (cancel)
  runner:   POST /api/cpu/claim {runner_id,lease_s,caps,slots,accept,hold,lane_max,disk} -> 200 {job} | 204
            GET  jobs/<id>/payload|patch|tools   POST jobs/<id>/heartbeat|result|release
  open:     GET  /api/cpu/health
  enroll:   POST /api/cpu/enroll {code,runner_id} -> {token,config} (single-use short-lived code, no bearer)
            GET  /api/cpu/config  (bearer) -> {config} runner tunables (concurrency, lease_s, ...)

Remote lane jobs (README "Remote lane jobs"): spec.setup / spec.llm / spec.artifacts / spec.log_stream.  A job that
asks for one is only handed to a runner whose fresh caps report the matching feature (setup_phase, llm_relay,
artifacts, log_stream); it waits for one otherwise.
  runner:   POST jobs/<id>/log {lease_token,off,data_b64,phase,progress} -> {size} | 409 | 416 {error:gap,size}
            PUT  jobs/<id>/artifacts (X-Lease-Token)   heartbeat also carries {phase,progress}
            GET  jobs/<id>/llm/v1/models   POST jobs/<id>/llm/v1/chat/completions (X-Lease-Token)
  producer: GET  jobs/<id>/log?off=N (raw, X-Log-Size)   GET jobs/<id>/artifacts
The llm routes forward to the local Darkbloom endpoint (~/.darkbloom/local.json, read per call) and add its key
here, so the key never leaves this host; spec.llm {models, max_calls, max_concurrency} bounds what a job may spend.
Only a loopback endpoint is used (no proxy, redirects refused, the key cut from every answer).
A remote job holds no GPU lane by itself (bundle_commit_status `cpu_wait`): submit spec.llm jobs only from a queue
job that holds the lane (the mini-swe-runner thin client), so the model is not unloaded under them.
Darkbloom Sandbox holds (Darkbloom lends this Mac to a CI VM and unloads its models): a pending spec.llm job is not
handed out while one lasts (GET jobs/<id> says why it waits), and a model call already in flight waits it out inside
LLM_TIMEOUT_S, then rides out the provider reloading (LLM_RETRY_S), answering 503 sandbox_hold only when the hold
outlasts it. job.llm_held counts the calls that met a hold, so a client can call that failure infra, not the model.
job.llm_wait_s adds up how long the answered calls waited for the provider first (a hold, a reload); v1/models reports it
as cpu_lane.wait_s, and as cpu_lane.wait_live_s a count that also holds the waits still under way and those of calls
that failed (it starts over when the API restarts), so the run can take the wait off its model's time limit as it grows
(the runner relay passes only the body on).
The hold reading is the queue's own (HOLD_VIEW = ollama-queue.py darkbloom_view, SANDBOX_HOLD_WAIT=0 turns it off).

Runner limits (runner README "Disk use"): a claim says what it can take, `accept` "all" | "plain" (no lane job:
none with spec.setup or spec.llm) | "lane" (lane jobs only: a lane-only runner, CPU_RUNNER_ACCEPT=lane) | "none"
(absent or unknown = "all"), and why the runner takes less work right now, `hold` {"all": why, "lane": why,
"plain": why} (stored per runner, replaced by every claim; a lane-only runner always holds "plain"). A pending
job's `waiting` gives those reasons once every online runner that could take it is held for it. `lane_max` and
`disk` (the runner's /work and cache usage) are kept for the dashboard feed (runners[].lane_max / lane_busy / hold /
disk).

SECURITY.  The dashboard API normally trusts every caller because Cloudflare
Access fronts it.  The runner calls over the LAN, so /api/cpu/* enforces its OWN
bearer token (constant-time compare; token file auto-created 0600) on every route
but health, and ALSO refuses any request that looks like it came through the
Cloudflare edge (Cf-* / X-Forwarded-For headers, or a public Host name): the
runner is LAN-only, so the tunnel can never be used to reach these routes even
with a valid token, and a missing/empty token file rejects everything.

The queue daemon uses `outstanding_by_bundle()` (read-only) so a bundle that is
only waiting on a CPU stage does not hold the GPU lanes (see
ollama-queue.py bundle_commit_status `cpu_wait`).  Local-fallback stages register
a `local` marker row (begin_local/end_local) so they count the same way.
"""
import base64
import contextlib
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = Path(os.environ.get("CPU_LANE_DIR") or (Path.home() / ".ollama-dispatch" / "cpu-jobs"))
TOKEN_FILE = Path(os.environ.get("CPU_RUNNER_TOKEN_FILE")
                  or (Path.home() / ".config" / "dispatch-cpu-runner" / "token"))
MAX_ATTEMPTS = 3
PRUNE_DAYS = float(os.environ.get("CPU_LANE_PRUNE_DAYS") or 7)      # rows
BLOB_DAYS = float(os.environ.get("CPU_LANE_BLOB_DAYS") or 1)        # payload blobs of finished jobs
UPLOAD_TTL_S = 1800            # an `uploading` job nobody finished
PENDING_TTL_S = 6 * 3600       # a `pending` job nobody ever claimed
MAX_BLOB = 1 << 30
MAX_JSON = 8 << 20
OUTSTANDING = ("uploading", "pending", "running")
TERMINAL = ("done", "failed_infra", "cancelled", "expired")
BLOB_NAMES = ("payload", "patch", "tools")
ENROLL_TTL_S = 3600
# remote lane jobs
PHASES = ("materialize", "deps", "setup", "running", "artifacts", "done")
FEATURE_CAPS = {"setup": "setup_phase", "llm": "llm_relay", "artifacts": "artifacts", "log_stream": "log_stream"}
LOG_MAX = 40 << 20             # streamed log kept per job (the runner stops itself at 32 MiB plus a note)
LOG_GET_MAX = 8 << 20          # most one GET jobs/<id>/log returns
ARTIFACTS_MAX = 300 << 20      # the runner caps its tarball at 256 MiB
LLM_BODY_MAX = 16 << 20
LLM_TIMEOUT_S = float(os.environ.get("CPU_LANE_LLM_TIMEOUT_S") or 1140)   # under the runner relay's 1200
LLM_RETRY_S = float(os.environ.get("CPU_LANE_LLM_RETRY_S") or 180)  # ride out a provider restart reloading its model
LLM_MODELS_TTL_S, LLM_MODELS_STALE_S = 30, 300
LLM_MAX_CALLS, LLM_MAX_CALLS_HARD = 400, 2000
LLM_CONCURRENCY, LLM_CONCURRENCY_HARD = 2, 4
LLM_RESP_MAX = 32 << 20
LLM_STICKY_S = 60              # the last good local.json reading stands in this long (a provider restart)
# Darkbloom lends this Mac to Darkbloom Sandbox jobs (CI VMs). While a hold lasts the provider unloads its models and
# turns every model call away, for as long as the CI job runs. The API points HOLD_VIEW at the queue's own reading
# (ollama-queue.py darkbloom_view, the one behind the launch loop's INFRA WAIT); None = no view = never held.
HOLD_VIEW = None
HOLD_TTL_S = 10
HOLD_POLL_S = 10              # a model call waits out a hold, re-checking this often, inside LLM_TIMEOUT_S
CANCEL_LEASE_S = 20           # a cancelled running job's lease is cut to this, so the reaper settles it soon
HOLD_TEXT = "darkbloom sandbox hold: this Mac's models are unloaded until the hold is released"
DEFAULT_RUNNER_CONFIG = {"concurrency": 5, "lease_s": 60, "max_job_timeout_s": 3600, "cache_max_entries": 24,
                         "node_options": "--max-old-space-size=6144"}
_enroll_fail = []              # monotonic timestamps of recent failed enrollments (global rate limit)
_ENROLL_MAX_FAILS, _ENROLL_WINDOW_S = 5, 60

# Headers only the Cloudflare edge / a reverse proxy adds.  Any one => not the LAN runner.
_PROXY_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-visitor", "cf-ipcountry",
                  "cf-access-jwt-assertion", "cf-access-authenticated-user-email",
                  "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


# ------------------------------------------------------------------ token
_tok_cache = {"mtime": None, "path": None, "val": ""}


def read_token(create=False, path=None):
    """Token string ('' when missing/empty).  create=True makes a random 32-byte hex
    token (0600, parent 0700) if the file is absent.  Never logged or returned by an API."""
    p = Path(path or TOKEN_FILE)
    try:
        if create and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32) + "\n")
        m = p.stat().st_mtime_ns
        if _tok_cache["path"] != str(p) or _tok_cache["mtime"] != m:
            _tok_cache.update(path=str(p), mtime=m, val=p.read_text().strip())
        return _tok_cache["val"]
    except OSError:
        return ""


def rotate_token(path=None):
    """Write a NEW random token atomically (0600). Every runner holding the old one gets 401 and must re-enroll."""
    p = Path(path or TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    os.replace(tmp, p)
    return p


def token_ok(header_value, token=None):
    tok = read_token() if token is None else token
    if not tok or not header_value or not header_value.startswith("Bearer "):
        return False
    return hmac.compare_digest(header_value[7:].strip().encode(), tok.encode())


def _host_is_lan(host):
    h = (host or "").strip().lower()
    if h.startswith("["):                     # [::1]:7684
        return True
    h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not h or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h) or "." not in h or h == "localhost":
        return True
    return h.endswith((".local", ".lan", ".home.arpa", ".internal"))


def via_proxy(headers):
    """True when the request carries any sign of the Cloudflare edge / a proxy hop."""
    try:
        keys = {k.lower() for k in headers.keys()}
    except Exception:
        return True
    if any(k in keys for k in _PROXY_HEADERS):
        return True
    return not _host_is_lan(headers.get("Host", ""))


# ------------------------------------------------------------------ remote lane spec, lease and hold helpers
def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _loads(s):
    """A stored JSON object, {} when absent or unreadable."""
    try:
        v = json.loads(s) if s else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def spec_error(spec):
    """'' when a job spec's remote lane fields (setup, llm, artifacts, log_stream) are well formed, else the 400 text.
    None/False = the feature is off. The rest of the spec is the runner's business."""
    st = spec.get("setup")
    if st is not None and st is not False:
        if isinstance(st, str):
            if not st.strip():
                return "spec.setup: empty command"
        elif isinstance(st, dict):
            extra = sorted(str(k) for k in set(st) - {"cmd", "timeout_s"})
            if extra:
                return ("spec.setup: unknown key(s) %s" % ", ".join(extra))[:200]
            cmd = st.get("cmd")
            if not ((isinstance(cmd, str) and cmd.strip())
                    or (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd))):
                return "spec.setup.cmd: a non-empty string or list of strings"
            t = st.get("timeout_s")
            if t is not None and not (_is_int(t) and t > 0):
                return "spec.setup.timeout_s: a positive integer"
        else:
            return "spec.setup: a command string or {cmd, timeout_s}"
    llm = spec.get("llm")
    if llm is not None and llm is not False and llm is not True:
        if not isinstance(llm, dict) or not llm:
            return "spec.llm: true or {models, max_calls, max_concurrency}"
        extra = sorted(str(k) for k in set(llm) - {"models", "max_calls", "max_concurrency"})
        if extra:
            return ("spec.llm: unknown key(s) %s" % ", ".join(extra))[:200]
        ms = llm.get("models")
        if ms is not None and not (isinstance(ms, list) and 0 < len(ms) <= 16
                                   and all(isinstance(m, str) and 0 < len(m) <= 200 for m in ms)):
            return "spec.llm.models: a list of 1-16 model ids"
        for k, hard in (("max_calls", LLM_MAX_CALLS_HARD), ("max_concurrency", LLM_CONCURRENCY_HARD)):
            v = llm.get(k)
            if v is not None and not (_is_int(v) and 1 <= v <= hard):
                return "spec.llm.%s: an integer 1..%d" % (k, hard)
    for k in ("artifacts", "log_stream"):
        v = spec.get(k)
        if v is not None and not isinstance(v, bool):
            return "spec.%s: true or false" % k
    return ""


def spec_needs(spec):
    """The runner caps a spec needs (FEATURE_CAPS values), in a stable order."""
    return [cap for k, cap in FEATURE_CAPS.items() if (spec or {}).get(k)]


def is_lane_spec(spec):
    """A lane job (spec.setup / spec.llm: a project venv and/or a model loop, the big ones on a runner's disk): what a
    claim's accept "plain" and a runner's hold "lane" leave out. The runner agent applies the same test."""
    return bool((spec or {}).get("setup") or (spec or {}).get("llm"))


def _hold_json(hold):
    """A claim's `hold` {"all"|"lane"|"plain": why} as stored (printable, <= 300 chars each); None when there is
    none."""
    if not isinstance(hold, dict):
        return None
    out = {}
    for k in ("all", "lane", "plain"):
        v = hold.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = "".join(ch for ch in v if ch.isprintable()).strip()[:300]
    return json.dumps(out, sort_keys=True) if out else None


def _disk_json(disk):
    """A claim's `disk` usage report as stored: up to 32 numeric fields named [a-z_]; None when there is nothing usable."""
    if not isinstance(disk, dict):
        return None
    out = {}
    for k, v in list(disk.items())[:64]:
        if isinstance(k, str) and re.fullmatch(r"[a-z_]{1,32}", k) and isinstance(v, (int, float)) \
                and not isinstance(v, bool) and math.isfinite(v):
            out[k] = round(float(v), 3)
        if len(out) >= 32:
            break
    return json.dumps(out, sort_keys=True) if out else None


def llm_limits(spec):
    """(allowed model ids or None = any, max_calls, max_concurrency) of a spec.llm job."""
    llm = (spec or {}).get("llm")
    d = llm if isinstance(llm, dict) else {}
    ms = d.get("models")
    models = [m for m in ms if isinstance(m, str) and m] if isinstance(ms, list) else None
    mc, cc = d.get("max_calls"), d.get("max_concurrency")
    return (models or None,
            min(mc, LLM_MAX_CALLS_HARD) if _is_int(mc) and mc > 0 else LLM_MAX_CALLS,
            min(cc, LLM_CONCURRENCY_HARD) if _is_int(cc) and cc > 0 else LLM_CONCURRENCY)


def _lease_eq(stored, given):
    """Constant-time lease token check."""
    return bool(stored) and isinstance(given, str) and bool(given) \
        and hmac.compare_digest(str(stored).encode(), given.encode())


def _lease_s(v, default=60):
    """A runner-asked lease length, clamped to 10..3600 s."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(10, min(3600, n)) if n > 0 else default


def _progress_text(v):
    """A progress line as stored: printable characters only, at most 200, '' for anything else."""
    if not isinstance(v, str):
        return ""
    return "".join(ch for ch in v if ch.isprintable()).strip()[:200]


_hold = {"t": None, "view": None, "val": (False, "")}
_hold_lock = threading.Lock()


def model_hold(fresh=False):
    """(held, cause): True while Darkbloom lends this Mac to a Darkbloom Sandbox job, so its models are unloaded and
    every model call is turned away until the hold is released. Read through HOLD_VIEW, cached HOLD_TTL_S. No view,
    or a view that fails, = not held: a model call then meets the provider's own 503 and retries. Never raises."""
    view = HOLD_VIEW
    if view is None:
        return False, ""
    now = time.monotonic()
    with _hold_lock:
        if not fresh and _hold["view"] is view and _hold["t"] is not None and 0 <= now - _hold["t"] < HOLD_TTL_S:
            return _hold["val"]
    try:
        v = view() or {}
        held = bool(v.get("hold"))
        cause = str(v.get("cause") or "")[:120] if held else ""
    except Exception:  # noqa: BLE001
        held, cause = False, ""
    with _hold_lock:
        _hold.update(t=time.monotonic(), view=view, val=(held, cause))
    return held, cause


# ------------------------------------------------------------------ store
# A runner re-sends its caps every ~60s; caps older than this are ignored (an old image that no longer
# reports must not keep advertising what a previous image had).
CAPS_FRESH_S = 180
DISK_FRESH_S = 900          # a runner re-sends its disk report when it changes or every ~60 s; older ones are not shown

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT 'remote',
  status TEXT NOT NULL, spec TEXT, label TEXT, stage TEXT, bundle_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0, runner_id TEXT, lease_token TEXT, lease_expires REAL DEFAULT 0,
  cancel INTEGER NOT NULL DEFAULT 0, result TEXT, created REAL, ready_at REAL, claimed_at REAL,
  finished_at REAL, expires_at REAL, has_patch INTEGER DEFAULT 0, has_tools INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, seq);
CREATE TABLE IF NOT EXISTS enroll(code_hash TEXT PRIMARY KEY, expires REAL NOT NULL, used REAL, created REAL);
CREATE TABLE IF NOT EXISTS runners(runner_id TEXT PRIMARY KEY, last_seen REAL, claims INTEGER DEFAULT 0);
"""


def _spec_owner(spec):
    """Display-only owner label carried in the job spec by cpu_stage (additive; '' when absent/unparseable)."""
    try:
        return str((json.loads(spec) if spec else {}).get("owner") or "")[:160]
    except Exception:  # noqa: BLE001
        return ""


class Store:
    def __init__(self, base=None, clock=time.time, max_attempts=MAX_ATTEMPTS):
        self.base = Path(base or BASE)
        self.clock = clock
        self.max_attempts = max_attempts
        self.blobs = self.base / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db = str(self.base / "jobs.sqlite")
        self.lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # runner capability report (additive, backward compatible: old rows/old runners have NULLs)
            cols = {r[1] for r in c.execute("PRAGMA table_info(runners)")}
            for col, typ in (("caps", "TEXT"), ("caps_t", "REAL"), ("slots", "INTEGER"), ("lane_max", "INTEGER"),
                             ("hold", "TEXT"), ("disk", "TEXT"), ("disk_t", "REAL")):
                if col not in cols:
                    try:
                        c.execute("ALTER TABLE runners ADD COLUMN %s %s" % (col, typ))
                    except sqlite3.OperationalError:       # another process migrated first
                        pass
            # remote lane job state (phase/progress from heartbeats and log posts, model call counts, artifacts)
            cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
            for col, typ in (("phase", "TEXT"), ("progress", "TEXT"), ("progress_t", "REAL"),
                             ("llm_calls", "INTEGER NOT NULL DEFAULT 0"), ("llm_errors", "INTEGER NOT NULL DEFAULT 0"),
                             ("llm_held", "INTEGER NOT NULL DEFAULT 0"), ("has_artifacts", "INTEGER NOT NULL DEFAULT 0"),
                             ("llm_wait_s", "REAL NOT NULL DEFAULT 0")):
                if col not in cols:
                    try:
                        c.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (col, typ))
                    except sqlite3.OperationalError:
                        pass

    # one short-lived autocommit connection per operation: safe across threads and
    # across processes (the queue daemon reads the same file).
    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            yield c
        finally:
            c.close()

    # ---- enrollment codes (single-use, short-lived; only a sha256 is stored) and runner tunables
    @staticmethod
    def _chash(code):
        return hashlib.sha256((code or "").strip().encode()).hexdigest()

    def mint_enroll(self, ttl_s=ENROLL_TTL_S):
        code = secrets.token_urlsafe(9)
        now = self.clock()
        with self._conn() as c:
            c.execute("DELETE FROM enroll WHERE expires < ? OR used IS NOT NULL", (now - 86400,))
            c.execute("INSERT INTO enroll(code_hash,expires,used,created) VALUES(?,?,NULL,?)",
                      (self._chash(code), now + float(ttl_s), now))
        return code

    def redeem_enroll(self, code):
        """True exactly once for a live code (constant-time compare over live hashes, atomic consume)."""
        h = self._chash(code)
        now = self.clock()
        with self._conn() as c:
            rows = c.execute("SELECT code_hash FROM enroll WHERE used IS NULL AND expires >= ?", (now,)).fetchall()
            hit = None
            for r in rows:
                if hmac.compare_digest(r["code_hash"].encode(), h.encode()):
                    hit = r["code_hash"]
            if hit is None:
                return False
            return c.execute("UPDATE enroll SET used=? WHERE code_hash=? AND used IS NULL", (now, hit)).rowcount == 1

    def clear_enroll(self):
        with self._conn() as c:
            c.execute("DELETE FROM enroll")

    def runner_config(self):
        conf = dict(DEFAULT_RUNNER_CONFIG)
        try:
            conf.update(json.loads((self.base / "runner-config.json").read_text()))
        except (OSError, ValueError):
            pass
        return conf

    def set_runner_config(self, **kv):
        conf = self.runner_config()
        conf.update(kv)
        tmp = self.base / "runner-config.json.tmp"
        tmp.write_text(json.dumps(conf, indent=1))
        os.replace(tmp, self.base / "runner-config.json")
        return conf

    # ---- helpers
    def _blob_dir(self, jid):
        return self.blobs / jid

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("spec", "result"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def get(self, jid):
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    # ---- producer
    def create(self, spec, label=None, stage=None, bundle_id=None):
        jid = uuid.uuid4().hex
        now = self.clock()
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO jobs(id,status,spec,label,stage,bundle_id,created) VALUES(?,?,?,?,?,?,?)",
                      (jid, "uploading", json.dumps(spec), label, stage or (spec or {}).get("stage"),
                       bundle_id, now))
        return jid

    def put_blob(self, jid, name, reader, length):
        """Stream `length` bytes from reader.read(n) to disk (atomic rename). -> bytes | None (no job)
        | -1 (job not accepting uploads)."""
        j = self.get(jid)
        if j is None:
            return None
        if j["status"] != "uploading":
            return -1
        d = self._blob_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".part")
        n = 0
        with open(tmp, "wb") as fh:
            while n < length:
                chunk = reader.read(min(1 << 20, length - n))
                if not chunk:
                    break
                fh.write(chunk)
                n += len(chunk)
        if n != length:
            tmp.unlink(missing_ok=True)
            raise ConnectionError("short upload %d/%d" % (n, length))
        os.replace(tmp, d / name)
        col = {"patch": "has_patch", "tools": "has_tools"}.get(name)
        if col:
            with self._conn() as c:
                c.execute("UPDATE jobs SET %s=1 WHERE id=?" % col, (jid,))
        return n

    def blob_path(self, jid, name):
        p = self._blob_dir(jid) / name
        return p if p.is_file() else None

    def ready(self, jid):
        """-> 'ok' | 'nojob' | 'nopayload'"""
        with self.lock, self._conn() as c:
            j = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not j:
                return "nojob"
            if self.blob_path(jid, "payload") is None:
                return "nopayload"
            c.execute("UPDATE jobs SET status='pending', ready_at=? WHERE id=? AND status='uploading'",
                      (self.clock(), jid))
        return "ok"

    def cancel(self, jid):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return False
            if r["status"] in ("uploading", "pending"):
                c.execute("UPDATE jobs SET status='cancelled', cancel=1, finished_at=? WHERE id=?",
                          (self.clock(), jid))
            elif r["status"] == "running":
                c.execute("UPDATE jobs SET cancel=1 WHERE id=?", (jid,))
        return True

    # ---- runner
    def reap(self):
        """Lease expiry + housekeeping.  Returns number of rows changed."""
        now = self.clock()
        n = 0
        with self.lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for r in c.execute("SELECT id,attempt,cancel FROM jobs WHERE kind='remote' AND status='running' "
                                   "AND lease_expires<?", (now,)).fetchall():
                    if r["cancel"]:
                        c.execute("UPDATE jobs SET status='cancelled',finished_at=?,lease_token=NULL,runner_id=NULL "
                                  "WHERE id=?", (now, r["id"]))
                    elif r["attempt"] >= self.max_attempts:
                        c.execute("UPDATE jobs SET status='failed_infra',finished_at=?,lease_token=NULL,"
                                  "runner_id=NULL,result=? WHERE id=?",
                                  (now, json.dumps({"exit_code": None, "infra_error":
                                                    "lease expired %d times" % r["attempt"]}), r["id"]))
                    else:
                        c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?",
                                  (r["id"],))
                    n += 1
                n += c.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='uploading' "
                               "AND created<?", (now, now - UPLOAD_TTL_S)).rowcount
                n += c.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='pending' "
                               "AND created<?", (now, now - PENDING_TTL_S)).rowcount
                n += c.execute("UPDATE jobs SET status='expired',finished_at=? WHERE kind='local' "
                               "AND status='running' AND expires_at<?", (now, now)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    @staticmethod
    def _caps_json(caps):
        """Validated, size-capped JSON for a runner's reported capabilities, or None (ignore)."""
        if not isinstance(caps, dict):
            return None
        try:
            s = json.dumps(caps, sort_keys=True)
        except (TypeError, ValueError):
            return None
        return s if len(s) <= 16384 else None

    @staticmethod
    def _fresh_caps(c, now, runner_id=None):
        """{runner_id: caps} of runners whose last capability report is fresh (CAPS_FRESH_S); one runner when given."""
        q, args = "SELECT runner_id,caps FROM runners WHERE caps IS NOT NULL AND caps_t>=?", [now - CAPS_FRESH_S]
        if runner_id is not None:
            q += " AND runner_id=?"
            args.append(runner_id)
        out = {}
        for r in c.execute(q, args):
            v = _loads(r["caps"])
            if v:
                out[r["runner_id"]] = v
        return out

    @staticmethod
    def _online_holds(c, now):
        """{runner_id: hold} of the runners online now (polled within CAPS_FRESH_S); {} = that runner holds nothing."""
        return {r["runner_id"]: _loads(r["hold"]) for r in
                c.execute("SELECT runner_id,hold FROM runners WHERE last_seen>=? ORDER BY runner_id",
                                (now - CAPS_FRESH_S,))}

    @staticmethod
    def _waiting(spec, held, fresh, online, holds=None):
        """Why a pending job is not being claimed ('' = a runner that fits is online: it is just queued).
        holds: {runner_id: hold} of the online runners (_online_holds); once every one that could take the job is
        held for it (hold "all", "lane" for a lane job, "plain" for any other), their reasons, prefixed by runner
        when there are several."""
        if spec.get("llm") and held:
            return "darkbloom sandbox hold: model jobs wait until it is released"
        if not online:
            return "no runner online"
        needs = spec_needs(spec)
        if needs and not any(all(cp.get(n) for n in needs) for cp in fresh.values()):
            return "no runner reports %s" % ", ".join(needs)
        if holds:
            lane = is_lane_spec(spec)
            why = []
            for rid, h in holds.items():
                if needs and not all((fresh.get(rid) or {}).get(n) for n in needs):
                    continue
                w = h.get("all") or (h.get("lane") if lane else h.get("plain"))
                if not w:
                    return ""
                why.append((rid, w))
            if len(why) == 1:
                return why[0][1]
            return "; ".join("%s: %s" % rw for rw in why)
        return ""

    def claim(self, runner_id, lease_s=60, caps=None, slots=None, accept=None, hold=None, lane_max=None, disk=None):
        """Atomic: the oldest pending remote job this runner can take -> running.  -> job dict | None.
        caps: the runner's capability report (node/python/packages/sqlite3 and the remote lane features), kept with a
        timestamp; a claim without one (runners re-send every ~60 s) uses the stored report while it is fresh, so a
        runner that stops reporting (older image) ages out instead of inheriting what it no longer has.  A job whose
        spec asks for a remote lane feature (FEATURE_CAPS) goes only to a runner that reports it, and a spec.llm job
        waits while a Darkbloom Sandbox hold has the models unloaded.  Each attempt starts clean: no log, phase,
        progress, model call counts or artifacts from an earlier one.
        accept / hold / lane_max / disk: the runner's limits (module doc "Runner limits"); accept "none" claims nothing,
        "plain" skips lane jobs (is_lane_spec), "lane" takes nothing else."""
        now = self.clock()
        lease_s = _lease_s(lease_s)
        self.reap()
        cj = self._caps_json(caps)
        hj, dj = _hold_json(hold), _disk_json(disk)
        accept = accept if accept in ("plain", "lane", "none") else "all"
        held = model_hold()[0]
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO runners(runner_id,last_seen,claims) VALUES(?,?,0) "
                      "ON CONFLICT(runner_id) DO UPDATE SET last_seen=excluded.last_seen", (runner_id, now))
            if cj is not None:
                c.execute("UPDATE runners SET caps=?, caps_t=? WHERE runner_id=?", (cj, now, runner_id))
            if isinstance(slots, int) and not isinstance(slots, bool) and 1 <= slots <= 256:
                c.execute("UPDATE runners SET slots=? WHERE runner_id=?", (slots, runner_id))
            c.execute("UPDATE runners SET hold=? WHERE runner_id=?", (hj, runner_id))
            if isinstance(lane_max, int) and not isinstance(lane_max, bool) and 0 <= lane_max <= 256:
                c.execute("UPDATE runners SET lane_max=? WHERE runner_id=?", (lane_max, runner_id))
            if dj is not None:
                c.execute("UPDATE runners SET disk=?, disk_t=? WHERE runner_id=?", (dj, now, runner_id))
            if accept == "none":
                return None
            eff = self._fresh_caps(c, now, runner_id).get(runner_id, {})
            c.execute("BEGIN IMMEDIATE")
            try:
                r = None
                for cand in c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='pending' AND cancel=0 "
                                      "ORDER BY seq LIMIT 200").fetchall():
                    spec = _loads(cand["spec"])
                    if any(not eff.get(cap) for cap in spec_needs(spec)) or (held and spec.get("llm")) \
                            or (accept == "plain" and is_lane_spec(spec)) \
                            or (accept == "lane" and not is_lane_spec(spec)):
                        continue
                    r = cand
                    break
                if r is None:
                    c.execute("COMMIT")
                    return None
                tok = secrets.token_hex(16)
                c.execute("UPDATE jobs SET status='running',attempt=attempt+1,runner_id=?,lease_token=?,"
                          "lease_expires=?,claimed_at=?,phase=NULL,progress=NULL,progress_t=NULL,llm_calls=0,"
                          "llm_errors=0,llm_held=0,llm_wait_s=0,has_artifacts=0 WHERE id=?",
                          (runner_id, tok, now + lease_s, now, r["id"]))
                c.execute("UPDATE runners SET claims=claims+1 WHERE runner_id=?", (runner_id,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            for name in ("log", "artifacts.tgz"):        # an earlier attempt's output (still under self.lock)
                try:
                    (self._blob_dir(r["id"]) / name).unlink()
                except FileNotFoundError:
                    pass
        spec = json.loads(r["spec"])
        spec.update(has_patch=bool(r["has_patch"]), has_tools=bool(r["has_tools"]))
        return {"id": r["id"], "spec": spec, "attempt": r["attempt"] + 1, "lease_token": tok}

    def _lease_op(self, jid, lease_token):
        j = self.get(jid)
        if j is None:
            return None, "nojob"
        if j["status"] != "running" or not _lease_eq(j["lease_token"], lease_token):
            return j, "lost"
        return j, "ok"

    @staticmethod
    def _progress_cols(j, phase, progress, now):
        """(SET fragment, args) for a phase/progress report, None when nothing changed. Unknown phases and empty
        progress lines keep what is stored."""
        ph = phase if phase in PHASES else j.get("phase")
        pr = _progress_text(progress) or j.get("progress")
        if ph == j.get("phase") and pr == j.get("progress"):
            return None
        return "phase=?,progress=?,progress_t=?", [ph, pr, now]

    def heartbeat(self, jid, lease_token, lease_s=60, runner_id=None, phase=None, progress=None):
        """-> ('ok', cancel) | ('lost', None) | ('nojob', None).  Also records the job's phase and progress line.
        A cancelled job's lease is cut to CANCEL_LEASE_S: the runner kills it and posts nothing, so the reaper
        settles it as cancelled soon after instead of a full lease later."""
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st, None
            now = self.clock()
            exp = now + _lease_s(lease_s)
            if j["cancel"]:
                exp = min(exp, now + CANCEL_LEASE_S)
            sets, args = ["lease_expires=?"], [exp]
            pc = self._progress_cols(j, phase, progress, now)
            if pc:
                sets.append(pc[0])
                args += pc[1]
            with self._conn() as c:
                c.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(sets), args + [jid])
                if runner_id:
                    c.execute("INSERT INTO runners(runner_id,last_seen,claims) VALUES(?,?,0) "
                              "ON CONFLICT(runner_id) DO UPDATE SET last_seen=excluded.last_seen", (runner_id, now))
            return "ok", bool(j["cancel"])

    # ---- remote lane: streamed log, artifacts, model call accounting
    def _log_path(self, jid):
        return self._blob_dir(jid) / "log"

    def log_size(self, jid):
        try:
            return self._log_path(jid).stat().st_size
        except OSError:
            return 0

    def append_log(self, jid, lease_token, off, data_b64, phase=None, progress=None):
        """Append a streamed chunk at byte offset `off`; a resent overlap adds only what is new.
        -> (status, size): 'ok' | 'nojob' | 'lost' | 'bad' | 'gap' (off is past what is held; size = held) | 'full'"""
        if not _is_int(off) or off < 0 or not isinstance(data_b64, str):
            return "bad", None
        try:
            data = base64.b64decode(data_b64, validate=True)
        except ValueError:                     # binascii.Error
            return "bad", None
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st, None
            p = self._log_path(jid)
            size = self.log_size(jid)
            if off > size:
                return "gap", size
            new = data[size - off:]
            if size + len(new) > LOG_MAX:
                return "full", size
            if new:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "ab") as fh:
                    fh.write(new)
                size += len(new)
            pc = self._progress_cols(j, phase, progress, self.clock())
            if pc:
                with self._conn() as c:
                    c.execute("UPDATE jobs SET %s WHERE id=?" % pc[0], pc[1] + [jid])
            return "ok", size

    def read_log(self, jid, off):
        """(bytes from `off`, at most LOG_GET_MAX; size held)."""
        try:
            with open(self._log_path(jid), "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                if off >= size:
                    return b"", size
                fh.seek(off)
                return fh.read(min(LOG_GET_MAX, size - off)), size
        except FileNotFoundError:
            return b"", 0

    def put_artifacts(self, jid, lease_token, reader, length):
        """Stream the runner's artifacts tarball to disk (temp file, then an atomic rename while the lease still
        holds). -> 'ok' | 'nojob' | 'lost'.  Raises ConnectionError on a short body."""
        with self.lock:
            _, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st
        d = self._blob_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ("artifacts.%s.part" % secrets.token_hex(6))
        try:
            n = 0
            with open(tmp, "wb") as fh:
                while n < length:
                    chunk = reader.read(min(1 << 20, length - n))
                    if not chunk:
                        break
                    fh.write(chunk)
                    n += len(chunk)
            if n != length:
                raise ConnectionError("short upload %d/%d" % (n, length))
            with self.lock:
                _, st = self._lease_op(jid, lease_token)
                if st != "ok":
                    return st
                os.replace(tmp, d / "artifacts.tgz")
                with self._conn() as c:
                    c.execute("UPDATE jobs SET has_artifacts=1 WHERE id=?", (jid,))
            return "ok"
        finally:
            tmp.unlink(missing_ok=True)

    def artifacts_path(self, jid):
        p = self._blob_dir(jid) / "artifacts.tgz"
        return p if p.is_file() else None

    def llm_take(self, jid, lease_token, max_calls):
        """One model call from the job's budget, atomic with the lease check. -> 'ok' | 'lost' | 'spent'"""
        with self.lock, self._conn() as c:
            r = c.execute("SELECT status,lease_token,llm_calls FROM jobs WHERE id=?", (jid,)).fetchone()
            if r is None or r["status"] != "running" or not _lease_eq(r["lease_token"], lease_token):
                return "lost"
            if (r["llm_calls"] or 0) >= max_calls:
                return "spent"
            c.execute("UPDATE jobs SET llm_calls=llm_calls+1 WHERE id=?", (jid,))
            return "ok"

    def llm_mark(self, jid, held=False):
        """Count a model call that failed (held=False) or met a Darkbloom Sandbox hold (held=True)."""
        col = "llm_held" if held else "llm_errors"
        with self._conn() as c:
            c.execute("UPDATE jobs SET %s=%s+1 WHERE id=?" % (col, col), (jid,))

    def llm_wait(self, jid, secs):
        """Add the seconds an answered model call waited for the provider (a hold, a reload) before it was sent on."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET llm_wait_s=llm_wait_s+? WHERE id=?", (float(secs), jid))

    def lease_live(self, jid, lease_token):
        return self._lease_op(jid, lease_token)[1] == "ok"

    def job_view(self, j):
        """GET /api/cpu/jobs/<id>: status, result and the remote lane state a producer polls."""
        spec = j.get("spec") if isinstance(j.get("spec"), dict) else {}
        live = j["status"] in ("pending", "running")
        held = bool(spec.get("llm")) and live and model_hold()[0]
        waiting = None
        if j["status"] == "pending":
            now = self.clock()
            with self._conn() as c:
                fresh = self._fresh_caps(c, now)
                holds = self._online_holds(c, now)
            waiting = self._waiting(spec, held, fresh, len(holds), holds) or None
        return {"id": j["id"], "status": j["status"], "attempt": j["attempt"], "result": j["result"],
                "runner_id": j["runner_id"], "phase": j.get("phase"), "progress": j.get("progress"),
                "progress_t": j.get("progress_t"), "log_size": self.log_size(j["id"]),
                "has_artifacts": bool(j.get("has_artifacts")), "llm_calls": j.get("llm_calls") or 0,
                "llm_errors": j.get("llm_errors") or 0, "llm_held": j.get("llm_held") or 0,
                "llm_wait_s": round(j.get("llm_wait_s") or 0.0, 1),
                "cancel": bool(j.get("cancel")), "model_hold": held, "waiting": waiting}

    def release(self, jid, lease_token):
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st
            with self._conn() as c:
                if j["cancel"]:
                    c.execute("UPDATE jobs SET status='cancelled',finished_at=?,lease_token=NULL,runner_id=NULL "
                              "WHERE id=?", (self.clock(), jid))
                else:
                    c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?", (jid,))
            return "ok"

    def result(self, jid, lease_token, body):
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st
            res = {k: v for k, v in body.items() if k not in ("lease_token",)}
            infra = body.get("infra_error")
            now = self.clock()
            if j["cancel"] and not infra:
                status = "cancelled"
            elif infra and infra == "isolation_unavailable" and j["attempt"] < self.max_attempts:
                status, res = "pending", None
            else:
                status = "failed_infra" if infra else "done"
            with self._conn() as c:
                if status == "pending":
                    c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?", (jid,))
                else:
                    c.execute("UPDATE jobs SET status=?,result=?,finished_at=?,lease_token=NULL WHERE id=?",
                              (status, json.dumps(res), now, jid))
            return "ok"

    # ---- local-stage markers (so a local CPU stage counts like a remote one)
    def begin_local(self, stage, bundle_id, timeout_s, label=None):
        jid = uuid.uuid4().hex
        now = self.clock()
        with self._conn() as c:
            c.execute("INSERT INTO jobs(id,kind,status,spec,label,stage,bundle_id,created,claimed_at,expires_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, "local", "running", json.dumps({"stage": stage}), label, stage, bundle_id, now, now,
                       now + float(timeout_s or 600) + 120))
        return jid

    def end_local(self, jid, exit_code=None):
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='done',finished_at=?,result=? WHERE id=? AND kind='local'",
                      (self.clock(), json.dumps({"exit_code": exit_code}), jid))

    # ---- queries
    def outstanding_by_bundle(self):
        """{bundle_id: [job ids]} of CPU work a bundle is waiting on (remote pending/running/
        uploading + live local markers).  Capped by age so a crashed caller cannot wedge a bundle."""
        now = self.clock()
        out = {}
        with self._conn() as c:
            for r in c.execute("SELECT id,kind,spec,bundle_id,created,expires_at FROM jobs "
                               "WHERE status IN ('uploading','pending','running') AND bundle_id IS NOT NULL"):
                if r["kind"] == "local":
                    if (r["expires_at"] or 0) < now:
                        continue
                else:
                    try:
                        t = float(json.loads(r["spec"] or "{}").get("timeout_s") or 600)
                    except Exception:
                        t = 600.0
                    if now - (r["created"] or now) > t + 1800:
                        continue
                out.setdefault(r["bundle_id"], []).append(r["id"])
        return out

    def summary(self, recent=15):
        now = self.clock()
        held, cause = model_hold()
        with self._conn() as c:
            counts = {r[0]: r[1] for r in c.execute("SELECT status,COUNT(*) FROM jobs WHERE kind='remote' "
                                                    "GROUP BY status")}
            local_active = c.execute("SELECT COUNT(*) FROM jobs WHERE kind='local' AND status='running' "
                                     "AND expires_at>=?", (now,)).fetchone()[0]
            running = []
            for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='running' ORDER BY seq"):
                spec = _loads(r["spec"])
                running.append(dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                                    owner=_spec_owner(r["spec"]),
                                    runner=r["runner_id"], attempt=r["attempt"],
                                    running_s=round(now - (r["claimed_at"] or now), 1),
                                    phase=r["phase"], progress=r["progress"],
                                    progress_s_ago=round(now - r["progress_t"], 1) if r["progress_t"] else None,
                                    llm=bool(spec.get("llm")), lane=is_lane_spec(spec), llm_calls=r["llm_calls"] or 0,
                                    llm_errors=r["llm_errors"] or 0, llm_held=r["llm_held"] or 0,
                                    llm_wait_s=round(r["llm_wait_s"] or 0.0, 1),
                                    cancelling=bool(r["cancel"]), log_size=self.log_size(r["id"]),
                                    has_artifacts=bool(r["has_artifacts"])))
            fresh = self._fresh_caps(c, now)
            holds = self._online_holds(c, now)
            pending = []
            for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='pending' ORDER BY seq LIMIT 20"):
                pending.append(dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                                    owner=_spec_owner(r["spec"]),
                                    waiting_s=round(now - (r["ready_at"] or r["created"] or now), 1),
                                    waiting=self._waiting(_loads(r["spec"]), held, fresh, len(holds), holds) or None))
            done = []
            for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND finished_at IS NOT NULL "
                               "ORDER BY finished_at DESC LIMIT ?", (recent,)):
                res = json.loads(r["result"]) if r["result"] else {}
                done.append(dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                                 status=r["status"], runner=(res or {}).get("runner_id") or r["runner_id"],
                                 exit_code=(res or {}).get("exit_code"),
                                 duration_s=round(r["finished_at"] - r["claimed_at"], 1) if r["claimed_at"] else None,
                                 queue_wait_s=round(r["claimed_at"] - (r["ready_at"] or r["created"]), 1)
                                 if r["claimed_at"] else None, finished_at=r["finished_at"]))
            runners = []
            for r in c.execute("SELECT * FROM runners ORDER BY last_seen DESC LIMIT 10"):
                ent = dict(runner=r["runner_id"], seen_s_ago=round(now - r["last_seen"], 1), claims=r["claims"])
                if r["slots"]:                      # saturation: every slot of this runner holds a running job
                    ent["slots"] = r["slots"]
                    ent["busy"] = sum(1 for j in running if j["runner"] == r["runner_id"])
                    ent["saturated"] = ent["busy"] >= ent["slots"]
                if r["lane_max"] is not None:       # disk guards (module doc "Runner limits")
                    ent["lane_max"] = r["lane_max"]
                    ent["lane_busy"] = sum(1 for j in running if j["runner"] == r["runner_id"] and j["lane"])
                if r["hold"]:
                    ent["hold"] = _loads(r["hold"])
                if r["disk"] and r["disk_t"] is not None and now - r["disk_t"] <= DISK_FRESH_S:
                    ent["disk"] = _loads(r["disk"])
                    ent["disk_s_ago"] = round(now - r["disk_t"], 1)
                ct = r["caps_t"]
                if r["caps"] and ct is not None and now - ct <= CAPS_FRESH_S:
                    try:
                        ent["caps"] = json.loads(r["caps"])
                    except ValueError:
                        pass
                runners.append(ent)
        waiting = self.outstanding_by_bundle()
        return {"enabled": True, "counts": counts, "queue_depth": counts.get("pending", 0),
                "running": running, "pending": pending, "recent": done, "runners": runners,
                "local_stages_active": local_active,
                "bundles_waiting": {k: len(v) for k, v in waiting.items()},
                "model_hold": {"held": held, "cause": cause or None}}

    def prune(self):
        now = self.clock()
        n = 0
        with self.lock, self._conn() as c:
            for r in c.execute("SELECT id FROM jobs WHERE status IN ('done','failed_infra','cancelled','expired') "
                               "AND finished_at<?", (now - BLOB_DAYS * 86400,)).fetchall():
                d = self._blob_dir(r["id"])
                if d.exists():
                    _rmtree(d)
            for r in c.execute("SELECT id FROM jobs WHERE status IN ('done','failed_infra','cancelled','expired') "
                               "AND finished_at<?", (now - PRUNE_DAYS * 86400,)).fetchall():
                _rmtree(self._blob_dir(r["id"]))
                c.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                n += 1
        # blob dirs with no row at all
        try:
            with self._conn() as c:
                known = {r[0] for r in c.execute("SELECT id FROM jobs")}
            for d in self.blobs.iterdir():
                if d.name not in known and now - d.stat().st_mtime > 3600:
                    _rmtree(d)
        except OSError:
            pass
        return n


def _rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------ singleton + reaper
_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = Store(BASE)
        return _store


def start_reaper(store=None, interval=10, prune_every=3600):
    """Daemon thread: lease reaper tick + hourly prune.  Never raises out of the thread."""
    s = store or get_store()

    def loop():
        last_prune = 0.0
        while True:
            try:
                s.reap()
                if time.time() - last_prune > prune_every:
                    s.prune()
                    last_prune = time.time()
            except Exception as e:      # noqa: BLE001
                print("[cpu-lane] reaper error: %r" % (e,), flush=True)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="cpu-lane-reaper", daemon=True)
    t.start()
    return t


def outstanding_by_bundle(base=None):
    """Read-only for the queue daemon.  {} on ANY problem (fail toward 'no cpu wait')."""
    try:
        b = Path(base or BASE)
        if not (b / "jobs.sqlite").exists():
            return {}
        s = Store(b)
        return s.outstanding_by_bundle()
    except Exception:
        return {}


def lan_ip():
    """This Mac's LAN IPv4 (what a runner should put in CPU_RUNNER_API), '' if undiscoverable.
    Prefers an RFC1918 address on a physical interface (en*) over the routing-table guess, which
    returns a WARP/CGNAT 100.x tunnel address when Cloudflare WARP is up."""
    import ipaddress
    import subprocess
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        iface = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                iface = line.split(":")[0]
            elif iface and iface.startswith("en") and line.strip().startswith("inet "):
                ip = line.split()[1]
                if ipaddress.ip_address(ip).is_private and not ip.startswith("169.254."):
                    return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.217", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


# ------------------------------------------------------------------ HTTP
_ID_RE = re.compile(r"^/api/cpu/jobs/([0-9a-f]{32})(?:/(\w+))?$")
_LLM_RE = re.compile(r"^/api/cpu/jobs/([0-9a-f]{32})/llm/v1/(chat/completions|models)$")


def _send(h, code, body=None, raw=None, ctype=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    h.send_response(code)
    h.send_header("Content-Type", ctype or ("application/octet-stream" if raw is not None else "application/json"))
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if data:
        h.wfile.write(data)


def _stream_file(h, p, ctype="application/octet-stream"):
    """Send file `p` as it is when opened (its fstat size), 1 MiB at a time; 404 when it is gone."""
    try:
        fh = open(p, "rb")
    except OSError:
        return _send(h, 404, {"error": "not found"})
    with fh:
        left = os.fstat(fh.fileno()).st_size
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(left))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        while left > 0:
            chunk = fh.read(min(1 << 20, left))
            if not chunk:                  # cut short under us: the client sees a short body, never a wrong one
                h.close_connection = True
                break
            h.wfile.write(chunk)
            left -= len(chunk)
    return None


def _oai_err(code, msg, typ, headers=None, kind="error"):
    """(code, ctype, body, headers, kind) carrying an OpenAI-style error body, which model clients parse."""
    body = json.dumps({"error": {"message": msg, "type": typ, "code": typ}}).encode()
    return code, "application/json", body, dict(headers or {}), kind


# ------------------------------------------------------------------ model relay: jobs/<id>/llm/v1/*
# A spec.llm job's model calls come here through its runner (agent.py relay: X-Lease-Token plus the runner bearer)
# and go on to the local Darkbloom endpoint with the key added here, so the key never leaves this Mac. Only a loopback
# endpoint is used, redirects are never followed, no proxy is consulted, and the key is cut from whatever comes back.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                        # urllib then raises HTTPError(3xx): answered as a 502


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
_LLM_DELAYS = (3, 6, 10, 15)               # between retries of a restarting/reloading provider (the last repeats)
_llm_lock = threading.Lock()
_llm_inflight = {}                         # job id -> model calls in flight
_llm_good = {"rec": None, "t": 0.0}        # last good local.json record (monotonic time)
_llm_models = {"t": None, "base": None, "data": None}
_llm_waits = {}                            # job id -> {call: [t0, t_attempt or None]}: calls in flight that met a hold or a retry
_llm_waited = {}                           # job id -> seconds its finished calls waited for the provider (answered or not)
_WAITED_KEEP = 256                         # jobs kept in _llm_waited (the least recently ended wait goes first)


def _wait_note(jid, call, t0, t_attempt=None):
    """Record that the call (in flight since t0) is waiting for the provider (t_attempt None: the wait still grows) or is
    in the attempt that began at t_attempt (its wait stops there, as job.llm_wait_s will count it)."""
    with _llm_lock:
        _llm_waits.setdefault(jid, {})[call] = [t0, t_attempt]


def _wait_drop(jid, call):
    """The call is over: its wait (if it waited) moves into the job's finished total in the same step, so wait_live_s
    neither dips nor counts it twice."""
    now = time.monotonic()
    with _llm_lock:
        d = _llm_waits.get(jid)
        row = d.pop(call, None) if d is not None else None
        if d is not None and not d:
            _llm_waits.pop(jid, None)
        if row is not None:
            t0, ta = row
            _llm_waited[jid] = _llm_waited.pop(jid, 0.0) + max(0.0, (ta if ta is not None else now) - t0)
            while len(_llm_waited) > _WAITED_KEEP:
                _llm_waited.pop(next(iter(_llm_waited)))


def wait_live_s(jid):
    """Seconds the job's model calls have waited for the provider (a hold, a reload) since this process started, the
    waits still under way included (each call in flight counts its own), and the waits of calls that then failed. It
    only grows while the queue API runs, so a run can credit the growth while a call is still waiting; it starts over
    from 0 when the API restarts (a run takes a drop as a new start, not as credit). Never raises."""
    now = time.monotonic()
    with _llm_lock:
        rows = list((_llm_waits.get(jid) or {}).values())
        done = _llm_waited.get(jid, 0.0)
    return done + sum(max(0.0, (ta if ta is not None else now) - t0) for t0, ta in rows)


def _local_json_path():
    return Path(os.environ.get("CPU_LANE_LLM_LOCAL_JSON") or os.environ.get("DARKBLOOM_LOCAL_JSON")
                or (Path.home() / ".darkbloom" / "local.json"))


def _loopback_base(b):
    """The endpoint root (no /v1) when `b` is a plain http(s) URL on this host (a loopback address or localhost),
    else None: the key is only ever sent to such a URL."""
    try:
        u = urllib.parse.urlsplit(str(b or "").strip())
        u.port                             # raises ValueError on a bad port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or u.username is not None or u.password is not None \
            or u.query or u.fragment or not u.hostname:
        return None
    host = u.hostname.lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            return None
    path = u.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, "", ""))


def llm_upstream():
    """(base, key) of the local Darkbloom endpoint, read from local.json on every call (the provider rewrites it on
    each restart), or (None, None) = off. Same reading as darkbloom_chat: a torn/empty file stands on the last good
    record for LLM_STICKY_S; a missing file, or a record with neither api_key nor base_url, is off and never masked;
    the base is the record's base_url, else DARKBLOOM_BASE_URL / http://127.0.0.1:8000 when it carries an api_key.
    Anything but a loopback base is off."""
    try:
        txt = _local_json_path().read_text()
    except OSError:
        return None, None
    try:
        rec = json.loads(txt)
    except ValueError:
        rec = None
    now = time.monotonic()
    with _llm_lock:
        if not isinstance(rec, dict):
            if _llm_good["rec"] is None or not 0 <= now - _llm_good["t"] < LLM_STICKY_S:
                return None, None
            rec = dict(_llm_good["rec"])
        elif rec.get("api_key") or rec.get("base_url"):
            _llm_good.update(rec=dict(rec), t=now)
    key = rec.get("api_key") if isinstance(rec.get("api_key"), str) and rec.get("api_key") else None
    b = str(rec.get("base_url") or "").strip()
    if not b and key:
        b = os.environ.get("DARKBLOOM_BASE_URL") or "http://127.0.0.1:8000"
    base = _loopback_base(b) if b else None
    return (base, key) if base else (None, None)


def _read_cap(resp, cap, deadline):
    """The whole body, refusing more than `cap` bytes (OverflowError) or reading past `deadline` (TimeoutError)."""
    read = getattr(resp, "read1", None) or resp.read
    out, n = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("past the model call deadline")
        chunk = read(min(1 << 16, cap + 1 - n))
        if not chunk:
            return b"".join(out)
        out.append(chunk)
        n += len(chunk)
        if n > cap:
            raise OverflowError("over %d bytes" % cap)


def _llm_once(base, key, sub, method, body, deadline):
    """One call to the local endpoint -> (code, content type, body, Retry-After). Raises URLError/OSError (no
    answer, a timeout), http.client.HTTPException (a broken answer) or OverflowError (over LLM_RESP_MAX)."""
    req = urllib.request.Request("%s/v1/%s" % (base, sub), data=body, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = _OPENER.open(req, timeout=max(1.0, deadline - time.monotonic()))
    except urllib.error.HTTPError as e:
        resp = e
    with contextlib.closing(resp):
        return resp.getcode(), resp.headers.get("Content-Type"), _read_cap(resp, LLM_RESP_MAX, deadline), \
            resp.headers.get("Retry-After")


def _is_timeout(e):
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def _llm_forward(S, jid, lease, sub, method, body):
    """Forward one model call -> (code, content type, body, headers, kind), kind ok | upstream | hold | lease | error.
    A Darkbloom Sandbox hold is waited out (re-checked every HOLD_POLL_S) and a provider that is restarting or
    reloading (503, no answer, local.json off) is retried 3, 6, 10, 15 s apart within LLM_RETRY_S of the call's start
    or of the hold's end, all inside LLM_TIMEOUT_S; the lease is re-checked after every wait. A timeout is a 504 and
    is not retried. The key is cut from whatever comes back. An answer (under 400) that came after a hold or a retry adds
    the time before its own attempt to job.llm_wait_s; a 5xx is the client's outage to count. While the call waits,
    wait_live_s(jid) counts the wait as it grows (v1/models: cpu_lane.wait_live_s), so the run can stop its clocks
    during a hold, not only after the answer."""
    t0 = time.monotonic()
    deadline = t0 + LLM_TIMEOUT_S
    retry_until = t0 + LLM_RETRY_S
    marked, tries, call = False, 0, object()
    try:
        while True:
            held, cause = model_hold(fresh=marked or tries > 0)
            if held:
                if not marked:
                    marked = True
                    S.llm_mark(jid, held=True)
                _wait_note(jid, call, t0)
                if time.monotonic() + HOLD_POLL_S >= deadline:
                    return _oai_err(503, HOLD_TEXT + (" (%s)" % cause if cause else ""), "sandbox_hold",
                                    {"Retry-After": str(int(HOLD_POLL_S * 3))}, kind="hold")
                time.sleep(HOLD_POLL_S)
                if not S.lease_live(jid, lease):
                    return _oai_err(409, "lease lost while the call waited out a darkbloom sandbox hold", "lease_lost",
                                    kind="lease")
                retry_until = max(retry_until, time.monotonic() + LLM_RETRY_S)   # the provider reloads after a hold
                continue
            base, key = llm_upstream()
            t_try = time.monotonic()
            if marked or tries:
                _wait_note(jid, call, t0, t_try)   # the wait ends where this attempt begins (what llm_wait_s will add)
            if base is None:
                last = _oai_err(503, "the local model endpoint is off (no usable ~/.darkbloom/local.json)", "provider_off")
            else:
                try:
                    code, ctype, data, ra = _llm_once(base, key, sub, method, body, deadline)
                except OverflowError:
                    return _oai_err(502, "the model response is over %d bytes" % LLM_RESP_MAX, "response_too_large")
                except (OSError, http.client.HTTPException) as e:
                    if _is_timeout(e):
                        return _oai_err(504, "the local model endpoint did not answer within %d s" % LLM_TIMEOUT_S,
                                        "timeout")
                    last = _oai_err(502, "the local model endpoint did not answer (%s)"
                                    % type(getattr(e, "reason", None) or e).__name__, "provider_unreachable")
                else:
                    if 300 <= code < 400:
                        return _oai_err(502, "the local model endpoint answered with a redirect (%d), not followed"
                                        % code, "upstream_redirect")
                    if key:
                        data = data.replace(key.encode(), b"[redacted]")
                    extra = {"Retry-After": ra.strip()} if ra and ra.strip().isdigit() else {}
                    last = (code, ctype or "application/json", data, extra, "ok" if code < 400 else "upstream")
                    if code != 503:
                        if code < 400 and (marked or tries):
                            _wait_drop(jid, call)   # its wait (to t_try) joins the finished total in the same step
                            S.llm_wait(jid, t_try - t0)
                        return last
            tries += 1
            _wait_note(jid, call, t0)
            delay = _LLM_DELAYS[min(tries, len(_LLM_DELAYS)) - 1]
            if time.monotonic() + delay > min(retry_until, deadline):
                return last
            time.sleep(delay)
            if not S.lease_live(jid, lease):
                return _oai_err(409, "lease lost while the call waited for the model endpoint", "lease_lost",
                                kind="lease")
    finally:
        _wait_drop(jid, call)


def _llm_model_list(allowed, wait_s=None, live_s=None):
    """GET v1/models -> (code, ctype, body, headers, kind): the local endpoint's list, cached LLM_MODELS_TTL_S (a copy
    up to LLM_MODELS_STALE_S old stands in while it does not answer), cut to the job's spec.llm.models, plus
    cpu_lane.wait_s (the job's llm_wait_s) and cpu_lane.wait_live_s (wait_live_s) when given. The 503 for an endpoint
    that does not answer carries cpu_lane too: a job reads its wait count there while a hold lasts."""
    now = time.monotonic()
    base, key = llm_upstream()
    with _llm_lock:
        c = dict(_llm_models)
    data = None
    if base and c["data"] is not None and c["base"] == base and 0 <= now - c["t"] < LLM_MODELS_TTL_S:
        data = c["data"]
    elif base:
        try:
            code, _, raw, _ = _llm_once(base, key, "models", "GET", None, time.monotonic() + 15)
            v = json.loads(raw) if code == 200 else None
            if isinstance(v, dict) and isinstance(v.get("data"), list):
                data = [m for m in v["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
                with _llm_lock:
                    _llm_models.update(t=time.monotonic(), base=base, data=data)
        except (OSError, ValueError, OverflowError, http.client.HTTPException):
            pass
    if data is None and c["data"] is not None and 0 <= now - c["t"] < LLM_MODELS_STALE_S:
        data = c["data"]
    lane = ({k: round(float(v), 1) for k, v in (("wait_s", wait_s), ("wait_live_s", live_s)) if v is not None}
            if wait_s is not None or live_s is not None else None)
    if data is None:
        out = _oai_err(503, "the local model endpoint is %s" % ("not answering" if base else "off"), "provider_unavailable")
        if lane is None:
            return out
        return out[:2] + (json.dumps({**json.loads(out[2]), "cpu_lane": lane}).encode(),) + out[3:]
    if allowed:
        ok = {m.lower() for m in allowed}
        data = [m for m in data if m["id"].lower() in ok]
    doc = {"object": "list", "data": data}
    if lane is not None:
        doc["cpu_lane"] = lane
    body = json.dumps(doc).encode()
    if key:
        body = body.replace(key.encode(), b"[redacted]")
    return 200, "application/json", body, {}, "ok"


def _llm(h, S, jid, sub, method):
    """jobs/<id>/llm/v1/chat/completions (POST) | models (GET), relayed by the job's runner. Checked in this order:
    413 body over LLM_BODY_MAX, 404 job, 409 lease (X-Lease-Token), 403 spec.llm off, 405 method, then for a call
    400 body/model, 403 model not in spec.llm.models, 429 spec.llm.max_concurrency, 403 spec.llm.max_calls spent."""
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        n = -1
    out = None
    if n < 0 or n > LLM_BODY_MAX:
        out = _oai_err(413 if n > 0 else 400, "request body over %d bytes" % LLM_BODY_MAX if n > 0
                       else "bad Content-Length", "invalid_request_error")
    j = S.get(jid) if out is None else None
    if out is None and j is None:
        out = _oai_err(404, "no such job", "not_found")
    lease = h.headers.get("X-Lease-Token") or ""
    if out is None and (j["status"] != "running" or not _lease_eq(j["lease_token"], lease)):
        out = _oai_err(409, "lease lost: this job does not hold its lease", "lease_lost")
    spec = j.get("spec") if out is None and isinstance(j.get("spec"), dict) else {}
    if out is None and not spec.get("llm"):
        out = _oai_err(403, "this job did not ask for model calls (spec.llm)", "llm_not_enabled")
    want = "POST" if sub == "chat/completions" else "GET"
    if out is None and method != want:
        out = _oai_err(405, "v1/%s takes %s" % (sub, want), "method_not_allowed", {"Allow": want})
    if out is not None:
        if n:
            h.close_connection = True      # the body is left unread
        return _send(h, out[0], raw=out[2], ctype=out[1], headers=out[3])
    allowed, max_calls, max_cc = llm_limits(spec)
    if sub == "models":
        out = _llm_model_list(allowed, j.get("llm_wait_s") or 0.0, wait_live_s(jid))
        return _send(h, out[0], raw=out[2], ctype=out[1], headers=out[3])
    body = h.rfile.read(n) if n else b""
    if len(body) != n:
        h.close_connection = True
        return None
    try:
        req = json.loads(body)
    except ValueError:
        req = None
    model = req.get("model") if isinstance(req, dict) else None
    if not isinstance(req, dict):
        out = _oai_err(400, "the body must be a JSON object", "invalid_request_error")
    elif not isinstance(model, str) or not model:
        out = _oai_err(400, "model required", "invalid_request_error")
    elif allowed and model.lower() not in {m.lower() for m in allowed}:
        out = _oai_err(403, "model %s is not in this job's spec.llm.models" % model[:200], "model_not_allowed")
    if out is not None:
        return _send(h, out[0], raw=out[2], ctype=out[1], headers=out[3])
    with _llm_lock:
        busy = _llm_inflight.get(jid, 0) >= max_cc
        if not busy:
            _llm_inflight[jid] = _llm_inflight.get(jid, 0) + 1
    if busy:
        out = _oai_err(429, "this job already has %d model calls in flight (spec.llm.max_concurrency)" % max_cc,
                       "concurrency_limit", {"Retry-After": "2"})
        return _send(h, out[0], raw=out[2], ctype=out[1], headers=out[3])
    try:
        st = S.llm_take(jid, lease, max_calls)
        if st == "lost":
            out = _oai_err(409, "lease lost: this job does not hold its lease", "lease_lost", kind="lease")
        elif st == "spent":
            out = _oai_err(403, "this job used its %d model calls (spec.llm.max_calls)" % max_calls, "budget_spent",
                           kind="lease")
        else:
            out = _llm_forward(S, jid, lease, sub, method, body)
            if out[0] >= 400 and out[4] not in ("hold", "lease"):
                S.llm_mark(jid)
    finally:
        with _llm_lock:
            left = _llm_inflight.get(jid, 1) - 1
            if left > 0:
                _llm_inflight[jid] = left
            else:
                _llm_inflight.pop(jid, None)
    return _send(h, out[0], raw=out[2], ctype=out[1], headers=out[3])


def _json_body(h):
    n = int(h.headers.get("Content-Length") or 0)
    if n > MAX_JSON:
        raise ValueError("body too large")
    raw = h.rfile.read(n) if n else b""
    return json.loads(raw) if raw else {}


def _enroll(h, store, token):
    """POST /api/cpu/enroll: trade a single-use code for the bearer token.  LAN-only (caller already passed the
    proxy check), globally rate limited after repeated failures, failures are slowed, codes are consumed once."""
    now = time.monotonic()
    _enroll_fail[:] = [t for t in _enroll_fail if now - t < _ENROLL_WINDOW_S]
    if len(_enroll_fail) >= _ENROLL_MAX_FAILS:
        return _send(h, 429, {"error": "too many failed enrollments; wait a minute"})
    try:
        b = _json_body(h)
    except (ValueError, json.JSONDecodeError):
        return _send(h, 400, {"error": "bad json"})
    code = str(b.get("code") or "")
    tok = read_token() if token is None else token
    S = store or get_store()
    if not tok or len(code) < 8 or len(code) > 128 or not S.redeem_enroll(code):
        _enroll_fail.append(time.monotonic())
        time.sleep(0.5)
        return _send(h, 403, {"error": "invalid, expired or already used enrollment code"})
    return _send(h, 200, {"token": tok, "runner_id": str(b.get("runner_id") or "")[:64], "config": S.runner_config()})


def handle(h, method, store=None, token=None):
    """Serve one /api/cpu/* request on handler `h`.  Always responds."""
    path = h.path.split("?")[0]
    if path == "/api/cpu/health" and method == "GET":
        return _send(h, 200, {"ok": True})
    if via_proxy(h.headers):
        return _send(h, 403, {"error": "cpu lane is LAN-only"})
    if path == "/api/cpu/enroll" and method == "POST":
        return _enroll(h, store, token)
    if not token_ok(h.headers.get("Authorization", ""), token):
        time.sleep(0.25)               # slow a brute-force; handler threads are cheap
        return _send(h, 401, {"error": "unauthorized"})
    S = store or get_store()
    try:
        if method == "POST" and path == "/api/cpu/jobs":
            b = _json_body(h)
            if not isinstance(b.get("spec"), dict) or not b["spec"].get("cmd"):
                return _send(h, 400, {"error": "spec.cmd required"})
            err = spec_error(b["spec"])
            if err:
                return _send(h, 400, {"error": err})
            return _send(h, 201, {"id": S.create(b["spec"], b.get("label"), b.get("stage"), b.get("bundle_id"))})
        if method == "POST" and path == "/api/cpu/claim":
            b = _json_body(h)
            if not b.get("runner_id"):
                return _send(h, 400, {"error": "runner_id required"})
            job = S.claim(str(b["runner_id"]), b.get("lease_s"), caps=b.get("caps"), slots=b.get("slots"),
                          accept=b.get("accept"), hold=b.get("hold"), lane_max=b.get("lane_max"), disk=b.get("disk"))
            return _send(h, 200, {"job": job}) if job else _send(h, 204)
        if method == "GET" and path == "/api/cpu/config":
            return _send(h, 200, {"config": S.runner_config()})
        if method == "GET" and path == "/api/cpu/runners":
            return _send(h, 200, {"runners": S.summary()["runners"]})
        m = _LLM_RE.match(path)
        if m:
            return _llm(h, S, m.group(1), m.group(2), method)
        m = _ID_RE.match(path)
        if not m or S.get(m.group(1)) is None:
            return _send(h, 404, {"error": "not found"})
        jid, sub = m.group(1), m.group(2)
        if sub is None and method == "GET":
            return _send(h, 200, S.job_view(S.get(jid)))
        if sub is None and method == "DELETE":
            S.cancel(jid)
            return _send(h, 200, {"ok": True})
        if sub in BLOB_NAMES:
            if method == "PUT":
                n = int(h.headers.get("Content-Length") or 0)
                if n > MAX_BLOB:
                    return _send(h, 413, {"error": "blob too large"})
                r = S.put_blob(jid, sub, h.rfile, n)
                if r == -1:
                    return _send(h, 409, {"error": "job not accepting uploads"})
                return _send(h, 200, {"ok": True, "bytes": r})
            if method == "GET":
                p = S.blob_path(jid, sub)
                if p is None:
                    return _send(h, 404, {"error": "no blob"})
                return _stream_file(h, p)
        if sub == "log" and method == "POST":
            b = _json_body(h)
            st, size = S.append_log(jid, b.get("lease_token"), b.get("off"), b.get("data_b64"),
                                    b.get("phase"), b.get("progress"))
            if st == "ok":
                return _send(h, 200, {"ok": True, "size": size})
            if st == "gap":
                return _send(h, 416, {"error": "gap", "size": size})
            if st == "full":
                return _send(h, 413, {"error": "log over %d bytes" % LOG_MAX, "size": size})
            if st == "bad":
                return _send(h, 400, {"error": "off (an integer >= 0) and data_b64 (base64) required"})
            return _send(h, 409, {"error": "lease lost"})
        if sub == "log" and method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("off") or ["0"]
            if not re.fullmatch(r"\d{1,15}", q[0]):
                return _send(h, 400, {"error": "off: a byte offset"})
            data, size = S.read_log(jid, int(q[0]))
            return _send(h, 200, raw=data, ctype="text/plain; charset=utf-8", headers={"X-Log-Size": str(size)})
        if sub == "artifacts" and method == "PUT":
            cl = h.headers.get("Content-Length")
            try:
                n = int(cl) if cl is not None else None
            except ValueError:
                n = -1
            if n is None or n < 0 or n > ARTIFACTS_MAX:
                h.close_connection = True      # the body is left unread
                if n is None:
                    return _send(h, 411, {"error": "Content-Length required"})
                return _send(h, 413 if n > ARTIFACTS_MAX else 400,
                             {"error": "artifacts over %d bytes" % ARTIFACTS_MAX if n > 0 else "bad Content-Length"})
            st = S.put_artifacts(jid, h.headers.get("X-Lease-Token") or "", h.rfile, n)
            if st == "ok":
                return _send(h, 200, {"ok": True, "bytes": n})
            h.close_connection = True
            return _send(h, 404, {"error": "not found"}) if st == "nojob" else _send(h, 409, {"error": "lease lost"})
        if sub == "artifacts" and method == "GET":
            p = S.artifacts_path(jid)
            if p is None:
                return _send(h, 404, {"error": "no artifacts"})
            return _stream_file(h, p, "application/gzip")
        if sub == "ready" and method == "POST":
            r = S.ready(jid)
            return _send(h, 400, {"error": "payload missing"}) if r == "nopayload" else _send(h, 200, {"ok": True})
        if sub in ("heartbeat", "result", "release") and method == "POST":
            b = _json_body(h)
            lt = b.get("lease_token")
            if sub == "heartbeat":
                st, cancel = S.heartbeat(jid, lt, b.get("lease_s"), b.get("runner_id"), b.get("phase"),
                                         b.get("progress"))
                return _send(h, 200, {"ok": True, "cancel": cancel}) if st == "ok" else _send(h, 409, {"error": "lease lost"})
            st = S.release(jid, lt) if sub == "release" else S.result(jid, lt, b)
            return _send(h, 200, {"ok": True}) if st == "ok" else _send(h, 409, {"error": "lease lost"})
        return _send(h, 404, {"error": "no route"})
    except (ValueError, json.JSONDecodeError) as e:
        return _send(h, 400, {"error": str(e)[:200]})
    except ConnectionError:
        return None
    except Exception as e:      # noqa: BLE001
        return _send(h, 500, {"error": type(e).__name__})


if __name__ == "__main__":
    # tiny CLI: `cpu_lane.py token` (create if absent, print PATH only), `cpu_lane.py ip`, `cpu_lane.py api-url`, `cpu_lane.py summary`
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "token":
        read_token(create=True)
        print(TOKEN_FILE)
    elif cmd == "ip":
        print(lan_ip())
    elif cmd == "api-url":          # the exact CPU_RUNNER_API value for the Unraid .env
        print("http://%s:7684" % (lan_ip() or "<mac-lan-ip>"))
    elif cmd == "enroll-code":      # single-use code for a new Unraid runner: paste it + the API URL into the template
        ttl = ENROLL_TTL_S
        if "--ttl" in sys.argv:
            ttl = int(sys.argv[sys.argv.index("--ttl") + 1])
        read_token(create=True)
        code = get_store().mint_enroll(ttl)
        print("CPU_RUNNER_API=http://%s:7684" % (lan_ip() or "<mac-lan-ip>"))
        print("CPU_RUNNER_ENROLL_CODE=%s" % code)
        print("(single use, expires in %d min; the container stores the token itself and never needs the code again)" % (ttl // 60))
    elif cmd == "rotate-token":     # revoke every runner: new token, outstanding codes cleared
        rotate_token()
        get_store().clear_enroll()
        print("token rotated; all runners are now rejected (401). Mint a new code: cpu_lane.py enroll-code")
    elif cmd == "config":           # `config` shows, `config key=value ...` sets (concurrency, lease_s, ...)
        st = get_store()
        kv = {}
        for a in sys.argv[2:]:
            k, _, v = a.partition("=")
            kv[k] = int(v) if v.isdigit() else v
        print(json.dumps(st.set_runner_config(**kv) if kv else st.runner_config(), indent=1))
    else:
        print(json.dumps(get_store().summary(), indent=1))
