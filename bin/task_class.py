#!/usr/bin/env python3
"""task_class -- shared task-class router + lane resolution for the harness-authoring front end
(ollama-dispatch-auto / -slice / -preflight).  Wave 3, 2026-10-09.

classify_task(...) is PURE: it reads only what it is given (spec text, target, extra edit files,
language, and a small `base` dict describing the base tree) and returns

    {"class": "create"|"modify"|"port"|"ui_dom"|"cross_file", "signals": [..], "budgets": {..}}

The class picks (1) a per-class author-prompt variant (`prompt_variant`) and (2) per-class
authoring budgets (`budgets`: iterations per stage, continuation rounds, stage re-prompts).
The class is recorded in .dispatch-harness.json under "task_class" (`record_task_class`).

resolve_lane / lane_args: the `--lane open|fixed|auto` flag.  `open` (default) is today's
tool loop; `fixed` and `auto` hand the worker `--mode fixed|open` (the fixed-lane seam,
~/bin/fixed_lane.py, is owned by the worker).  The flag is only forwarded when the queue's
`enqueue` actually accepts --mode (probe), so a queue without the seam is never broken by it.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CLASSES = ("create", "modify", "port", "ui_dom", "cross_file")
LANES = ("open", "fixed", "auto")

# stage -> max worker iterations, per class. `continue_rounds` = legacy continuation rounds
# after the staged flow, `reprompts` = no-tool-call/no-write re-prompts per stage.
BUDGETS = {
    "create":     {"task": 6, "fixture": 14, "refimpl": 12, "continue_rounds": 2, "reprompts": 2},
    "modify":     {"task": 6, "fixture": 14, "refimpl": 12, "continue_rounds": 3, "reprompts": 2},
    "port":       {"task": 8, "fixture": 14, "refimpl": 18, "continue_rounds": 3, "reprompts": 2},
    "ui_dom":     {"task": 8, "fixture": 20, "refimpl": 14, "continue_rounds": 3, "reprompts": 2},
    "cross_file": {"task": 8, "fixture": 16, "refimpl": 20, "continue_rounds": 3, "reprompts": 3},
}

_UI_RE = re.compile(
    r"querySelector|document\.(?:get|create|query)|\bDOM\b|content[- ]script|userscript|"
    r"MutationObserver|innerHTML|textContent|addEventListener|page\.evaluate|\bselectors?\b|"
    r"\bwidget\b|\bmodal\b|\bbutton\b|<div|\bCSS\b|\bwindow\.", re.I)
_UI_TARGET_RE = re.compile(r"\.(?:tsx|jsx|html|vue|svelte|css)$|(?:^|/)(?:content|contentScript|"
                           r"popup|components?)(?:/|\.)", re.I)
_PORT_RE = re.compile(
    r"\bport(?:ed|ing)?\b[^.\n]{0,40}\b(?:from|of|to)\b|\btranslat(?:e|ed|ing)\b[^.\n]{0,40}\b(?:from|to)\b|"
    r"\breference implementation\b|\bmirror(?:s|ing)?\b[^.\n]{0,30}\b(?:in|of|from)\b|"
    r"\bsame (?:logic|behaviou?r|algorithm) as\b|\brewrite\b[^.\n]{0,40}\bin (?:python|typescript|javascript|rust|go|swift)\b|"
    r"\bcopy (?:the )?(?:logic|behaviou?r)\b", re.I)
_CREATE_RE = re.compile(
    r"\b(?:create|add|introduce|write|build)\s+(?:a\s+|an\s+|the\s+)?new\s+(?:module|file|helper|"
    r"library|lib|service|component|function|class|script|endpoint|route)\b|\bnew (?:file|module)\b", re.I)
_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,5}|[\w-]+\.(?:py|ts|tsx|js|jsx|mjs|swift|rs|go|cs|sh))\b")


def _paths(text):
    return sorted({m.group(1) for m in _PATH_RE.finditer(text or "")})


def classify_task(intent="", interface="", target="", edit_files=(), lang="python", base=None):
    """PURE. `base` (all optional): {"target_exists": bool, "target_lines": int,
    "creation_task": bool}.  First matching rule wins: ui_dom > port > cross_file > create > modify."""
    base = base or {}
    text = " ".join(str(x) for x in (intent, interface) if x)
    extras = [f for f in (edit_files or ()) if f and f != target]
    sig = []
    ui_hits = _UI_RE.findall(text)
    if _UI_TARGET_RE.search(target or "") or len(ui_hits) >= 2 or (ui_hits and re.search(r"\.(?:tsx|jsx)\b", text)):
        sig.append(f"ui: target={target!r} words={sorted({h.lower() for h in ui_hits})[:4]}")
        cls = "ui_dom"
    elif _PORT_RE.search(text):
        sig.append("port: " + _PORT_RE.search(text).group(0)[:60])
        cls = "port"
    elif extras or len([p for p in _paths(text) if p != target]) >= 3:
        sig.append(f"cross_file: extra_edit_files={extras[:4]} paths_in_spec={len(_paths(text))}")
        cls = "cross_file"
    elif (base.get("creation_task") or base.get("target_exists") is False or _CREATE_RE.search(text)):
        sig.append("create: " + ("manifest creation_task" if base.get("creation_task") else
                                 "target absent at base" if base.get("target_exists") is False else
                                 "spec says new module/file"))
        cls = "create"
    else:
        sig.append("modify: existing target"
                   + (f" ({base['target_lines']} lines)" if base.get("target_lines") else ""))
        cls = "modify"
    return {"class": cls, "signals": sig[:4], "budgets": dict(BUDGETS[cls])}


def prompt_variant(cls):
    """PURE. A short per-class paragraph appended to each authoring stage prompt."""
    return {
        "create": ("## Task class: CREATE a new module\nThe target does not exist at the base "
                   "commit. `refimpl.py` must WRITE the whole file (OLD is empty / the file is "
                   "created). Do not look for an existing implementation to anchor on, and do "
                   "not demand literals from files that are not there.\n"),
        "modify": ("## Task class: MODIFY an existing file\nThe target exists at base. `refimpl.py` "
                   "edits it with exact OLD anchors copied from the BASE text (`git show HEAD:<file>`), "
                   "never from a later or hand-edited copy. Read the target once, then write.\n"),
        "port": ("## Task class: PORT from a reference\nThe behaviour already exists in a "
                 "reference named in the spec. Read the reference ONCE, then write; the fixture "
                 "asserts the reference's observable behaviour on inputs it names, not its "
                 "structure. Do not re-derive the algorithm in prose.\n"),
        "ui_dom": ("## Task class: UI / DOM\nUse the sealed `dispatch-env` fake DOM provided in this "
                   "worktree; never hand-roll a document/window. Assert on the resulting DOM "
                   "state or emitted events, with one case per selector/state the spec names.\n"),
        "cross_file": ("## Task class: CROSS-FILE change\nSeveral files change together. `refimpl.py` "
                       "must edit EVERY one from its BASE text; the fixture must fail if any one "
                       "file's change is missing (one assertion per file).\n"),
    }.get(cls, "")


def record_task_class(wt, rec, lane=None):
    """Merge {"task_class": {...}} into <wt>/.dispatch-harness.json atomically. Returns True when
    written. Never raises (the manifest may not exist yet)."""
    try:
        p = Path(wt) / ".dispatch-harness.json"
        d = json.loads(p.read_text()) if p.is_file() else {}
        if not isinstance(d, dict):
            return False
        d["task_class"] = dict(rec, **({"lane": lane} if lane else {}))
        tmp = p.with_name(p.name + ".tc.tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def read_task_class(wt):
    """The recorded task-class dict, or None."""
    try:
        d = json.loads((Path(wt) / ".dispatch-harness.json").read_text())
        tc = d.get("task_class")
        return tc if isinstance(tc, dict) and tc.get("class") in CLASSES else None
    except Exception:
        return None


def base_facts(wt, target):
    """Facts about the BASE tree (HEAD of the scaffolded worktree) for classify_task."""
    out = {}
    try:
        man = json.loads((Path(wt) / ".dispatch-harness.json").read_text())
        out["creation_task"] = bool(man.get("creation_task"))
    except Exception:
        pass
    if target:
        try:
            r = subprocess.run(["git", "-C", str(wt), "show", f"HEAD:{target}"],
                               capture_output=True, text=True, timeout=30)
            out["target_exists"] = r.returncode == 0
            if r.returncode == 0:
                out["target_lines"] = r.stdout.count("\n")
        except Exception:
            pass
    return out


# ---- lane -------------------------------------------------------------------------------
def resolve_lane(lane, cls=None):
    """PURE. 'open' | 'fixed' for the worker's --mode.  `auto` picks the fixed repair lane
    only for a single-file MODIFY; every other class (and an unknown class) stays open."""
    lane = (lane or "open").lower()
    if lane not in LANES:
        raise ValueError(f"--lane must be one of {LANES}, got {lane!r}")
    if lane == "auto":
        return "fixed" if cls == "modify" else "open"
    return lane


_MODE_SUPPORT = {}


def queue_supports_flag(flag, queue_path=None, _run=None):
    """True when `ollama-queue.py enqueue --help` lists `flag` (e.g. --mode, --fixed-target: the
    worker fixed-lane seam, plumbed through the queue). Cached per (path, flag). Fail-closed."""
    qp = str(queue_path or os.path.expanduser("~/bin/ollama-queue.py"))
    key = (qp, flag)
    if key in _MODE_SUPPORT and _run is None:
        return _MODE_SUPPORT[key]
    try:
        run = _run or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=60))
        r = run([sys.executable, qp, "enqueue", "--help"])
        ok = flag in ((r.stdout or "") + (r.stderr or ""))
    except Exception:
        ok = False
    if _run is None:
        _MODE_SUPPORT[key] = ok
    return ok


def queue_supports_mode(queue_path=None, _run=None):
    return queue_supports_flag("--mode", queue_path, _run)


def lane_args(lane, cls=None, queue_path=None, _supports=None, targets=(), _supports_targets=None):
    """Extra `enqueue` argv for the lane: [] for the open lane (today's behaviour, byte-identical)
    or when the queue does not accept --mode; else ['--mode', 'fixed'|'open'] plus, for the fixed
    lane, one explicit `--fixed-target FILE` per declared target file (so the worker does not have
    to parse TASK.md) when the queue accepts that flag too."""
    eff = resolve_lane(lane, cls)
    if (lane or "open").lower() == "open":
        return []
    sup = _supports if _supports is not None else queue_supports_mode(queue_path)
    if not sup:
        return []
    out = ["--mode", eff]
    if eff == "fixed" and targets:
        st = (_supports_targets if _supports_targets is not None
              else queue_supports_flag("--fixed-target", queue_path))
        if st:
            for tg in targets:
                if tg:
                    out += ["--fixed-target", str(tg)]
    return out
