#!/usr/bin/env python3
"""escalation_verdict.py -- turn an escalation into ONE machine-checked verdict + action.

Why (2026-10-09): the watcher's local reviewer rambles, often never writes a
`VERDICT:` line, and self-heal then answered "skip: verdict none is not a
fixture/harness defect". Rows piled up for a human. This module is the single place
a verdict is decided, in strict priority order (research 2026-10-09: key on the
MECHANICAL signature, not LLM prose):

  1. mechanical : the queue's own failure_class (operator/spec/harness/context)
  2. structured : an `ESC_RESULT: {"verdict": "b", ...}` line (or a ```json block)
  3. VERDICT line: `VERDICT: (b) -- ...`
  4. inferred   : "the verdict is (b)", "(c) is the diagnosis", "harness defect" ...
  5. reason     : keywords in the escalation reason itself
  6. default    : 'b' (re-spec) -- a verdict is NEVER left as None.

Verdicts: a not-a-real-failure / already satisfied -> close | resume
          b spec wrong or under-specified           -> re-author with findings, then re-spec
          c harness defect                          -> harness repair round
          d model incapable                         -> bigger model (configurable) or re-slice

LLM-derived verdicts are CACHED by hash of their inputs and never re-asked
(flip-flop risk). Stdlib only; every function is pure unless it takes a path.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

ESC_DIR = Path.home() / ".ollama-dispatch" / "escalations"
CONFIG_PATH = Path.home() / ".ollama-dispatch" / "escalation-actions.json"
KILL_FILE = ESC_DIR / "ACTIONS-OFF"
VERDICT_CACHE = ESC_DIR / "verdict-cache.json"

DEFAULTS = {
    # The owner 2026-10-09: the (d) "bigger model" may be ANY model Darkbloom can host; it
    # is configuration, never hardcoded. null => no model rung (d falls to re-slice).
    "bigger_model": None,
    "bigger_host": None,
    "bigger_num_ctx": None,
    # research: "same failure signature twice => re-spec rather than retry"
    "abandon_same_signature_n": 2,
    # research: retry cap 3 attempts per task
    "max_attempts_per_task": 3,
    "respec_max_per_chain": 2,   # per ROOT task (label base), across worktrees
    "slice_respec_max": 1,        # re-spec retries per slice before the final rung
    "respec_max_per_day": 4,
    # classify-only re-run of the reviewer when it left no usable verdict
    "rerun_review_max": 1,
}


def load_config(path=None, env=None):
    cfg = dict(DEFAULTS)
    try:
        d = json.loads(Path(path or CONFIG_PATH).read_text())
        if isinstance(d, dict):
            cfg.update({k: v for k, v in d.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    env = os.environ if env is None else env
    if env.get("ESC_BIGGER_MODEL"):
        cfg["bigger_model"] = env["ESC_BIGGER_MODEL"]
    if env.get("ESC_BIGGER_HOST"):
        cfg["bigger_host"] = env["ESC_BIGGER_HOST"]
    if env.get("ESC_ABANDON_N"):
        try:
            cfg["abandon_same_signature_n"] = max(1, int(env["ESC_ABANDON_N"]))
        except ValueError:
            pass
    return cfg


def actions_killed(kill_file=None, env=None):
    """Kill switch: the file ACTIONS-OFF in the escalations dir, or ESC_ACTIONS=off.
    Diagnosis still runs; no automatic ACTION is taken."""
    env = os.environ if env is None else env
    if str(env.get("ESC_ACTIONS", "on")).lower() == "off":
        return True
    return Path(kill_file or KILL_FILE).exists()


# --------------------------------------------------------------------- parsing
_VERDICT_LINE = re.compile(r"^\W*VERDICT\W*\(?\s*([abcd])\b", re.I)
_RESULT_LINE = re.compile(r"ESC_RESULT:\s*(\{.*\})", re.I)
_INFERRED = (
    re.compile(r"\b(?:my |the |final )?verdict\s+(?:is|=)\s*:?\s*\(?([abcd])\)?(?![a-z])", re.I),
    re.compile(r"\(([abcd])\)\s+is\s+the\s+(?:most likely |correct |right )?"
               r"(?:diagnosis|answer|verdict|classification)", re.I),
    re.compile(r"\bit(?:'s| is) (?:case |option )?\(([abcd])\)", re.I),
    re.compile(r"\bclassif(?:y|ied|ication)[^.\n]{0,40}\(([abcd])\)", re.I),
)
# keyword fallback over the review text (last resort before the reason)
_KEYWORDS = (
    ("a", re.compile(r"already satisfied|nothing to fix|not a real failure|false alarm", re.I)),
    ("c", re.compile(r"harness (?:or dispatch )?defect|harness/dispatch defect|"
                     r"fixture (?:passes|cannot fail)|dispatch defect|pipeline bug", re.I)),
    ("b", re.compile(r"spec (?:is )?(?:wrong|under-?specified)|under-?specified|"
                     r"unsatisfiable|task is wrong", re.I)),
    ("d", re.compile(r"model (?:is )?incapab|genuine model|capability limit", re.I)),
)

CLASS_TO_VERDICT = {          # the queue's failure_class taxonomy (ollama-queue classify_failure)
    "spec": "b",
    "harness": "c",
    "operator": "a",          # forced stop / pause: not a real failure -> resume
    "context": "d",           # ran out of context: needs a bigger window/model
}
_STRICT_CLASSES = frozenset(CLASS_TO_VERDICT)   # nonconvergence/model/None defer to the reviewer


def parse_structured(review):
    """The ESC_RESULT JSON (last one wins) or a ```json block with a 'verdict' key."""
    best = None
    for m in _RESULT_LINE.finditer(review or ""):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and str(d.get("verdict", "")).strip().lower()[:1] in "abcd" \
                and str(d.get("verdict", "")).strip():
            best = d
    if best is None:
        for m in re.finditer(r"```json\s*(\{.*?\})\s*```", review or "", re.S):
            try:
                d = json.loads(m.group(1))
            except ValueError:
                continue
            v = str((d or {}).get("verdict", "")).strip().strip("()").lower()
            if isinstance(d, dict) and len(v) == 1 and v in "abcd":
                best = d
    if best is not None:
        best = dict(best, verdict=str(best["verdict"]).strip().strip("()").lower()[:1])
    return best


def parse_review(review):
    """(letter|None, source). Pure."""
    s = parse_structured(review)
    if s:
        return s["verdict"], "structured"
    for line in (review or "").splitlines():
        m = _VERDICT_LINE.match(line.strip().lstrip("*# ").strip())
        if m:
            return m.group(1).lower(), "verdict-line"
    last = None
    for rx in _INFERRED:
        for m in rx.finditer(review or ""):
            if last is None or m.start() > last[0]:
                last = (m.start(), m.group(1).lower())
    if last:
        return last[1], "inferred"
    # keyword vote over the TAIL of the review (the conclusion, not the exploration)
    tail = (review or "")[-1500:]
    hits = [(m.start(), v) for v, rx in _KEYWORDS for m in [rx.search(tail)] if m]
    if hits:
        return sorted(hits)[-1][1], "keyword"
    return None, None


_REASON_HINTS = (
    ("b", re.compile(r"fixture passes at baseline|PASSES at baseline|cannot discriminate|"
                     r"SPEC_DEFECT|unsatisfiable|under-?specified|TODO placeholders", re.I)),
    ("a", re.compile(r"external pause|SIGTERM|force stopped|PAUSED FOR REVIEW", re.I)),
    ("c", re.compile(r"harness|worker crashed|traceback|no iterations", re.I)),
    ("d", re.compile(r"iteration cap|did not converge|nonconvergence", re.I)),
)


def reason_hint(reason):
    for v, rx in _REASON_HINTS:
        if rx.search(reason or ""):
            return v
    return None


def input_hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(b"\x00")
        h.update(str(p if p is not None else "").encode("utf-8", "replace"))
    return h.hexdigest()[:20]


def _cache_load(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve(review, failure_class=None, reason=None, cache_path=None, key_parts=()):
    """The ONE verdict. Returns {"verdict","source","why","cached"}; verdict is never None.

    `key_parts` (the context/review inputs) key the cache: an LLM-derived verdict is
    stored on first resolution and returned unchanged afterwards (never re-asked)."""
    cache_path = Path(cache_path or VERDICT_CACHE)
    key = input_hash(failure_class, reason, review, *key_parts)
    cached = _cache_load(cache_path).get(key)
    if cached:
        return dict(cached, cached=True)
    fc = (failure_class or "").strip().lower()
    out = None
    if fc in _STRICT_CLASSES:
        out = {"verdict": CLASS_TO_VERDICT[fc], "source": "mechanical",
               "why": "queue failure_class=%s" % fc}
    if out is None:
        v, src = parse_review(review)
        if v:
            out = {"verdict": v, "source": src, "why": "review %s" % src}
    if out is None:
        v = reason_hint(reason)
        if v:
            out = {"verdict": v, "source": "reason", "why": "keywords in the escalation reason"}
    if out is None:
        out = {"verdict": "b", "source": "default",
               "why": "no verdict anywhere -> re-spec (bounded by the attempt caps)"}
    out["cached"] = False
    try:
        c = _cache_load(cache_path)
        c[key] = {k: out[k] for k in ("verdict", "source", "why")}
        c[key]["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if len(c) > 2000:
            for k in sorted(c, key=lambda x: c[x].get("at", ""))[:500]:
                c.pop(k, None)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(c, indent=1))
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return out


# A review file that is NOT a model verdict: the model review was capped by the worker
# (named exit reason) or never spawned because the context carried no evidence. The first
# line is a fixed marker; every consumer that would ACT on a review (self-heal retry/respec/
# skip/close) must refuse these -- the escalation stays open for a human, nothing is done.
NO_VERDICT_MARKERS = ("REVIEW CAPPED", "NO EVIDENCE AVAILABLE")


def is_no_verdict_review(review):
    """PURE. True when the review's first non-blank line is a NO_VERDICT_MARKERS marker."""
    for line in str(review or "").splitlines():
        s = line.strip().lstrip("*# <!->").strip()
        if s:
            return s.upper().startswith(NO_VERDICT_MARKERS)
    return False


def has_verdict(review):
    """True when the review itself carries a verdict (any parse level above 'keyword')."""
    v, src = parse_review(review)
    return bool(v) and src != "keyword"


# ------------------------------------------------------------------ signatures
_NOISE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{7,}\b|\d+")


def failure_signature(failure_class=None, failure_detail=None, terminal_reason=None):
    """MECHANICAL signature of one failure: class + normalized detail + terminal reason
    (digits/hex ids masked so iteration counts and job ids do not make two identical
    failures look different)."""
    parts = [str(failure_class or "?").lower(), _NOISE.sub("#", str(failure_detail or "").lower()).strip(),
             str(terminal_reason or "").lower()]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def same_signature_count(signatures, current):
    return sum(1 for s in signatures if s and s == current)


ACTIONS = {"a": "close-or-resume", "b": "reauthor-then-respec", "c": "harness-repair",
           "d": "bigger-model-or-reslice"}
