"""queue_config: one place for deployment-specific settings (hosts, URLs, names, paths).

Lookup order for get(key):
  1. environment: the explicit legacy names passed as env=..., then QUEUE_ + key upper-cased
     with dots turned into underscores (key "ollama.studio_url" -> QUEUE_OLLAMA_STUDIO_URL);
  2. the TOML file $QUEUE_CONFIG, else ~/.config/ollama-queue/config.toml (dotted keys walk tables);
  3. the default passed by the caller.

An empty environment variable counts as unset. The file is cached and re-read when its mtime
changes. A missing or unreadable file is not an error (the defaults apply). Stdlib only, Python 3.11+.
"""
import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

_CACHE = {"path": None, "mtime": None, "data": {}}


def config_path():
    p = os.environ.get("QUEUE_CONFIG")
    if p:
        return Path(p).expanduser()
    return Path.home() / ".config" / "ollama-queue" / "config.toml"


def _load():
    path = config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _CACHE.update(path=str(path), mtime=None, data={})
        return _CACHE["data"]
    if _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    data = {}
    if tomllib is not None:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError) as e:  # ValueError covers tomllib.TOMLDecodeError
            print(f"queue_config: cannot read {path}: {e}", file=sys.stderr)
            data = {}
    _CACHE.update(path=str(path), mtime=mtime, data=data)
    return data


def env_name(key):
    return "QUEUE_" + key.upper().replace(".", "_")


def _env_names(key, env):
    if env is None:
        names = []
    elif isinstance(env, str):
        names = [env]
    else:
        names = list(env)
    names.append(env_name(key))
    return names


def get(key, default=None, env=None):
    """Value for a dotted key, or default. env: legacy env var name(s) checked before QUEUE_<KEY>."""
    for n in _env_names(key, env):
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    node = _load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def file_get(key, default=None):
    """Value for a dotted key from the config FILE only (no environment), for settings that must not be
    overridable by whatever was exported last."""
    node = _load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def get_str(key, default="", env=None):
    v = get(key, default, env)
    return default if v is None else str(v)


def get_list(key, default=None, env=None):
    """A list value. A TOML array is returned as is; an env string is split on commas."""
    v = get(key, None, env)
    if v is None:
        return list(default or [])
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def get_int(key, default=0, env=None):
    v = get(key, None, env)
    try:
        return default if v is None else int(v)
    except (TypeError, ValueError):
        return default


def get_bool(key, default=False, env=None):
    v = get(key, None, env)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def standby():
    """True while the container runs in standby ($QUEUE_STANDBY truthy): it holds a copy of the Mac queue's state
    and must not act on it (no daemon, no notification, no write to the Mac, no lease). Unset = the Mac today."""
    return (os.environ.get("QUEUE_STANDBY") or "").strip().lower() in ("1", "true", "yes", "on")


def drain_marker():
    """The file whose presence means "draining": the queue launches nothing new and takes no new jobs while running ones
    finish (fallback to the Mac). Lives in the state dir; the state tarball never carries it."""
    return state_dir() / "queue-drain.json"


def draining():
    """True while drain_marker() exists. Absent (the Mac today, a normal container) = every path as before."""
    try:
        return drain_marker().exists()
    except OSError:
        return False


def dispatch_dir():
    """The queue's second state folder (CPU lane store, event log, escalations, hosts.json): $QUEUE_DISPATCH_DIR,
    else ~/.ollama-dispatch."""
    p = os.environ.get("QUEUE_DISPATCH_DIR")
    return Path(p).expanduser() if p else Path.home() / ".ollama-dispatch"


def state_dir():
    """Where the queue keeps its state JSON, locks and logs: $QUEUE_STATE_DIR, else ~/bin."""
    p = os.environ.get("QUEUE_STATE_DIR")
    if p:
        return Path(p).expanduser()
    return Path.home() / "bin"


def handoff_dir():
    """The handoff panel's folder (pending/complete pages, acted.json): $HANDOFF_DIR, then paths.handoff_dir
    in the config file, else ~/Desktop/GitHub Projects/ollama/ollama-handoff (where the Mac keeps it)."""
    p = get_str("paths.handoff_dir", "", env="HANDOFF_DIR")
    if p:
        return Path(p).expanduser()
    return Path.home() / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff"


def operator_name():
    """Display name of the person the queue escalates to ("needs <name>")."""
    return get_str("operator.name", "the operator", env="QUEUE_OPERATOR_NAME")


def operator_handle():
    """Short lowercase handle used as a data value (default signoff reviewer, keychain account)."""
    return get_str("operator.handle", "operator", env="QUEUE_OPERATOR_HANDLE")


# ---------------------------------------------------------------------------------------------
# Darkbloom endpoint (base URL + API key). Mac: ~/.darkbloom/local.json, exactly as before.
# Container: DARKBLOOM_BASE_URL / DARKBLOOM_API_KEY from the environment. The key is never logged.
# ---------------------------------------------------------------------------------------------
DARKBLOOM_FALLBACK_BASE = "http://127.0.0.1:8000"


def darkbloom_local_json_path():
    p = os.environ.get("DARKBLOOM_LOCAL_JSON")
    return Path(p).expanduser() if p else Path.home() / ".darkbloom" / "local.json"


def darkbloom_local_record():
    """The vendor's local.json as a dict ({} when missing, torn or not an object)."""
    import json
    try:
        rec = json.loads(darkbloom_local_json_path().read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _env_nonempty(name):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def darkbloom_apply_env(rec, v1=False):
    """A local.json-shaped record with DARKBLOOM_BASE_URL / DARKBLOOM_API_KEY laid over it. With neither
    variable set the record comes back unchanged (the same object), which keeps the Mac path as it was.
    v1=True: callers that use base_url as an OpenAI API root (the real local.json carries ".../v1") get "/v1"
    added to an environment base that lacks it."""
    base, key = _env_nonempty("DARKBLOOM_BASE_URL"), _env_nonempty("DARKBLOOM_API_KEY")
    if not base and not key:
        return rec
    out = dict(rec) if isinstance(rec, dict) else {}
    if base:
        base = base.rstrip("/")
        if v1 and not base.endswith("/v1"):
            base += "/v1"
        out["base_url"] = base
    if key:
        out["api_key"] = key
    return out


def darkbloom_default_base():
    """Base used when a record carries a key but no base_url: DARKBLOOM_BASE_URL, then darkbloom.base_url,
    then http://127.0.0.1:8000. No trailing slash."""
    return get_str("darkbloom.base_url", DARKBLOOM_FALLBACK_BASE, env="DARKBLOOM_BASE_URL").rstrip("/")


def darkbloom_endpoint():
    """(base_url, api_key) of the Darkbloom local API. Each part is taken from the first layer that has it:
    environment, then ~/.darkbloom/local.json (base_url / api_key), then config darkbloom.base_url (base
    only), then the default http://127.0.0.1:8000 (key None). Base has no trailing slash."""
    rec = darkbloom_local_record()
    base = _env_nonempty("DARKBLOOM_BASE_URL") or (str(rec.get("base_url") or "").strip() or None)
    key = _env_nonempty("DARKBLOOM_API_KEY")
    if not key and isinstance(rec.get("api_key"), str) and rec["api_key"]:
        key = rec["api_key"]
    if not base:
        base = get_str("darkbloom.base_url", DARKBLOOM_FALLBACK_BASE)
    return base.rstrip("/"), key


def _host_form(url):
    h = str(url or "").strip().rstrip("/")
    if h.endswith("/v1"):
        h = h[:-3]
    return h.rstrip("/")


def darkbloom_lane_hosts():
    """Host strings (no /v1) that mean the Darkbloom lane: the two loopback forms of port 8000 plus whatever
    the endpoint layers name (environment, local.json, config)."""
    hosts = ["http://127.0.0.1:8000", "http://localhost:8000"]
    for h in (_host_form(darkbloom_endpoint()[0]), _host_form(darkbloom_default_base())):
        if h and h not in hosts:
            hosts.append(h)
    return tuple(hosts)


def is_darkbloom_host(url):
    h = _host_form(url)
    return bool(h) and h in darkbloom_lane_hosts()


def queue_api():
    """(base_url, token) for the queue's HTTP API. Base: env QUEUE_API, then QUEUE_API_URL, then config
    queue.api_url, then http://127.0.0.1:7684. Token: env QUEUE_API_TOKEN, else the file
    ~/.config/ollama-queue/queue-api-token when its mode is 0600, else None (loopback peers need none)."""
    base = _env_nonempty("QUEUE_API") or _env_nonempty("QUEUE_API_URL") or get_str("queue.api_url", "http://127.0.0.1:7684")
    token = _env_nonempty("QUEUE_API_TOKEN")
    if not token:
        f = Path.home() / ".config" / "ollama-queue" / "queue-api-token"
        try:
            if (f.stat().st_mode & 0o777) == 0o600:
                token = f.read_text().strip() or None
        except OSError:
            token = None
    return base.rstrip("/"), token


def queue_api_headers():
    """{"Authorization": "Bearer ..."} when a token exists, else {}."""
    t = queue_api()[1]
    return {"Authorization": "Bearer " + t} if t else {}


QUEUE_STATE_NAME = "ollama-queue-state.json"


def _is_loopback_hostname(host):
    h = (host or "").strip().lower().strip("[]")
    return h in ("localhost", "::1") or h.startswith("127.") or h == "::ffff:127.0.0.1"


def in_container():
    """True inside the queue container: $QUEUE_IN_CONTAINER=1, which the image sets. Only the variable counts, not
    Docker's /.dockerenv marker: the test sandbox is itself a container, and there the Mac's loopback studio must
    still count (test-ollama-inventory's pick_host checks)."""
    v = os.environ.get("QUEUE_IN_CONTAINER")
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def studio_url_ok(url):
    """False for a loopback URL (inside the container it names the container itself)."""
    from urllib.parse import urlsplit
    try:
        return not _is_loopback_hostname(urlsplit(url).hostname)
    except ValueError:
        return False


def studio_url(default="http://127.0.0.1:11434"):
    """Base URL of the big-memory Ollama host ("studio"): ollama.studio_url (env OLLAMA_STUDIO_URL) over `default`.
    Outside the container, unchanged. Inside the container a loopback URL means the container itself, never the host
    that was meant, so it is dropped: "" = no studio host (callers treat the host as absent, fail closed)."""
    url = get_str("ollama.studio_url", default, env="OLLAMA_STUDIO_URL").rstrip("/")
    if url and in_container():
        if not studio_url_ok(url):
            return ""
    return url


def queue_is_remote():
    """True when the queue API this process talks to (queue_api()) is on another machine: its host is not loopback.
    On the Mac before the cutover, and inside the queue container, it is loopback, so the state file is the truth."""
    from urllib.parse import urlsplit
    try:
        host = urlsplit(queue_api()[0]).hostname
    except ValueError:
        return False
    return not _is_loopback_hostname(host)


def queue_state_path():
    """The state file the queue on THIS machine writes: state_dir()/ollama-queue-state.json."""
    return state_dir() / QUEUE_STATE_NAME


def load_queue_state(path=None, timeout=5.0):
    """The queue state dict ({"jobs": [...], ...}), or None when it can't be read. Never raises, never logs a token.
    path is the file the caller would read. None, or this machine's default state file (queue_state_path()), means
    "the queue's state": that file while the queue runs here; GET <queue api>/api/state with the bearer token when
    queue_is_remote(), because the copy left on this disk stops changing at the cutover. Any other path (tests,
    explicit overrides) is read as a plain file."""
    import json
    default = queue_state_path()
    is_default = path is None or os.path.realpath(os.path.expanduser(str(path))) == os.path.realpath(str(default))
    if is_default and queue_is_remote():
        import urllib.request
        req = urllib.request.Request(queue_api()[0] + "/api/state", headers=queue_api_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                d = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - down, 401/403, timeout, bad JSON: all mean "no state"
            return None
        return d if isinstance(d, dict) else None
    try:
        d = json.loads(Path(os.path.expanduser(str(path)) if path is not None else default).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def agent_url():
    """Base URL of the Mac status service (queue-agent.py) from env QUEUE_AGENT_URL, else None. Unset = this process
    runs on the Mac itself and reads the vendor files directly (the code path before the container existed)."""
    u = _env_nonempty("QUEUE_AGENT_URL")
    return u.rstrip("/") if u else None


def agent_get(path, timeout=3.0, text=False):
    """GET <QUEUE_AGENT_URL><path> with the bearer token from QUEUE_AGENT_TOKEN. -> parsed JSON (the raw text when
    text=True), or None when the agent is unset, unreachable, answers non-200 or sends garbage. Never raises, never
    logs the token."""
    base = agent_url()
    if not base:
        return None
    import json
    import urllib.request
    req = urllib.request.Request(base + path)
    tok = _env_nonempty("QUEUE_AGENT_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8", "replace")
        return body if text else json.loads(body)
    except Exception:  # noqa: BLE001 - agent down, timeout, bad JSON: all mean "no answer"
        return None


def agent_post(path, body, timeout=5.0):
    """POST JSON `body` to <QUEUE_AGENT_URL><path> with the bearer token from QUEUE_AGENT_TOKEN. -> the parsed JSON
    answer, or None when the agent is unset, unreachable, answers non-200 or sends garbage. Never raises, never logs
    the token. In standby nothing is sent (the Mac keeps its own state; this copy must not touch it)."""
    if standby():
        return None
    base = agent_url()
    if not base:
        return None
    import json
    import urllib.request
    try:
        data = json.dumps(body).encode()
    except (TypeError, ValueError):
        return None
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    tok = _env_nonempty("QUEUE_AGENT_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            d = json.loads(resp.read(1_000_000).decode("utf-8", "replace"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001 - agent down, 4xx/5xx, timeout, bad JSON: all mean "no answer"
        return None


def _main(argv):
    """CLI for shell scripts. Never prints a key. Prints one value per command."""
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "darkbloom-base":
        print(darkbloom_endpoint()[0])
    elif cmd == "queue-api":
        print(queue_api()[0])
    elif cmd == "queue-state":
        # the state JSON wherever the queue runs (shell readers: `python3 queue_config.py queue-state | jq ...`)
        import json
        d = load_queue_state()
        if d is None:
            print("queue state unavailable", file=sys.stderr)
            return 1
        print(json.dumps(d))
    else:
        print("usage: queue_config.py darkbloom-base|queue-api|queue-state", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
