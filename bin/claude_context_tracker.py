"""Pure decision logic for the Claude context-handoff trigger.

Reads a session transcript's LATEST assistant `usage` to compute how full the
model's context window is, and decides whether (and at most once per session)
to fire the `/handoff` skill. No side effects beyond the optional fired-state
marker file; hook wiring and pending.md writing are out of scope here.

Context size for a turn = input_tokens + cache_read_input_tokens +
cache_creation_input_tokens. `output_tokens` is that turn's generation and is
NOT part of the context size.
"""

import json
import os

# Tunable model -> context-window map. Claude Code strips the "[1m]" marker
# from the transcript's `message.model`, so key on the BARE ids it actually
# records; these models run at 1M in practice. Unknown ids fall back to
# DEFAULT_WINDOW (see window_for_model).
DEFAULT_WINDOW = 200000
ONE_M_WINDOW = 1000000
MODEL_WINDOWS = {
    "claude-opus-5": ONE_M_WINDOW,
    "claude-opus-4-8": ONE_M_WINDOW,
    "claude-opus-4-7": ONE_M_WINDOW,
    "claude-opus-4-6": ONE_M_WINDOW,
    "claude-sonnet-5": ONE_M_WINDOW,
    "claude-haiku-4-5": DEFAULT_WINDOW,
}

_INPUT_KEYS = ("input_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens")


def _as_int(value):
    """Coerce a usage value to int; None / missing / non-numeric -> 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def context_size_from_usage(usage):
    """Sum the three input-side usage keys; never raises.

    `output_tokens` is deliberately NOT counted -- it is that turn's
    generation, not part of the context the model saw.
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in _INPUT_KEYS:
        total += _as_int(usage.get(key))
    return total


def context_size_from_transcript(path):
    """Context size from the LAST assistant line carrying a usage object.

    Skips malformed lines; returns 0 on missing/unreadable file or when no
    assistant/usage line exists. Never raises.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, ValueError):
        return 0

    last_usage = None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            last_usage = usage

    if last_usage is None:
        return 0
    return context_size_from_usage(last_usage)


def window_for_model(model_id):
    """Map a model id to its context-window size (default 200000).

    Primary signal is the BARE-id map (the transcript strips "[1m]", so bare
    ids are what actually appear); a stray "[1m]" in the id still maps to the
    1M window as belt-and-braces.
    """
    if isinstance(model_id, str):
        if model_id in MODEL_WINDOWS:
            return MODEL_WINDOWS[model_id]
        if "[1m]" in model_id:
            return ONE_M_WINDOW
    return DEFAULT_WINDOW


def max_context_from_transcript(path):
    """LARGEST context size across ALL assistant usage lines (max, not last).

    Reuses `context_size_from_usage` per line; skips malformed lines; returns
    0 on missing/unreadable file or when no assistant/usage line exists.
    Never raises.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, ValueError):
        return 0

    max_size = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            size = context_size_from_usage(usage)
            if size > max_size:
                max_size = size
    return max_size


def window_from_transcript(path, model_id):
    """Context window for a session, with an observed-max override.

    If the transcript ever shows a context larger than DEFAULT_WINDOW that is
    physically impossible in a 200K window, so it forces ONE_M_WINDOW
    regardless of the model map; otherwise fall back to `window_for_model`.
    """
    if max_context_from_transcript(path) > DEFAULT_WINDOW:
        return ONE_M_WINDOW
    return window_for_model(model_id)


def context_fraction(context_size, window):
    """context_size / window; 0.0 when window <= 0 (no ZeroDivision)."""
    try:
        if window is None or window <= 0:
            return 0.0
        return float(context_size) / float(window)
    except (TypeError, ValueError):
        return 0.0


def should_trigger_handoff(context_size, window, threshold=0.80):
    """True iff context_fraction >= threshold; False when window <= 0."""
    if window is None or window <= 0:
        return False
    try:
        fraction = float(context_size) / float(window)
    except (TypeError, ValueError):
        return False
    return fraction >= threshold


def _state_path(session_id, state_dir):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(session_id))
    return os.path.join(state_dir, "fired-" + safe)


def has_fired(session_id, state_dir):
    """True if this session already fired the handoff."""
    try:
        return os.path.exists(_state_path(session_id, state_dir))
    except (OSError, TypeError):
        return False


def mark_fired(session_id, state_dir):
    """Record that this session has fired; best-effort, never raises."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(_state_path(session_id, state_dir), "w", encoding="utf-8") as fh:
            fh.write("fired\n")
    except (OSError, TypeError):
        pass


def should_fire_now(session_id, context_size, window, state_dir, threshold=0.80):
    """Fire at most once per session when over threshold.

    True only when the context is over threshold AND this session has not
    already fired; on firing it marks the session (so a second call in the
    same session returns False). Under threshold: False, and no mark.
    """
    if not should_trigger_handoff(context_size, window, threshold):
        return False
    if has_fired(session_id, state_dir):
        return False
    mark_fired(session_id, state_dir)
    return True
