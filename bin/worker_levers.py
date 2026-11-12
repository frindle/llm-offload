#!/usr/bin/env python3
"""worker_levers.py -- four OPT-IN convergence levers for ollama-worker.py CODING runs (2026-10-10).

WHY. Two failed A/B-baseline jobs (absamp-10102153-aA-s1, qwen3.6-35b-a3b via Darkbloom):
  * 1f9ef6805037 (30 iters): 20 read_file calls on a 521-line file in 10-40 line windows, never ran the
    verify mid-run, ended 0/6 tests because the edits never carried the new counters.
  * 76d747232905 (20 iters): ran `python3 refimpl.py .` (the sealed REFERENCE SOLUTION), `bash verify.sh`
    passed at iteration 5, then `git checkout -- dashboard_chat.py` reverted its own passing state and spent
    the remaining iterations editing refimpl.py instead of the target.

THE SWITCHES (all default OFF except NOTHINK_RUN, default ON since 2026-10-15; unset == byte-identical to the pre-lever worker for the others). Each is an env var read AT
CALL TIME, and a per-job passthrough with no queue change exists: a `Worker-flags: K=1 K=1` line in the task
text (like `Sampling-profile:`) sets the env for that worker process (allow-listed names, "0"/"1" only,
never overrides an env var that is already set).
  VERIFY_ON_EDIT=1   after a successful edit_file/write_file to a non-harness file, run the task verify on every
                     2nd such edit (max 6 runs/run; skipped once any verify took > 120 s) and append a compact
                     failing-cases summary (<= 1500 chars) to that tool result. On PASS the result says so and
                     tells the model to call task_complete. Uses the worker's _quick_verify, so the existing
                     EDIT_CHECKPOINT snapshot hook fires on a pass exactly as before (not duplicated).
  PROTECT_HARNESS=1  ONLY for a coding job over a SEALED harness (HEAD subject "seal dispatch harness...", a
                     verify.sh plus refimpl.py / a fixture in the tree, not an authoring/refine task, not the
                     harness self-check verify): refimpl.py is invisible (list_files / read_file / edit / write /
                     run_bash discovery) and `python3 refimpl.py` is refused; edit_file / write_file / shell
                     writes / git checkout|restore|stash against the harness files (verify.sh, verify.test.*,
                     test_fixture.py, check_literals.py, TASK.md, refimpl.py) are refused with an instructive
                     message; whole-tree reverts (git stash / reset --hard / clean / checkout .) are refused, and
                     so is reverting any file while the last verify run PASSED (the 76d747232905 failure).
                     (refimpl.py is NOT moved out of the tree: a tracked deletion would show in every
                     worktree-diff guard; verify.sh never needs it -- checked on all 21 replay tasks.)
  READ_MIN_WINDOW=1  read_file with 0 < length < 120 is served >= 120 lines (or to EOF) and the result notes the
                     real range.

  PROSE_STICKY_NOTHINK=1  (L4, 2026-10-11) after the FIRST prose-runaway abort / output-cap cut-off with no tool call in a
                     run, the recovery request profile (thinking OFF + the recovery sampling) is kept for ALL
                     remaining author turns instead of one. Evidence: absamp aA-s1 a0be9/c8f4f (prose_loop at ~9/20):
                     thinking-ON turns loop ~3000 tokens in a planning block, the one-shot recovery turn succeeds
                     in ~50 tokens, the next thinking-ON turn loops again. Turns before the first abort and every
                     non-author role are untouched; the worker owns the two gated call sites.

  READONLY_JOB=1     (2026-10-11) a read-only investigation job (qagent run --readonly stamps this in its task preamble):
                     worker_robust.loop_config disables the nav_streak detector ("N navigation calls and still no
                     edit"), which is meaningless when the job must never edit. Identical-call loops, prose runaway,
                     no_progress, the wall cap and max-iters are untouched. Default off = coding runs unchanged.

  PROSE_ACT_NUDGE=1  (2026-10-11, A/B-measure before enabling) when the prose-runaway guard aborts a turn and the run has made NO
                     edit yet (worktree still byte-identical to dispatch start), the injected correction additionally
                     says: stop analysing the tests, make your best first edit now, then run bash verify.sh and fix from
                     the failing assertion. Evidence: Amazon task, 3 runs looped re-deriving a test contradiction with
                     zero edits. Default off = the existing nudge text unchanged.

  PROSE_PRESENCE_PENALTY=1  (2026-10-12, A/B arm B, measure before enabling) author-role requests carry
                     presence_penalty=1.0 (the Qwen cards allow 0-2 "to reduce endless repetitions"; profile value is 0.0)
                     for the whole run. Default off = the profile value, request bodies byte-identical.
  NOTHINK_RUN        (2026-10-12, A/B arm C) enable_thinking=false for EVERY author turn of the run (unlike
                     PROSE_STICKY_NOTHINK, which waits for the first runaway). DEFAULT ON since 2026-10-15 (the one
                     default-on lever); NOTHINK_RUN=0/false/off restores the old request bodies byte-for-byte. Author
                     role only. With it on, PROSE_STICKY_NOTHINK is a no-op (redundant).
                     AUTO-REVERT GUARD (decided 2026-10-15): if the first 20 live author runs after the flip pass below
                     75% (arm A baseline ~79%) or a NEW failure class appears, set NOTHINK_RUN=0 (queue daemon env /
                     launchd EnvironmentVariables, or a per-job `Worker-flags: NOTHINK_RUN=0` task line) and report.
                     The other arm C guards (early_action, green_hold, green_restore) stay on hold.

  REASONING_PARSER=qwen3  (2026-10-12, A/B first, DEFAULT OFF; the only flag whose value is not 0/1) Darkbloom's LOCAL endpoint
                     delivers Qwen3.6's thinking INSIDE message.content (reasoning_content empty) unless the request carries
                     `reasoning_parser`; with it, reasoning arrives in reasoning_content and content is the clean answer, which
                     makes reasoning_budget_chars / the reasoning-loop guard / --preserve-reasoning see it and removes the
                     "prose runaway" class. model_profile.reasoning_parser_for sends the field only if the model's profile
                     (model_profiles.yaml `reasoning_parser:`, card-cited) declares the same parser for a darkbloom backend,
                     only on the openai-style path to the Darkbloom lane, only on turns with thinking not off. Residual leak
                     (probed): a NON-streaming turn cut at max_tokens inside an unclosed think block keeps the thinking in
                     content with reasoning empty -- unclosed_think_cutoff() detects it; the worker treats it as a cutoff and
                     never replays it. Default off = request bodies byte-identical.

PURE apart from the module-level state dicts (reset per run by arm_run) and the one `git log` in
sealed_coding_harness. No import of the worker; the worker owns the (tiny, gated) call sites."""
import fnmatch
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

FLAGS = ("VERIFY_ON_EDIT", "PROTECT_HARNESS", "READ_MIN_WINDOW", "PROSE_STICKY_NOTHINK", "READONLY_JOB", "PROSE_ACT_NUDGE",
         "PROSE_PRESENCE_PENALTY", "NOTHINK_RUN", "REASONING_PARSER")
REASONING_PARSER_VALUES = ("qwen3",)      # the only non-0/1 flag value; the profile must declare the same parser
MIN_WINDOW_LINES = 120
VOE_EVERY = 2
VOE_MAX_RUNS = 6
VOE_SKIP_IF_SLOWER_S = 120.0
VOE_MAX_CHARS = 1500

HARNESS_FILES = ("verify.sh", "test_fixture.py", "check_literals.py", "TASK.md", "refimpl.py",
                 ".dispatch-harness.json")
HARNESS_GLOBS = ("verify.test.*",)
HIDDEN_FILES = ("refimpl.py",)


def flag(name, environ=None) -> bool:
    return (os.environ if environ is None else environ).get(name) == "1"


# --------------------------------------------------------------------------- task-text passthrough
_MARKER_RE = re.compile(r"(?im)^[ \t]*Worker-flags:[ \t]*(.+?)[ \t]*$")


def apply_task_flags(task, environ=None, log=None):
    """`Worker-flags: VERIFY_ON_EDIT=1 PROTECT_HARNESS=1` line(s) in the task text -> env (allow-listed, 0/1).
    An env var that is already set wins. -> the list of KEY=VAL actually applied."""
    environ = os.environ if environ is None else environ
    applied = []
    for line in _MARKER_RE.findall(task or ""):
        for tok in re.split(r"[,\s]+", line.strip()):
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            k, v = k.strip().upper(), v.strip()
            if k == "REASONING_PARSER" and v in REASONING_PARSER_VALUES and k not in environ:
                environ[k] = v
                applied.append(f"{k}={v}")
                continue
            if k not in FLAGS or v not in ("0", "1") or k in environ:
                continue
            environ[k] = v
            applied.append(f"{k}={v}")
    if applied and log:
        log(f"[worker] WORKER FLAGS {' '.join(applied)} (from the task's Worker-flags line).")
    return applied


# --------------------------------------------------------------------------- PROSE_ACT_NUDGE
ACT_NUDGE_TEXT = ("Stop analysing the tests. Make your best first edit now with the edit tool, then run "
                  "bash verify.sh and read the failing assertion; fix from that output.")


def act_nudge_suffix(no_edit_yet, environ=None) -> str:
    """'' unless PROSE_ACT_NUDGE=1 AND the run has made no edit yet; else the text to append to the nudge."""
    return (" " + ACT_NUDGE_TEXT) if (no_edit_yet and flag("PROSE_ACT_NUDGE", environ)) else ""


# --------------------------------------------------------------------------- A/B levers (2026-10-12)
PRESENCE_PENALTY_VALUE = 1.0


def presence_penalty_override(role, environ=None):
    """1.0 when PROSE_PRESENCE_PENALTY=1 and the role is the author; else None (= keep the profile value)."""
    return PRESENCE_PENALTY_VALUE if (flag("PROSE_PRESENCE_PENALTY", environ) and (role or "author") == "author") else None


def flag_default_on(name, environ=None) -> bool:
    """Default-ON switch: True unless the env var is explicitly 0 / false / off (case-insensitive, trimmed)."""
    v = (os.environ if environ is None else environ).get(name)
    return v is None or str(v).strip().lower() not in ("0", "false", "off")


def nothink_run(role, environ=None) -> bool:
    """True for the author role unless NOTHINK_RUN is explicitly 0/false/off: thinking is off for the whole run.
    DEFAULT ON since 2026-10-15 (A/B bundle prose-sticky-ab2-20261014: arm C 14/14 first-attempt pass vs 11/14,
    prose-runaway runs 0/14 vs 11/14). NOTHINK_RUN=0 (env, or `Worker-flags: NOTHINK_RUN=0`) restores the old behaviour."""
    return flag_default_on("NOTHINK_RUN", environ) and (role or "author") == "author"


def reasoning_parser_request(environ=None):
    """The parser name the REASONING_PARSER lever asks for ('qwen3'), else None (lever off / unknown value)."""
    v = (os.environ if environ is None else environ).get("REASONING_PARSER")
    return v if v in REASONING_PARSER_VALUES else None


_COT_MARKERS = re.compile(r"(?i)(thinking process|thinking sequence|let me (check|think|re-?read|look|analy[sz]e)|"
                          r"\bwait,|\bokay,|the user (wants|is asking|asked)|\bi need to\b|\bdeconstruct\b|"
                          r"\bhmm\b|let's (see|check|think|analy[sz]e))")
UNCLOSED_THINK_HUGE_CHARS = 12000


def unclosed_think_cutoff(msg, done_reason, usage, max_tokens, parser_on, thinking_on=True):
    """PURE. True when the reasoning parser was requested for a thinking turn yet the turn's THINKING is sitting in
    `content`: no reasoning came back, no tool call, no `</think>` (the closed form is already split by the
    worker's _strip_inline_think), and either the turn was cut at the output cap (finish_reason length /
    completion >= max_tokens -- probed: a non-streaming turn cut mid-think keeps the unclosed block in content) or
    it is huge and reads as chain of thought. Such a turn is a cutoff / runaway, never an answer or history."""
    if not parser_on or not thinking_on:
        return False
    if msg.get("tool_calls") or msg.get("thinking") or msg.get("reasoning") or msg.get("reasoning_content"):
        return False
    c = msg.get("content") or ""
    if not c.strip() or "</think>" in c:
        return False
    if str(done_reason or "").lower() == "length":
        return True
    ct = (usage or {}).get("completion_tokens") or 0
    if max_tokens and ct >= max_tokens:
        return True
    return len(c) >= UNCLOSED_THINK_HUGE_CHARS and bool(_COT_MARKERS.search(c[:4000]))


# --------------------------------------------------------------------------- L3 READ_MIN_WINDOW
def min_window_length(ln):
    """The effective read_file length: raised to MIN_WINDOW_LINES when READ_MIN_WINDOW=1 and the call asked
    for 0 < length < MIN. None / 0 / negative (= 'default page') and every length >= MIN pass through."""
    if flag("READ_MIN_WINDOW") and ln is not None and 0 < ln < MIN_WINDOW_LINES:
        return MIN_WINDOW_LINES
    return ln


def min_window_note(asked, start, last, total) -> str:
    return (f"[READ_MIN_WINDOW: you asked for {asked} line(s); reads are served at least "
            f"{MIN_WINDOW_LINES}, so lines {start}-{last} of {total} are shown. Ask for >= "
            f"{MIN_WINDOW_LINES} lines per read, or use run_bash `grep -n` to locate a line first.]")


# --------------------------------------------------------------------------- L4 PROSE_STICKY_NOTHINK
def l4_note_abort(role, enable_thinking, log=None) -> bool:
    """Called by the worker on every prose-runaway abort / output-cap cut-off with no tool call (after the
    one-shot recovery is queued). The FIRST one in a run engages the sticky recovery profile -- only when the
    flag is on, the role is the author, and the model profile has a thinking switch (enable_thinking is not
    None). -> True when sticky is (now or already) engaged."""
    try:
        if _L4["sticky"]:
            return True
        if not flag("PROSE_STICKY_NOTHINK") or role != "author" or enable_thinking is None:
            return False
        if nothink_run(role):      # thinking is already off every turn: B is redundant, do not add its recovery profile
            return False
        _L4["sticky"] = True
        if log:
            log("[worker] L4: prose runaway seen - thinking stays OFF for the rest of this run")
        return True
    except Exception:
        return False


def l4_sticky() -> bool:
    """True from the first abort on (flag on) -> every later author turn uses the recovery request profile."""
    return bool(_L4["sticky"] and flag("PROSE_STICKY_NOTHINK"))


# --------------------------------------------------------------------------- shared run state
_VOE = {"edits": 0, "runs": 0, "last_secs": 0.0}
_PH = {"cwd": None, "verify": None, "passing": False}
_LAST_VERIFY_SECS = [0.0]
_L4 = {"sticky": False}


def note_verify_secs(secs):
    """Called by the worker's _quick_verify (always; pure bookkeeping) so L1 can skip a slow verify."""
    try:
        _LAST_VERIFY_SECS[0] = float(secs)
    except Exception:
        pass


def _norm(cwd):
    try:
        return str(Path(cwd).resolve())
    except Exception:
        return str(cwd)


# --------------------------------------------------------------------------- L2 PROTECT_HARNESS
def is_harness_rel(rel) -> bool:
    rel = str(rel).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if "/" in rel:
        return False                      # the sealed harness lives at the worktree root
    return rel in HARNESS_FILES or any(fnmatch.fnmatch(rel, g) for g in HARNESS_GLOBS)


def sealed_coding_harness(cwd, task, task_kind, verify, runner=None) -> bool:
    """A coding job over a SEALED harness (the replay/slicer coding path): not an authoring/refine/continue
    task, the verify is not the harness self-check, HEAD is the 'seal dispatch harness' commit, and the tree
    holds verify.sh plus the reference solution or a fixture. Anything unclear -> False (lever stays off)."""
    try:
        if task_kind != "coding" or not verify:
            return False
        head = (task or "").lstrip()[:80]
        if head.startswith(("# AUTHORING TASK", "# REFINE TASK", "# CONTINUE --")):
            return False
        if "auto-harness-check" in str(verify):
            return False
        c = Path(cwd)
        if not (c / "verify.sh").is_file():
            return False
        if not ((c / "refimpl.py").is_file() or (c / "test_fixture.py").is_file()
                or any(c.glob("verify.test.*"))):
            return False
        run = runner or subprocess.run
        r = run(["git", "log", "-1", "--format=%s"], cwd=str(c), capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and r.stdout.strip().startswith("seal dispatch harness")
    except Exception:
        return False


def ph_active(cwd) -> bool:
    return bool(flag("PROTECT_HARNESS") and _PH["cwd"] and _PH["cwd"] == _norm(cwd))


def _rel(cwd, path_arg):
    try:
        p = (Path(cwd) / str(path_arg)).resolve() if not os.path.isabs(str(path_arg)) else Path(path_arg).resolve()
        return str(p.relative_to(Path(cwd).resolve())).replace("\\", "/")
    except Exception:
        return None


def ph_hidden(cwd, child) -> bool:
    """list_files filter: is this directory entry (a Path under cwd) invisible?"""
    try:
        return ph_active(cwd) and _rel(cwd, child) in HIDDEN_FILES
    except Exception:
        return False


def ph_file_verdict(cwd, path_arg, op):
    """read_file / edit_file / write_file gate. op is 'read' or 'write'. -> an ERROR/REFUSED string or None."""
    try:
        if not ph_active(cwd):
            return None
        if op == "read":
            _dv = _deep_read_verdict(cwd, path_arg)            # L2 deep guard (PH_DEEP_GUARD=0 = off)
            if _dv:
                return _dv
        rel = _rel(cwd, path_arg)
        if rel is None:
            return None
        if rel in HIDDEN_FILES:
            return f"ERROR: file not found: {path_arg} -- use list_files to see what exists."
        if op == "write" and is_harness_rel(rel):
            return (f"REFUSED: {rel} is part of the sealed verification harness (verify.sh, verify.test.*, "
                    f"test_fixture.py, check_literals.py, TASK.md). It is NOT what you are asked to change: "
                    f"editing it invalidates the check. Read it if you need to, then fix the TARGET source "
                    f"file named in the task so the unchanged verify passes.")
        return None
    except Exception:
        return None


_SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_REDIR_HARNESS = re.compile(
    r"(?<![0-9&<])>>?\s*['\"]?(?:\./)?(?:verify\.sh|verify\.test\.[\w.]+|test_fixture\.py|check_literals\.py|TASK\.md|refimpl\.py)\b")
_MUTATORS = re.compile(r"\b(?:sed\s+(?:-\w*i|--in-place)|tee|mv|rm|cp|truncate|chmod|perl\s+-\w*i|ed|install)\b")
_HARNESS_WORD = re.compile(r"(?:\bverify\.sh|\bverify\.test\.[\w.]+|\btest_fixture\.py|\bcheck_literals\.py|\bTASK\.md|\brefimpl\.py)")
_GIT_REVERT_SUBS = ("checkout", "restore", "stash", "reset", "clean", "switch")


def _git_segments(command):
    for seg in _SEG_SPLIT.split(command or ""):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if "git" not in toks:
            continue
        rest = toks[toks.index("git") + 1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):      # git -C path / -c k=v / --no-pager
            i += 2 if rest[i] in ("-C", "-c") else 1
        if i < len(rest):
            yield rest[i], rest[i + 1:]


def _git_revert_verdict(sub, args, passing):
    """-> a REFUSED string or None for one `git <sub> <args>` that could discard working-tree state."""
    if sub not in _GIT_REVERT_SUBS:
        return None
    paths = [a for a in args if not a.startswith("-") and a != "--"]
    if sub == "stash":
        if paths and paths[0] in ("list", "show"):
            return None
        return ("REFUSED: `git stash` would set your own edits aside and is not available in this task. Use "
                "`git diff` to inspect your changes and edit_file to adjust them.")
    if sub == "clean":
        return "REFUSED: `git clean` deletes files and is not available in this task."
    if sub == "reset":
        if "--hard" in args or "--merge" in args:
            return ("REFUSED: `git reset --hard` would discard all of your edits and is not available in this "
                    "task. Use `git diff` to inspect them and edit_file to change them.")
        return None
    if sub == "switch" or (sub == "checkout" and any(a in ("-b", "-B", "--orphan") for a in args)):
        return None
    if sub == "checkout" and paths and "--" not in args and not any(
            p in ("." , ":/") or _HARNESS_WORD.search(p) or "/" in p or "." in p for p in paths):
        return None                                        # `git checkout somebranch`
    harness = [p for p in paths if _HARNESS_WORD.search(p)]
    if harness:
        return (f"REFUSED: `git {sub}` of {', '.join(harness)} is not allowed: the verification harness is "
                f"sealed and must stay exactly as committed. Change the TARGET source file instead.")
    if any(p in (".", ":/", "*") for p in paths) or not paths:
        return (f"REFUSED: `git {sub}` over the whole tree would discard ALL of your edits and is not "
                f"available in this task. Use `git diff` to inspect your changes and edit_file to adjust them.")
    if passing:
        return (f"REFUSED: `git {sub} {' '.join(paths)}` would revert a file while the last verify run PASSED. "
                f"Your tree currently satisfies the verify -- do NOT discard it. Call task_complete (or, if "
                f"you really must change something, make a small edit_file and re-run the verify).")
    return None


def ph_bash_verdict(cwd, command):
    """run_bash gate. -> a REFUSED string or None."""
    try:
        if not ph_active(cwd):
            return None
        cmd = str(command or "")
        if re.search(r"refimpl", cmd, re.I):
            return ("REFUSED: refimpl.py is not one of your inputs in this task (it is the reference "
                    "solution the gate uses and it is not available to you). Implement the change yourself "
                    "in the target source file named in the task, then check it with `bash verify.sh`.")
        _dv = ph_deep_bash_verdict(cwd, cmd)                     # L2 deep guard (PH_DEEP_GUARD=0 = off)
        if _dv:
            return _dv
        for seg in _SEG_SPLIT.split(cmd):
            if _HARNESS_WORD.search(seg) and (_REDIR_HARNESS.search(seg) or _MUTATORS.search(seg)):
                return ("REFUSED: that command would modify the sealed verification harness (verify.sh, "
                        "verify.test.*, test_fixture.py, check_literals.py, TASK.md). Do not change it -- fix "
                        "the TARGET source file named in the task so the unchanged verify passes.")
        if _REDIR_HARNESS.search(cmd):
            return ("REFUSED: that command would overwrite a sealed harness file. Do not change it -- fix "
                    "the TARGET source file named in the task.")
        for sub, args in _git_segments(cmd):
            v = _git_revert_verdict(sub, args, _PH["passing"])
            if v:
                return v
        return None
    except Exception:
        return None


_SCRUB_TAIL = re.compile(r"(?:^|[\s/])refimpl\.py$")
_SCRUB_GREP = re.compile(r"^(?:\./)?refimpl\.py[:\-]\d+")


def ph_scrub(cwd, text):
    """Drop directory-listing / grep-hit lines that name refimpl.py from run_bash output (ls, find, git status,
    grep -r). Code lines that merely mention it in a comment are left alone."""
    text = ph_deep_scrub(cwd, text)                              # L2 deep guard (PH_DEEP_GUARD=0 = off)
    try:
        if not text or not ph_active(cwd) or "refimpl" not in text:
            return text
        keep = []
        for ln in text.splitlines(keepends=True):
            s = ln.strip()
            if "refimpl" in s and (_SCRUB_GREP.match(s) or (len(s.split()) <= 10 and _SCRUB_TAIL.search(s))):
                continue
            keep.append(ln)
        return "".join(keep)
    except Exception:
        return text


def ph_note_bash(cwd, command, rc):
    """A run_bash that CONTAINS the task verify and exits 0 -> the tree currently passes."""
    try:
        if not ph_active(cwd) or rc != 0:
            return
        v = " ".join(str(_PH.get("verify") or "").split())
        c = " ".join(str(command or "").replace("2>&1", " ").split())
        if v and c == v:          # exactly the verify (a pipe / && chain would mask its exit status)
            _PH["passing"] = True
    except Exception:
        pass


def ph_diag_verdict(cwd, requests) -> str | None:
    """request_diagnostics could `file`/`grep` refimpl.py: refuse any request that names it."""
    try:
        if ph_active(cwd) and "refimpl" in repr(requests):
            return ("ERROR: refimpl.py is not available in this task. Implement the change in the target "
                    "source file and check it with `bash verify.sh`.")
    except Exception:
        pass
    return ph_deep_diag_verdict(cwd, requests)                   # L2 deep guard (PH_DEEP_GUARD=0 = off)


# --------------------------------------------------------------------------- L2 deep guard (2026-10-14)
# WHY. The A/B review (prose-sticky-ab, 2026-10-14) found refimpl.py is COMMITTED into every sealed task tree; the L2 gate above
# hides the file NAME (list_files / read_file / any command that says "refimpl"), but probe_leaks.py showed 19 indirect paths to
# its CONTENT or its EXECUTION with PROTECT_HARNESS=1 armed: `git show HEAD` / `git log -p` / `git diff HEAD~1` (the seal-history
# patches), `cat r*` / `cat *.py` / `head *` / `grep -rh`, `git archive | tar -xO`, `git cat-file -p <blob>`, `python3 r*.py .`,
# `for f in *.py; do python3 $f`, `cp r*.py x && python3 x`, read_file of an in-tree copy, and request_diagnostics git show /
# log -p / grep (whose output the worker never scrubs). Also: other branches of the SOURCE repo (`git log --all`, `git show
# main:<target>`) carry the real, later fix. This block closes those paths. It is part of PROTECT_HARNESS (armed only for a sealed
# coding job, as above) and has its own kill switch: PH_DEEP_GUARD=0 (env) restores the pre-2026-10-14 L2 behaviour exactly.
# Content is matched, not names: an output span that reproduces >= PH_RUN_LINES consecutive lines of refimpl.py (>= PH_RUN_CHARS
# non-blank chars) is withheld UNLESS that same span exists in a tracked file of the working tree (so the model's OWN correct
# edit, or the unchanged base code refimpl quotes as OLD, is never hidden from it).
PH_RUN_LINES = 3
PH_RUN_CHARS = 60
PH_SCAN_MAX_BYTES = 1_000_000
_PH_DEEP = {"keys": None, "index": None, "blob": None, "tracked": None, "order": None}
_PH_FILE_CACHE = {}
_GLOB_CH = re.compile(r"[*?\[]")
_EXEC_COPY = re.compile(r"(?:^|[\s;&|(`])(?:python[\d.]*|pypy[\d.]*|sh|bash|zsh|dash|ksh|node|deno|bun|tsx|ts-node|perl|ruby|"
                        r"source|exec|eval|xargs|cp|mv|ln|rsync|install|tar|zip|ditto|dd|cpio|tee)(?=$|[\s;&|)`])")
_GREP_CMD = re.compile(r"(?:^|[\s;&|(`])(?:grep|egrep|fgrep|rg|ag|ack)(?=$|\s)")
_FOREIGN_OPTS = ("--all", "--branches", "--remotes", "--tags", "--glob", "--reflog", "-g", "--walk-reflogs")
_HISTORY_SUBS = ("show", "log", "diff", "cat-file", "archive", "ls-tree", "grep", "blame", "rev-list", "checkout", "restore",
                 "whatchanged", "format-patch", "shortlog", "annotate", "difftool", "range-diff", "merge-base", "name-rev")
_NAME_LINE_TOK = re.compile(r"^[\"'`|,:;()\[\]{}<>=]*(?:\S*/)?refimpl\.py[\"'`|,:;()\[\]{}<>=]*$")
_GREP_PREFIX = re.compile(r"^(?:\S*/)?refimpl\.py[:\-=]")
_DIFF_HDR = re.compile(r"^diff --(?:git|cc|combined) ")
_LN_PREFIX = re.compile(r"^\s*\d+(?:\t|:|-|\|)\s?")
_WITHHELD = "[PROTECT_HARNESS: {n} line(s) of the sealed reference solution withheld -- it is not one of your inputs]"


def ph_deep_active(cwd) -> bool:
    return ph_active(cwd) and os.environ.get("PH_DEEP_GUARD") != "0"


def _git_out(cwd, *args, timeout=15):
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def _deep_arm(cwd):
    """Fingerprint refimpl.py (working copy, else HEAD's blob) and the tracked-file list. Never raises."""
    _PH_DEEP.update(keys=None, index=None, blob=None, tracked=None, order=None)
    _PH_FILE_CACHE.clear()
    try:
        c = Path(cwd)
        text = None
        if (c / "refimpl.py").is_file():
            text = (c / "refimpl.py").read_text(errors="ignore")
        else:
            rc, out = _git_out(c, "show", "HEAD:refimpl.py")
            text = out if rc == 0 else None
        rc, out = _git_out(c, "rev-parse", "--verify", "--quiet", "HEAD:refimpl.py")
        _PH_DEEP["blob"] = out.strip() if rc == 0 and out.strip() else None
        rc, out = _git_out(c, "ls-files", "-z")
        _PH_DEEP["tracked"] = {p for p in out.split("\0") if p and p not in HIDDEN_FILES} if rc == 0 else set()
        if text:
            keys = [ln.strip() for ln in text.splitlines()]
            idx = {}
            for i, k in enumerate(keys):
                if k:
                    idx.setdefault(k, []).append(i)
            _PH_DEEP["keys"], _PH_DEEP["index"] = keys, idx
        # files refimpl names (its targets) are checked first by the legit-span test
        _PH_DEEP["order"] = sorted(_PH_DEEP["tracked"], key=lambda r: (r not in (text or ""), r))
    except Exception:
        pass


def _okeys(line):
    s = line.strip()
    m = _LN_PREFIX.match(line)
    return (s, line[m.end():].strip()) if m else (s,)


def _tracked_text(cwd, rel):
    """Stripped-line text of one tracked file, cached by (mtime, size) so a re-read only happens after an edit."""
    try:
        p = Path(cwd) / rel
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = _PH_FILE_CACHE.get(rel)
        if hit and hit[0] == key:
            return hit[1]
        txt = ("\n".join(x.strip() for x in p.read_text(errors="ignore").splitlines())
               if p.is_file() and st.st_size <= PH_SCAN_MAX_BYTES else "")
        _PH_FILE_CACHE[rel] = (key, txt)
        return txt
    except Exception:
        return ""


def _legit_span(cwd, keys, cache=None):
    """True when this refimpl span also exists in a TRACKED file of the current tree (base code / the model's own edit)."""
    want = "\n".join(keys)
    for rel in (_PH_DEEP["order"] or sorted(_PH_DEEP["tracked"] or ())):
        if want in _tracked_text(cwd, rel):
            return True
    return False


def _ref_spans(cwd, lines, cache=None):
    """-> [(start, end)] spans of `lines` that reproduce a refimpl.py passage not present in any tracked file."""
    keys, idx = _PH_DEEP["keys"], _PH_DEEP["index"]
    if not keys or not lines:
        return []
    cache = {} if cache is None else cache
    spans, i, n = [], 0, len(lines)
    while i < n:
        best = (0, 0)
        for k0 in _okeys(lines[i]):
            for p in (idx.get(k0) or ())[:64] if k0 else ():
                ln = 0
                while i + ln < n and p + ln < len(keys) and keys[p + ln] in _okeys(lines[i + ln]):
                    ln += 1
                if ln > best[0]:
                    best = (ln, p)
        ln, p = best
        if ln >= PH_RUN_LINES:
            span_keys = keys[p:p + ln]
            if sum(len(k) for k in span_keys) >= PH_RUN_CHARS and not _legit_span(cwd, span_keys, cache):
                spans.append((i, i + ln))
            i += ln
            continue
        i += 1
    return spans


def _contains_ref(cwd, text):
    try:
        return bool(_ref_spans(cwd, text.splitlines()))
    except Exception:
        return False


def ph_deep_scrub(cwd, text):
    """Output filter: drop refimpl.py diff sections (git show / log -p / diff) and name + grep-prefix lines, and withhold any
    content span that reproduces refimpl.py. Never raises; returns the text unchanged when the deep guard is off."""
    try:
        if not text or not ph_deep_active(cwd):
            return text
        src = text.splitlines(keepends=True)
        out, dropped, skip = [], 0, None
        for ln in src:
            s = ln.rstrip("\r\n")
            if skip == "diff" and (_DIFF_HDR.match(s) or s.startswith(("commit ", "From "))):
                skip = None
            if skip:
                dropped += 1
                continue
            if _DIFF_HDR.match(s) and "refimpl.py" in s:
                skip, dropped = "diff", dropped + 1
                continue
            toks = s.split()
            if "refimpl" in s and (_GREP_PREFIX.match(s.strip()) or
                                   (len(toks) <= 12 and any(_NAME_LINE_TOK.match(t) for t in toks))):
                dropped += 1
                continue
            out.append(ln)
        if dropped:
            out.append(_WITHHELD.format(n=dropped) + "\n")
        spans = _ref_spans(cwd, [x.rstrip("\r\n") for x in out])
        if not dropped and not spans:
            return text
        if spans:
            res, j = [], 0
            for a, b in spans:
                res.extend(out[j:a])
                res.append(_WITHHELD.format(n=b - a) + "\n")
                j = b
            res.extend(out[j:])
            out = res
        return "".join(out)
    except Exception:
        return text


def _deep_read_verdict(cwd, path_arg):
    """read_file of an UNTRACKED in-tree file that reproduces refimpl.py (a cp / hardlink of it) -> not found."""
    try:
        if not ph_deep_active(cwd):
            return None
        rel = _rel(cwd, path_arg)
        if rel is None or rel in HIDDEN_FILES or rel in (_PH_DEEP["tracked"] or ()):
            return None
        p = Path(cwd) / rel
        if p.is_file() and p.stat().st_size <= PH_SCAN_MAX_BYTES and _contains_ref(cwd, p.read_text(errors="ignore")):
            return f"ERROR: file not found: {path_arg} -- use list_files to see what exists."
    except Exception:
        pass
    return None


def _is_ancestor_commit(cwd, rev):
    """True/False for a rev that names a commit (ancestor-or-self of HEAD?); None when it is not a commit at all."""
    rc, out = _git_out(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}")
    if rc != 0 or not out.strip():
        return None
    rc2, _ = _git_out(cwd, "merge-base", "--is-ancestor", out.strip(), "HEAD")
    return rc2 == 0


def _deep_git_verdict(cwd, sub, args):
    if sub in ("reflog", "clone", "fetch", "pull", "worktree", "bundle", "fast-export", "replace", "notes", "for-each-ref",
               "show-ref") or (sub == "stash" and args[:1] == ["show"]) or (sub == "branch" and any(
                   a in ("-a", "-r", "--all", "--remotes") for a in args)):
        return (f"REFUSED: `git {sub}` is not available in this task. Only this task's own history (HEAD and its "
                f"ancestors) is in scope; use `git diff` / `git log` / `git show HEAD` for that.")
    if sub not in _HISTORY_SUBS:
        return None
    for a in args:
        if a in _FOREIGN_OPTS or a.startswith(tuple(o + "=" for o in _FOREIGN_OPTS)):
            return (f"REFUSED: `git {sub} {a}` reaches other branches of the source repository, which are not part "
                    f"of this task. Only HEAD and its ancestors are in scope.")
    blob = _PH_DEEP["blob"]
    for a in args:
        if a == "--":
            break
        if a.startswith("-") or (Path(cwd) / a).exists():
            continue
        if blob and len(a) >= 6 and re.fullmatch(r"[0-9a-fA-F]+", a) and blob.startswith(a.lower()):
            return "REFUSED: that object is the sealed reference solution (refimpl.py); it is not available in this task."
        revpart = a.split(":", 1)[0] if ":" in a else a
        for r in [x for x in re.split(r"\.{2,3}", revpart) if x]:
            anc = _is_ancestor_commit(cwd, r)
            if anc is False:
                return (f"REFUSED: `{r}` is not HEAD or one of its ancestors -- other branches / later commits of the "
                        f"source repository are not part of this task. Work from HEAD.")
        if ":" in a and blob:
            rc, out = _git_out(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", a)
            if rc == 0 and out.strip() == blob:
                return "REFUSED: that object is the sealed reference solution (refimpl.py); it is not available in this task."
    return None


def _expand_globs(cwd, tok):
    import glob as _glob
    try:
        base = tok if os.path.isabs(tok) else str(Path(cwd) / tok)
        return _glob.glob(base, recursive=True)[:5000]
    except Exception:
        return []


def ph_deep_bash_verdict(cwd, command):
    """run_bash gate (deep). -> a REFUSED string or None."""
    try:
        if not ph_deep_active(cwd):
            return None
        cmd = str(command or "")
        ref = (Path(cwd) / "refimpl.py").resolve()
        tracked = _PH_DEEP["tracked"] or set()
        execy = bool(_EXEC_COPY.search(cmd))
        for seg in _SEG_SPLIT.split(cmd):
            try:
                toks = shlex.split(seg)
            except ValueError:
                toks = seg.split()
            for t in toks:
                if execy and _GLOB_CH.search(t) and any(Path(x).resolve() == ref for x in _expand_globs(cwd, t)):
                    return (f"REFUSED: the pattern `{t}` matches refimpl.py (the sealed reference solution), which is "
                            f"not one of your inputs. Name the files you mean explicitly.")
                if _GLOB_CH.search(t) or t.startswith("-") or len(t) > 400:
                    continue
                try:
                    p = Path(t) if os.path.isabs(t) else Path(cwd) / t
                    if (p.is_file() and p.resolve() != ref and p.stat().st_size <= PH_SCAN_MAX_BYTES
                            and _rel(cwd, p) not in tracked and _contains_ref(cwd, p.read_text(errors="ignore"))):
                        return (f"REFUSED: {t} is a copy of the sealed reference solution (refimpl.py), which is not "
                                f"available in this task. Implement the change yourself in the target file.")
                except Exception:
                    pass
            if _GREP_CMD.search(" " + seg) and any(x in ("-h", "--no-filename") or (x.startswith("-") and not x.startswith("--")
                                                                                     and "h" in x[1:]) for x in toks):
                return ("REFUSED: grep without file names (-h) is not available in this task; use `grep -rn PATTERN "
                        "<dir>` so each hit shows its file.")
        for sub, args in _git_segments(cmd):
            v = _deep_git_verdict(cwd, sub, args)
            if v:
                return v
        return None
    except Exception:
        return None


def ph_deep_diag_verdict(cwd, requests):
    """request_diagnostics (its output is NOT scrubbed by the worker): refuse the kinds that can print refimpl content."""
    try:
        if not ph_deep_active(cwd):
            return None
        for rq in requests if isinstance(requests, list) else []:
            if not isinstance(rq, dict):
                continue
            kind = rq.get("kind")
            if kind == "git":
                a = [str(x) for x in (rq.get("args") or [])]
                sub = a[0] if a else ""
                rest = a[1:]
                revs = [x for x in rest if not x.startswith("-") and x != "--" and not (Path(cwd) / x).exists()]
                if (sub in ("show", "blame", "grep", "annotate", "cat-file") or
                        (sub == "log" and any(x in ("-p", "-u", "--patch") or x.startswith("--patch") for x in rest)) or
                        (sub == "diff" and revs) or (sub == "stash" and "show" in rest) or
                        any(x in _FOREIGN_OPTS for x in rest)):
                    return ("ERROR: that git request is not available through request_diagnostics in this task; run it "
                            "with run_bash instead (e.g. `git diff`, `git log --oneline -5`).")
            elif kind == "grep":
                g = str(rq.get("glob") or "")
                if not g or any(fnmatch.fnmatch("refimpl.py", x) for x in (g, g.split("/")[-1])):
                    return ("ERROR: request_diagnostics grep needs a `glob` naming the files to search in this task "
                            "(e.g. \"glob\": \"src/*.py\"); or use run_bash `grep -rn PATTERN <dir>`.")
            elif kind == "file":
                v = _deep_read_verdict(cwd, rq.get("path") or "")
                if v:
                    return v
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- L1 VERIFY_ON_EDIT
_FAIL_HEAD = re.compile(
    r"^\s*(?:not ok\b|[✖✕×]\s|FAIL(?:ED)?\b|--- FAIL\b|ERROR:|ERROR\b|AssertionError\b|Traceback\b|"
    r"\s*(?:\w+Error|\w+Exception):)", re.I)
_ASSERT_LINE = re.compile(r"(assert|expected|actual|!==|===|should|got\b|\bwanted\b|\w+Error)", re.I)


def summarize_failures(output, limit=VOE_MAX_CHARS) -> str:
    """PURE. Compact 'which cases failed + the first assertion line of each' from a verify's output."""
    lines = (output or "").splitlines()
    cases, seen = [], set()
    i = 0
    while i < len(lines) and len(cases) < 14:
        raw = lines[i]
        s = raw.strip()
        if s and _FAIL_HEAD.search(raw) and not s.lower().startswith(("fails=", "failed: 0")):
            head = s[:200]
            if head not in seen:
                seen.add(head)
                item = "  - " + head
                for nxt in lines[i + 1:i + 7]:
                    t = nxt.strip()
                    if t and t != s and _ASSERT_LINE.search(t) and not _FAIL_HEAD.search(nxt):
                        item += "\n      " + t[:200]
                        break
                cases.append(item)
        i += 1
    if not cases:
        tail = (output or "").strip()[-600:]
        return "(no recognizable failing-case lines; verify output tail)\n" + tail
    body = "\n".join(cases)
    if len(body) > limit:
        body = body[:limit - 20].rstrip() + "\n  ... (truncated)"
    return body


def arm_run(cwd, task, task_kind, verify, log=None):
    """Called once per run_task after the verify/task are known. Resets per-run state; arms L2 when the job is
    a sealed coding job. Never raises."""
    try:
        _VOE.update(edits=0, runs=0, last_secs=0.0)
        _PH.update(cwd=None, verify=verify, passing=False)
        _PH_DEEP.update(keys=None, index=None, blob=None, tracked=None, order=None)
        _L4["sticky"] = False
        if flag("PROTECT_HARNESS"):
            if sealed_coding_harness(cwd, task, task_kind, verify):
                _PH["cwd"] = _norm(cwd)
                _deep_arm(cwd)
                if log:
                    log("[worker] PROTECT_HARNESS: sealed coding harness detected -- refimpl.py hidden, harness "
                        "files read-only, harness/whole-tree git reverts refused.")
                    if os.environ.get("PH_DEEP_GUARD") != "0":
                        log(f"[worker] PROTECT_HARNESS deep guard armed: refimpl.py content withheld from every tool "
                            f"output (history/glob/copy/diagnostics paths), other branches out of scope "
                            f"({len(_PH_DEEP['keys'] or [])} refimpl lines fingerprinted; PH_DEEP_GUARD=0 disables).")
            elif log:
                log("[worker] PROTECT_HARNESS requested but this is not a sealed coding job -- lever not armed.")
        if flag("VERIFY_ON_EDIT") and log:
            log(f"[worker] VERIFY_ON_EDIT armed: verify every {VOE_EVERY}nd successful non-harness edit "
                f"(max {VOE_MAX_RUNS} runs, skipped once a verify took > {int(VOE_SKIP_IF_SLOWER_S)}s).")
        if flag("PROSE_STICKY_NOTHINK") and log:
            log("[worker] PROSE_STICKY_NOTHINK armed: after the first prose-runaway abort, thinking stays OFF "
                "for the rest of the run.")
        if flag("READ_MIN_WINDOW") and log:
            log(f"[worker] READ_MIN_WINDOW armed: read_file windows < {MIN_WINDOW_LINES} lines are widened.")
    except Exception:
        pass


def after_edit(cwd, verify, path, result, runner, task_kind="coding", log=None, clock=time.monotonic) -> str:
    """Hook for a finished write_file/edit_file. -> text to append to the tool result ('' = nothing).
    Handles the L2 'edit invalidates the passing state' bookkeeping and the whole of L1."""
    try:
        if not isinstance(result, str) or not result.startswith("OK: "):
            return ""
        if ph_active(cwd):
            _PH["passing"] = False
        if not flag("VERIFY_ON_EDIT") or task_kind != "coding" or not verify:
            return ""
        rel = _rel(cwd, path) if path is not None else None
        if rel is None or is_harness_rel(rel):
            return ""
        _VOE["edits"] += 1
        if _VOE["edits"] % VOE_EVERY:
            return ""
        if _VOE["runs"] >= VOE_MAX_RUNS:
            return ""
        if max(_VOE["last_secs"], _LAST_VERIFY_SECS[0]) > VOE_SKIP_IF_SLOWER_S:
            return ""
        t0 = clock()
        ok, out = runner(verify, cwd)
        secs = clock() - t0
        _VOE["runs"] += 1
        _VOE["last_secs"] = secs
        if log:
            log(f"[worker] VERIFY_ON_EDIT run {_VOE['runs']}/{VOE_MAX_RUNS} after edit of {rel}: "
                f"{'PASS' if ok else 'FAIL'} in {secs:.0f}s")
        if ok:
            if ph_active(cwd):
                _PH["passing"] = True
            return ("\n\n[VERIFY ON EDIT: the task's verify command now PASSES (exit 0). The work is done -- do "
                    "NOT edit further and do NOT revert anything; call task_complete now.]")
        return (f"\n\n[VERIFY ON EDIT (run {_VOE['runs']}/{VOE_MAX_RUNS}): after this edit the task's verify "
                f"command still FAILS. Failing cases:\n{summarize_failures(out)}\n"
                f"Informational only: finish the edits you planned, then fix what is still listed.]")
    except Exception:
        return ""
