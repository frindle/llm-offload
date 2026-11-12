#!/usr/bin/env python3
"""fixed_lane -- FIXED REPAIR-AND-VALIDATE LANE for SMALL dispatch tasks.

NOT wired into the worker or the queue (see ~/.ollama-dispatch/merge-queue/
2026-10-09-fixed-lane-notes.md for the exact wiring). A standalone module so the
worker/queue owners can adopt it behind a flag after the replay measurement.

Why: the open agent loop (ollama-worker.py run_task) spends most of its failures on
runaway turns, tool-call parse errors and iteration exhaustion. When the spec already
localizes the work (<=2 declared target files + a fast deterministic verify command)
the job is repair + validate, which needs no LLM-chosen actions (Agentless, SWE-Gym
Table 3, Aider edit-format data; see 2026-10-09-pipeline-review-agents.md s.5).

Flow (all deterministic except the one model turn per round):
  0. lane_eligible(task)                      router predicate
  1. context assembly (target files, related-import skeletons, spec, baseline-RED
     verify output)                            no LLM
  2. ONE model turn, NO tools, producing SEARCH/REPLACE blocks (or whole-file output
     when every target is <= whole_max_lines)  thinking policy from the lane config
  3. deterministic apply (match ladder; ambiguous/no-match -> Aider-style message)
     + edit_guard syntax / lint-delta guard
  4. run verify
  5. <= `reflections` (2) reflection rounds feeding the verify output back
  6. still red -> restart from the clean baseline as a FRESH sample (K from the
     profile: 4 on the 35B-A3B, 2 on the 9B) with temperature variation
  7. exhaustion -> structured LaneResult carrying the best diff + last verify output
     (`handoff`) so the caller escalates to the open loop SEEDED with that diff.
Selection among samples: verify pass first, then smallest diff, then fewest hunks.
No majority vote.

Everything external is injectable: `transport` (model), `runner` (verify command),
`clock`, `sleep`, `log`.

CLI:  fixed_lane.py eligible --task T.json     print eligibility
      fixed_lane.py run --task T.json [--model M] [--k N] [--host URL] [--no-stream]
task JSON: {"cwd","targets":[...],"verify","spec","verify_timeout_s","protected":[...]}
"""
import argparse
import ast
import difflib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import edit_guard as _eg            # noqa: E402
import model_profile as _mp         # noqa: E402

# ---------------------------------------------------------------------------------------
# Terminal reasons -- register in ollama-queue.py _FAILURE_CONTEXT_REASONS (and the
# failure_ledger classify) so these count as `context`, not `model`. See the notes file.
# ---------------------------------------------------------------------------------------
FIXED_LANE_EXHAUSTED = "fixed_lane_exhausted"          # edits applied, verify never green
FIXED_LANE_APPLY_FAILED = "fixed_lane_apply_failed"    # no sample ever landed an edit
FIXED_LANE_TRANSPORT_ERROR = "fixed_lane_transport_error"   # model unreachable (infra, retry)
FIXED_LANE_FAILURE_REASONS = (FIXED_LANE_EXHAUSTED, FIXED_LANE_APPLY_FAILED,
                              FIXED_LANE_TRANSPORT_ERROR)

# non-terminal outcomes the router/caller must handle
ST_PASSED = "passed"
ST_INELIGIBLE = "ineligible"          # run the open loop instead
ST_ALREADY_GREEN = "already_green"    # baseline verify already passes: nothing to repair

# ---------------------------------------------------------------------------------------
# Lane configuration (per profile). Optional override section in model_profiles.yaml:
#   fixed_lane: {defaults: {...}, models: {<profile_key>: {...}}}
# ---------------------------------------------------------------------------------------
DEFAULT_LANE_CONFIG = {
    "format": "auto",              # auto | sr | whole
    "whole_max_lines": 150,        # auto: whole-file when EVERY target is <= this many lines
    "max_target_lines": 400,       # eligibility: bigger targets need skeleton mode (not built)
    "max_targets": 2,
    "max_verify_s": 60,            # eligibility: declared verify timeout ceiling
    "reflections": 2,
    "k": 2,
    "temperatures": [None, 0.7, 0.9, 1.0],   # None = the profile's own temperature
    "presence_penalty": 0.0,       # the non-thinking card value (1.5) penalises verbatim
                                   # reproduction of the file; override. A/B in replay.
    "thinking": "off",             # off | on | low (on + reasoning_effort:"low")
    "max_tokens_off": 6144,
    "max_tokens_think": 12288,
    "reasoning_budget_chars": 12000,   # thinking chars with no answer before a turn is cut
    "wall_s_per_turn": 420,
    "max_wall_s": 1500,            # whole-lane wall cap: no new sample starts after this
    "context_frac": 0.5,           # of the profile ctx (Aider: silent truncation collapses edits)
    "chars_per_token": 3.0,
    "verify_tail_chars": 4096,
    "shrink_floor": 0.4,           # whole-file reply under this fraction of the old size = rejected
    "stop_at_first_pass": True,
    "related_files": 6,
    "related_lines": 60,
}
MODEL_LANE_CONFIG = {         # keyed by substring of the resolved profile key (lower-case)
    "35b-a3b": {"k": 4},
    "9b": {"k": 2},
}

_EXPLORATORY_RX = re.compile(
    r"\b(explor\w*|investigat\w*|find out|figure out|research|audit|survey|brainstorm|"
    r"root[- ]cause|diagnos\w*|determine why|understand why|open[- ]ended|look (?:around|into)|"
    r"which files?|where (?:is|are|does)|trace (?:the|why))\b", re.I)


def lane_config(model, overrides=None, data=None):
    """Resolved lane config for `model` (profile-key substring defaults, then the optional
    yaml `fixed_lane:` section, then explicit overrides)."""
    cfg = dict(DEFAULT_LANE_CONFIG)
    try:
        prof = _mp.get_profile(model, "non_thinking", "darkbloom")
        key = str(prof.get("profile_key") or "").lower() + " " + str(prof.get("model") or "").lower()
    except Exception:
        prof, key = {}, str(model or "").lower()
    for frag, vals in MODEL_LANE_CONFIG.items():
        if frag in key:
            cfg.update(vals)
            break
    try:
        d = data if data is not None else _mp.load_profiles()
        sec = d.get("fixed_lane") or {}
        cfg.update(sec.get("defaults") or {})
        pk = str(prof.get("profile_key") or "")
        cfg.update((sec.get("models") or {}).get(pk) or {})
    except Exception:
        pass
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


# ---------------------------------------------------------------------------------------
# 0. Router predicate
# ---------------------------------------------------------------------------------------
def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _norm_rel(cwd, p):
    """Normalise a declared path to a cwd-relative posix path; None when it escapes cwd."""
    p = str(p or "").strip()
    if not p:
        return None
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))
    root = os.path.realpath(cwd)
    real = os.path.realpath(full)
    if real != root and not real.startswith(root + os.sep):
        return None
    return os.path.relpath(full, cwd).replace(os.sep, "/")


def lane_eligible(task, cfg=None, model=None):
    """-> (ok, reasons). The lane is for SMALL, already-localized, fast-to-verify tasks:
    1..max_targets declared target files (each text, <= max_target_lines), a verify
    command whose declared timeout is <= max_verify_s, and no exploratory intent in the
    task title/spec. Anything else -> False, with the reasons (the caller runs the open
    loop)."""
    cfg = cfg or lane_config(model or "")
    reasons = []
    cwd = str(task.get("cwd") or "")
    targets = [t for t in (task.get("targets") or []) if str(t).strip()]
    if not targets:
        reasons.append("no declared target files")
    if len(targets) > int(cfg["max_targets"]):
        reasons.append(f"{len(targets)} target files declared (max {cfg['max_targets']})")
    if not str(task.get("verify") or "").strip():
        reasons.append("no verify command")
    vt = task.get("verify_timeout_s")
    if vt is not None:
        try:
            if float(vt) > float(cfg["max_verify_s"]):
                reasons.append(f"verify timeout {vt}s > {cfg['max_verify_s']}s (not fast)")
        except (TypeError, ValueError):
            reasons.append("verify_timeout_s is not a number")
    intent = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    m = _EXPLORATORY_RX.search(intent)
    if m or task.get("exploratory"):
        reasons.append(f"exploratory intent ({m.group(0)!r})" if m else "task flagged exploratory")
    seen = set()
    for t in targets:
        rel = _norm_rel(cwd, t) if cwd else None
        if rel is None:
            reasons.append(f"target {t!r} is outside the working directory")
            continue
        if rel in seen:
            continue
        seen.add(rel)
        full = os.path.join(cwd, rel)
        if os.path.exists(full):
            txt = _read_text(full)
            if txt is None:
                reasons.append(f"target {rel} is not readable text")
            elif txt.count("\n") + 1 > int(cfg["max_target_lines"]):
                reasons.append(f"target {rel} has {txt.count(chr(10)) + 1} lines "
                               f"(> {cfg['max_target_lines']}; needs skeleton mode)")
    return (not reasons), reasons


# ---------------------------------------------------------------------------------------
# Think stripping (local copy of worker_robust.strip_think semantics; parity-tested)
# ---------------------------------------------------------------------------------------
_THINK_CLOSED_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def strip_think(text):
    text = text or ""
    info = {"closed": 0, "unterminated": False, "stray_close": False}
    out, n = _THINK_CLOSED_RE.subn("", text)
    info["closed"] = n
    m = _THINK_OPEN_RE.search(out)
    if m:
        info["unterminated"] = True
        out = out[:m.start()]
    m = _THINK_CLOSE_RE.search(out)
    if m:
        info["stray_close"] = True
        out = out[m.end():]
    return out.strip(), info


# ---------------------------------------------------------------------------------------
# 1. Context assembly
# ---------------------------------------------------------------------------------------
def tail_text(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} chars elided ...]\n" + text[-limit:]


def _py_imports(text):
    mods = []
    try:
        tree = ast.parse(text)
    except Exception:
        return mods
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [(a.name, 0) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            mods.append((n.module or "", n.level or 0))
    return mods


_JS_IMPORT_RX = re.compile(r"""(?:from\s+|require\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")


def _related_paths(cwd, rel, text, limit):
    out = []
    base = os.path.dirname(os.path.join(cwd, rel))
    if rel.endswith(".py"):
        for mod, level in _py_imports(text):
            parts = [p for p in mod.split(".") if p]
            roots = [base if level else cwd]
            if level > 1:
                roots = [os.path.normpath(os.path.join(base, *[".."] * (level - 1)))]
            for root in roots + ([base] if not level else []):
                cands = [os.path.join(root, *parts) + ".py", os.path.join(root, *parts, "__init__.py")] if parts else []
                for c in cands:
                    if os.path.isfile(c):
                        out.append(c)
                        break
    elif rel.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
        for m in _JS_IMPORT_RX.finditer(text):
            tgt = os.path.normpath(os.path.join(base, m.group(1)))
            for c in [tgt] + [tgt + e for e in (".js", ".mjs", ".ts", ".tsx", ".jsx")] + \
                    [os.path.join(tgt, "index.js"), os.path.join(tgt, "index.ts")]:
                if os.path.isfile(c):
                    out.append(c)
                    break
    seen, uniq = set(), []
    for p in out:
        r = os.path.relpath(p, cwd).replace(os.sep, "/")
        if r not in seen and not r.startswith(".."):
            seen.add(r)
            uniq.append(r)
    return uniq[:limit]


_SKEL_RX = re.compile(r"^(\s*)(async\s+def|def|class|export\b|function\b|interface\b|type\s+\w+\s*=|"
                      r"const\s+\w+\s*=\s*(?:async\s*)?\(|[A-Z][A-Z0-9_]{2,}\s*=)")


def skeleton(text, max_lines):
    """Signatures-only view of a related file (Agentless: skeleton beats full file)."""
    out = [ln.rstrip() for ln in text.splitlines() if _SKEL_RX.match(ln)]
    return "\n".join(out[:max_lines])


class ContextTooLarge(Exception):
    pass


SYSTEM_SR = """You are a precise code-repair engine. You cannot run commands or call tools: you only reply with edits in the exact format below, and a harness applies them and runs the verify command for you.

Reply format: at most 3 sentences of reasoning, then one or more SEARCH/REPLACE blocks. Every block is:

path/to/file.ext
```
<<<<<<< SEARCH
a contiguous chunk of lines copied EXACTLY from the current file
=======
the lines to put in its place
>>>>>>> REPLACE
```

Rules:
- The path is the FULL path alone on the line above the block, exactly as declared below. Only edit the declared files.
- The SEARCH lines must EXACTLY match the current file content, character for character, including indentation, comments and blank lines. Do not use `...` to elide code.
- A SEARCH chunk only replaces its first match, so include enough surrounding lines to match ONE place uniquely. Keep blocks small; use several blocks for several changes.
- REPLACE needs proper indentation: write every leading space.
- To insert code, SEARCH a nearby existing line and REPLACE it with that line plus the new lines. To delete code, leave REPLACE empty.
- Do not edit tests, the verify command or any file that is not declared."""

SYSTEM_WHOLE = """You are a precise code-repair engine. You cannot run commands or call tools: you only reply with the complete new content of each declared file you change, and a harness writes it and runs the verify command for you.

Reply format: at most 3 sentences of reasoning, then for EACH file you change:

path/to/file.ext
```
<the ENTIRE new content of the file, every line, from the first to the last>
```

Rules:
- The path is the FULL path alone on the line above the fence, exactly as declared below. Only output declared files; leave unchanged files out.
- Output the whole file: never abbreviate, never write `...` or "rest of file unchanged" - omitted lines are DELETED.
- Do not edit tests, the verify command or any file that is not declared."""


def _fence(path, text):
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{path}\n{ticks}\n{text}{'' if text.endswith(chr(10)) or not text else chr(10)}{ticks}"


def render_files(files, order):
    parts = []
    for p in order:
        t = files.get(p)
        parts.append(_fence(p, t) if t is not None else f"{p}\n(this file does not exist yet: create it)")
    return "\n\n".join(parts)


def build_user_prompt(task, files, order, related, verify_out, fmt):
    spec = str(task.get("spec") or "").strip()
    p = []
    if task.get("title"):
        p.append(f"# Task: {task['title']}")
    p.append("## Specification\n" + (spec or "(none)"))
    p.append("## Declared files (the only files you may edit)\n" + "\n".join(f"- {t}" for t in order))
    p.append("## Current content of the declared files\n" + render_files(files, order))
    if related:
        p.append("## Related code (read-only signatures)\n" + "\n\n".join(
            f"{path}\n```\n{sk}\n```" for path, sk in related if sk))
    p.append(f"## Verify command (it must pass)\n{task.get('verify')}")
    p.append("## Current verify output (FAILING)\n```\n" + (verify_out.strip() or "(no output)") + "\n```")
    p.append("Fix the code so the verify command passes. " +
             ("Reply with SEARCH/REPLACE blocks." if fmt == "sr" else "Reply with the complete new content of each file you change."))
    return "\n\n".join(p)


def choose_format(cfg, files, order):
    f = cfg.get("format", "auto")
    if f in ("sr", "whole"):
        return f
    for p in order:
        t = files.get(p)
        if t is not None and t.count("\n") + 1 > int(cfg["whole_max_lines"]):
            return "sr"
    return "whole"


def build_context(task, cfg, files, order, verify_out, ctx_tokens):
    """-> dict(system, user, fmt, chars). Drops related skeletons, then shortens the verify
    tail, to stay under context_frac of the profile ctx; ContextTooLarge if the targets
    alone do not fit (caller runs the open loop)."""
    fmt = choose_format(cfg, files, order)
    cwd = task["cwd"]
    related = []
    for rel in order:
        txt = files.get(rel)
        if not txt:
            continue
        for rp in _related_paths(cwd, rel, txt, int(cfg["related_files"])):
            if rp in order or any(rp == r[0] for r in related):
                continue
            t = _read_text(os.path.join(cwd, rp))
            if t:
                related.append((rp, skeleton(t, int(cfg["related_lines"]))))
    budget = int(float(cfg["context_frac"]) * float(ctx_tokens) * float(cfg["chars_per_token"]))
    system = SYSTEM_SR if fmt == "sr" else SYSTEM_WHOLE
    tail = int(cfg["verify_tail_chars"])
    dropped = []
    for attempt in range(3):
        vo = tail_text(verify_out, tail)
        user = build_user_prompt(task, files, order, related, vo, fmt)
        n = len(system) + len(user)
        if n <= budget:
            return {"system": system, "user": user, "fmt": fmt, "chars": n, "dropped": dropped}
        if attempt == 0:
            related, dropped = [], dropped + ["related"]
        elif attempt == 1:
            tail, dropped = max(1024, tail // 4), dropped + ["verify_tail"]
    raise ContextTooLarge(f"prompt {n} chars > budget {budget} chars (context_frac={cfg['context_frac']}, "
                          f"ctx={ctx_tokens})")


# ---------------------------------------------------------------------------------------
# 2b. Parsing the model reply
# ---------------------------------------------------------------------------------------
_HEAD_RX = re.compile(r"^\s*<{5,9}\s*SEARCH\s*>?\s*$")
_DIV_RX = re.compile(r"^\s*={5,9}\s*$")
_TAIL_RX = re.compile(r"^\s*>{5,9}\s*REPLACE\s*>?\s*$")
_FENCE_RX = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+\-.]*)\s*$")


def _clean_path(line):
    s = line.strip().strip("*").strip()
    s = s.strip("`").strip().rstrip(":").strip()
    s = re.sub(r"^(?:file|path)\s*:\s*", "", s, flags=re.I).strip("`").strip()
    return s


def parse_search_replace(text, default_path=None):
    """-> (edits, errors). edit = {"path","search":[lines],"replace":[lines],"n"}.
    Tolerant of fences and 5-9 marker characters; reports unterminated/malformed blocks."""
    lines = (text or "").split("\n")
    edits, errors = [], []
    i, last_path, n = 0, default_path, 0
    while i < len(lines):
        if not _HEAD_RX.match(lines[i]):
            i += 1
            continue
        n += 1
        path = None
        j = i - 1
        while j >= 0:
            s = lines[j]
            if not s.strip() or _FENCE_RX.match(s):
                j -= 1
                continue
            path = _clean_path(s)
            break
        if not path or (" " in path and not os.path.splitext(path)[1]) or len(path) > 300:
            path = last_path
        if not path:
            errors.append(f"block {n}: no file path given above the SEARCH marker")
        k = i + 1
        search = []
        while k < len(lines) and not _DIV_RX.match(lines[k]):
            if _TAIL_RX.match(lines[k]) or _HEAD_RX.match(lines[k]):
                break
            search.append(lines[k])
            k += 1
        if k >= len(lines) or not _DIV_RX.match(lines[k]):
            errors.append(f"block {n}: SEARCH section has no ======= divider (reply truncated or malformed)")
            i = k
            continue
        k += 1
        repl = []
        while k < len(lines) and not _TAIL_RX.match(lines[k]):
            if _HEAD_RX.match(lines[k]):
                break
            repl.append(lines[k])
            k += 1
        if k >= len(lines) or not _TAIL_RX.match(lines[k]):
            errors.append(f"block {n}: REPLACE section never closed with >>>>>>> REPLACE "
                          f"(reply truncated or malformed)")
            i = k
            continue
        if path:
            last_path = path
            edits.append({"path": path, "search": search, "replace": repl, "n": n})
        i = k + 1
    return edits, errors


def parse_whole_files(text, targets):
    """-> (files {path: text}, errors). A file block is a path line followed by a fenced
    block; the fence closes at the LAST fence line before the next file block (so fences
    inside the file content survive)."""
    lines = (text or "").split("\n")
    tset = {t: t for t in targets}
    openers = []
    for idx, ln in enumerate(lines):
        m = _FENCE_RX.match(ln)
        if not m:
            continue
        j = idx - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        cand = _clean_path(lines[j]) if j >= 0 else ""
        cand = cand[2:] if cand.startswith("./") else cand
        if cand in tset:
            openers.append((idx, cand, m.group(1)))
    errors = []
    if not openers and len(targets) == 1:
        for idx, ln in enumerate(lines):          # a single fenced block, no path line
            m = _FENCE_RX.match(ln)
            if m:
                openers.append((idx, targets[0], m.group(1)))
                break
    files = {}
    for n, (idx, path, fence) in enumerate(openers):
        end_limit = openers[n + 1][0] if n + 1 < len(openers) else len(lines)
        close = None
        for k in range(end_limit - 1, idx, -1):
            m = _FENCE_RX.match(lines[k])
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                close = k
                break
        if close is None:
            errors.append(f"{path}: fenced file block never closed (reply truncated)")
            continue
        body = "\n".join(lines[idx + 1:close])
        files[path] = body + "\n"
    if not openers:
        errors.append("no file blocks found (expected: path line, then a fenced block with the whole file)")
    return files, errors


_LAZY_RX = re.compile(r"^\s*(?:#|//|/\*|<!--|--|;)?\s*\.\.\.\s*(?:\*/|-->)?\s*$|"
                      r"(?:rest|remainder|remaining|other|existing|previous|same)\s+(?:of\s+)?(?:the\s+)?"
                      r"(?:file|code|content|lines|methods|functions|implementation)\s+"
                      r"(?:is\s+|are\s+|stays?\s+|remains?\s+)?(?:unchanged|omitted|the same|as before)", re.I)


def lazy_placeholder(lines, before_lines=()):
    """First line that looks like an elision placeholder and did not exist before."""
    seen = set(before_lines)
    for ln in lines:
        if _LAZY_RX.search(ln) and ln not in seen:
            return ln
    return None


# ---------------------------------------------------------------------------------------
# 3. Deterministic apply
# ---------------------------------------------------------------------------------------
def _indent(s):
    return len(s) - len(s.lstrip(" \t"))


def _windows(lines, search, eq):
    n = len(search)
    if n == 0 or n > len(lines):
        return []
    return [i for i in range(len(lines) - n + 1) if all(eq(lines[i + j], search[j]) for j in range(n))]


def find_block(lines, search):
    """Match ladder. -> (status, start, how, delta, count). status: ok | none | ambiguous.
    delta = indentation (spaces) to add to the REPLACE lines (can be negative)."""
    s = list(search)
    while s and not s[0].strip():       # spurious leading blank lines
        s = s[1:]
    while s and not s[-1].strip():
        s = s[:-1]
    if not s:
        return "none", None, "empty", 0, 0
    rungs = [
        ("exact", s, lambda a, b: a == b),
        ("trailing-space", s, lambda a, b: a.rstrip() == b.rstrip()),
    ]
    for how, srch, eq in rungs:
        hits = _windows(lines, srch, eq)
        if len(hits) == 1:
            return "ok", hits[0], how, 0, 1
        if len(hits) > 1:
            return "ambiguous", None, how, 0, len(hits)

    def uniform(window, srch):
        deltas = {_indent(w) - _indent(x) for w, x in zip(window, srch) if x.strip()}
        return len(deltas) == 1 and all((not x.strip()) == (not w.strip()) for w, x in zip(window, srch))

    n = len(s)
    hits = [i for i in range(len(lines) - n + 1)
            if all(lines[i + j].strip() == s[j].strip() for j in range(n)) and uniform(lines[i:i + n], s)]
    if len(hits) == 1:
        w = lines[hits[0]:hits[0] + n]
        d = next(_indent(a) - _indent(b) for a, b in zip(w, s) if b.strip())
        return "ok", hits[0], "indent-shift", d, 1
    if len(hits) > 1:
        return "ambiguous", None, "indent-shift", 0, len(hits)
    col = lambda x: " ".join(x.split())
    hits = [i for i in range(len(lines) - n + 1) if all(col(lines[i + j]) == col(s[j]) for j in range(n))]
    if len(hits) == 1:
        w = lines[hits[0]:hits[0] + n]
        d = next((_indent(a) - _indent(b) for a, b in zip(w, s) if b.strip()), 0)
        return "ok", hits[0], "whitespace-collapsed", d, 1
    if len(hits) > 1:
        return "ambiguous", None, "whitespace-collapsed", 0, len(hits)
    return "none", None, "none", 0, 0


def _reindent(lines, delta):
    if not delta:
        return list(lines)
    out = []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
        elif delta > 0:
            out.append(" " * delta + ln)
        else:
            k = min(-delta, _indent(ln))
            out.append(ln[k:])
    return out


def did_you_mean(lines, search, path):
    """Aider-style hint: the closest actual lines (SequenceMatcher >= 0.6)."""
    s = [x for x in search]
    n = max(1, len(s))
    want = "\n".join(s)
    best, best_r = None, 0.0
    for i in range(max(1, len(lines) - n + 1)):
        cand = "\n".join(lines[i:i + n])
        sm = difflib.SequenceMatcher(None, cand, want)
        if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
            continue
        r = sm.ratio()
        if r > best_r:
            best, best_r = (i, lines[i:i + n]), r
    if best and best_r >= 0.6:
        return (f"Did you mean to match some of these actual lines from {path}?\n```\n"
                + "\n".join(best[1][:15]) + "\n```")
    return ""


def apply_search_replace(files, edits, allowed, protected=()):
    """Apply blocks to the in-memory `files` {path: text|None}. -> dict(new_files, applied,
    failed). new_files holds only paths that changed. failed items: {"n","path","error"}.
    Per-block ladder; the SEARCH must resolve to exactly ONE place."""
    work = {p: (None if files.get(p) is None else files[p]) for p in allowed}
    state = {}
    for p, t in work.items():
        t = t or ""
        ls = t.split("\n")
        had_nl = t.endswith("\n") or not t
        if ls and ls[-1] == "" and t:
            ls = ls[:-1]
        state[p] = {"lines": ls if t else [], "nl": had_nl, "orig": files.get(p)}
    applied, failed = [], []
    allow = {a: a for a in allowed}
    for e in edits:
        raw = e["path"][2:] if e["path"].startswith("./") else e["path"]
        p = allow.get(raw)
        if p is None or p in protected:
            failed.append({"n": e["n"], "path": e["path"],
                           "error": f"{e['path']} is not a declared editable file "
                                    f"(only edit: {', '.join(sorted(allowed))})"})
            continue
        st = state[p]
        ph = lazy_placeholder(e["search"] + e["replace"], st["lines"])
        if ph:
            failed.append({"n": e["n"], "path": p,
                           "error": f"block contains an elision placeholder ({ph.strip()!r}); write every line out in full"})
            continue
        if not any(x.strip() for x in e["search"]):
            if not st["lines"]:
                st["lines"] = list(e["replace"])
                st["nl"] = True
                applied.append({"n": e["n"], "path": p, "how": "create"})
            else:
                failed.append({"n": e["n"], "path": p, "error":
                               f"empty SEARCH section but {p} is not empty: SEARCH must copy the existing lines to replace"})
            continue
        status, start, how, delta, cnt = find_block(st["lines"], e["search"])
        if status == "ok":
            s = list(e["search"])
            lead = 0
            while lead < len(s) and not s[lead].strip():
                lead += 1
            trail = len(s)
            while trail > lead and not s[trail - 1].strip():
                trail -= 1
            span = trail - lead
            repl = _reindent(e["replace"], delta)
            st["lines"][start:start + span] = repl
            applied.append({"n": e["n"], "path": p, "how": how})
        elif status == "ambiguous":
            failed.append({"n": e["n"], "path": p, "error":
                           f"SEARCH section matches {cnt} places in {p}; include more surrounding lines so it matches exactly ONE:\n"
                           "```\n" + "\n".join(e["search"][:12]) + "\n```"})
        else:
            hint = did_you_mean(st["lines"], e["search"], p)
            already = ""
            rep = [x for x in e["replace"]]
            if any(x.strip() for x in rep) and _windows(st["lines"], rep, lambda a, b: a.rstrip() == b.rstrip()):
                already = (f"\nAre you sure you need this SEARCH/REPLACE block? The REPLACE lines are already in {p}!")
            failed.append({"n": e["n"], "path": p, "error":
                           f"SEARCH section does not match {p}:\n```\n" + "\n".join(e["search"][:12]) +
                           "\n```\nThe SEARCH section must exactly match an existing block of lines including all "
                           "whitespace, comments and indentation." + (("\n" + hint) if hint else "") + already})
    new_files = {}
    for p, st in state.items():
        if st["lines"] == [] and st["orig"] in (None, ""):
            continue
        text = "\n".join(st["lines"]) + ("\n" if st["nl"] and st["lines"] else "")
        if text != (st["orig"] or ""):
            new_files[p] = text
    return {"new_files": new_files, "applied": applied, "failed": failed}


def apply_whole(files, replies, allowed, finish_reason, cfg, protected=()):
    """Whole-file apply with truncation / elision / shrink checks."""
    new_files, failed, applied = {}, [], []
    if finish_reason == "length":
        return {"new_files": {}, "applied": [], "failed": [{"n": 0, "path": "*", "error":
                "your reply hit the output length limit and was cut off: it is incomplete, nothing was written. "
                "Be concise and reply with the complete files."}]}
    for p, text in replies.items():
        if p not in allowed or p in protected:
            failed.append({"n": 0, "path": p, "error": f"{p} is not a declared editable file"})
            continue
        old = files.get(p) or ""
        ph = lazy_placeholder(text.split("\n"), old.split("\n"))
        if ph:
            failed.append({"n": 0, "path": p, "error": f"file contains an elision placeholder ({ph.strip()!r}); "
                           "output the entire file with every line written out"})
            continue
        if old and len(text) < float(cfg["shrink_floor"]) * len(old):
            failed.append({"n": 0, "path": p, "error": f"the new {p} is {len(text)} chars vs {len(old)} before: "
                           "looks like omitted content; output the entire file"})
            continue
        if text != old:
            new_files[p] = text
        applied.append({"n": 0, "path": p, "how": "whole"})
    return {"new_files": new_files, "applied": applied, "failed": failed}


def guard_new_files(files, new_files, guard_fn=None, run=None):
    """Run the edit guard on each changed file. -> (accepted, rejected{path: [reasons]})."""
    guard_fn = guard_fn or _eg.guard_edit
    ok, bad = {}, {}
    for p, after in new_files.items():
        r = guard_fn(p, files.get(p) or "", after, run) if guard_fn is _eg.guard_edit else guard_fn(p, files.get(p) or "", after)
        if r.ok:
            ok[p] = after
        else:
            bad[p] = list(r.reasons)
    return ok, bad


# ---------------------------------------------------------------------------------------
# 2a. Transport (OpenAI-compatible, streaming or not) + runaway guard
# ---------------------------------------------------------------------------------------
class TransportError(Exception):
    pass


class Reply:
    def __init__(self, content="", reasoning="", finish_reason=None, usage=None, cut=None, elapsed=0.0):
        self.content, self.reasoning, self.finish_reason = content, reasoning, finish_reason
        self.usage = usage or {}
        self.cut, self.elapsed = cut, elapsed


def _degenerate(text):
    tail = [ln.strip() for ln in text[-6000:].split("\n") if ln.strip()]
    if len(tail) >= 25 and len(set(tail[-25:])) == 1 and len(tail[-1]) > 3:
        return True
    return False


class RunawayGuard:
    """Cuts a stream that thinks forever, answers forever, repeats a line forever or
    outlives the per-turn wall cap. `thinking_on`: with thinking enabled Darkbloom returns
    the chain of thought INLINE in content, so content before the first </think> counts
    as reasoning (the same observation the worker's retry-non-thinking change is built on)."""

    def __init__(self, reasoning_budget_chars, content_cap_chars, wall_s, clock=time.time, thinking_on=False):
        self.rb, self.cc, self.wall = int(reasoning_budget_chars), int(content_cap_chars), float(wall_s)
        self.clock, self.thinking_on = clock, thinking_on
        self.t0 = clock()
        self.reasoning = 0
        self.content = ""
        self.closed = False

    def feed(self, kind, text):
        """kind: 'reasoning' | 'content'. -> cut reason or None."""
        if self.clock() - self.t0 > self.wall:
            return "wall_time"
        if kind == "reasoning":
            self.reasoning += len(text)
        else:
            self.content += text
            if self.thinking_on and not self.closed:
                if _THINK_CLOSE_RE.search(self.content):
                    self.closed = True
                    self.reasoning += len(self.content[:_THINK_CLOSE_RE.search(self.content).start()])
                else:
                    self.reasoning = max(self.reasoning, len(self.content))
            if len(self.content) > self.cc:
                return "content_cap"
            if len(self.content) % 512 < len(text) and _degenerate(self.content):
                return "degenerate"
        answered = bool(self.content.strip()) and (not self.thinking_on or self.closed)
        if self.rb and not answered and self.reasoning >= self.rb:
            return "reasoning_budget"
        return None


def resolve_host(explicit=None):
    """Same resolution as ollama-worker.py: explicit > FIXED_LANE_HOST > the Darkbloom local
    endpoint (darkbloom_chat.base_url) > the worker's last-resort default."""
    if explicit:
        return str(explicit).rstrip("/")
    e = os.environ.get("FIXED_LANE_HOST")
    if e:
        return e.rstrip("/")
    try:
        import darkbloom_chat as _dc
        b = _dc.base_url()
        if b:
            return b.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:8091"


def resolve_model(model=None):
    try:
        import darkbloom_chat as _dc
        return _dc.alias_model(model or _dc.DEFAULT_MODEL)
    except Exception:
        return model or "qwen3.6-35b-a3b-vl-mtp-mxfp8"


def _default_key():
    try:
        import darkbloom_chat as _dc
        return str(_dc._record().get("api_key") or "")
    except Exception:
        return ""


class OpenAITransport:
    """One chat completion against an OpenAI-compatible /v1/chat/completions. The API key is
    re-read per request (Darkbloom rewrites it on restart) and never logged."""

    def __init__(self, host=None, api_key_fn=None, stream=True, timeout=600, urlopen=None,
                 sleep=None, clock=time.time, retries=2):
        self.host = resolve_host(host)
        self.api_key_fn = api_key_fn or _default_key
        self.stream, self.timeout = stream, timeout
        self.urlopen = urlopen or urllib.request.urlopen
        self.sleep, self.clock, self.retries = sleep or time.sleep, clock, retries

    def _open(self, body):
        attempt, slot_wait = 0, 0.0
        while True:
            hdr = {"Content-Type": "application/json"}
            key = self.api_key_fn()
            if key:
                hdr["Authorization"] = "Bearer " + key
            if body.get("stream"):
                hdr["Accept"] = "text/event-stream"
            req = urllib.request.Request(self.host + "/v1/chat/completions",
                                         data=json.dumps(body).encode(), headers=hdr, method="POST")
            try:
                return self.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:500]
                except Exception:
                    detail = ""
                slot_busy = False
                try:
                    import darkbloom_chat as _dc
                    slot_busy = _dc.is_slot_busy(detail)
                except Exception:
                    pass
                if slot_busy and slot_wait < 300:
                    self.sleep(10)
                    slot_wait += 10
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    attempt += 1
                    self.sleep(min(30, 5 * attempt))
                    continue
                raise TransportError(f"HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < self.retries:
                    attempt += 1
                    self.sleep(min(30, 5 * attempt))
                    continue
                raise TransportError(f"connect failed: {e}") from None

    def complete(self, body, guard=None):
        t0 = self.clock()
        body = dict(body)
        body["stream"] = bool(self.stream)
        if self.stream:
            body["stream_options"] = {"include_usage": True}
        resp = self._open(body)
        content, reasoning, finish, usage, cut = [], [], None, {}, None
        try:
            if not self.stream:
                with resp:
                    d = json.loads(resp.read())
                ch = (d.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                c = msg.get("content") or ""
                r = msg.get("reasoning_content") or msg.get("reasoning") or ""
                rep = Reply(c, r, ch.get("finish_reason"), d.get("usage") or {}, None, self.clock() - t0)
                if guard is not None:   # cannot cut a finished response, but flag an overrun
                    rep.cut = guard.feed("reasoning", r) if r else None
                    rep.cut = rep.cut or guard.feed("content", c)
                return rep
            with resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        d = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(d.get("usage"), dict):
                        usage = d["usage"]
                    chs = d.get("choices") or []
                    if not chs:
                        continue
                    ch = chs[0] or {}
                    delta = ch.get("delta") or ch.get("message") or {}
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    c = delta.get("content") or ""
                    if r:
                        reasoning.append(r)
                        if guard is not None:
                            cut = guard.feed("reasoning", r)
                    if c:
                        content.append(c)
                        if guard is not None and not cut:
                            cut = guard.feed("content", c)
                    if guard is not None and not cut and not (r or c):
                        cut = guard.feed("content", "")     # wall-time check on keep-alives
                    if cut:
                        break
        except (OSError, ValueError) as e:
            if not content and not reasoning:
                raise TransportError(f"stream failed: {e}") from None
            cut = cut or "stream_error"
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return Reply("".join(content), "".join(reasoning), finish, usage, cut, self.clock() - t0)


def build_request(model, cfg, messages, temperature, think):
    """The chat body. NEVER contains tools / tool_choice / functions (asserted)."""
    thinking_enabled = think in ("on", "low")
    prof = _mp.get_profile(model, "author" if thinking_enabled else "non_thinking", "darkbloom")
    ov = {"temperature": temperature,
          "presence_penalty": cfg.get("presence_penalty"),
          "max_tokens": int(cfg["max_tokens_think"] if thinking_enabled else cfg["max_tokens_off"])}
    think_arg = None
    if prof.get("enable_thinking") is not None:      # model has a thinking switch
        think_arg = thinking_enabled
    f = _mp.build_request_fields(model, "author" if thinking_enabled else "non_thinking", "openai",
                                 overrides=ov, think=think_arg, lane="darkbloom")
    body = {"model": prof["model"], "messages": messages}
    body.update(f)
    if think == "low":
        body["reasoning_effort"] = "low"
    for banned in ("tools", "tool_choice", "functions", "function_call"):
        assert banned not in body, f"fixed lane must never send {banned}"
    return body


# ---------------------------------------------------------------------------------------
# Workspace + verify runner
# ---------------------------------------------------------------------------------------
def _drop_pyc(full):
    """Python's pyc staleness check is (int mtime, size): an edit that keeps the size
    (`a - b` -> `a + b`) inside the same second would verify against the OLD bytecode.
    Remove the cached bytecode of every file the lane writes."""
    d, base = os.path.split(full)
    stem = os.path.splitext(base)[0]
    pc = os.path.join(d, "__pycache__")
    try:
        for n in os.listdir(pc):
            if n.startswith(stem + ".") and n.endswith(".pyc"):
                os.unlink(os.path.join(pc, n))
    except OSError:
        pass


class Workspace:
    def __init__(self, cwd, targets):
        self.cwd = cwd
        self.targets = targets
        self.orig = {}
        for t in targets:
            full = os.path.join(cwd, t)
            self.orig[t] = _read_text(full) if os.path.exists(full) else None

    def read_all(self):
        return {t: (_read_text(os.path.join(self.cwd, t)) if os.path.exists(os.path.join(self.cwd, t)) else None)
                for t in self.targets}

    def write(self, rel, text):
        full = os.path.join(self.cwd, rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        tmp = full + f".fl{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, full)
        _drop_pyc(full)

    def restore(self):
        for t, txt in self.orig.items():
            full = os.path.join(self.cwd, t)
            if txt is None:
                if os.path.exists(full):
                    os.unlink(full)
                    _drop_pyc(full)
            else:
                self.write(t, txt)

    def diff(self, files=None):
        """Unified diff of `files` ({rel: text}) vs the baseline; defaults to current disk."""
        cur = files if files is not None else self.read_all()
        out, hunks, changed = [], 0, 0
        for t in self.targets:
            a, b = self.orig.get(t) or "", cur.get(t) or ""
            if a == b:
                continue
            d = list(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                          f"a/{t}", f"b/{t}", n=3))
            hunks += sum(1 for x in d if x.startswith("@@"))
            changed += sum(1 for x in d if (x.startswith("+") or x.startswith("-"))
                           and not x.startswith(("+++", "---")))
            out.append("".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in d))
        return "".join(out), hunks, changed


def default_runner(cmd, cwd, timeout):
    """-> (rc, output). rc None on timeout. Own process group so a timeout kills children."""
    try:
        p = subprocess.Popen(["/bin/bash", "-c", cmd], cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except OSError as e:
        return 127, f"cannot run verify: {e}"
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out[-65536:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = p.communicate()
        return None, (out or "")[-65536:] + f"\n[verify timed out after {timeout}s]"


# ---------------------------------------------------------------------------------------
# Reflection messages
# ---------------------------------------------------------------------------------------
def reflection_message(fmt, files, order, apply_errors, guard_errors, verify_out, applied_any, cfg, cut=None):
    parts = []
    if cut:
        parts.append(f"Your previous reply was CUT OFF ({cut}) before it finished, so it was ignored. "
                     "Answer directly and concisely now, without long deliberation.")
    if guard_errors:
        parts.append("These edits were REJECTED by the post-edit check and NOT written:\n" +
                     "\n".join(f"- {e}" for e in guard_errors))
    if apply_errors:
        parts.append(("Some edits could not be applied and were NOT written:\n" if applied_any else
                      "None of your edits could be applied:\n") + "\n\n".join(apply_errors) +
                     ("\nThe other blocks were applied successfully. Don't re-send them." if applied_any and fmt == "sr" else ""))
    if verify_out is not None:
        parts.append("The verify command still FAILS. Output:\n```\n" +
                     tail_text(verify_out, int(cfg["verify_tail_chars"])).strip() + "\n```")
    parts.append("The CURRENT content of the declared files (your applied edits are included):\n\n" +
                 render_files(files, order))
    parts.append("Reply with " + ("corrected SEARCH/REPLACE blocks against the CURRENT content above."
                                  if fmt == "sr" else "the complete corrected content of each file you change."))
    return "\n\n".join(parts)


def _fail_score(out):
    return sum(1 for ln in (out or "").splitlines()
               if re.search(r"\b(FAIL(?:ED)?|ERROR|Traceback|AssertionError|not ok)\b", ln))


# ---------------------------------------------------------------------------------------
# One sample = clean baseline + (1 + reflections) rounds
# ---------------------------------------------------------------------------------------
def _clip(s, n=300):
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."


def run_sample(idx, task, cfg, ws, order, ctx, model, transport, runner, clock, log, temperature,
               guard_fn=None, run=None):
    ws.restore()
    t0 = clock()
    sample = {"index": idx, "temperature": temperature, "rounds": [], "passed": False, "status": "red",
              "applied_any": False, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "last_verify_output": "", "fail_score": 10 ** 6, "files": {}, "diff": "", "hunks": 0,
              "diff_lines": 0}
    messages = [{"role": "system", "content": ctx["system"]}, {"role": "user", "content": ctx["user"]}]
    fmt = ctx["fmt"]
    think = cfg["thinking"] if cfg.get("thinking") in ("on", "low") else "off"
    vtimeout = min(float(task.get("verify_timeout_s") or cfg["max_verify_s"]), 600)
    protected = set(_norm_rel(task["cwd"], p) or p for p in (task.get("protected") or []))
    last_cut = None
    nrounds = 1 + int(cfg["reflections"])
    for r in range(nrounds):
        rec = {"round": r, "think": think, "cut": None, "applied": 0, "apply_failed": 0, "guard_rejected": 0,
               "verify_rc": None, "finish_reason": None}
        body = build_request(model, cfg, messages, temperature, think)
        thinking_on = think in ("on", "low")
        guard = RunawayGuard(cfg["reasoning_budget_chars"], body["max_tokens"] * 5,
                             cfg["wall_s_per_turn"], clock, thinking_on)
        try:
            reply = transport.complete(body, guard)
        except TransportError as e:
            rec["error"] = _clip(e)
            sample["rounds"].append(rec)
            sample["status"] = "transport_error"
            log(f"[fixed-lane] sample {idx} round {r}: transport error {_clip(e)}")
            break
        u = reply.usage or {}
        pt = int(u.get("prompt_tokens") or sum(len(m["content"]) for m in messages) // 4)
        ct = int(u.get("completion_tokens") or (len(reply.content) + len(reply.reasoning)) // 4)
        sample["prompt_tokens"] += pt
        sample["completion_tokens"] += ct
        sample["tokens"] += pt + ct
        rec["finish_reason"] = reply.finish_reason
        visible, tinfo = strip_think(reply.content)
        if reply.cut or (tinfo["unterminated"] and not visible):
            cut = reply.cut or "unterminated_think"
            rec["cut"] = cut
            sample["rounds"].append(rec)
            log(f"[fixed-lane] sample {idx} round {r}: turn cut ({cut})")
            if think != "off":
                think = "off"        # the thinking IS the problem: next turn without it
            messages.append({"role": "user", "content": reflection_message(
                fmt, ws.read_all(), order, [], [], None, False, cfg, cut=cut)})
            last_cut = cut
            continue
        last_cut = None
        files_now = ws.read_all()
        if fmt == "sr":
            edits, perrs = parse_search_replace(visible, order[0] if len(order) == 1 else None)
            res = apply_search_replace(files_now, edits, order, protected) if edits else \
                {"new_files": {}, "applied": [], "failed": []}
            if reply.finish_reason == "length" and not perrs and edits:
                perrs.append("reply hit the output limit; the last block may be incomplete")
        else:
            replies, perrs = parse_whole_files(visible, order)
            res = apply_whole(files_now, replies, order, reply.finish_reason, cfg, protected) if replies or perrs else \
                {"new_files": {}, "applied": [], "failed": []}
            if reply.finish_reason == "length":
                perrs = []
        accepted, rejected = guard_new_files(files_now, res["new_files"], guard_fn, run)
        # a file the guard rejected counts as failed edits
        apply_errors = [f"[block {f['n']}] {f['error']}" if f["n"] else f["error"] for f in res["failed"]]
        apply_errors += [f"malformed reply: {e}" for e in perrs]
        guard_errors = [x for rs in rejected.values() for x in rs]
        if not edits_present(fmt, res, perrs) and not apply_errors and not guard_errors:
            apply_errors.append("your reply contained no SEARCH/REPLACE blocks" if fmt == "sr"
                                else "your reply contained no file blocks")
        rec["applied"] = len(accepted)
        rec["apply_failed"] = len(res["failed"]) + len(perrs)
        rec["guard_rejected"] = len(rejected)
        for p, txt in accepted.items():
            ws.write(p, txt)
        if accepted:
            sample["applied_any"] = True
            rc, out = runner(task["verify"], task["cwd"], vtimeout)
            rec["verify_rc"] = rc
            sample["last_verify_output"] = out
            sample["fail_score"] = _fail_score(out)
            if rc == 0:
                sample["passed"], sample["status"] = True, "passed"
                sample["rounds"].append(rec)
                break
            vout = out
        else:
            vout = None
        sample["rounds"].append(rec)
        messages.append({"role": "assistant", "content": visible[:8000]})
        messages.append({"role": "user", "content": reflection_message(
            fmt, ws.read_all(), order, apply_errors, guard_errors, vout, bool(accepted), cfg)})
    if sample["status"] == "red" and not sample["applied_any"]:
        sample["status"] = "apply_failed"
    final = ws.read_all()
    sample["files"] = {p: t for p, t in final.items() if t is not None and t != (ws.orig.get(p) or "")} \
        if sample["applied_any"] else {}
    d, h, n = ws.diff(final)
    sample["diff"], sample["hunks"], sample["diff_lines"] = d, h, n
    sample["wall_s"] = round(clock() - t0, 3)
    return sample


def edits_present(fmt, res, perrs):
    return bool(res["applied"] or res["failed"] or res["new_files"] or perrs)


# ---------------------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------------------
def selection_key(s):
    """Lower is better. Verify pass first; ties -> smallest diff, then fewest hunks, then
    fewer tokens. Non-passing samples: ones that landed edits beat ones that did not, then
    fewer failure lines in the last verify output, then smaller diff. No majority vote."""
    if s["passed"]:
        return (0, s["diff_lines"], s["hunks"], s["tokens"], s["index"])
    if s["applied_any"]:
        return (1, s["fail_score"], s["diff_lines"], s["hunks"], s["index"])
    return (2, 0, 0, 0, s["index"])


def select_best(samples):
    return min(samples, key=selection_key) if samples else None


def summarize_sample(s, with_files=False):
    out = {k: s[k] for k in ("index", "temperature", "passed", "status", "applied_any", "tokens", "hunks",
                             "diff_lines", "fail_score", "wall_s") if k in s}
    out["rounds"] = s["rounds"]
    out["diff"] = s["diff"]
    if with_files:
        out["files"] = s["files"]
        out["last_verify_output"] = s["last_verify_output"]
    return out


# ---------------------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------------------
def run_lane(task, *, model=None, cfg=None, overrides=None, transport=None, host=None, stream=True,
             runner=None, clock=time.time, sleep=None, log=None, guard_fn=None, run=None, k=None,
             keep_state="auto"):
    """Run the lane on one task. Returns a JSON-serialisable LaneResult dict:
      status: passed | exhausted | apply_failed | transport_error | ineligible | already_green
      reason: one of FIXED_LANE_FAILURE_REASONS for the three failure statuses, else None
      best / samples / handoff / tokens / wall_s / events
    keep_state: 'auto' = leave the winning edit applied on a pass, the clean baseline
    otherwise; 'best' = always leave the best sample's edit applied; 'baseline' = never."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    runner = runner or default_runner
    model = resolve_model(model)
    cfg = dict(cfg) if cfg else lane_config(model, overrides)
    if k is not None:
        cfg["k"] = int(k)
    t_start = clock()
    cwd = task["cwd"]
    result = {"status": None, "reason": None, "model": model, "config": {x: cfg[x] for x in
              ("format", "k", "reflections", "thinking", "temperatures")}, "samples": [], "best": None,
              "handoff": None, "tokens": 0, "wall_s": 0.0, "eligibility": None, "events": []}
    ok, why = lane_eligible(task, cfg)
    result["eligibility"] = {"eligible": ok, "reasons": why}
    if not ok:
        result["status"] = ST_INELIGIBLE
        return result
    order = []
    for t in task["targets"]:
        rel = _norm_rel(cwd, t)
        if rel and rel not in order:
            order.append(rel)
    ws = Workspace(cwd, order)
    vtimeout = min(float(task.get("verify_timeout_s") or cfg["max_verify_s"]), 600)
    rc0, out0 = runner(task["verify"], cwd, vtimeout)
    if rc0 == 0:
        result["status"] = ST_ALREADY_GREEN
        result["wall_s"] = round(clock() - t_start, 3)
        return result
    try:
        prof = _mp.get_profile(model, "non_thinking", "darkbloom")
        ctx_tokens = prof.get("ctx") or 16384
        ctx = build_context(task, cfg, ws.read_all(), order, out0, ctx_tokens)
    except ContextTooLarge as e:
        result["status"] = ST_INELIGIBLE
        result["eligibility"] = {"eligible": False, "reasons": [f"context too large: {e}"]}
        return result
    transport = transport or OpenAITransport(host=host, stream=stream, sleep=sleep, clock=clock)
    temps = list(cfg["temperatures"]) or [None]
    samples = []
    for i in range(max(1, int(cfg["k"]))):
        if i and clock() - t_start > float(cfg["max_wall_s"]):
            result["events"].append(f"wall cap {cfg['max_wall_s']}s reached before sample {i}")
            break
        temperature = temps[min(i, len(temps) - 1)]
        s = run_sample(i, task, cfg, ws, order, ctx, model, transport, runner, clock, log, temperature,
                       guard_fn, run)
        samples.append(s)
        log(f"[fixed-lane] sample {i}: {s['status']} diff_lines={s['diff_lines']} tokens={s['tokens']}")
        if s["status"] == "transport_error" and not s["applied_any"] and i == 0 and \
                all(r.get("error") for r in s["rounds"]):
            break                      # endpoint down: more samples will not help
        if s["passed"] and cfg.get("stop_at_first_pass", True):
            break
    best = select_best(samples)
    result["samples"] = [summarize_sample(s) for s in samples]
    result["tokens"] = sum(s["tokens"] for s in samples)
    result["selected_index"] = best["index"] if best else None
    result["best"] = summarize_sample(best, with_files=True) if best else None
    if best and best["passed"]:
        result["status"] = ST_PASSED
        if keep_state != "baseline" and samples[-1] is not best:
            ws.restore()
            for p, t in best["files"].items():
                ws.write(p, t)
        elif keep_state == "baseline":
            ws.restore()
    else:
        if best and best["status"] == "transport_error" and not any(s["applied_any"] for s in samples):
            result["status"], result["reason"] = "transport_error", FIXED_LANE_TRANSPORT_ERROR
        elif not any(s["applied_any"] for s in samples):
            result["status"], result["reason"] = "apply_failed", FIXED_LANE_APPLY_FAILED
        else:
            result["status"], result["reason"] = "exhausted", FIXED_LANE_EXHAUSTED
        ws.restore()
        if keep_state == "best" and best and best["applied_any"]:
            for p, t in best["files"].items():
                ws.write(p, t)
        result["handoff"] = {
            "reason": result["reason"],
            "seed_diff": best["diff"] if best else "",
            "seed_files": best["files"] if best else {},
            "last_verify_output": tail_text(best["last_verify_output"] if best and best["last_verify_output"] else out0, 4096),
            "baseline_verify_output": tail_text(out0, 4096),
            "note": ("Open-loop worker: the seed_diff is the lane's best attempt (apply it with `git apply` "
                     "or write seed_files); the last verify output shows what is still red."),
        }
    result["wall_s"] = round(clock() - t_start, 3)
    return result


# ---------------------------------------------------------------------------------------
def _cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("eligible", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--task", required=True)
        sp.add_argument("--model")
    r = sub.choices["run"]
    r.add_argument("--k", type=int)
    r.add_argument("--host")
    r.add_argument("--no-stream", action="store_true")
    r.add_argument("--thinking", choices=("off", "on", "low"))
    r.add_argument("--keep-state", default="auto", choices=("auto", "best", "baseline"))
    a = ap.parse_args(argv)
    task = json.load(open(a.task))
    task.setdefault("cwd", os.getcwd())
    if a.cmd == "eligible":
        ok, why = lane_eligible(task, lane_config(resolve_model(a.model)))
        print(json.dumps({"eligible": ok, "reasons": why}))
        return 0 if ok else 1
    res = run_lane(task, model=a.model, k=a.k, host=a.host, stream=not a.no_stream,
                   overrides={"thinking": a.thinking}, keep_state=a.keep_state)
    print(json.dumps(res, indent=1))
    return 0 if res["status"] in (ST_PASSED, ST_ALREADY_GREEN) else 1


if __name__ == "__main__":
    sys.exit(_cli())
