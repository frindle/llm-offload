"""Single-shot chat against Darkbloom's local OpenAI endpoint, for the queue's
non-worker RUNNERS (code-review-agent.py, studio-research.py), whose own Model
classes speak Ollama's /api/chat. They call `chat()` here when `is_darkbloom(host)`;
everything else about them is untouched.

Facts this encodes (measured live 2026-10-01, qwen3.6-35b-a3b-vl-mtp-mxfp8):
  * Auth is a Bearer key from ~/.darkbloom/local.json, re-read per call (Darkbloom
    rewrites it on every provider restart). It is never logged or put in argv.
  * response_format json_schema IS honored (enum respected).
  * Thinking arrives INLINE in `content` (no separate reasoning field), and it eats
    the token budget -- the same failure the runners already document for Ollama.
    `chat_template_kwargs.enable_thinking=false` turns it off (0.4s vs 4s).
  * Legacy Ollama tags (qwen3.8:27b-q4_K_M, ...) are aliased to the Darkbloom
    default, mirroring ollama-queue.py's _darkbloom_model().
"""
import copy
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import model_profile as _mp   # model cards -> request fields (model_profiles.yaml)
import queue_config as _qc    # Darkbloom endpoint layers: env, local.json, config

LOCAL_JSON = os.path.expanduser("~/.darkbloom/local.json")
DEFAULT_MODEL = os.environ.get("DARKBLOOM_DEFAULT_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8")


def _sticky_s_from_env(v):
    """PURE. Same contract as ollama-queue._sticky_s_from_env / lane_plan.sticky_s_from_env:
    off/false/no/disable -> 0 (kill switch); unset/blank/garbage/negative -> 60."""
    s = str(v if v is not None else "").strip().lower()
    if s in ("off", "false", "no", "disable", "disabled"):
        return 0.0
    try:
        f = float(s)
    except ValueError:
        return 60.0
    return f if f >= 0 and f == f else 60.0


# F1 (pipeline-hardening-design.md): Darkbloom rewrites local.json on every provider restart,
# so it is briefly EMPTY / torn. Every read site that chose a protocol, base URL or Bearer
# key from one fresh read (code-review-agent.py picks /api/chat vs /v1 per call; DEFAULT_HOST
# fell back to the RETIRED Studio Ollama localhost:11434) split mid-restart -- the C10 class.
# A transient read (file EXISTS but is empty / unparsable / not an object) now answers with
# the last GOOD record for DARKBLOOM_STICKY_S seconds. A MISSING file, or a parsed record
# without api_key/base_url, is an explicit "off" and is never masked. Kill switch:
# DARKBLOOM_STICKY_S=0 (the pre-fix behaviour, exactly).
_GOOD = [None, 0.0]          # [last good record (dict), monotonic time]
_GOOD_SERVED = [frozenset(), 0.0]


def _now():
    return time.monotonic()


def _sticky_s():
    return _sticky_s_from_env(os.environ.get("DARKBLOOM_STICKY_S"))


def _raw_record():
    """(record, transient). transient=True only when the file exists but is empty/torn."""
    try:
        txt = open(LOCAL_JSON).read()
    except OSError:
        return _qc.darkbloom_apply_env({}), False
    try:
        rec = json.loads(txt)
    except ValueError:
        return _qc.darkbloom_apply_env({}), True
    if not isinstance(rec, dict):
        return _qc.darkbloom_apply_env({}), True
    return _qc.darkbloom_apply_env(rec), False


def _record():
    rec, transient = _raw_record()
    if not transient:
        if rec.get("api_key") or rec.get("base_url"):
            _GOOD[0], _GOOD[1] = dict(rec), _now()
        return rec
    s = _sticky_s()
    if s > 0 and _GOOD[0] and _now() - _GOOD[1] < s:
        return dict(_GOOD[0])
    return _qc.darkbloom_apply_env({})


def record_transient():
    """True when local.json exists but is empty/torn right now (a restart rewrite in flight)."""
    return _raw_record()[1]


# Darkbloom 0.9.17 (auto-updated 2026-10-04) rewrites local.json with ONLY
# `api_key` -- no `base_url`. Every consumer then decided "Darkbloom is off":
# the queue routed Darkbloom-only jobs to Unraid (27de99a9d14c, 1977f33a6919,
# 759f2ebc2ea1, edf9e823be47) and the worker sent no Bearer key (the 401s). A
# record that carries an api_key IS the local endpoint being on; its address is
# the fixed local port (DARKBLOOM_BASE_URL overrides).
DEFAULT_BASE = _qc.darkbloom_default_base()


def base_from_record(rec, default=None):
    """PURE. Endpoint root (no /v1) from a local.json record: its base_url when
    present, else `default` (DEFAULT_BASE) when it carries an api_key, else None."""
    rec = rec if isinstance(rec, dict) else {}
    b = str(rec.get("base_url") or "").strip().rstrip("/")
    if not b and rec.get("api_key"):
        b = (default or DEFAULT_BASE).rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b if b.startswith("http") else None


def base_url():
    return base_from_record(_record())


def api_key():
    """The Darkbloom key from the sticky-cached record (survives a mid-restart empty rewrite), or None.
    For standalone tools that used to open the vendor file themselves (W2 item 6)."""
    k = _record().get("api_key")
    return k if isinstance(k, str) and k else None


PROVIDER_TOML = os.path.expanduser("~/.config/darkbloom/provider.toml")
LOADED_MODELS_JSON = os.path.expanduser("~/.darkbloom/loaded-models.json")


def served_models(toml_text=None, loaded=None):
    """Lower-cased model ids Darkbloom serves: provider.toml enabled_models /
    preload_models plus loaded-models.json. Args inject text/list for tests."""
    import re as _re
    toml_text_given, loaded_given = toml_text is not None, loaded is not None
    if toml_text is None:
        try:
            toml_text = open(PROVIDER_TOML).read()
        except OSError:
            toml_text = ""
    if loaded is None:
        try:
            loaded = json.load(open(LOADED_MODELS_JSON)).get("models") or []
        except (OSError, ValueError, AttributeError):
            loaded = []
    out = set()
    for m in _re.finditer(r"(?m)^\s*(?:enabled_models|preload_models)\s*=\s*\[([^\]]*)\]",
                          toml_text or ""):
        out |= {x.lower() for x in _re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))}
    out |= {str(x).lower() for x in loaded if x}
    if toml_text_given or loaded_given:
        return out
    if out:
        _GOOD_SERVED[0], _GOOD_SERVED[1] = frozenset(out), _now()
    elif _sticky_s() > 0 and _GOOD_SERVED[0] and _now() - _GOOD_SERVED[1] < _sticky_s():
        return set(_GOOD_SERVED[0])
    return out


def is_darkbloom_only_model(model, served=None):
    """True for a bare model id (no Ollama ':tag') that Darkbloom serves. Such a
    model does not exist on any Ollama host, so it must never be routed or
    `pull`ed there."""
    m = str(model or "").strip()
    if not m or ":" in m:
        return False
    return m.lower() in (served_models() if served is None else served)


def is_darkbloom(host):
    h = str(host).rstrip("/")
    if h.endswith("/v1"):
        h = h[:-3]
    b = base_url()
    if b:
        return h == b
    # No usable record. While the file is merely mid-rewrite and there is no last-good value
    # (a fresh process started inside the restart window), the fixed local endpoint still IS
    # Darkbloom. A missing file / explicit off record is never masked.
    return bool(h) and _sticky_s() > 0 and record_transient() and h == DEFAULT_BASE


LEGACY_OLLAMA_HOSTS = ("http://localhost:11434", "http://127.0.0.1:11434")


def default_host():
    """Default --host for the runners: the Darkbloom endpoint; DEFAULT_BASE while local.json is
    mid-rewrite; the retired Studio Ollama ONLY when Darkbloom is not configured at all."""
    b = base_url()
    if b:
        return b
    if _sticky_s() > 0 and record_transient():
        return DEFAULT_BASE
    return LEGACY_OLLAMA_HOSTS[0]


def legacy_ollama_misroute(host):
    """Reason string when `host` is the retired local Ollama while Darkbloom IS the configured
    backend (local.json present, even if empty), else None. Callers on the Ollama /api/chat
    path raise on a non-None reason instead of silently sending to the wrong protocol.
    Kill switch DARKBLOOM_REVIEW_GUARD=0."""
    if os.environ.get("DARKBLOOM_REVIEW_GUARD", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    h = str(host or "").rstrip("/")
    if h not in LEGACY_OLLAMA_HOSTS:
        return None
    if os.path.exists(LOCAL_JSON) or _GOOD[0]:
        return (f"darkbloom_misroute: {h} is the retired Studio Ollama but Darkbloom is the configured "
                f"backend ({LOCAL_JSON} present, record unreadable/unkeyed); refusing to send to the "
                f"Ollama /api/chat protocol")
    return None


def alias_model(model):
    """Legacy Ollama-style tag (contains ':') -> the Darkbloom default; a bare id
    passes through unchanged so a typo fails loudly at the endpoint."""
    # Profile-declared alias first (explicit + logged once); then the legacy
    # catch-all for any other Ollama-style tag, which is now ALSO logged, never silent.
    resolved, _ = _mp.resolve_alias(model, "darkbloom")
    if resolved != model:
        return resolved
    if not model or ":" in model:
        _mp._warn(("dbc-alias", str(model)),
                  f"model {model!r} is not a Darkbloom id and has no profile alias; "
                  f"falling back to DEFAULT_MODEL {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    # Served casing, same helper + kill switch (DARKBLOOM_CASE_CANON=0) as ollama-queue.py
    # _darkbloom_model: 'qwen3.5-9b' must reach the case-sensitive endpoint as 'Qwen3.5-9B'.
    if os.environ.get("DARKBLOOM_CASE_CANON", "1").strip() != "0":
        return _mp.canonical_served_id(
            model, _mp.served_exact_ids(PROVIDER_TOML, LOADED_MODELS_JSON))
    return model


# Per-process accounting so a runner can report throughput (tok/s) for the whole run.
STATS = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0,
         "schema_422": 0, "schema_dropped": 0, "schema_rungs": {}}


def _relax_schema(node):
    """Copy of a JSON schema with the constraints a post-hoc validator is likeliest to
    trip on removed (size bounds). Never adds constraints: closing objects would make
    a post-hoc validator stricter, the opposite of relaxing."""
    if isinstance(node, dict):
        out = {k: _relax_schema(v) for k, v in node.items()
               if k not in ("maxItems", "minItems", "maxLength", "minLength")}
        return out
    if isinstance(node, list):
        return [_relax_schema(v) for v in node]
    return node


def stats():
    """Totals across every chat() call in this process; tok_s = completion tokens / wall seconds."""
    out = copy.deepcopy(STATS)
    out["seconds"] = round(out["seconds"], 2)
    out["tok_s"] = round(out["completion_tokens"] / out["seconds"], 1) if out["seconds"] > 0 else None
    return out


_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.S)


def _first_violation(v, sch, path):
    """Minimal stdlib JSON-Schema check (type/enum/required/properties/items/bounds/
    additionalProperties); returns the first violation as 'msg at path' or None."""
    t = sch.get("type")
    tmap = {"object": dict, "array": list, "string": str, "boolean": bool,
            "integer": int, "number": (int, float), "null": type(None)}
    if t in tmap and not (isinstance(v, tmap[t]) and not (t in ("integer", "number") and isinstance(v, bool))):
        return f"expected {t}, got {type(v).__name__} at {path or '/'}"
    if "enum" in sch and v not in sch["enum"]:
        return f"{v!r} not in enum {sch['enum']} at {path or '/'}"
    if isinstance(v, str):
        if "maxLength" in sch and len(v) > sch["maxLength"]:
            return f"string longer than {sch['maxLength']} at {path or '/'}"
        if "minLength" in sch and len(v) < sch["minLength"]:
            return f"string shorter than {sch['minLength']} at {path or '/'}"
    if isinstance(v, list):
        if "maxItems" in sch and len(v) > sch["maxItems"]:
            return f"{len(v)} items > maxItems {sch['maxItems']} at {path or '/'}"
        if "minItems" in sch and len(v) < sch["minItems"]:
            return f"{len(v)} items < minItems {sch['minItems']} at {path or '/'}"
        for i, it in enumerate(v):
            if isinstance(sch.get("items"), dict):
                e = _first_violation(it, sch["items"], f"{path}/{i}")
                if e:
                    return e
    if isinstance(v, dict):
        for k in sch.get("required", []):
            if k not in v:
                return f"missing required '{k}' at {path or '/'}"
        props = sch.get("properties") or {}
        if sch.get("additionalProperties") is False:
            extra = [k for k in v if k not in props]
            if extra:
                return f"additional property {extra[0]!r} not allowed at {path or '/'}"
        for k, sub in props.items():
            if k in v and isinstance(sub, dict):
                e = _first_violation(v[k], sub, f"{path}/{k}")
                if e:
                    return e
    return None


# A request for a hosted-but-cold model while every Darkbloom model slot holds an
# in-flight request is REFUSED ("N model slot(s) are active; cannot load '<id>'",
# 0.9.17 provider strings), not queued. That is a capacity wait, not a failure:
# back off (doubling from SLOT_BUSY_STEP_S, <=60s) for up to SLOT_BUSY_WAIT_S
# cumulative per request without spending the 3 transient-retry attempts.
# Mirrors ollama-worker.py is_darkbloom_slot_busy / _slot_busy_retry (2026-10-03).
SLOT_BUSY_WAIT_S = float(os.environ.get("SLOT_BUSY_WAIT_S", "1800"))
SLOT_BUSY_STEP_S = float(os.environ.get("SLOT_BUSY_STEP_S", "5"))
_SLOT_BUSY_RE = re.compile(
    r"model slot\(s\) are (?:active|occupied)|cannot load '|"
    r"try again when a request finishes|"
    r"slot became unavailable for eviction|"
    # HTTP 429 when a cold model cannot be placed while other models hold the
    # provider's memory (live 2026-10-03, f77f4c8def05/e8ff2cfcba0f: qwen3.6-35b
    # evicted, Gemma jobs running; provider log "Inference failure: capacity").
    # Same capacity wait, same SLOT_BUSY_WAIT_S bound.
    r"provider capacity is temporarily unavailable", re.I)


def is_slot_busy(text):
    return bool(_SLOT_BUSY_RE.search(str(text or "")))


def _post(base, wire, timeout, sleep=None, urlopen=None):
    """One chat request. Transient failures (429/5xx: shared slots at the cap, a provider
    restart) are re-sent after a short wait with the key re-read each time; a slot-busy
    refusal of a cold model load is waited out (bounded, uncharged); anything else,
    including a 422, propagates to the caller's schema ladder with its body intact."""
    sleep = sleep or time.sleep
    urlopen = urlopen or urllib.request.urlopen
    attempt = 0
    slot_wait, slot_tries = 0.0, 0
    while True:
        req = urllib.request.Request(
            base + "/v1/chat/completions", data=json.dumps(wire).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(_record().get("api_key") or "")})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read() or b""
            except Exception:
                body = b""
            if is_slot_busy(body.decode("utf-8", "replace")) and slot_wait < SLOT_BUSY_WAIT_S:
                step = min(SLOT_BUSY_STEP_S * (2 ** slot_tries), 60.0,
                           max(1.0, SLOT_BUSY_WAIT_S - slot_wait))
                slot_tries += 1
                slot_wait += step
                sleep(step)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                attempt += 1
                sleep(5 * attempt)
                continue
            raise urllib.error.HTTPError(e.url, e.code, e.msg, e.hdrs,
                                         io.BytesIO(body)) from None


# Darkbloom (MLX) reads `repetition_penalty` and otherwise uses the model's
# generation_config, which for qwen3.6-35b-a3b-vl-mtp-mxfp8 sets NO penalty -- so every
# runner call decoded at temperature 0 with nothing pushing it out of a loop (the
# Ollama path these runners came from always had Ollama's default 1.1). Live
# 2026-10-04: regate-47d71a149da5 / regate-0813211c0643 crashed "no iterations" after
# both schema rungs 422'd and the unconstrained output was a runaway JSON string
# (bc15112d643a's recorded violation: "Unterminated string ... char 340",
# finish_reason=length after ~16k tokens). The local-validation retry also gets a
# stronger penalty, since a same-context resend reproduces the same runaway.
# (Superseded 2026-10-08: the card's repetition_penalty=1.0 + presence_penalty now come
# from model_profiles.yaml; only the same-context RETRY still raises it.)
RETRY_REPETITION_PENALTY = 1.2


def chat(host, model, system, user, schema=None, temperature=None, max_tokens=None,
         think=None, timeout=900, role="review", schema_start=None, reasoning_effort=None):
    """Returns (content, finish_reason). Raises RuntimeError on HTTP/transport
    failure (callers already treat that as a failed call).
    Sampling/max_tokens/thinking come from the model card profile for `role`
    (default "review" = non-thinking); temperature/max_tokens/think are explicit
    overrides and apply only when not None. think=True switches to the author
    (thinking_coding) profile.
    schema_start ("relaxed" or "none") starts the schema ladder below at that rung
    instead of "strict"; reasoning_effort ("low"/"medium"/"high") is sent as-is
    (Darkbloom 0.10.11: Gemma 4 thinks when sent reasoning_effort). Both default
    to None, which leaves the request exactly as before (review test, 2026-11-09)."""
    base = base_url()
    if not base:
        raise RuntimeError("darkbloom endpoint not configured (~/.darkbloom/local.json)")
    model = alias_model(model)
    if think:
        role = "author"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
    }
    body.update(_mp.build_request_fields(
        model, role, "openai",
        overrides={"temperature": temperature, "max_tokens": max_tokens},
        think=(True if think else None)))
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    max_tokens = body.get("max_tokens")
    # Darkbloom answers 422 "Internal inference failure" for some inputs when
    # response_format json_schema is set (ok-le1: 3/3 repeatable at temp 0; the same
    # request WITHOUT the schema returns 200). Its binary shows no grammar-constrained
    # decoding: it prompts for JSON then validates the output server-side. PROVEN from
    # the 0.9.14 binary: the 422 body is "Generated output did not satisfy response_format".
    # Walk a ladder from strictest to none and record which rung finally worked, so a
    # dropped schema is never silent: STATS["schema_rungs"] counts the rung per call,
    # STATS["schema_dropped"] counts calls that ended with no schema at all.
    rungs = [None] if schema is None else [
        ("strict", schema, True),
        ("relaxed", _relax_schema(schema), False),
        ("none", None, None),
    ]
    if schema is not None and schema_start in ("relaxed", "none"):
        rungs = rungs[[r[0] for r in rungs].index(schema_start):]
    data, t0, used = None, time.time(), None
    for i, rung in enumerate(rungs):
        if rung is not None:
            name, sch, strict = rung
            body.pop("response_format", None)
            if sch is not None:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "out", "schema": sch,
                                                           "strict": strict}}
        if rung is not None and rung[0] in ("relaxed", "none") and "_json_contract" not in body:
            # No server-side schema left: say what shape is required in the prompt
            # itself, or the model answers in prose and the caller reads "no findings".
            body["_json_contract"] = True
            body["messages"] = [dict(body["messages"][0], content=body["messages"][0]["content"]
                                     + "\n\nRespond with ONLY a single JSON object matching this JSON Schema, "
                                     "no prose, no code fences:\n" + json.dumps(schema))] + body["messages"][1:]
        if rung is not None and rung[0] == "relaxed":
            # Darkbloom validates the GENERATED output ("Generated output did not
            # satisfy response_format", in the 0.9.14 binary), so identical-schema
            # retries are no-ops. The likeliest way output fails its own schema is
            # truncation: give the relaxed rung twice the token budget.
            body["max_tokens"] = int(max_tokens or 0) * 2
        t0 = time.time()
        wire = {k: v for k, v in body.items() if k != "_json_contract"}
        try:
            data = _post(base, wire, timeout)
            if rung is not None:
                used = name
                STATS["schema_rungs"][name] = STATS["schema_rungs"].get(name, 0) + 1
                if name == "none":
                    STATS["schema_dropped"] += 1
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:200]
            if e.code == 422 and rung is not None and i < len(rungs) - 1:
                import sys
                print(f"[darkbloom_chat] 422 on schema rung {rung[0]!r}: {msg}",
                      file=sys.stderr, flush=True)
                STATS["schema_422"] += 1
                continue
            raise RuntimeError(f"darkbloom HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise RuntimeError(f"darkbloom unreachable: {e}") from None
    STATS["calls"] += 1
    STATS["seconds"] += time.time() - t0
    _u = data.get("usage") or {}
    STATS["prompt_tokens"] += int(_u.get("prompt_tokens") or 0)
    STATS["completion_tokens"] += int(_u.get("completion_tokens") or 0)
    choice = (data.get("choices") or [{}])[0]
    content = _THINK_RE.sub("", (choice.get("message") or {}).get("content") or "")
    if schema is not None and used in ("relaxed", "none"):
        # DIAGNOSTIC: the server 422'd the schema rung(s) above, so find out WHY by checking
        # the unconstrained output against the schema ourselves and recording the first
        # violation (the 422 body only says "did not satisfy response_format").
        STATS.setdefault("schema_violations", [])
        if len(STATS["schema_violations"]) < 5:
            m0 = re.search(r"\{.*\}", content, re.S)
            err = None
            if not m0:
                err = "no JSON object in output: " + content[:120]
            else:
                try:
                    err = _first_violation(json.loads(m0.group(0)), schema, "")
                except ValueError as ve:
                    err = "invalid JSON: " + str(ve)[:100]
            STATS["schema_violations"].append(
                {"rung": used, "schema_keys": sorted((schema.get("properties") or {}).keys())[:8],
                 "violation": err or "none -- output validates locally",
                 "finish_reason": choice.get("finish_reason"), "out_head": content[:100]})
    if schema is not None and used == "none":
        # Unconstrained output: VALIDATE it locally against the caller's schema (the
        # server no longer checks anything at this rung). Evidence (diag bdf48a469bb1,
        # 2026-10-01): Darkbloom 422'd strict+relaxed for output that validates locally,
        # so its post-hoc check is the unreliable part -- but a bare "is it a dict" check
        # here let genuinely schema-violating output through as if it were valid.
        # One retry at temperature>0 (a temp-0 resend is the identical sample) before
        # raising, so a single bad sample is not a failed call.
        err = _local_schema_error(content, schema)
        if err:
            STATS["schema_local_retry"] = STATS.get("schema_local_retry", 0) + 1
            wire = {k: v for k, v in body.items() if k != "_json_contract"}
            wire["temperature"] = max(float(body.get("temperature") or 0.0), RETRY_TEMPERATURE)
            wire["repetition_penalty"] = max(float(body.get("repetition_penalty") or 1.0),
                                             RETRY_REPETITION_PENALTY)
            wire["repeat_penalty"] = wire["repetition_penalty"]
            try:
                data = _post(base, wire, timeout)
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"darkbloom HTTP {e.code}: "
                                   f"{e.read().decode(errors='replace')[:200]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise RuntimeError(f"darkbloom unreachable: {e}") from None
            STATS["calls"] += 1
            _u = data.get("usage") or {}
            STATS["prompt_tokens"] += int(_u.get("prompt_tokens") or 0)
            STATS["completion_tokens"] += int(_u.get("completion_tokens") or 0)
            choice = (data.get("choices") or [{}])[0]
            content = _THINK_RE.sub("", (choice.get("message") or {}).get("content") or "")
            err2 = _local_schema_error(content, schema)
            if len(STATS.setdefault("schema_violations", [])) < 5:
                STATS["schema_violations"].append(
                    {"rung": "none-retry", "schema_keys": sorted((schema.get("properties") or {}).keys())[:8],
                     "violation": err2 or "none -- output validates locally",
                     "first_violation": err,
                     "finish_reason": choice.get("finish_reason"), "out_head": content[:100]})
            if err2:
                raise RuntimeError("darkbloom: schema rungs exhausted and output fails the schema "
                                   f"locally ({err2}) after one temperature>0 retry: " + content[:120])
    elif schema is not None and used == "relaxed":
        # Unconstrained-ish output: refuse to hand back prose a caller would misread.
        m = re.search(r"\{.*\}", content, re.S)
        try:
            ok = bool(m) and isinstance(json.loads(m.group(0)), dict)
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError("darkbloom: schema rungs exhausted and output is not JSON: " + content[:120])
    return content, choice.get("finish_reason")


RETRY_TEMPERATURE = 0.4


def _local_schema_error(content, schema):
    """First local schema violation of the JSON object in `content`, or None when it
    validates. A missing/unparseable object is itself a violation."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return "no JSON object in output"
    try:
        v = json.loads(m.group(0))
    except ValueError as ve:
        return "invalid JSON: " + str(ve)[:100]
    if not isinstance(v, dict):
        return "top-level JSON is not an object"
    return _first_violation(v, schema, "")


def _self_test():
    """Offline: fake the HTTP layer and drive chat() through the 422 ladder."""
    import io
    fails = []

    def check(name, got, want):
        ok = got == want
        print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f" (got {got!r}, want {want!r})"))
        if not ok:
            fails.append(name)

    sch = {"type": "object", "required": ["findings"], "additionalProperties": False,
           "properties": {"findings": {"type": "array", "items": {
               "type": "object", "required": ["quote"], "properties": {"quote": {"type": "string"}}}}}}
    good = '{"findings": [{"quote": "x"}]}'
    bad = '{"findings": [{"nope": 1}]}'

    def run(script):
        """script: list of ('422'|content) answered in order; returns (result|exc, temps)."""
        seq, temps = list(script), []

        def fake_post(_base, wire, _timeout):
            temps.append((wire.get("temperature"), "response_format" in wire,
                          wire.get("repetition_penalty")))
            item = seq.pop(0)
            if item == "422":
                raise urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b'{"error":"Internal inference failure."}'))
            return {"choices": [{"message": {"content": item}, "finish_reason": "stop"}], "usage": {}}
        global _post, base_url
        o_post, o_base = _post, base_url
        _post, base_url = fake_post, (lambda: "http://fake")
        for k in list(STATS):
            STATS[k] = {} if k == "schema_rungs" else (0 if isinstance(STATS[k], (int, float)) else STATS[k])
        STATS.pop("schema_violations", None)
        try:
            return chat("http://fake", DEFAULT_MODEL, "sys", "user", schema=sch), temps
        except RuntimeError as e:
            return e, temps
        finally:
            _post, base_url = o_post, o_base

    # strict succeeds: one call, no retry
    r, t = run([good])
    check("strict rung ok -> 1 call", len(t), 1)
    # 422,422 then valid at none: accepted without a retry
    r, t = run(["422", "422", good])
    check("none rung valid output accepted", isinstance(r, tuple) and r[0] == good, True)
    check("none rung valid -> no extra retry call", len(t), 3)
    # 422,422 then INVALID at none, valid on retry: retried once at temperature>0
    r, t = run(["422", "422", bad, good])
    check("none rung invalid -> retried and accepted", isinstance(r, tuple) and r[0] == good, True)
    check("retry is the 4th call", len(t), 4)
    check("retry temperature > 0", (t[-1][0] or 0) > 0, True)
    check("retry carries no response_format", t[-1][1], False)
    check("every call carries the profile repetition_penalty (Darkbloom's only penalty key)",
          all(x[2] == 1.0 for x in t[:-1]), True)  # card value (profile)
    check("retry raises repetition_penalty", (t[-1][2] or 0) > 1.0, True)
    check("schema_local_retry counted", STATS.get("schema_local_retry"), 1)
    check("violation recorded for the retry",
          (STATS.get("schema_violations") or [{}])[-1].get("first_violation", "").startswith("missing required"), True)
    # invalid twice: raises (never hands back schema-violating JSON)
    r, t = run(["422", "422", bad, bad])
    check("invalid twice -> RuntimeError", isinstance(r, RuntimeError), True)
    check("error names the local violation", "fails the schema locally" in str(r), True)
    print("SELF_TEST_OK" if not fails else f"SELF_TEST_FAILED: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys as _sys
    if "--self-test" in _sys.argv:
        _sys.exit(_self_test())
