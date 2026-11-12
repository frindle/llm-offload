#!/usr/bin/env python3
"""conformance_log: per-request wire records + drift classification (design section 3).

PURE LIBRARY, UNWIRED (Wave 0). Nothing imports this yet; Wave 2 wires the four send sites
(worker call_ollama openai/ollama branches, call_openai_streaming, preflight, darkbloom_chat._post)
and Wave 3 the reap comparison.

One JSON line per request: what actually went on the wire (host, path, api, model, a hash of the
non-message fields, the key FINGERPRINT, status, latency, usage, finish reason, echoed model). A
FULL record additionally carries the redacted `fields` dict, only the first time a given
(plan, fsha, host, model) is seen. Messages, tool schemas, response_format bodies and keys are
NEVER written; a literal key supplied by the caller is scrubbed from the line as defence in depth.

Bounded volume: per-record size cap, per-day size cap, 14-day retention and a total size cap,
enforced by prune(). Logging never raises. Kill switches: CONFORMANCE_LOG=0 (no records),
CONFORMANCE_CHECK=0 (classify() returns a skipped verdict).

classify(plan, records, expected_calls=None) -> {"ok","drift":[{"class","n","detail"}],"n"} with
classes HOST_DRIFT API_DRIFT MODEL_DRIFT PARAM_DRIFT KEY_DRIFT CTX_DRIFT GAP (design table).
Expected parameters come from model_profile.build_request_fields (never reimplemented here);
tests inject `expected_fn`.
"""
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

SCHEMA = 1
DEFAULT_DIR = Path(os.path.expanduser("~/.ollama-dispatch/conformance"))
RETENTION_DAYS = 14
TOTAL_CAP_BYTES = 50 * 1024 * 1024
DAY_CAP_BYTES = 20 * 1024 * 1024
MAX_RECORD_BYTES = 8192
MAX_FIELDS_BYTES = 4096
API_PATHS = {"openai": ("/v1/chat/completions",), "ollama": ("/api/chat",)}
DRIFT_CLASSES = ("HOST_DRIFT", "API_DRIFT", "MODEL_DRIFT", "PARAM_DRIFT", "KEY_DRIFT", "CTX_DRIFT", "GAP")
# body keys that never go to disk (content / schemas); only their presence or a hash survives
_DROP_KEYS = {"messages", "prompt", "input", "tools", "functions", "system", "model", "stream_options"}
_SECRET_RE = re.compile(r"(^|_)(api_?key|secret|token|password|bearer|authorization)(_|$)", re.I)


# ---------------------------------------------------------------- switches
def _off(v):
    return str(v).strip().lower() in ("0", "off", "false", "no", "disable", "disabled")


def log_enabled(env=None):
    return not _off((os.environ if env is None else env).get("CONFORMANCE_LOG", "1"))


def check_enabled(env=None):
    return not _off((os.environ if env is None else env).get("CONFORMANCE_CHECK", "1"))


# ---------------------------------------------------------------- hashing / redaction
def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def sha8(o):
    return hashlib.sha256(_canon(o).encode()).hexdigest()[:8]


def key_fp8(key):
    """sha256(key)[:8] hex, or None. Same digest lane_plan.key_fp prefixes with 'sha256:'."""
    return hashlib.sha256(str(key).encode()).hexdigest()[:8] if key else None


def norm_fp(fp):
    s = str(fp or "")
    return (s[7:] if s.startswith("sha256:") else s) or None


def norm_url(u):
    s = str(u or "").strip().rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3]
    return s.rstrip("/")


def wire_fields(body):
    """Redacted request-body fields: no messages/prompt/tools/model, no secret-named keys.
    response_format and tools survive only as presence markers / hashes."""
    out = {}
    tools_sha = None
    b = body if isinstance(body, dict) else {}
    for k, v in b.items():
        if k == "tools" and v:
            tools_sha = sha8(v)
            continue
        if k in _DROP_KEYS or _SECRET_RE.search(str(k)):
            continue
        if k == "response_format":
            out[k] = "present"
            continue
        out[k] = _scrub_nested(v)
    if tools_sha:
        out["_tools_sha"] = tools_sha
    return out


def _scrub_nested(v):
    if isinstance(v, dict):
        return {k: _scrub_nested(x) for k, x in v.items() if not _SECRET_RE.search(str(k))}
    if isinstance(v, (list, tuple)):
        return [_scrub_nested(x) for x in v]
    return v


def _scrub_line(line, secrets):
    hit = False
    for s in secrets or ():
        if s and len(str(s)) >= 8 and str(s) in line:
            line = line.replace(str(s), "[REDACTED]")
            hit = True
    return line, hit


# ---------------------------------------------------------------- record building (PURE)
def build_record(*, now, job=None, att=None, plan=None, n=0, role=None, host=None, path=None, api=None,
                 model=None, body=None, key=None, kfp=None, status=None, ms=None, ptok=None, ctok=None,
                 fin=None, rmodel=None, ev=None, ov=None, step=0, full=False):
    """PURE. `plan`: LanePlan (to_dict()/.plan_id/.sha) or its dict. `ov`: names of body fields the
    caller deliberately overrode (excluded from PARAM comparison). `full` adds the fields dict."""
    pd = plan.to_dict() if hasattr(plan, "to_dict") else (plan or {})
    fields = wire_fields(body)
    rec = {"v": SCHEMA, "t": round(float(now), 3), "job": job, "att": att,
           "plan": pd.get("plan_id") if pd else None, "psha": (pd.get("sha") or "")[:8] or None if pd else None,
           "n": n, "role": role, "host": norm_url(host), "path": path, "api": api, "model": model,
           "fsha": sha8(fields), "kfp": norm_fp(kfp) or key_fp8(key), "st": status, "ms": ms,
           "ptok": ptok, "ctok": ctok, "fin": fin, "rmodel": rmodel, "ev": ev}
    if ov:
        rec["ov"] = sorted(set(ov))
    if step:
        rec["step"] = int(step)
    if full:
        rec["fields"] = fields
    return rec


# ---------------------------------------------------------------- writer
def _day(t):
    return _dt.datetime.fromtimestamp(float(t), _dt.timezone.utc).strftime("%Y-%m-%d")


class Recorder:
    """Stateful writer: decides compact vs FULL (first per (plan, fsha, host, model)), enforces the
    size caps, appends one line per request under flock. record() never raises."""

    def __init__(self, directory=None, env=None, secrets=(), clock=time.time):
        self.dir = Path(directory) if directory else DEFAULT_DIR
        self.env = env
        self.secrets = tuple(secrets)
        self.clock = clock
        self._seen = set()
        self.dropped = 0
        self.errors = 0

    def record(self, *, body=None, key=None, **kw):
        """Build + write. Returns True when a line was written. Never raises."""
        try:
            if not log_enabled(self.env):
                return False
            now = kw.pop("now", None) or self.clock()
            plan = kw.get("plan")
            pid = (plan.to_dict() if hasattr(plan, "to_dict") else (plan or {})).get("plan_id") if plan else None
            fields = wire_fields(body)
            sig = (pid, sha8(fields), norm_url(kw.get("host")), kw.get("model"))
            full = sig not in self._seen
            rec = build_record(now=now, body=body, key=key, full=full, **kw)
            if full and len(_canon(rec.get("fields", {}))) > MAX_FIELDS_BYTES:
                rec.pop("fields", None)
                rec["fields_truncated"] = True
            ok = self._write(rec, now, secrets=self.secrets + ((key,) if key else ()))
            if ok and full and "fields" in rec:
                self._seen.add(sig)
            return ok
        except Exception:        # noqa: BLE001 -- logging must never break a request
            self.errors += 1
            return False

    def _write(self, rec, now, secrets=()):
        line = _canon(rec)
        line, hit = _scrub_line(line, secrets)
        if hit:
            rec2 = json.loads(line)
            rec2["redacted"] = True
            line = _canon(rec2)
        if len(line) > MAX_RECORD_BYTES:
            rec.pop("fields", None)
            rec["fields_truncated"] = True
            line = _scrub_line(_canon(rec), secrets)[0]
            if len(line) > MAX_RECORD_BYTES:
                self.dropped += 1
                return False
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{_day(now)}.jsonl"
        fd_lock = os.open(str(self.dir / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd_lock, fcntl.LOCK_EX)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size + len(line) + 1 > DAY_CAP_BYTES:
                self.dropped += 1
                return False
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode())      # ONE write per record
            finally:
                os.close(fd)
        finally:
            try:
                fcntl.flock(fd_lock, fcntl.LOCK_UN)
            finally:
                os.close(fd_lock)
        return True


# ---------------------------------------------------------------- shadow-mode mismatch + summary files
MISMATCH_NAME = "plan-mismatch.jsonl"
SUMMARY_NAME = "summaries.jsonl"
MISMATCH_CAP_BYTES = 2 * 1024 * 1024          # one rotation (.1) -> at most ~4 MB on disk
SUMMARY_CAP_BYTES = 1024 * 1024
MISMATCH_FIELD_MAX = 300
_MISMATCH_SEEN = set()
_MISMATCH_SEEN_MAX = 2000


def _clip(v):
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
    return s if len(s) <= MISMATCH_FIELD_MAX else s[:MISMATCH_FIELD_MAX] + "..."


def _append_capped(name, rec, directory, cap, secrets=()):
    """One O_APPEND write of `rec` to <dir>/<name> under the dir flock; when the file would pass `cap`
    it is first rotated to <name>.1 (one generation). Raises on I/O error (callers wrap)."""
    d = Path(directory) if directory else DEFAULT_DIR
    d.mkdir(parents=True, exist_ok=True)
    line = _scrub_line(_canon(rec), secrets)[0]
    path = d / name
    fd_lock = os.open(str(d / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd_lock, fcntl.LOCK_EX)
        try:
            if path.stat().st_size + len(line) + 1 > cap:
                os.replace(str(path), str(d / (name + ".1")))
        except OSError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    finally:
        try:
            fcntl.flock(fd_lock, fcntl.LOCK_UN)
        finally:
            os.close(fd_lock)
    return True


def write_mismatch(cls, field, plan_v, legacy_v, *, job=None, att=None, where=None, kind="mismatch",
                   directory=None, now=None, secrets=(), env=None):
    """Shadow mode (LANE_PLAN=1, LANE_PLAN_ENFORCE=0): the legacy path still decides; every place the
    plan-derived value differs is appended here, tagged with a drift class (DRIFT_CLASSES, or another
    class such as PLAN_WOULD_EXPIRE). kind: mismatch | drift (reap comparison) | info | would_block.
    Deduped per process on (job, where, class, field, values); size-capped with one rotation; secrets
    scrubbed; values clipped. Honours CONFORMANCE_LOG=0. True when a line was written. NEVER raises."""
    try:
        if not log_enabled(env):
            return False
        sig = (job, where, cls, field, _clip(plan_v), _clip(legacy_v))
        if sig in _MISMATCH_SEEN:
            return False
        if len(_MISMATCH_SEEN) >= _MISMATCH_SEEN_MAX:
            _MISMATCH_SEEN.clear()
        _MISMATCH_SEEN.add(sig)
        t = time.time() if now is None else float(now)
        rec = {"v": SCHEMA, "t": round(t, 3), "kind": kind, "class": cls, "field": field,
               "plan": _clip(plan_v), "legacy": _clip(legacy_v), "job": job, "att": att, "where": where}
        return _append_capped(MISMATCH_NAME, rec, directory, MISMATCH_CAP_BYTES, secrets)
    except Exception:        # noqa: BLE001
        return False


def write_summary(rec, *, directory=None, now=None, env=None):
    """End-of-dispatch row {job, att, calls, recorded}: how many chat calls the worker made, so the reap
    comparison can flag GAP (calls with no records). Capped (one rotation). NEVER raises."""
    try:
        if not log_enabled(env):
            return False
        r = {"v": SCHEMA, "t": round(time.time() if now is None else float(now), 3)}
        r.update(rec or {})
        return _append_capped(SUMMARY_NAME, r, directory, SUMMARY_CAP_BYTES)
    except Exception:        # noqa: BLE001
        return False


def _read_jsonl_pair(name, directory, since=None):
    d = Path(directory) if directory else DEFAULT_DIR
    out = []
    for n in (name + ".1", name):
        try:
            fh = open(d / n, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(r, dict) and (since is None or (r.get("t") or 0) >= since):
                    out.append(r)
    return out


def read_mismatches(directory=None, since=None):
    """Parsed mismatch rows (oldest first), torn lines skipped. Used by the shadow exit-criterion check."""
    return _read_jsonl_pair(MISMATCH_NAME, directory, since)


def read_summaries(directory=None, since=None):
    return _read_jsonl_pair(SUMMARY_NAME, directory, since)


# ---------------------------------------------------------------- retention
def prune(directory=None, now=None, days=RETENTION_DAYS, cap_bytes=TOTAL_CAP_BYTES):
    """Delete day files older than `days` (by filename date, UTC) then oldest-first until the total
    is under `cap_bytes`. Returns the list of removed file names. Never raises, never touches
    non-conformance files."""
    d = Path(directory) if directory else DEFAULT_DIR
    removed = []
    try:
        files = sorted(p for p in d.glob("????-??-??.jsonl"))
    except OSError:
        return removed
    t = time.time() if now is None else float(now)
    cutoff = _day(t - days * 86400)
    for p in list(files):
        if p.stem < cutoff:
            try:
                p.unlink()
                removed.append(p.name)
                files.remove(p)
            except OSError:
                pass
    total = 0
    sizes = {}
    for p in files:
        try:
            sizes[p] = p.stat().st_size
            total += sizes[p]
        except OSError:
            pass
    for p in files:                               # oldest first (sorted by date name)
        if total <= cap_bytes:
            break
        try:
            p.unlink()
            removed.append(p.name)
            total -= sizes.get(p, 0)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- reader
def iter_records(directory=None, since=None, until=None, plan=None, job=None, att=None):
    """Yield parsed records, oldest day first. Torn / non-JSON / non-object lines are skipped.
    Filters: time window (since/until epoch), plan id, job id, attempt id."""
    d = Path(directory) if directory else DEFAULT_DIR
    try:
        files = sorted(d.glob("????-??-??.jsonl"))
    except OSError:
        return
    lo = _day(since) if since is not None else None
    hi = _day(until) if until is not None else None
    for p in files:
        if (lo and p.stem < lo) or (hi and p.stem > hi):
            continue
        try:
            fh = open(p, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                t = r.get("t")
                if since is not None and isinstance(t, (int, float)) and t < since:
                    continue
                if until is not None and isinstance(t, (int, float)) and t > until:
                    continue
                if plan is not None and r.get("plan") != plan:
                    continue
                if job is not None and r.get("job") != job:
                    continue
                if att is not None and r.get("att") != att:
                    continue
                yield r


# ---------------------------------------------------------------- drift classification
def default_expected(plan, role, step=0):
    """Expected wire fields for (plan, role, step) from model_profile.build_request_fields, or None
    when model_profile is unavailable / the model is unknown."""
    try:
        import model_profile as mp
        ov = {"num_ctx": plan.get("num_ctx")} if plan.get("num_ctx") else None
        return mp.build_request_fields(plan["model"], role or (plan.get("profile") or {}).get("role") or "author",
                                       plan["api"], overrides=ov, lane=plan.get("lane"), sampling_step=step or 0)
    except Exception:       # noqa: BLE001
        return None


def resolve_fields(records):
    """Give compact records the `fields` of the full record sharing their fsha (same plan/host/model).
    Returns new list; records that stay field-less carry '_nofields': True."""
    full = {}
    for r in records:
        if "fields" in r:
            full[(r.get("plan"), r.get("host"), r.get("model"), r.get("fsha"))] = r["fields"]
    out = []
    for r in records:
        r = dict(r)
        if "fields" not in r:
            f = full.get((r.get("plan"), r.get("host"), r.get("model"), r.get("fsha")))
            if f is not None:
                r["fields"] = f
            else:
                r["_nofields"] = True
        out.append(r)
    return out


def _subset_diff(expected, actual, ignore=(), path=""):
    """Paths where `actual` lacks or differs from `expected` (extras in actual are fine)."""
    diffs = []
    for k, ev in expected.items():
        p = f"{path}.{k}" if path else k
        if k in ignore or p in ignore:
            continue
        if k not in actual:
            diffs.append(f"{p}: missing (want {ev!r})")
        elif isinstance(ev, dict) and isinstance(actual[k], dict):
            diffs += _subset_diff(ev, actual[k], ignore, p)
        elif actual[k] != ev:
            diffs.append(f"{p}: {actual[k]!r} != {ev!r}")
    return diffs


def classify(plan, records, expected_calls=None, expected_fn=None, env=None):
    """Compare an attempt's records against its plan. `plan`: dict (LanePlan.to_dict()) or object.
    Returns {"ok": bool|None, "drift": [{"class","n","detail"}], "n": N, "skipped": bool}."""
    if not check_enabled(env):
        return {"ok": None, "drift": [], "n": 0, "skipped": True}
    p = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
    recs = resolve_fields(list(records))
    found = {}

    def flag(cls, detail):
        e = found.setdefault(cls, {"class": cls, "n": 0, "detail": detail})
        e["n"] += 1

    phost, papi, pmodel = norm_url(p.get("host")), p.get("api"), p.get("model")
    pauth = p.get("auth") or {}
    pfp = norm_fp(pauth.get("fp"))
    allowed_fps = {pfp} | {norm_fp(r.get("new_fp")) for r in (pauth.get("rotations") or [])}
    is_db = p.get("lane") == "studio-db"
    rotated_seen = False
    exp_fn = expected_fn or default_expected
    exp_cache = {}
    for r in recs:
        if norm_url(r.get("host")) != phost:
            flag("HOST_DRIFT", f"host {r.get('host')!r} != plan {phost!r}")
        path = str(r.get("path") or "")
        if r.get("api") != papi or not any(path.startswith(x) for x in API_PATHS.get(papi, ())):
            flag("API_DRIFT", f"api/path {r.get('api')!r} {path!r} != plan {papi!r}")
        if r.get("model") != pmodel or (r.get("rmodel") is not None and r.get("rmodel") != pmodel):
            flag("MODEL_DRIFT", f"model {r.get('model')!r} rmodel {r.get('rmodel')!r} != plan {pmodel!r}")
        if r.get("ev") == "key_rotated":
            rotated_seen = True
        kfp = norm_fp(r.get("kfp"))
        if is_db:
            if kfp is None or (kfp not in allowed_fps and not rotated_seen):
                flag("KEY_DRIFT", f"kfp {kfp!r} on studio-db, plan {pfp!r}")
        elif kfp is not None:
            flag("KEY_DRIFT", f"key fingerprint {kfp!r} sent on non-Darkbloom lane {p.get('lane')!r}")
        flds = r.get("fields")
        if papi == "ollama" and isinstance(flds, dict):
            nc = (flds.get("options") or {}).get("num_ctx")
            if p.get("num_ctx") is not None and nc != p.get("num_ctx"):
                flag("CTX_DRIFT", f"num_ctx {nc!r} != plan {p.get('num_ctx')!r}")
        if r.get("_nofields"):
            continue                     # compact record whose full twin was lost: counted via GAP below
        step = int(r.get("step") or 0)
        role = r.get("role")
        key = (role, step)
        if key not in exp_cache:
            exp_cache[key] = exp_fn(p, role, step)
        exp = exp_cache[key]
        if exp is not None and isinstance(flds, dict):
            ign = set(r.get("ov") or ())
            if papi == "ollama":
                ign |= {"options.num_ctx"}           # judged as CTX_DRIFT
            for d in _subset_diff(exp, flds, ign):
                flag("PARAM_DRIFT", d)
    n = len(recs)
    if expected_calls is not None and expected_calls > n:
        flag("GAP", f"{expected_calls} worker requests, {n} records")
    if any(r.get("_nofields") for r in recs):
        flag("GAP", "compact record(s) with no full record for their fsha")
    drift = [found[c] for c in DRIFT_CLASSES if c in found]
    return {"ok": not drift, "drift": drift, "n": n, "skipped": False}


def sweep(records, expected_fn=None):
    """Hourly distribution check for requests with NO plan (runners, gate/review, goose chat):
    per (model, role, api) every full record must carry the profile's fields. Returns drift rows."""
    out = []
    exp_fn = expected_fn
    for r in resolve_fields(list(records)):
        if r.get("_nofields") or not isinstance(r.get("fields"), dict):
            continue
        pseudo = {"model": r.get("model"), "api": r.get("api"), "lane": None}
        exp = (exp_fn or default_expected)(pseudo, r.get("role"), int(r.get("step") or 0))
        if exp is None:
            continue
        ign = set(r.get("ov") or ())
        diffs = _subset_diff(exp, r["fields"], ign | ({"options.num_ctx"} if r.get("api") == "ollama" else set()))
        if diffs:
            out.append({"class": "PARAM_DRIFT", "model": r.get("model"), "role": r.get("role"),
                        "t": r.get("t"), "detail": diffs[0]})
    return out
