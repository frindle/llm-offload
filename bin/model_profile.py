#!/usr/bin/env python3
"""model_profile -- loader for model_profiles.yaml, the single source of truth for
what we send to a model (sampling, max_tokens, ctx, thinking control, tool-call
format, stop words). Every value in the YAML is transcribed from a vendored model
card under references/models/ and cites it (`source:`).

Public API
  get_profile(model, role="author", lane=None) -> dict   flat, resolved profile
  build_request_fields(model, role, api, overrides=None, think=None, lane=None)
        -> dict   exactly what to merge into the request body:
        api="openai": top-level temperature/top_p/top_k/min_p/presence_penalty/
                      repetition_penalty(+repeat_penalty alias)/max_tokens/stop and
                      chat_template_kwargs{enable_thinking,preserve_thinking}, and (ONLY when the caller passes
                      reasoning_parser AND the profile declares it for a darkbloom backend) reasoning_parser
        api="ollama": {"options": {...num_ctx,num_predict,repeat_penalty...},
                       "think": bool}   (think only when the profile/override sets it)
  resolve_alias(model, lane=None) -> (model, aliased_from_or_None)   logged once
  check_profiles(path=None) -> [problems]   drift/citation check (used by canary)
  python3 model_profile.py --show MODEL [ROLE] | --check | --rehash

`overrides` (explicit CLI/caller values) replace profile values only when not None.
Unknown models get the YAML `fallback` profile with a LOUD warning (once per model).
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES_PATH = Path(os.environ.get("MODEL_PROFILES_PATH") or HERE / "model_profiles.yaml")

_CACHE = {}
_WARNED = set()

SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty",
                 "repetition_penalty")
MODE_KEYS = SAMPLING_KEYS + ("max_tokens", "ctx", "enable_thinking",
                             "preserve_thinking", "preserve_reasoning", "stop")


def _warn(key, msg):
    """Log a warning once per process per key, to stderr (never silent)."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"[model_profile] WARNING: {msg}", file=sys.stderr, flush=True)


def load_profiles(path=None, refresh=False):
    p = Path(path) if path else PROFILES_PATH
    key = str(p)
    if not refresh and key in _CACHE:
        return _CACHE[key]
    try:
        import yaml
    except ImportError as e:  # loud: the profile is the only source of defaults
        raise RuntimeError(f"model_profile: PyYAML missing in {sys.executable} ({e}); "
                           f"cannot read {p}") from None
    with open(p) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "models" not in data:
        raise RuntimeError(f"model_profile: {p} has no 'models' table")
    _CACHE[key] = data
    return data


def cards_root(data=None):
    data = data or load_profiles()
    return Path(os.environ.get("MODEL_CARDS_ROOT")
                or os.path.expanduser(str(data.get("cards_root") or "")))


def _find_entry(data, model):
    m = str(model or "").strip()
    models = data["models"]
    if m in models:
        return m, models[m]
    ml = m.lower()
    for k, v in models.items():
        if k.lower() == ml:
            return k, v
        if ml in [str(n).lower() for n in (v.get("names") or [])]:
            return k, v
    return None, None


def resolve_alias(model, lane=None, data=None):
    """Explicit, logged alias resolution. Returns (resolved_model, aliased_from).
    Aliases apply only on the lanes they declare (`lanes:`); with no lane given a lane-scoped alias does NOT apply."""
    data = data or load_profiles()
    a = (data.get("aliases") or {}).get(str(model or ""))
    if not a:
        return model, None
    lanes = a.get("lanes")
    if lanes and lane not in lanes:   # lane-scoped alias: never applies without a lane
        return model, None
    _warn(("alias", model, a["to"]),
          f"ALIAS {model} -> {a['to']} on lane {lane or 'any'}: {a.get('reason', '')} "
          f"[{a.get('source', 'no source')}]")
    return a["to"], model


def served_exact_ids(toml_path, loaded_path):
    """The Darkbloom model ids in their EXACT served casing: provider.toml enabled_models /
    preload_models plus loaded-models.json. [] when unreadable (never raises). The queue and
    darkbloom_chat both read through this so they cannot drift (2026-10-13 review item 3)."""
    import json as _json
    import re as _re
    out = []
    try:
        with open(toml_path) as f:
            txt = f.read()
        for m in _re.finditer(r"(?m)^\s*(?:enabled_models|preload_models)\s*=\s*\[([^\]]*)\]", txt):
            out += [x for x in _re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))]
    except OSError:
        pass
    try:
        with open(loaded_path) as f:
            out += [str(x) for x in (_json.load(f).get("models") or []) if x]
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(out))


def canonical_served_id(model, exact_ids):
    """PURE (+ one logged warning). Darkbloom's /v1/models readiness probe and its chat endpoint
    want the SERVED casing ('Qwen3.5-9B'), while routing treats ids case-insensitively
    (_is_darkbloom_only_model), so a job naming 'qwen3.5-9b' routed to Darkbloom and then sat in
    INFRA WAIT forever. Returns the served spelling when `model` matches exactly one served id
    case-insensitively; otherwise `model` unchanged (unknown id / ambiguous: fail loudly at the
    endpoint, never guess). The rewrite is logged once per process -- never a silent alias."""
    m = str(model or "")
    ids = [str(x) for x in (exact_ids or ())]
    if not m or m in ids:
        return m
    hits = {x for x in ids if x.lower() == m.lower()}
    if len(hits) != 1:
        return m
    hit = next(iter(hits))
    _warn(("casing", m, hit),
          f"CASING {m!r} -> {hit!r}: Darkbloom serves the id as {hit!r} and its /v1/models probe "
          f"and chat endpoint are case-sensitive (provider.toml/loaded-models.json)")
    return hit


def _role_mode(data, role):
    roles = data.get("roles") or {}
    r = str(role or "author")
    if r in roles:
        return roles[r]
    _warn(("role", r), f"unknown role {r!r}; treating as 'author' (thinking_coding)")
    return roles.get("author", "thinking_coding")


def get_profile(model, role="author", lane=None, data=None):
    """Flat resolved profile for (model, role). Never raises for an unknown model:
    returns the fallback profile with fallback=True and a loud warning."""
    data = data or load_profiles()
    resolved, aliased_from = resolve_alias(model, lane, data)
    key, entry = _find_entry(data, resolved)
    mode = _role_mode(data, role)
    if entry is None:
        _warn(("fallback", str(resolved)),
              f"NO PROFILE for model {resolved!r} -- using FALLBACK legacy defaults "
              f"(no model card). Add an entry to {PROFILES_PATH} citing its card.")
        entry, key = data["fallback"], "<fallback>"
    modes = entry.get("modes") or {}
    vals = dict(modes.get(mode) or modes.get("thinking_coding") or {})
    # DISPATCH_SAMPLING=legacy (2026-10-10): A/B control arm + rollback. Replaces the card-derived
    # sampling/budget/thinking values with the YAML `fallback` (pre-2026-10-08 hardcoded worker
    # defaults: temp 0.15, rep 1.1, max_tokens 8192, ctx 16384, no top_p/top_k, no thinking control).
    # Unset/anything else = card profile (the shipped default).
    if os.environ.get("DISPATCH_SAMPLING", "").strip().lower() == "legacy":
        vals = dict((data["fallback"].get("modes") or {}).get(mode) or
                    (data["fallback"].get("modes") or {}).get("thinking_coding") or {})
        _warn(("legacy", str(key)), f"DISPATCH_SAMPLING=legacy: using LEGACY sampling for {key}:{mode}")
    out = {k: vals.get(k) for k in MODE_KEYS}
    out["stop"] = list(vals.get("stop") or [])
    # Per-role thinking policy (2026-10-09): a mode may carry `thinking: off|low|on` (or a bool).
    # off/on pin enable_thinking; low keeps the mode's enable_thinking (reasoning_effort is NOT
    # visibly honored by Darkbloom) and is only surfaced as out["thinking"]. Absent = unchanged.
    _th = vals.get("thinking")
    _tn = str(_th).strip().lower() if _th is not None else None
    if _tn in ("off", "false", "no", "none", "0"):
        out["enable_thinking"], out["thinking"] = False, "off"
    elif _tn in ("on", "true", "yes", "1"):
        out["enable_thinking"], out["thinking"] = True, "on"
    elif _tn in ("low", "medium", "high"):
        out["thinking"] = _tn
    else:
        out["thinking"] = None
    out.update({
        "model": resolved, "requested_model": model, "aliased_from": aliased_from,
        "profile_key": key, "role": role, "mode": mode,
        "tool_call_format": entry.get("tool_call_format", "native"),
        "card": entry.get("card"), "source": entry.get("source"),
        "fallback": bool(entry.get("fallback")),
    })
    # Phase 3 robustness knobs (worker_robust.py): global `robust_defaults` <- per-model `robust`.
    rb = dict(data.get("robust_defaults") or {})
    for k, v in (entry.get("robust") or {}).items():
        if isinstance(v, dict) and isinstance(rb.get(k), dict):
            rb[k] = {**rb[k], **v}
        else:
            rb[k] = v
    out["robust"] = rb
    return out


def reasoning_parser_for(model, role="author", requested=None, lane=None, data=None):
    """The `reasoning_parser` request value to send, or None. `requested` is the lever's value (worker_levers
    REASONING_PARSER); it is honoured ONLY when the model's profile declares that same parser for a darkbloom backend
    (`reasoning_parser:` in model_profiles.yaml, citing the card), so a model/profile the card+probe do not support
    never gets the field, whatever the lever says."""
    if not requested:
        return None
    data = data or load_profiles()
    resolved, _ = resolve_alias(model, lane, data)
    _k, entry = _find_entry(data, resolved)
    rp = (entry or {}).get("reasoning_parser")
    if not isinstance(rp, dict) or str(rp.get("name") or "") != str(requested):
        return None
    if "darkbloom" not in (rp.get("backends") or []) or (entry or {}).get("backend") != "darkbloom":
        return None
    return str(rp["name"])


def build_request_fields(model, role, api, overrides=None, think=None, lane=None,
                         turn_kind=None, arm=None, sampling_step=0, seed=None, sampling_arm=None,
                         reasoning_parser=None):
    """What to put in the request body for `api` ("openai" | "ollama").
    `overrides`: {temperature, top_p, top_k, min_p, presence_penalty,
    repetition_penalty, max_tokens, num_ctx, stop}; None values are ignored.
    `think`: tri-state CLI override of enable_thinking (None = profile)."""
    p = get_profile(model, role, lane)
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    v = {k: ov.get(k, p[k]) for k in SAMPLING_KEYS}
    max_tokens = ov.get("max_tokens", p["max_tokens"])
    stop = ov.get("stop", p["stop"])
    ctx = ov.get("num_ctx", ov.get("ctx", p["ctx"]))
    thinking = think if think is not None else p["enable_thinking"]
    if think is None and turn_kind is not None:
        _pol = thinking_policy(role, turn_kind, arm)      # None = keep the profile's value
        if _pol is not None:
            thinking = _pol
    # SAMPLING ESCALATION LADDER (A/B arm, default OFF -> the block is skipped and the body is
    # byte-identical to before). One declared step for ONE turn; see sampling_step_overrides().
    _seed = None
    if sampling_step:
        _so = sampling_step_overrides(role, sampling_step, arm=arm, sampling_arm=sampling_arm)
        if _so:
            for _k in ("temperature", "repetition_penalty", "presence_penalty"):
                if _k in _so:
                    v[_k] = max(float(v[_k]) if v.get(_k) is not None else float("-inf"), _so[_k])
            if _so.get("thinking") is False and think is None and thinking is not False:
                thinking = False
            if _so.get("fresh_seed") and seed is not None:
                _seed = int(seed)
    if api == "openai":
        f = {k: val for k, val in v.items() if val is not None}
        if "repetition_penalty" in f:
            # llama-server reads repeat_penalty, Darkbloom/MLX reads repetition_penalty;
            # each ignores the other's name.
            f["repeat_penalty"] = f["repetition_penalty"]
        if max_tokens is not None:
            f["max_tokens"] = max_tokens
        if stop:
            f["stop"] = list(stop)
        if _seed is not None:
            f["seed"] = _seed
        ctk = {}
        if thinking is not None:
            ctk["enable_thinking"] = bool(thinking)
        if p["preserve_thinking"] is not None and thinking:
            ctk["preserve_thinking"] = bool(p["preserve_thinking"])
        if ctk:
            f["chat_template_kwargs"] = ctk
        _rp = reasoning_parser_for(model, role, reasoning_parser, lane)   # default None: field absent, body unchanged
        if _rp and thinking is not False:    # a thinking-off turn has no reasoning to split: body stays byte-identical to arm A
            f["reasoning_parser"] = _rp
        return f
    if api == "ollama":
        opts = {}
        for k, val in v.items():
            if val is None:
                continue
            opts["repeat_penalty" if k == "repetition_penalty" else k] = val
        if ctx is not None:
            opts["num_ctx"] = ctx
        if max_tokens is not None:
            opts["num_predict"] = max_tokens
        if stop:
            opts["stop"] = list(stop)
        if _seed is not None:
            opts["seed"] = _seed
        f = {"options": opts}
        if thinking is not None:
            f["think"] = bool(thinking)
        return f
    raise ValueError(f"build_request_fields: unknown api {api!r}")


def role_wants_reasoning_kept(model, role, lane=None):
    return bool(get_profile(model, role, lane)["preserve_reasoning"])


# ---------------------------------------------------------------- thinking policy / escalation
# THINKING_POLICY_MARK (2026-10-09). Settings live in model_profiles.yaml, never in code.
#   thinking_policy.roles[role]   -> "profile" (use the role's mode enable_thinking; the default,
#                                    i.e. NO behaviour change) or the name of an arm
#   thinking_policy.arms[arm]     -> {turn_kind: on|off|profile}, turn kinds TURN_KINDS
#   thinking_policy.ab_roles      -> roles the experiment arm (arg or env MODEL_THINKING_ARM)
#                                    may override; every other role keeps its pinned policy
#   escalation.roles[role]        -> {models: [...], claude: bool}: the ordered models a bigger-model
#                                    step may use (any Darkbloom-hostable model with a profile)
# Known probe facts (2026-10-09, Darkbloom): Qwen thinking arrives INSIDE content; chat_template_kwargs.
# enable_thinking=false is honored; reasoning_effort is NOT visibly honored (never sent); tool_choice
# required/named gives 400.
TURN_KINDS = ("oneshot", "tool_first", "tool_after_failure", "tool_mechanical")
_ONOFF = {"on": True, "true": True, "off": False, "false": False}


def thinking_policy(role, turn_kind, arm=None, data=None):
    """True / False = force enable_thinking for this (role, turn kind); None = defer to the
    role's profile mode. `arm` (or env MODEL_THINKING_ARM) overrides only roles listed in
    thinking_policy.ab_roles."""
    data = data or load_profiles()
    tp = data.get("thinking_policy") or {}
    arms = tp.get("arms") or {}
    pol = (tp.get("roles") or {}).get(str(role or "author"), "profile")
    arm = arm or os.environ.get("MODEL_THINKING_ARM") or None
    if arm and str(role or "author") in (tp.get("ab_roles") or []):
        pol = arm
    if pol in (None, "profile"):
        return None
    table = arms.get(pol)
    if not isinstance(table, dict):
        _warn(("arm", pol), f"thinking_policy: unknown arm {pol!r} for role {role!r}; using the profile")
        return None
    v = str(table.get(turn_kind, "profile")).strip().lower()
    return _ONOFF.get(v)


# SAMPLING_ESCALATION_MARK (2026-10-09). A declared A/B ARM, default OFF. Settings live in
# model_profiles.yaml `sampling_escalation:`; the arm is `arm:` there (or env MODEL_SAMPLING_ARM /
# the sampling_arm argument): "off" (default) or "ladder". With the arm off build_request_fields
# ignores sampling_step entirely. The ladder is the worker's one-turn recovery after a runaway /
# cut-off turn (ollama-worker.py `_ladder`); it is NOT validated on Qwen3.6 -- see
# docs/sampling-escalation-ab.md for the evidence grading and the A/B plan.
SAMPLING_ARMS = ("off", "ladder")
_STEP_KEYS = ("temperature_min", "repetition_penalty_min", "presence_penalty", "thinking",
              "requires_thinking_arm", "fresh_seed")


def sampling_arm_of(arm=None, data=None):
    """The effective sampling arm: explicit arg > env MODEL_SAMPLING_ARM > yaml `arm:` > "off"."""
    data = data or load_profiles()
    cfg = data.get("sampling_escalation") or {}
    ya = cfg.get("arm")
    ya = "off" if ya is False else ya  # bare `off` in YAML 1.1 loads as False
    a = arm or os.environ.get("MODEL_SAMPLING_ARM") or ya or "off"
    return str(a).strip().lower() if str(a).strip().lower() in SAMPLING_ARMS else "off"


def sampling_step_overrides(role, step, arm=None, sampling_arm=None, data=None):  # noqa: arm = THINKING arm
    """{temperature, repetition_penalty, presence_penalty, thinking(False), fresh_seed} for ladder
    `step` (1, 2), or {} when the arm is off / the role is not in `roles` / the step is undeclared.
    `thinking: off` only applies when the effective THINKING arm equals the step's
    `requires_thinking_arm` (hybrid): the A/B for the thinking policy owns that dimension."""
    data = data or load_profiles()
    if sampling_arm_of(sampling_arm, data) != "ladder":
        return {}
    cfg = data.get("sampling_escalation") or {}
    if str(role or "author") not in (cfg.get("roles") or []):
        return {}
    st = ((cfg.get("ladder") or {}).get(int(step)) or (cfg.get("ladder") or {}).get(str(step)))
    if not isinstance(st, dict):
        return {}
    out = {}
    if st.get("temperature_min") is not None:
        out["temperature"] = float(st["temperature_min"])
    if st.get("repetition_penalty_min") is not None:
        out["repetition_penalty"] = float(st["repetition_penalty_min"])
    if st.get("presence_penalty") is not None:
        out["presence_penalty"] = float(st["presence_penalty"])
    if st.get("fresh_seed"):
        out["fresh_seed"] = True
    if str(st.get("thinking", "")).strip().lower() in ("off", "false"):
        need = st.get("requires_thinking_arm")
        have = arm or os.environ.get("MODEL_THINKING_ARM") or None
        if not need or need == have:
            out["thinking"] = False
    return out


def sampling_ladder_enabled(role, data=None, arm=None):
    """True when the ladder arm is on AND `role` may use it (the worker keys its state machine on this)."""
    data = data or load_profiles()
    if sampling_arm_of(arm, data) != "ladder":
        return False
    return str(role or "author") in ((data.get("sampling_escalation") or {}).get("roles") or [])


def check_sampling(data):
    """Problems in the sampling_escalation section (used by check_profiles). The shipped default
    arm must be OFF."""
    out = []
    cfg = data.get("sampling_escalation")
    if cfg is None:
        return out
    if cfg.get("arm", "off") not in ("off", False):
        out.append("sampling_escalation.arm must be 'off' in the shipped profile (A/B arm only)")
    for r in cfg.get("roles") or []:
        if r not in (data.get("roles") or {}):
            out.append(f"sampling_escalation.roles: unknown role {r!r}")
    arms = ((data.get("thinking_policy") or {}).get("arms") or {})
    for stp, st in (cfg.get("ladder") or {}).items():
        if str(stp) not in ("1", "2"):
            out.append(f"sampling_escalation.ladder: unknown step {stp!r} (steps 1 and 2; step 3 is the named exit)")
        for k, val in (st or {}).items():
            if k not in _STEP_KEYS:
                out.append(f"sampling_escalation.ladder.{stp}: unknown key {k!r}")
        for k, lo, hi in (("temperature_min", 0.0, 2.0), ("repetition_penalty_min", 1.0, 2.0),
                          ("presence_penalty", 0.0, 2.0)):
            if (st or {}).get(k) is not None and not (lo <= float(st[k]) <= hi):
                out.append(f"sampling_escalation.ladder.{stp}.{k}={st[k]} outside [{lo}, {hi}]")
        rta = (st or {}).get("requires_thinking_arm")
        if rta and rta not in arms:
            out.append(f"sampling_escalation.ladder.{stp}.requires_thinking_arm: unknown arm {rta!r}")
    if not str(cfg.get("evidence") or "").strip():
        out.append("sampling_escalation: missing evidence: (cite what each step rests on and its grade)")
    return out


def escalation_models(role, data=None):
    """Ordered list of model names a bigger-model/escalation step may use for `role` (from the
    YAML `escalation:` section; [] when none is configured)."""
    data = data or load_profiles()
    e = ((data.get("escalation") or {}).get("roles") or {}).get(str(role or "author")) or {}
    return [str(m) for m in (e.get("models") or []) if m]


def escalation_allows_claude(role, data=None):
    data = data or load_profiles()
    e = ((data.get("escalation") or {}).get("roles") or {}).get(str(role or "author")) or {}
    return bool(e.get("claude"))


def check_policy(data):
    """Problems in thinking_policy / escalation (used by check_profiles)."""
    out = []
    tp = data.get("thinking_policy") or {}
    arms = tp.get("arms") or {}
    for arm, table in arms.items():
        for k, v in (table or {}).items():
            if k not in TURN_KINDS:
                out.append(f"thinking_policy.arms.{arm}: unknown turn kind {k!r}")
            if str(v).strip().lower() not in ("on", "off", "true", "false", "profile"):
                out.append(f"thinking_policy.arms.{arm}.{k}: bad value {v!r}")
    for role, pol in (tp.get("roles") or {}).items():
        if pol not in ("profile", None) and pol not in arms:
            out.append(f"thinking_policy.roles.{role}: unknown arm {pol!r}")
    for ab in tp.get("ab_roles") or []:
        if ab not in (data.get("roles") or {}):
            out.append(f"thinking_policy.ab_roles: unknown role {ab!r}")
    for role, e in ((data.get("escalation") or {}).get("roles") or {}).items():
        if role not in (data.get("roles") or {}):
            out.append(f"escalation.roles.{role}: unknown role")
        for m in (e or {}).get("models") or []:
            if _find_entry(data, m)[1] is None:
                out.append(f"escalation.roles.{role}: model {m!r} has no profile entry (add one citing its card)")
    return out


# ---------------------------------------------------------------- drift/citations
def card_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_KEY_RX = {"temperature": r"temperature", "top_p": r"top_?p", "top_k": r"top_?k",
           "min_p": r"min_?p", "presence_penalty": r"presence_?penalty",
           "repetition_penalty": r"repetition_?penalty"}


def card_supports(card_text, mode):
    """True when some window of 4 consecutive card lines states this mode's
    temperature, top_p, top_k (always) and presence_penalty / repetition_penalty
    (when non-default), each as KEY=VALUE / KEY: VALUE, compared numerically. This is
    what ties the YAML numbers to the card text, so editing a number without the card
    backing it fails the drift check."""
    import re
    lines = card_text.splitlines()
    need = {"temperature": mode.get("temperature"), "top_p": mode.get("top_p"),
            "top_k": mode.get("top_k")}
    if mode.get("presence_penalty") not in (None, 0, 0.0):
        need["presence_penalty"] = mode["presence_penalty"]
    if mode.get("repetition_penalty") not in (None, 1, 1.0):
        need["repetition_penalty"] = mode["repetition_penalty"]
    need = {k: v for k, v in need.items() if v is not None}
    for i in range(len(lines)):
        w = " ".join(lines[i:i + 4]).lower()
        ok = True
        for k, v in need.items():
            found = [float(x) for x in re.findall(_KEY_RX[k] + r"\W{0,4}(-?\d+(?:\.\d+)?)", w)]
            if not any(abs(f - float(v)) < 1e-9 for f in found):
                ok = False
                break
        if ok:
            return True
    return False


def check_profiles(path=None):
    """List of problems (empty = OK): every model has a card file that exists, a
    `source:` citation, card_sha256 matching the card on disk, a mode for every role,
    max_tokens >= 32768 for thinking_coding, and every alias target exists."""
    problems = []
    data = load_profiles(path, refresh=True)
    root = cards_root(data)
    for name, e in data["models"].items():
        if not str(e.get("source") or "").strip():
            problems.append(f"{name}: missing source: citation")
        card = e.get("card")
        if not card:
            problems.append(f"{name}: missing card:")
            continue
        cp = root / card
        if not cp.is_file():
            problems.append(f"{name}: cited card does not exist: {cp}")
        else:
            want = str(e.get("card_sha256") or "")
            got = card_sha256(cp)
            if want != got:
                problems.append(f"{name}: card changed since profile was written "
                                f"(card_sha256 {want[:12] or 'missing'} != {got[:12]}); "
                                f"re-review {card} then run model_profile.py --rehash")
        card_text = cp.read_text(errors="replace") if cp.is_file() else ""
        for mode in set((data.get("roles") or {}).values()):
            m = (e.get("modes") or {}).get(mode)
            if not isinstance(m, dict):
                problems.append(f"{name}: missing mode {mode}")
                continue
            miss = [k for k in MODE_KEYS if k not in m]
            if miss:
                problems.append(f"{name}.{mode}: missing keys {miss}")
            if card_text and not card_supports(card_text, m):
                problems.append(f"{name}.{mode}: sampling values are not stated together in "
                                f"{card} (temperature/top_p/top_k/penalties do not match the card)")
        tc = (e.get("modes") or {}).get("thinking_coding") or {}
        if (tc.get("max_tokens") or 0) < 32768:
            problems.append(f"{name}.thinking_coding: max_tokens < 32768")
        if not e.get("tool_call_format"):
            problems.append(f"{name}: missing tool_call_format")
        rp = e.get("reasoning_parser")
        if rp is not None:
            if not (isinstance(rp, dict) and rp.get("name") and rp.get("backends") and str(rp.get("source") or "").strip()):
                problems.append(f"{name}: reasoning_parser needs name, backends and a source: citation")
            elif card_text and str(rp.get("card_flag") or "") not in card_text:
                problems.append(f"{name}: reasoning_parser {rp.get('name')!r} but the card does not state "
                                f"{rp.get('card_flag')!r}")
    problems += check_policy(data)
    problems += check_sampling(data)
    for src, a in (data.get("aliases") or {}).items():
        if _find_entry(data, a.get("to"))[1] is None:
            problems.append(f"alias {src}: target {a.get('to')} has no profile")
        if not str(a.get("source") or "").strip():
            problems.append(f"alias {src}: missing source:")
    if not str((data.get("fallback") or {}).get("source") or "").strip():
        problems.append("fallback: missing source:")
    return problems


def rehash(path=None):
    """Rewrite every card_sha256 in the YAML in place (text edit, keeps comments)."""
    import re
    p = Path(path) if path else PROFILES_PATH
    data = load_profiles(p, refresh=True)
    root = cards_root(data)
    text = p.read_text()
    for name, e in data["models"].items():
        cp = root / e["card"]
        if not cp.is_file():
            continue
        h = card_sha256(cp)
        pat = re.compile(r'(\n  (?:"%s"|%s):[^\n]*\n(?:(?!\n  \S).)*?card_sha256: )"[^"]*"' %
                         (re.escape(name), re.escape(name)), re.S)
        text, n = pat.subn(lambda m: m.group(1) + '"%s"' % h, text, count=1)
        if not n:
            print(f"rehash: could not locate {name}", file=sys.stderr)
    p.write_text(text)
    _CACHE.clear()


def resolve_model(model, lane="darkbloom", served=None, data=None):
    """PURE-ish. Returns (resolved, suggestions); resolved is None when unknown."""
    import difflib
    m = str(model or "").strip()
    if not m:
        return (None, [])
    data = data or load_profiles()
    resolved, _ = resolve_alias(m, lane, data)
    key, _ = _find_entry(data, resolved)
    if key is not None:
        return (key, [])
    hits = [sid for sid in (served or []) if str(sid).lower() == m.lower()]
    if len(hits) == 1:
        return (hits[0], [])
    candidates = list(data.get("models", {}).keys())
    candidates += [str(n) for v in data.get("models", {}).values() for n in (v.get("names") or [])]
    candidates += list((data.get("aliases") or {}).keys())
    candidates += served or []
    ml = m.lower()
    candidates_lowercased = [c.lower() for c in candidates]
    matches = difflib.get_close_matches(ml, candidates_lowercased, n=3, cutoff=0.5)
    result = []
    seen = set()
    for match in matches:
        idx = candidates_lowercased.index(match)
        orig = candidates[idx]
        if orig not in seen:
            seen.add(orig)
            result.append(orig)
    return (None, result[:3])


def live_model_ids(rec, timeout=2.0):
    """GET <base>/v1/models with the record's api_key; ids list, or None when unreachable."""
    try:
        if not isinstance(rec, dict):
            return None
        import urllib.request
        base = rec.get("base_url", "")
        if isinstance(base, str):
            base = base.strip().rstrip("/")
        else:
            base = ""
        if not base and rec.get("api_key"):
            base = "http://127.0.0.1:8000"
        if base.endswith("/v1"):
            base = base[:-len("/v1")]
        if not base.startswith("http"):
            return None
        url = base + "/v1/models"
        req = urllib.request.Request(url)
        api_key = rec.get("api_key")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        rows = body["data"]
        return [str(x["id"]) for x in rows if isinstance(x, dict) and x.get("id")]
    except Exception:
        return None



def vet_model(model, rec, files_served, lane="darkbloom", timeout=2.0, data=None):
    """Returns (resolved, error, warning); the ONE gate used by enqueue and mini-submit."""
    live = live_model_ids(rec, timeout)
    if live is not None:
        served = live
        warning = None
    else:
        served = list(files_served or ())
        warning = "live Darkbloom /v1/models unreachable; validated against model_profiles and provider files only"
    resolved, suggestions = resolve_model(model, lane, served, data)
    if resolved:
        return (resolved, None, warning)
    m = str(model)
    if suggestions:
        error = f"unknown model {m!r}; closest: {', '.join(suggestions)}"
    else:
        error = f"unknown model {m!r}; no close match in model_profiles or served models"
    return (None, error, warning)


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--check":
        probs = check_profiles()
        for x in probs:
            print("PROBLEM:", x)
        print("OK" if not probs else f"{len(probs)} problem(s)")
        return 1 if probs else 0
    if argv[0] == "--rehash":
        rehash()
        print("rehashed")
        return 0
    if argv[0] == "--show" and len(argv) >= 2:
        role = argv[2] if len(argv) > 2 else "author"
        print(json.dumps({"profile": get_profile(argv[1], role),
                          "openai": build_request_fields(argv[1], role, "openai"),
                          "ollama": build_request_fields(argv[1], role, "ollama")},
                         indent=2, default=str))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
