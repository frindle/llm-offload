"""Configuration and host-routing for the Ollama dispatch pipeline.

Everything here is environment-driven and portable. There is no macOS,
launchd, Keychain, or vault coupling anywhere in this package.

Config lives under a single directory, resolved at use time:

    $OLLAMA_DISPATCH_HOME        (default: ~/.ollama-dispatch)
      hosts.json                 the host table (see hosts.json.example)
      defaults.json              default model + host
      model-ladder.json          fallback model ladder for gate auto-fix

All three files are optional; sensible defaults apply when they are absent.
The host table can also be supplied entirely through env vars, so a container
needs no writable config dir at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def dispatch_home() -> Path:
    """Root config directory, resolved at call time (never cached)."""
    return Path(
        os.environ.get("OLLAMA_DISPATCH_HOME", str(Path.home() / ".ollama-dispatch"))
    ).expanduser()


def hosts_file() -> Path:
    return dispatch_home() / "hosts.json"


def defaults_file() -> Path:
    return dispatch_home() / "defaults.json"


def model_ladder_file() -> Path:
    return dispatch_home() / "model-ladder.json"


# --------------------------------------------------------------------------
# Defaults (env-overridable)
# --------------------------------------------------------------------------

# The out-of-the-box host if no host table exists at all.
DEFAULT_HOST_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

# Default model tag. Any Ollama model that supports tool-calling works.
DEFAULT_MODEL = os.environ.get("OLLAMA_DISPATCH_MODEL", "qwen2.5-coder:14b")

# Name of the "primary" host in the table: the big-budget host that is
# preferred and used as the queue-there fallback. Overflow hosts are only ever
# picked when a model provably fits their configured budget.
BIG_HOST_NAME = os.environ.get("OLLAMA_DISPATCH_PRIMARY_HOST", "primary")

DEFAULT_TEMPERATURE = float(os.environ.get("OLLAMA_DISPATCH_TEMPERATURE", "0.15"))
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_DISPATCH_NUM_CTX", "16384"))

# A seed host table written on first run when no hosts.json exists and no
# host env vars are set. Deliberately a single loopback host so a fresh
# checkout does something sensible with a local `ollama serve`.
SEEDED_HOSTS = {
    "hosts": {
        "primary": {"url": DEFAULT_HOST_URL, "usable_bytes": None},
    }
}


# --------------------------------------------------------------------------
# Host table
# --------------------------------------------------------------------------

def _hosts_from_env() -> dict | None:
    """Build a host table from env vars, for container/CI use with no config dir.

    OLLAMA_DISPATCH_HOSTS may be a JSON object ({"name": {"url": ..,
    "usable_bytes": ..}}), or a comma-separated list of name=url pairs.
    Returns None if unset so the file-based path is used instead.
    """
    raw = os.environ.get("OLLAMA_DISPATCH_HOSTS")
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return _normalize_hosts(obj)
    hosts = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        name, url = pair.split("=", 1)
        hosts[name.strip()] = {"url": url.strip(), "usable_bytes": None}
    return _normalize_hosts({"hosts": hosts}) if hosts else None


def _normalize_hosts(obj: dict) -> dict:
    """Accept either {"hosts": {...}} or a bare {name: spec} mapping and
    normalize each spec to {"url": str, "usable_bytes": int|None}."""
    table = obj.get("hosts", obj) if isinstance(obj, dict) else {}
    out = {}
    for name, spec in table.items():
        if isinstance(spec, str):
            spec = {"url": spec}
        url = spec.get("url")
        if not url:
            continue
        ub = spec.get("usable_bytes")
        out[name] = {"url": url, "usable_bytes": int(ub) if ub else None}
    return out


def load_hosts() -> dict:
    """The active host table, resolved at use time.

    Precedence: OLLAMA_DISPATCH_HOSTS env -> $OLLAMA_DISPATCH_HOME/hosts.json
    -> seeded single-loopback default (written to disk on first run when the
    config dir is writable).
    """
    env_hosts = _hosts_from_env()
    if env_hosts:
        return env_hosts

    path = hosts_file()
    if path.exists():
        try:
            return _normalize_hosts(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass

    seeded = _normalize_hosts(SEEDED_HOSTS)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_hosts(seeded)
    except OSError:
        pass  # read-only config dir (e.g. container) -- use in-memory seed
    return seeded


def save_hosts(hosts: dict) -> None:
    """Persist the host table atomically (write-temp-then-rename)."""
    path = hosts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hosts": hosts}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def host_url(name: str, default: str | None = None) -> str | None:
    spec = load_hosts().get(name)
    return spec["url"] if spec else (default or DEFAULT_HOST_URL)


def host_usable_bytes(name: str):
    spec = load_hosts().get(name)
    return spec.get("usable_bytes") if spec else None


def load_defaults() -> dict:
    path = defaults_file()
    out = {"model": DEFAULT_MODEL, "host": BIG_HOST_NAME}
    if path.exists():
        try:
            out.update(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def load_model_ladder() -> list:
    """Fallback model ladder (cheapest -> most capable) for gate auto-fix.

    Env OLLAMA_DISPATCH_MODEL_LADDER (comma-separated model tags) overrides the
    file. Each file entry is a model string or {"model": .., "host": ..}.
    """
    env = os.environ.get("OLLAMA_DISPATCH_MODEL_LADDER")
    if env:
        return [{"model": m.strip()} for m in env.split(",") if m.strip()]
    path = model_ladder_file()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("ladder", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError):
            pass
    return [{"model": DEFAULT_MODEL}]
