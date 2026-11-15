"""Plex-automation bypass: request vetting for the forwarder (see queue-api-plex-bypass)."""
import hmac
import json

MAX_BODY = 64 * 1024


def served_model_ids(loaded_path=None):
    import os
    if loaded_path is None:
        loaded_path = os.path.expanduser("~/.darkbloom/loaded-models.json")
    try:
        with open(loaded_path, "r") as f:
            data = json.load(f)
        models = data.get("models", [])
        result = [m for m in models if isinstance(m, str) and m]
        if result:
            return result
    except Exception:
        pass
    try:
        import darkbloom_chat
        return sorted(darkbloom_chat.served_models())
    except Exception:
        return []


def cached_served(ttl=30.0, now=None, loader=None):
    import time
    if now is None:
        now = time.monotonic
    cache_key = "served_cache"
    cache_loader_key = "served_cache_loader"
    cache_result_key = "served_cache_result"
    cache_stamp_key = "served_cache_stamp"
    _cache = getattr(cached_served, "_cache", None)
    if _cache is None:
        _cache = {}
        cached_served._cache = _cache
    current_loader = loader or served_model_ids
    loader_id = id(current_loader)
    stamp = _cache.get(cache_stamp_key)
    result = _cache.get(cache_result_key)
    if stamp is not None and result is not None and result != []:
        elapsed = now() - stamp
        if 0 <= elapsed < ttl:
            return result
    val = current_loader()
    if val:
        _cache[cache_result_key] = val
        _cache[cache_stamp_key] = now()
    return val


def resolve_model(requested, served):
    if isinstance(requested, str):
        lower = requested.lower()
        for s in served:
            if isinstance(s, str) and s.lower() == lower:
                return s
    if served:
        return served[0]
    return None





def read_secret(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def prepare_request(auth_header, raw_body, token, served=None):
    # 1. Auth check
    if not token:
        return (401, b"unauthorized")
    if not auth_header or not auth_header.startswith("Bearer "):
        return (401, b"unauthorized")
    provided = auth_header[7:].strip()
    if not hmac.compare_digest(provided.encode(), token.encode()):
        return (401, b"unauthorized")

    # 2. Size check
    if len(raw_body) > MAX_BODY:
        return (413, b"body too large")

    # 3. Parse
    try:
        d = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return (400, b"bad request")
    if not isinstance(d, dict):
        return (400, b"bad request")
    msgs = d.get("messages")
    if not isinstance(msgs, list) or len(msgs) == 0:
        return (400, b"bad request")
    for m in msgs:
        if not isinstance(m, dict):
            return (400, b"bad request")

    # 4. Set model from served list and return
    sl = (served or cached_served)()
    if not sl:
        return (503, b"no model served")
    d["model"] = resolve_model(d.get("model"), sl)
    return (200, json.dumps(d).encode())
