#!/usr/bin/env python3
"""edit_guard -- post-edit syntax / lint-DELTA guard, shared by the fixed repair lane
(fixed_lane.py) and available to the worker's edit tools.

Principle (Agentless `check_syntax` + `lint_code`, SWE-agent's edit linter guard):
an edit is rejected only if it makes things WORSE than the file already was.
  * syntax: the file parsed before and no longer parses after -> reject
  * lint:   the set of fatal diagnostics after minus the set before is non-empty -> reject
A file that was already broken, or that this module cannot judge (unknown type, missing
tool), is never rejected for pre-existing problems: the guard returns ok.

Public API
  guard_edit(path, before, after, run=None) -> GuardResult(ok, reasons, checked)
  syntax_errors(path, text, run=None)       -> list[str]   ([] = parses / unjudgeable)
  fatal_lint(path, text)                    -> set[str]    (line-number free, so a moved
                                                            line is not "new")
Python fatal lint: flake8/pyflakes `--select=E9,F821,F823,F831,F406,F407,F701,F702,
F704,F706` equivalent when flake8 is importable; otherwise a conservative built-in
undefined-name check (a name loaded but bound NOWHERE in the module, not a builtin).
`run` (optional) is an injectable subprocess runner: run(argv, input_text, timeout) ->
(returncode, output) used for node/bash syntax checks.
"""
import ast
import builtins
import json
import os
import subprocess
import sys
import tempfile
from collections import namedtuple

GuardResult = namedtuple("GuardResult", "ok reasons checked")

_PY_EXT = {".py"}
_JS_EXT = {".js", ".mjs", ".cjs"}
_SH_EXT = {".sh", ".bash"}
_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__path__",
                                  "__builtins__", "__package__", "__loader__", "__class__",
                                  "__debug__", "WindowsError", "reveal_type"}


def _ext(path):
    return os.path.splitext(str(path))[1].lower()


def _default_run(argv, input_text, timeout):
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except FileNotFoundError:
        return None, "tool missing"
    except subprocess.TimeoutExpired:
        return None, "timeout"


def _py_syntax(text):
    try:
        ast.parse(text)
        return []
    except SyntaxError as e:
        return [f"SyntaxError: {e.msg} (line {e.lineno})"]
    except (ValueError, RecursionError) as e:
        return [f"SyntaxError: {e}"]


def _bound_names(tree):
    """Every name bound ANYWHERE in the module (any scope). Conservative on purpose: a
    name bound in some other function still counts as bound, so a hit is a real typo /
    deleted definition, never a scoping false positive."""
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            if not isinstance(n, ast.ClassDef):
                a = n.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                    if arg is not None:
                        bound.add(arg.arg)
        elif isinstance(n, ast.Lambda):
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None:
                    bound.add(arg.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])
                if al.name == "*":
                    bound.add("*")
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchStar) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
        elif hasattr(ast, "TypeVar") and isinstance(n, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            bound.add(n.name)
        elif hasattr(ast, "TypeAlias") and isinstance(n, ast.TypeAlias) and isinstance(n.name, ast.Name):
            bound.add(n.name.id)
    return bound


def _py_fatal_lint(text):
    """F821-class diagnostics as stable strings (no line numbers)."""
    try:
        tree = ast.parse(text)
    except Exception:
        return set()
    try:  # real linter when present
        import pyflakes.api   # noqa: F401
        import pyflakes.checker
        w = pyflakes.checker.Checker(tree, "<edit>")
        out = set()
        for m in w.messages:
            cls = type(m).__name__
            if cls in ("UndefinedName", "UndefinedLocal", "DuplicateArgument", "ReturnOutsideFunction",
                       "YieldOutsideFunction", "ContinueOutsideLoop", "BreakOutsideLoop",
                       "DefaultExceptNotLast", "TwoStarredExpressions", "TooManyExpressionsInStarredAssignment",
                       "ForwardAnnotationSyntaxError", "RaiseNotImplemented"):
                out.add(f"{cls}: {m.message % m.message_args}")
        return out
    except ImportError:
        pass
    bound = _bound_names(tree)
    if "*" in bound:     # star import: cannot know what it binds
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in bound and n.id not in _BUILTINS:
                out.add(f"UndefinedName: undefined name {n.id!r}")
    return out


def _json_syntax(text):
    try:
        json.loads(text)
        return []
    except ValueError as e:
        return [f"JSONDecodeError: {e}"]


def _tmp_check(argv_prefix, suffix, text, run, timeout=20):
    fd, p = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        rc, out = (run or _default_run)(list(argv_prefix) + [p], None, timeout)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
    if rc is None:        # tool missing / timeout: unjudgeable
        return None
    if rc == 0:
        return []
    first = next((ln for ln in out.splitlines() if "rror" in ln), out.strip()[:200])
    return [first.replace(p, "<file>")[:300]]


def syntax_errors(path, text, run=None):
    """[] when the text parses OR the type cannot be judged; else a list of messages."""
    ext = _ext(path)
    if ext in _PY_EXT:
        return _py_syntax(text)
    if ext == ".json":
        return _json_syntax(text)
    if ext in _JS_EXT:
        r = _tmp_check(["node", "--check"], ext, text, run)
        return r or []
    if ext in _SH_EXT:
        r = _tmp_check(["bash", "-n"], ext, text, run)
        return r or []
    return []


def fatal_lint(path, text):
    if _ext(path) in _PY_EXT:
        return _py_fatal_lint(text)
    return set()


def guard_edit(path, before, after, run=None):
    """Reject an edit only if it regresses syntax or adds a fatal lint diagnostic."""
    reasons, checked = [], []
    if before == after:
        return GuardResult(False, ["edit changed nothing (file identical after the edit)"], ["noop"])
    s_before = syntax_errors(path, before, run) if before else []
    s_after = syntax_errors(path, after, run)
    checked.append("syntax")
    if s_after and not s_before:
        reasons.append(f"edit breaks syntax of {path}: {s_after[0]}")
    elif s_after and s_before and s_after[0] != s_before[0]:
        # was broken, still broken but differently: do not accept silently either
        reasons.append(f"{path} still does not parse after the edit: {s_after[0]}")
    if not s_after:
        new = fatal_lint(path, after) - fatal_lint(path, before)
        checked.append("lint-delta")
        if new:
            reasons.append(f"edit adds fatal lint errors in {path}: " + "; ".join(sorted(new)[:5]))
    return GuardResult(not reasons, reasons, checked)


if __name__ == "__main__":
    # tiny CLI: edit_guard.py FILE BEFORE_FILE  -> guard AFTER=FILE vs BEFORE_FILE
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    a = open(sys.argv[1]).read()
    b = open(sys.argv[2]).read()
    r = guard_edit(sys.argv[1], b, a)
    print("OK" if r.ok else "REJECT: " + " | ".join(r.reasons))
    sys.exit(0 if r.ok else 1)
