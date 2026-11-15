#!/usr/bin/env python3
"""lane_plan: the SINGLE reader of vendor state, and the immutable resolved-lane PLAN.

Design: machine-config/docs/pipeline-hardening-design.md section 1. WAVE 0: this module is
import-safe and NOT wired into ollama-queue.py / ollama-worker.py / darkbloom_chat.py yet.

Why it exists: the lane, host, API style, model, ctx, key and profile used to be decided piecemeal
by re-reading mutable vendor files (~/.darkbloom/local.json, provider.toml, loaded-models.json,
daemon-state.json) at ~7 sites, each protected by its own sticky cache. A "choose with one read,
use with another" split (C10) is then one provider restart away. Here:

  take_snapshot(prev)  ONE consistent read of every vendor file per daemon tick.
                       Torn / empty / pid-mismatched files -> state "restarting"; inside the
                       sticky window the WHOLE previous snapshot is returned (last-good), exactly
                       the behaviour of the three legacy caches (_DARKBLOOM_URL_GOOD,
                       _DARKBLOOM_SERVED_GOOD, _DARKBLOOM_EXACT_CACHE). A MISSING local.json is
                       "off" and is never masked.
  resolve(...)         PURE given the snapshot: frozen LanePlan. Carries a key REFERENCE
                       (fingerprint + source), never the key.
  open_key_pipe(snap)  the key travels to the worker through an inherited pipe fd
                       (Popen pass_fds), never argv / env / disk / logs.
  refresh_key(plan)    the ONE sanctioned re-read after plan creation (HTTP 401 handling).
  expiry is EVENT based (401 / changed endpoint / model no longer served), never wall-clock.

Kill switches: LANE_PLAN=0 (env) or the file ~/.ollama-dispatch/lane-plan.disabled.
Secrets: the key lives only in a KeyBox (repr/str/pickle/json safe). Logs and plan files carry
sha256 fingerprints ("sha256:1a2b3c4d") only.
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import copy
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from types import MappingProxyType

SCHEMA = 1
DARKBLOOM_LANE = "studio-db"          # alias kept: gate env GATE_REGATE_HOST uses it
BONSAI_LANE = "bonsai"
DEFAULT_MODEL = os.environ.get("DARKBLOOM_DEFAULT_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8")
DEFAULT_BASE = queue_config.get_str("darkbloom.base_url", "http://127.0.0.1:8000", env="DARKBLOOM_BASE_URL").rstrip("/")
DISABLE_FILE = Path(os.path.expanduser("~/.ollama-dispatch/lane-plan.disabled"))
_SECRET_FIELD_RE = re.compile(r"(^|_)(api_?key|secret|token|password|bearer)(_|$)", re.I)


# ---------------------------------------------------------------- kill switch / env parsing
def enabled(env=None, disable_file=None):
    """False when LANE_PLAN=0 (env) or the disable file exists. Evaluated per call."""
    e = os.environ if env is None else env
    if str(e.get("LANE_PLAN", "1")).strip().lower() in ("0", "off", "false", "no", "disable", "disabled"):
        return False
    return not Path(disable_file if disable_file is not None else DISABLE_FILE).exists()


def sticky_s_from_env(v):
    """PURE. Same contract as ollama-queue._sticky_s_from_env: off/false/no/disable -> 0;
    unset/blank/garbage/negative/NaN -> 60."""
    s = str(v if v is not None else "").strip().lower()
    if s in ("off", "false", "no", "disable", "disabled"):
        return 0.0
    try:
        f = float(s)
    except ValueError:
        return 60.0
    return f if f >= 0 and f == f else 60.0


def _sticky(sticky_s):
    return sticky_s_from_env(os.environ.get("DARKBLOOM_STICKY_S")) if sticky_s is None else float(sticky_s)


# ---------------------------------------------------------------- secrets
def key_fp(key):
    """sha256 fingerprint, safe to log (the key is high-entropy). None for no key."""
    if not key:
        return None
    return "sha256:" + hashlib.sha256(str(key).encode()).hexdigest()[:8]


class KeyBox:
    """Holds a secret. repr/str/format/pickle/copy never reveal it; only .reveal() does."""
    __slots__ = ("_k", "fp")

    def __init__(self, key):
        object.__setattr__(self, "_k", str(key or ""))
        object.__setattr__(self, "fp", key_fp(key))

    def __setattr__(self, *_):
        raise AttributeError("KeyBox is immutable")

    def reveal(self):
        return self._k

    def __bool__(self):
        return bool(self._k)

    def __repr__(self):
        return f"<KeyBox {self.fp}>"

    __str__ = __repr__

    def __format__(self, spec):
        return repr(self)

    def __reduce__(self):
        raise TypeError("KeyBox must not be pickled")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return isinstance(other, KeyBox) and self._k == other._k

    def __hash__(self):
        return hash(self.fp)


class _Frozen(dict):
    def _ro(self, *a, **k):
        raise TypeError("frozen")
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _ro

    def __deepcopy__(self, memo):
        return _Frozen({k: copy.deepcopy(v, memo) for k, v in self.items()})


def _freeze(o):
    if isinstance(o, dict):
        return _Frozen({k: _freeze(v) for k, v in o.items()})
    if isinstance(o, (list, tuple)):
        return tuple(_freeze(v) for v in o)
    if isinstance(o, (set, frozenset)):
        return frozenset(o)
    return o


def _thaw(o):
    if isinstance(o, dict):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_thaw(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, KeyBox):
        return None                      # never serialise a key, even by accident
    return o


def _canon(o):
    return json.dumps(_thaw(o), sort_keys=True, separators=(",", ":"), default=str)


def _sha(o, n=64):
    return hashlib.sha256(_canon(o).encode()).hexdigest()[:n]


def scan_for_secret_fields(o, path=""):
    """Names of keys in a structure that look like secret holders (api_key, token, ...).
    A plan/snapshot dict must have none. Returns a list of dotted paths."""
    bad = []
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}" if path else str(k)
            if _SECRET_FIELD_RE.search(str(k)) and k not in ("key_ref",):
                bad.append(p)
            bad += scan_for_secret_fields(v, p)
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            bad += scan_for_secret_fields(v, f"{path}[{i}]")
    return bad


# ---------------------------------------------------------------- sources
@dataclasses.dataclass(frozen=True)
class Sources:
    """Where the vendor files live. Overridable so tests never touch the real ~/.darkbloom."""
    local_json: Path = Path(os.path.expanduser("~/.darkbloom/local.json"))
    daemon_state: Path = Path(os.path.expanduser("~/.darkbloom/daemon-state.json"))
    loaded_models: Path = Path(os.path.expanduser("~/.darkbloom/loaded-models.json"))
    provider_pid: Path = Path(os.path.expanduser("~/.darkbloom/provider.pid"))
    provider_toml: Path = Path(os.path.expanduser("~/.config/darkbloom/provider.toml"))
    hosts_json: Path = Path(os.path.expanduser("~/.ollama-dispatch/hosts.json"))
    bonsai_host: Path = Path(os.path.expanduser("~/.config/ollama-queue/bonsai-host"))
    profiles_yaml: Path = Path(os.path.expanduser("~/bin/model_profiles.yaml"))

    @classmethod
    def under(cls, root):
        """All files under one directory (test sandboxes)."""
        r = Path(root)
        return cls(r / "local.json", r / "daemon-state.json", r / "loaded-models.json",
                   r / "provider.pid", r / "provider.toml", r / "hosts.json",
                   r / "bonsai-host", r / "model_profiles.yaml")

    def paths(self):
        return [self.local_json, self.daemon_state, self.loaded_models, self.provider_pid,
                self.provider_toml, self.hosts_json, self.bonsai_host]


def _read(path):
    """(status, text): status 'ok' | 'missing' | 'error'. Never raises."""
    try:
        return "ok", Path(path).read_text()
    except FileNotFoundError:
        return "missing", ""
    except (OSError, UnicodeDecodeError):
        return "error", ""


def _json_obj(path):
    """(status, dict|None): 'ok' parsed object; 'missing'; 'torn' (exists, empty/unparsable/
    non-object); 'error'."""
    st, txt = _read(path)
    if st != "ok":
        return st, None
    if not txt.strip():
        return "torn", None
    try:
        o = json.loads(txt)
    except ValueError:
        return "torn", None
    return ("ok", o) if isinstance(o, dict) else ("torn", None)


def norm_url(u):
    """Endpoint root with no trailing slash and no /v1 suffix; '' for non-http."""
    s = str(u or "").strip().rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3]
    return s.rstrip("/") if s.startswith("http") else ""


def base_from_record(rec, default=None):
    """PURE. Mirrors darkbloom_chat.base_from_record: base_url when present, else the fixed
    local port when the record carries an api_key (0.9.17 dropped base_url)."""
    rec = rec if isinstance(rec, dict) else {}
    b = norm_url(rec.get("base_url"))
    if not b and rec.get("api_key"):
        b = norm_url(default or DEFAULT_BASE)
    return b or None


_ARRAY_RE = re.compile(r"(?m)^\s*(?:enabled_models|preload_models)\s*=\s*\[([^\]]*)\]")


def served_from_texts(toml_text, loaded_obj):
    """PURE. Exact-cased served ids, de-duplicated in order: provider.toml enabled_models /
    preload_models then loaded-models.json. Same readers as model_profile.served_exact_ids."""
    out = []
    for m in _ARRAY_RE.finditer(toml_text or ""):
        out += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    if isinstance(loaded_obj, dict):
        out += [str(x) for x in (loaded_obj.get("models") or []) if x]
    return list(dict.fromkeys(out))


def canonical_served(model, exact_ids):
    """PURE, no warning. Served casing when `model` matches exactly one served id
    case-insensitively; else unchanged (unknown / ambiguous -> fail loudly at the endpoint)."""
    m = str(model or "")
    ids = [str(x) for x in (exact_ids or ())]
    if not m or m in ids:
        return m
    hits = {x for x in ids if x.lower() == m.lower()}
    return next(iter(hits)) if len(hits) == 1 else m


# ---------------------------------------------------------------- snapshot
@dataclasses.dataclass(frozen=True)
class DarkbloomState:
    state: str                       # up | off | restarting | unknown
    base: object = None              # "http://127.0.0.1:8000" | None
    gen: object = None               # {"pid","start_us","version","local_updated_at"} | None
    key_fp: object = None
    key: object = None               # KeyBox | None   (in memory only)
    served_exact: tuple = ()
    served_lower: frozenset = frozenset()
    advertised: tuple = ()
    ctx_ceiling: object = None
    provider_pid: object = None
    provider_alive: object = None
    gen_verified: bool = False       # local.json pid agreed with daemon-state pid
    stale_s: float = 0.0             # >0 => last-good answer inside the restart window
    good_at: object = None           # `now` of the last real up-read this state derives from
    why: str = ""

    def public(self):
        """Dict for logs / plan files. Never contains the key."""
        d = {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "key"}
        return _thaw(d)


@dataclasses.dataclass(frozen=True)
class ProviderSnapshot:
    taken_at: float
    darkbloom: DarkbloomState
    ollama_hosts: object             # frozen {name: {"url","usable_bytes"}}
    bonsai: object                   # frozen {"url": str|None}
    sources_sha: str
    served_good_at: object = None    # when the served list was last non-empty

    def public(self):
        return {"taken_at": self.taken_at, "darkbloom": self.darkbloom.public(),
                "ollama_hosts": _thaw(self.ollama_hosts), "bonsai": _thaw(self.bonsai),
                "sources_sha": self.sources_sha}

    def __repr__(self):
        return f"<ProviderSnapshot {self.darkbloom.state} base={self.darkbloom.base} " \
               f"fp={self.darkbloom.key_fp} stale={self.darkbloom.stale_s:.0f}s>"


def _sources_sha(src):
    parts = []
    for p in src.paths():
        try:
            s = Path(p).stat()
            parts.append([str(p), s.st_mtime_ns, s.st_size])
        except OSError:
            parts.append([str(p), None, None])
    return _sha(parts, 16)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OSError):
        return None if not isinstance(pid, int) else True


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def take_snapshot(prev=None, now=None, sources=None, sticky_s=None, pid_alive=None):
    """ONE consistent read of every vendor file. Never raises. `now` is a monotonic-ish float
    (injected in tests); `prev` is the previous ProviderSnapshot (last-good source).

    Darkbloom state:
      off          local.json missing, or parsed without api_key/base_url (explicit; NEVER masked)
      restarting   local.json present but empty/torn/non-object, OR its pid disagrees with
                   daemon-state's process_identity.pid (a restart rewrite in flight).
                   Inside `sticky_s` of the last good read the previous Darkbloom state is
                   returned whole (stale_s stamped); outside it, state "restarting" with no base.
      up           parsed, carries base+key, pid consistent (or daemon-state unavailable).
      unknown      local.json unreadable (permission/IO error).
    The served list is sticky independently (legacy _DARKBLOOM_SERVED_GOOD): an EMPTY read within
    the window of a non-empty one answers with the previous list."""
    src = sources or Sources()
    t = time.monotonic() if now is None else float(now)
    win = _sticky(sticky_s)
    alive = pid_alive or _pid_alive
    pdb = prev.darkbloom if prev is not None else None

    # --- served list (own sticky) -------------------------------------------------------
    _, toml_txt = _read(src.provider_toml)
    _, loaded = _json_obj(src.loaded_models)
    served = served_from_texts(toml_txt, loaded)
    served_good_at = t if served else None
    if not served and win > 0 and prev is not None and prev.served_good_at is not None \
            and pdb is not None and pdb.served_exact and t - prev.served_good_at < win:
        served = list(pdb.served_exact)
        served_good_at = prev.served_good_at

    # --- daemon-state / provider pid ----------------------------------------------------
    _, ds = _json_obj(src.daemon_state)
    ds = ds or {}
    pi = ds.get("process_identity") if isinstance(ds.get("process_identity"), dict) else {}
    ds_pid, start_us = _int(pi.get("pid")), _int(pi.get("start_time_micros"))
    st_pp, pp_txt = _read(src.provider_pid)
    try:
        prov_pid = int(pp_txt.strip()) if st_pp == "ok" else None
    except ValueError:
        prov_pid = None
    advertised = tuple(str(x) for x in (ds.get("advertised_models") or []) if x) \
        if isinstance(ds.get("advertised_models"), list) else ()

    # --- local.json ---------------------------------------------------------------------
    st, rec = _json_obj(src.local_json)
    if st == "ok":
        rec = queue_config.darkbloom_apply_env(rec)          # env DARKBLOOM_BASE_URL / DARKBLOOM_API_KEY laid over (no-op unset)
    elif st == "missing" and (os.environ.get("DARKBLOOM_API_KEY") or os.environ.get("DARKBLOOM_BASE_URL")):
        st, rec = "ok", queue_config.darkbloom_apply_env({})   # container: no vendor file, the environment names the endpoint

    def last_good(why):
        if win > 0 and pdb is not None and pdb.state == "up" and pdb.good_at is not None \
                and t - pdb.good_at < win:
            return dataclasses.replace(pdb, stale_s=t - pdb.good_at, why=why,
                                       served_exact=tuple(served) or pdb.served_exact,
                                       served_lower=frozenset(x.lower() for x in served) or pdb.served_lower)
        return DarkbloomState(state="restarting", why=why, served_exact=tuple(served),
                              served_lower=frozenset(x.lower() for x in served), advertised=advertised)

    if st == "missing":
        db = DarkbloomState(state="off", why="local.json missing", served_exact=tuple(served),
                            served_lower=frozenset(x.lower() for x in served), advertised=advertised)
    elif st == "error":
        db = DarkbloomState(state="unknown", why="local.json unreadable", served_exact=tuple(served),
                            served_lower=frozenset(x.lower() for x in served), advertised=advertised)
    elif st == "torn":
        db = last_good("local.json empty/torn (restart rewrite)")
    else:
        base = base_from_record(rec)
        key = rec.get("api_key")
        if not base:
            db = DarkbloomState(state="off", why="local.json has no api_key/base_url",
                                served_exact=tuple(served),
                                served_lower=frozenset(x.lower() for x in served), advertised=advertised)
        else:
            lpid = _int(rec.get("pid"))
            verified = lpid is not None and ds_pid is not None and lpid == ds_pid
            if lpid is not None and ds_pid is not None and lpid != ds_pid:
                db = last_good(f"local.json pid {lpid} != daemon-state pid {ds_pid} (restart in flight)")
            else:
                kb = KeyBox(key) if key else None
                db = DarkbloomState(
                    state="up", base=base,
                    gen={"pid": lpid if lpid is not None else ds_pid, "start_us": start_us,
                         "version": str(rec.get("version") or ds.get("version") or "") or None,
                         "local_updated_at": rec.get("updated_at")},
                    key_fp=kb.fp if kb else None, key=kb,
                    served_exact=tuple(served), served_lower=frozenset(x.lower() for x in served),
                    advertised=advertised, provider_pid=prov_pid,
                    provider_alive=(alive(prov_pid) if prov_pid else None),
                    gen_verified=verified, good_at=t, why="ok")

    # --- other hosts ----------------------------------------------------------------------
    hosts = {}
    _, hj = _json_obj(src.hosts_json)
    for name, h in ((hj or {}).get("hosts") or {}).items():
        if isinstance(h, dict) and norm_url(h.get("url")):
            hosts[str(name)] = {"url": norm_url(h["url"]), "usable_bytes": h.get("usable_bytes")}
    _, btxt = _read(src.bonsai_host)
    bonsai = {"url": norm_url(btxt.strip()) or None}

    return ProviderSnapshot(taken_at=t, darkbloom=db, ollama_hosts=_freeze(hosts),
                            bonsai=_freeze(bonsai), sources_sha=_sources_sha(src),
                            served_good_at=served_good_at)


# ---------------------------------------------------------------- plan
class LaneUnavailable(Exception):
    """The lane cannot be planned right now (caller: INFRA WAIT, not a job failure)."""

    def __init__(self, cause, detail=""):
        super().__init__(f"{cause}: {detail}" if detail else cause)
        self.cause, self.detail = cause, detail


@dataclasses.dataclass(frozen=True)
class LanePlan:
    v: int
    plan_id: str
    lane: str
    host: str
    api: str
    model_requested: str
    model: str
    alias: object
    num_ctx: object
    ctx_source: str
    profile: object
    flags: object
    auth: object
    provider: object
    created_at: float
    resolver_sha: str
    sha: str = ""

    def to_dict(self):
        d = _thaw({f.name: getattr(self, f.name) for f in dataclasses.fields(self)})
        return d

    def public(self):
        return self.to_dict()

    def log_line(self):
        a = self.auth or {}
        return (f"PLAN {self.plan_id} lane={self.lane} host={self.host} api={self.api} "
                f"model={self.model} ctx={self.num_ctx} kfp={a.get('fp')} sha={self.sha[:8]}")

    def __repr__(self):
        return f"<LanePlan {self.log_line()}>"


_RESOLVER_SHA = None


def resolver_sha():
    global _RESOLVER_SHA
    if _RESOLVER_SHA is None:
        try:
            _RESOLVER_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
        except OSError:
            _RESOLVER_SHA = "unknown"
    return _RESOLVER_SHA


def _file_sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _default_profile(model, role, lane_name, api, sources):
    """Profile identity via model_profile.py (the only owner of profile semantics). Returns
    (resolved_model, alias_note, profile_dict|None, ctx|None). Import failure -> no profile."""
    try:
        import sys
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import model_profile as mp
        resolved, _ = mp.resolve_alias(model, "darkbloom" if api == "openai" else None)
        prof = mp.get_profile(resolved, role, lane_name)
        fields = mp.build_request_fields(resolved, role, api, lane=lane_name)
        ident = {"key": prof.get("profile_key"), "mode": prof.get("mode"), "role": role,
                 "yaml_sha": _file_sha(sources.profiles_yaml),
                 "fields_sha": _sha(fields, 16)}
        return resolved, (f"profile:{model}->{resolved}" if resolved != model else None), \
            ident, prof.get("ctx")
    except Exception:       # noqa: BLE001 -- resolve must degrade, never raise on profile trouble
        return model, None, None, None


def resolve(job, lane_url, snap, role="author", sources=None, profile_fn=None, now=None, env=None):
    """PURE given `snap` (+ profile files). Returns a frozen LanePlan, None when the kill switch
    is off, and raises LaneUnavailable when the lane cannot be planned (-> INFRA WAIT).

    job: {"id","attempt_id","model","num_ctx"?}. lane_url: the chooser's pick (host root).
    The Darkbloom-only-model-on-an-Ollama-host mismatch (the C10 shape) is rejected here."""
    if not enabled(env):
        return None
    src = sources or Sources()
    job = job or {}
    host = norm_url(lane_url)
    if not host:
        raise LaneUnavailable("no_lane", "chooser gave no host")
    db = snap.darkbloom
    requested = str(job.get("model") or "")
    is_db = bool(db.base) and host == db.base
    bonsai_url = (snap.bonsai or {}).get("url")
    served = list(db.served_exact)
    only_db = bool(requested) and ":" not in requested and requested.lower() in db.served_lower
    if is_db and db.state not in ("up",):
        raise LaneUnavailable("darkbloom_" + db.state, db.why)
    if is_db and not db.key:
        raise LaneUnavailable("darkbloom_no_key", "local.json has no api_key")
    if not is_db and only_db and db.state == "restarting" and not db.base:
        raise LaneUnavailable("darkbloom_restarting", db.why)       # a restart in flight: wait, not a misroute
    if not is_db and only_db:
        raise LaneUnavailable("model_lane_mismatch",
                              f"{requested!r} is served by Darkbloom only; chooser picked {host}")
    if is_db:
        lane, api = DARKBLOOM_LANE, "openai"
    elif bonsai_url and host == bonsai_url:
        lane, api = BONSAI_LANE, "openai"
    else:
        lane, api = None, "ollama"
        for name, h in snap.ollama_hosts.items():
            if h["url"] == host:
                lane = name
        lane = lane or "ollama:" + host

    pf = profile_fn or _default_profile
    model, alias, profile, pctx = pf(requested, role, lane, api, src)
    if is_db:
        if ":" in str(model) or not model:           # legacy Ollama tag with no profile alias
            alias = alias or f"legacy:{requested}->{DEFAULT_MODEL}"
            model = DEFAULT_MODEL
        model = canonical_served(model, served)
        if served and model not in served and model.lower() not in db.served_lower:
            raise LaneUnavailable("model_not_served", f"{model!r} not in served set")
    num_ctx = job.get("num_ctx")
    ctx_source = "job"
    if not num_ctx:
        num_ctx, ctx_source = (pctx, "profile") if pctx else (db.ctx_ceiling, "fallback") if is_db else (None, "fallback")
    auth = {"kind": "darkbloom-local" if is_db else "none",
            "fp": db.key_fp if is_db else None,
            "delivery": "fd" if is_db else "none",
            "source": "local.json" if is_db else None,
            "gen": _thaw(db.gen) if is_db else None,
            "rotations": []}
    provider = {"vendor": "darkbloom" if is_db else ("llama-server" if lane == BONSAI_LANE else "ollama"),
                "version": (db.gen or {}).get("version") if is_db else None,
                "served_sha": _sha(sorted(served), 16) if is_db else None,
                "served": sorted(served) if is_db else []}
    flags = {"preserve_reasoning": lane == BONSAI_LANE}   # _build_cmd: --preserve-reasoning for the llama-server lane only
    jid = str(job.get("id") or "")
    att = str(job.get("attempt_id") or "")
    body = dict(v=SCHEMA, lane=lane, host=host, api=api, model_requested=requested, model=model,
                alias=alias, num_ctx=num_ctx, ctx_source=ctx_source, profile=profile, flags=flags,
                auth=auth, provider=provider, resolver_sha=resolver_sha())
    sha = _sha(body)                      # excludes plan_id / created_at: determinism
    plan = LanePlan(v=SCHEMA, plan_id=f"{jid}-{att}" if (jid or att) else sha[:16],
                    lane=lane, host=host, api=api, model_requested=requested, model=model,
                    alias=alias, num_ctx=num_ctx, ctx_source=ctx_source, profile=_freeze(profile),
                    flags=_freeze(flags), auth=_freeze(auth), provider=_freeze(provider),
                    created_at=snap.taken_at if now is None else float(now),
                    resolver_sha=resolver_sha(), sha=sha)
    bad = scan_for_secret_fields(plan.to_dict())
    if bad:                               # structural guard: a key-bearing field must not exist
        raise AssertionError(f"plan contains secret-looking fields: {bad}")
    return plan


def write_plan(plan, plans_dir):
    """Atomic 0600 write of the plan JSON (no key). Returns the path."""
    d = Path(plans_dir)
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"{re.sub(r'[^A-Za-z0-9._-]', '_', plan.plan_id)}.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".plan-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(plan.to_dict(), f, sort_keys=True, indent=1)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def load_plan(path):
    """Read a plan file back; verifies the recorded sha. Raises ValueError on mismatch."""
    d = json.loads(Path(path).read_text())
    want = d.get("sha")
    body = {k: d[k] for k in d if k not in ("plan_id", "created_at", "sha")}
    if _sha(body) != want:
        raise ValueError("PLAN_SHA_MISMATCH")
    return d


# ---------------------------------------------------------------- key delivery (fd, never disk)
def open_key_pipe(snap, env=None):
    """Create an inherited pipe holding the Darkbloom key. Returns read fd (caller passes it in
    Popen(pass_fds=(fd,)) with argv `--key-fd N`, then os.close()s it in the parent), or None when
    disabled / no key. The key is <64KiB so the write never blocks; the write end is closed here."""
    if not enabled(env):
        return None
    kb = snap.darkbloom.key
    if not kb:
        return None
    r, w = os.pipe()
    try:
        os.write(w, kb.reveal().encode())
    finally:
        os.close(w)
    return r


def read_key_fd(fd):
    """Worker side: read the key once from the inherited fd and close it. Returns KeyBox."""
    chunks = []
    with os.fdopen(fd, "rb", closefd=True) as f:
        chunks.append(f.read())
    return KeyBox(b"".join(chunks).decode().strip())


# ---------------------------------------------------------------- event-based expiry / rotation
def expire_reason(plan, snap):
    """PURE. Why `plan` is no longer valid against a FRESH snapshot, or None. There is NO
    wall-clock term: an old healthy plan stays valid. Transient restart (state restarting) is
    NOT expiry -- the caller keeps waiting."""
    db = snap.darkbloom
    if plan.lane != DARKBLOOM_LANE:
        return None
    if db.state == "off":
        return "darkbloom_off"
    if db.state == "restarting":
        return None
    if db.state == "up" and db.base != plan.host:
        return f"endpoint_changed {plan.host}->{db.base}"
    if db.state == "up" and db.served_exact and plan.model not in db.served_exact:
        return f"model_not_served {plan.model}"
    return None


@dataclasses.dataclass
class RefreshResult:
    status: str                    # ok | unchanged | expired | disabled
    fp: object = None
    why: str = ""
    key: object = None             # KeyBox on ok
    rotation: object = None        # {"at","old_fp","new_fp","gen"} on ok (fingerprints only)


def refresh_key(plan, now=None, sources=None, wait_s=None, sleep=time.sleep, clock=time.monotonic, env=None):
    """The ONE sanctioned re-read after plan creation (HTTP 401 handling).
    ok        same base and a new key fp or new gen -> swap the key in memory + one uncharged retry.
    unchanged key/gen identical for the whole wait -> a bad key, not a rotation (normal retry accounting).
    expired   different base / off / plan.model no longer served (-> paused exit, fresh plan on relaunch).
    A restart window (state restarting) is waited out up to wait_s (DARKBLOOM_KEY_ROTATE_WAIT_S, 8)."""
    if not enabled(env):
        return RefreshResult("disabled", why="LANE_PLAN=0")
    if wait_s is None:
        try:
            wait_s = float(os.environ.get("DARKBLOOM_KEY_ROTATE_WAIT_S", "8"))
        except ValueError:
            wait_s = 8.0
    t0 = clock()
    old_fp = (plan.auth or {}).get("fp")
    old_gen = (plan.auth or {}).get("gen") or {}
    while True:
        snap = take_snapshot(None, now=None if now is None else now, sources=sources, sticky_s=0)
        why = expire_reason(plan, snap)
        if why:
            return RefreshResult("expired", why=why)
        db = snap.darkbloom
        if db.state == "up" and db.key:
            gen_changed = (db.gen or {}).get("pid") != old_gen.get("pid") or \
                (db.gen or {}).get("start_us") != old_gen.get("start_us") or \
                (db.gen or {}).get("version") != old_gen.get("version")
            if db.key_fp != old_fp or gen_changed:
                rot = {"at": snap.taken_at, "old_fp": old_fp, "new_fp": db.key_fp, "gen": _thaw(db.gen)}
                return RefreshResult("ok", fp=db.key_fp, key=db.key, rotation=rot)
        if clock() - t0 >= wait_s:
            return RefreshResult("unchanged", fp=db.key_fp, why="key and gen unchanged after wait")
        sleep(0.5)


# ---------------------------------------------------------------- self check
if __name__ == "__main__":
    import sys
    s = take_snapshot()
    print(s)
    print(json.dumps(s.public(), indent=1, default=str))
    sys.exit(0)
