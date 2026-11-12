#!/usr/bin/env python3
"""verify-relevance.py -- does a verify test the PROPERTY, or a proxy for it?

WHY THIS EXISTS (the ceiling on autonomous sign-off)
-----------------------------------------------------
The dispatch gate proves a verify DISCRIMINATES: it fails on the untouched
baseline and passes with a reference implementation applied. That is necessary
and it is where the gate stopped. It is NOT sufficient, because a verify can be
discriminating while testing something other than the property the task asked
for:

    grep -q "THRESHOLD * 2" target.py            # a LITERAL proxy
    ast.walk(...) finds a Compare with GtE        # an AST proxy
    is_safe({"count": 99}, True) == True         # a BENIGN behavioural case

Each of these fails at baseline and passes with the fix, so each passes the
both-ways proof -- and each also passes with `>=` flipped to `>`, with the flag
gate deleted, with the threshold off by one. That is how broken work has passed
a green verify in this project. "Assert the property, not a proxy" and "verify
relevance, not just discrimination" are the two memories this file mechanises.

THE SIGNAL: targeted mutation of the reference implementation
-------------------------------------------------------------
We already hold, at gate time, a reference implementation that turns the verify
green. Perturb it in ways that BREAK THE PROPERTY IT INTRODUCED and ask whether
the verify notices. The perturbations ("mutants") are generated on the lines the
reference implementation ADDED -- that region is, by construction, the fix -- so
each mutant is "the fix, but wrong in one specific way":

    comparison flipped at its boundary   (>= -> >, == -> !=)
    boolean glue swapped                 (and <-> or), `not` dropped
    a constant nudged                    (n -> n+1, n-1, True -> False)
    an arithmetic operator swapped       (* -> +)
    a branch condition negated / forced  (if c -> if not c / if True)
    a return value blanked or negated    (return x -> return None / not x)
    an added statement deleted           (the partial revert)
    an added hunk reverted whole         (language-agnostic partial revert)

A verify that tests the property KILLS these (goes red). A verify that tests a
proxy lets them SURVIVE, because the proxy is still satisfied: the literal is
still in the file, the AST still has a Compare, the benign case still returns
True.

WHAT IS AND IS NOT COUNTED AS EVIDENCE -- the three filters
-----------------------------------------------------------
Mutation testing's classical weakness is the EQUIVALENT MUTANT: a change that
alters the text but not the behaviour, which no test can kill and which then
reads as a gap in the test. Three filters keep that from turning this check
into noise, and every one of them errs in the SAFE direction (fewer mutants,
never a mutant that flatters the verify):

  1. ONLY THE ADDED LINES ARE MUTATED. Mutating untouched code would measure
     the verify's coverage of pre-existing behaviour, which is not the question
     and would penalise a perfectly relevant verify for not being a regression
     suite.

  2. LITERAL-BREAKING MUTANTS ARE NOT EVIDENCE. If a mutant removes one of the
     task's `## Must contain` literals from the file, the scaffold's own
     check_literals step kills it regardless of what the behavioural checks do.
     Such a kill says nothing about relevance, so those mutants are generated,
     run, reported -- and EXCLUDED from the score. Without this filter a pure
     grep verify would score well on every mutant that happened to touch its
     literal. This is the single most important design decision in the file.

  3. INNOCUOUS STATEMENTS ARE NOT MUTATED. Logging, printing, docstrings,
     comments, imports, assertions: deleting or perturbing these is expected to
     be unobservable, and a relevance check that penalises a verify for not
     catching a no-op is wrong. The skip list is a heuristic and it errs toward
     skipping -- a skipped mutant costs at most one piece of evidence, an
     equivalent mutant that is counted costs a false "low relevance" flag.

Kills by CRASH (a traceback in the verify output) are counted as kills but
reported separately: a crash on a mutant means the verify EXECUTED the mutated
path with an input that reached it, which is behavioural evidence, but a verify
that only compiles the file kills nothing this way because every mutant here is
built to parse. `.pyc` caches are removed before every run: a mutant of the same
byte-length written in the same second would otherwise load stale bytecode and
read as a survivor.

THE DECISION, and the asymmetry it is built around
--------------------------------------------------
    score = killed / evidence_mutants        (evidence = literal-preserving)

    relevant   score >= threshold  AND  evidence_mutants >= min_mutants
    low        score <  threshold  AND  evidence_mutants >= min_mutants
    unproven   fewer than min_mutants evidence mutants, refimpl not green,
               nothing mutable (non-Python target with a single hunk), or the
               time budget ran out before min_mutants were tried

The two error directions are NOT symmetric. A false "relevant" lets broken
work auto-approve; a false "low" costs a human a look they were already giving.
So the defaults are strict (threshold 0.8, min 3 evidence mutants) and every
"could not tell" is UNPROVEN, never a pass. Survivors are NAMED in the output,
each with its class and the source it produced, so a human can settle in
seconds whether a survivor is an equivalent mutant or a real hole -- the check
is designed to be argued with, not obeyed blindly.

MEASURED (test-verify-relevance.py, and one real dispatch), 2026-09-03
----------------------------------------------------------------------
Toy fixture (is_safe threshold), every verify below clears both-ways:
    behavioural verify with adversarial cases      score 1.00   relevant
    same verify, refimpl padded with log/print     score 1.00   relevant
    grep-two-literals proxy                        score 0.00   low
    AST-shape proxy (finds a GtE Compare + an If)  score 0.20   low
    benign behavioural (count=99 True, count=1 F)  score 0.40   low
    HALF the property (flag gate only)             score 0.64   low
Real dispatch (claude-vs-ollama-tokens, 56-line refimpl, human-reviewed
fixture, 40 mutants in 6s):
    original fixture                 mutant 0.725  site 0.89   LOW
      survivors named: the zero-data text branch (L295, 8 mutants), the
      `claude_tokens` alias key (L247), the bool guard (L142)
    fixture + the 3 cases those survivors point at
                                     mutant 0.925  site 0.975  RELEVANT
      remaining 3 survivors are domain-equivalent (`+`->`-` on a both-zero
      check; a bool-valued usage field that never occurs)
So: every proxy shape we have been burned by scores <= 0.64; a relevant
verify scores >= 0.9; the threshold 0.8 sits in the gap with 0.16 on the
proxy side and 0.1 on the relevant side. The real-dispatch LOW is the
honest answer -- the holes were real requirements from the task's own
"must NOT change" list -- and the survivor list turned it RELEVANT in three
added cases. That actionability is the point: the check argues, it does
not just refuse.

THREE SCORER HOLES, measured and closed 2026-09-03 (test section I)
------------------------------------------------------------------
  1. TRUNCATION WAS CLASS-BALANCED, NOT SITE-BALANCED. The toy fixture at
     --max-mutants 3 tried L7 and L9 and never L8, and said RELEVANT with
     truncated=true; wt-tokens' cap of 40 put seven mutants on one line and
     left three hunks untried. Now: every site is tried once before any
     site twice, `untried_sites` is reported, and an untried site makes the
     run UNPROVEN (never relevant).
  2. AN UNEXERCISED SITE WAS A FOOTNOTE. wt-tokens with only the alias case
     removed: L247's single mutant survived and the verdict stayed RELEVANT
     0.9 (mutant) / 0.925 (site mean) -- a task "must NOT change" item
     untested, and every average clearing the threshold because at twenty
     sites one hole is a 5% dent. Now: any site where every tried mutant
     survived (>= 1, was >= 2) makes the run LOW, and `site_coverage`
     (sites with at least one kill) is the third score in the min().
     Re-measured: original fixture LOW 0.8 naming L247/L295/L298 (the three
     task items); improved RELEVANT 0.925 coverage 1.0; alias-removed LOW
     naming L247. The wide toy (six gates, five tested) has mutant 0.85 and
     site 0.833 -- both over the threshold -- and is LOW naming gate_6.
  3. CRASH-KILL INFLATION was the suspected third hole: a smoke-run proxy
     that executes the code and asserts nothing. Measured 0.0 on the toy
     and 0 crash kills on wt-tokens -- NOT a hole today; the smoke proxy is
     now a permanent arm so it stays that way.
  The cost of 1 and 2 is a false LOW on a line whose only mutant is
  equivalent (a dead store) or a run whose cap is under the site count;
  both name the line, both are a human look, neither is an approval.

FALSE-POSITIVE / FALSE-NEGATIVE ANALYSIS
---------------------------------------
  false RELEVANT (expensive: broken work auto-approves)
    - a verify that diffs the target against a golden copy kills every
      mutant and scores 1.0 while testing nothing behavioural. Not seen in
      this project; would show as 100% kill including crash-free constant
      nudges of message strings. Mitigation available: an "equivalence
      canary" mutant (rename a local variable) that a golden-diff kills and
      a behavioural verify does not. NOT implemented; the golden-diff shape
      also fails verify-quality's literal check in practice.
    - a fixture that asserts exactly the refimpl's outputs on exactly one
      input still kills most mutants of a small fix. It is relevant to that
      input only. Layer 3 (change class) bounds the blast radius; the human
      read is the backstop in shadow mode.
  false LOW (cheap: a human looks)
    - equivalent mutants: fallback constants (`or 0`), rounding precision,
      docstring hunks, logger setup were all seen on the first real run and
      each got a filter (const-default / FORMATTING_CALL_NAMES /
      python_code_lines / INNOCUOUS_CALL_NAMES). More will appear; each one
      costs a look, not an approval, and is named in the survivor list.
    - per-mutant scoring overweights a complex untested line; per-site
      scoring is reported alongside and the verdict takes the lower.

KNOWN LIMITS (honest, and written down so they survive the authors)
-------------------------------------------------------------------
  * A verify that is relevant to the ADDED code but the task's real property
    lives in code the refimpl did not change (a wrong-site refimpl) is not
    caught; that is a refimpl defect and refimpl-satisfies owns it.
  * Mutants are generated for Python via AST spans; other languages get only
    hunk-level partial reverts, which need >= 2 hunks to say anything. A Swift
    dispatch with one hunk is UNPROVEN, which is the correct answer.
  * A verify that is a near-complete oracle for the fixed function will kill
    equivalent mutants too rarely to matter; a verify that kills EVERYTHING,
    including obvious equivalents, is worth a second look -- it may be
    diffing the file against a golden copy, which is a proxy of a different
    kind. The `kill_all_including_equivalent` heuristic is NOT implemented;
    the survivor list is the place a human sees it.
  * It costs one verify run per mutant. Capped by --max-mutants and
    --budget-s; a capped run reports `truncated` and still needs min_mutants.

USAGE
-----
  verify-relevance.py <worktree> --refimpl fix.patch [--verify 'bash verify.sh']
  verify-relevance.py <worktree> --refimpl-cmd 'python3 refimpl.py' --json
  verify-relevance.py <worktree> --applied      # tree ALREADY carries the fix;
                                                # measured in place, fix restored

Exit codes: 0 relevant, 1 low, 3 unproven, 2 usage/error.
The preflight gate imports `measure_applied()` directly.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_THRESHOLD = 0.8
DEFAULT_MIN_MUTANTS = 3
DEFAULT_MAX_MUTANTS = 40
DEFAULT_BUDGET_S = 600
VERIFY_TIMEOUT_S = 900

# Calls whose deletion or perturbation is expected to be unobservable. Erring
# toward skipping is the safe direction (see filter 3 in the module docstring).
INNOCUOUS_CALL_NAMES = {
    "print", "log", "debug", "info", "warning", "warn", "error", "exception",
    "critical", "trace", "logger", "logging", "pprint", "getLogger",
    # project audit/activity logger: records a human-readable activity string,
    # never carries the behavioural property. Nudging a constant inside its
    # message (e.g. a `/86400` day figure) is an equivalent mutant, same class
    # as `log.info` above.
    "record_activity",
}
COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "--")
# Constants that are PRESENTATION, not property: `round(x, 2)` -> `round(x, 3)`
# changes nothing a tolerance-based fixture can see, and a task never asks for
# "2 decimal places" as its property. Measured on the first real dispatch
# (claude-vs-ollama-tokens): 3 of 16 survivors were the precision argument of
# round(). Skipping them is the safe direction (fewer mutants, never a kinder
# score for a proxy).
FORMATTING_CALL_NAMES = {"round", "format", "ljust", "rjust", "center", "zfill",
                         "quantize", "truncate"}

# BENIGN-MUTATION ALLOWLIST (pain point #2). Three classes of added line are
# UNOBSERVABLE by construction, so a verify that does not notice their deletion
# is not thereby a proxy verify -- flagging them wasted whole GO iterations on
# the first greenfield FastAPI dispatch. Each class is skipped in the SAFE
# direction (fewer evidence mutants, never a kinder score for a real proxy):
#
#   1. Resource cleanup as a BARE statement -- `conn.close()`, `await
#      session.aclose()`, `f.flush()`. Deleting it leaks a handle; no
#      behavioural fixture can see that within one process, so its stmt-delete
#      is an equivalent mutant. Only the bare-statement form is exempt: a
#      cleanup call whose RETURN is used (`x = q.close()`) is untouched.
#   2. A human MESSAGE string passed as a keyword argument a framework treats
#      as prose -- `HTTPException(status_code=404, detail="not found")`,
#      `raise ValueError(message="...")`. The `status_code` beside it is still
#      mutated (it IS the property); only the prose kwarg's const-str is skipped.
#   3. Anything the author marks `# relevance: unobservable` (also `ignore` /
#      `benign`) on the SAME physical line -- the escape hatch for the one case
#      a static rule cannot decide, e.g. a redundant `session.commit()` that a
#      later commit masks. Author-explicit, one line at a time, never a blanket.
#
# None of these can exempt a genuinely behavioural line: cleanup deletion is
# unobservable, a prose message carries no property, and the annotation is a
# deliberate per-line act. The behavioural mutants (compare-flip, return-flip,
# const-int on real logic, cond-*) are untouched, so the revert-test in
# test-ollama-dispatch-preflight.py still NO-GOes a weakened fixture.
CLEANUP_CALL_NAMES = {"close", "aclose", "flush", "dispose", "disconnect",
                      "shutdown", "release", "cleanup", "teardown"}
MESSAGE_KWARGS = {"detail", "message", "msg", "description", "reason", "hint",
                  "help", "title", "error_message", "err_msg"}
# Comment marker is `#` (Python) or `//` (JS/TS); the word must be one of the
# three opt-out keywords, whole-word.
RELEVANCE_OPT_OUT = re.compile(r"(?:#|//)\s*relevance:\s*(?:unobservable|ignore|benign)\b")

# BEHAVIOURAL OPT-OUTS ARE NOT HONOURED (2026-10-06, rt-bfmr-link-sync-feedback r1
# + rt-bg-commitments-fix-sync-guard). Authoring models put the opt-out on the
# DECISION lines -- `if (webError) {`, `return Response.json({...}, {status: 409})`,
# `if (webRows == 0)` -- and every mutant of them vanished from the evidence set,
# so preflight read 34/34 RELEVANT on a fixture that never tested those branches
# (the sync-guard refimpl opted out the whole guard, in both copies). The escape
# hatch exists for lines no case CAN observe (logging, cleanup, a redundant call a
# later one masks, a constant table); a branch condition, a comparison, a return
# of a computed value or a status/response construction is the property itself.
# Such an opt-out is IGNORED (its mutants are generated and counted as usual) and
# REPORTED (rec["optout_rejected"]) -- a survivor there is a real hole, or a
# redundant line to DELETE, never something to annotate away. Comment and string
# text are masked first, so `"a == b"` or the marker itself never classify a line.
_OPTOUT_LOG_START = re.compile(
    r"^\s*(?:console\.\w+|(?:self\.|this\.)?_?(?:log|logger|logging)(?:\.\w+)?|print|"
    r"pprint|debug|warn|record_activity)\s*\(")
_OPTOUT_RULES = (
    ("branch condition", re.compile(
        r"^\s*(?:\}\s*)?(?:if|elif|else\s+if|while|for|switch|case|unless)\b"
        r"|\bif\s*\(|\bif\b.+\belse\b")),
    ("return of a computed value", re.compile(
        r"(?:^\s*|[;{:]\s*|=>\s*)return\b\s*(?!(?:None|null|undefined|void\s+0)\s*(?:[;})]|$))[^\s;})]")),
    ("comparison", re.compile(
        r"===?|!==?|(?<![=<>!])<=|(?<![=])>=|\s<\s|\s>\s|\bis\s+not\b|\bnot\s+in\b"
        r"|\s(?:in|is)\s")),
    ("boolean / fallback operator", re.compile(r"&&|\|\||\?\?|\s(?:and|or)\s|^\s*not\s|\bnot\s+\w")),
    ("ternary", re.compile(r"\?\s*[^?.:\s][^:]*:")),
    ("status/response construction", re.compile(
        r"\bResponse\b|\bNextResponse\b|\bJSONResponse\b|\bHTTPException\b"
        r"|\bstatus(?:Code|_code)?\s*[:=(]|\.status\s*\(|\bsendStatus\b|\bres\.(?:json|send)\s*\(")),
    ("throw/raise", re.compile(r"^\s*(?:throw|raise)\b")),
)


def _mask_code_line(line: str) -> str:
    """The line with its trailing comment and every string literal's CONTENT
    masked, so only code is classified. Heuristic (per physical line), erring
    toward classifying a line as behavioural -- the safe direction."""
    s = RELEVANCE_OPT_OUT.split(line, maxsplit=1)[0]
    s = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", "S", s)
    s = re.sub(r"(?:^|\s)(?://|#).*$", "", s)
    return s


def behavioural_optout_kind(line: str) -> str | None:
    """PURE. Why an opt-out on this line must NOT be honoured (the rule that
    matched), or None when the line may legitimately opt out (logging, cleanup,
    a plain assignment/call, a constant-table row, a comment)."""
    code = _mask_code_line(line)
    if not code.strip() or code.strip().startswith(("#", "//", "/*", "*")):
        return None
    if _OPTOUT_LOG_START.search(code):
        return None
    for kind, rx in _OPTOUT_RULES:
        if rx.search(code):
            return kind
    return None


def optout_lines(text: str, added=None) -> tuple[set[int], list[dict]]:
    """PURE. (honoured opt-out line numbers, rejected opt-outs). A rejected
    opt-out is a marker on a behavioural line; it is reported, never honoured.
    `added` restricts the REPORT to the dispatch's added lines (a marker on an
    untouched pre-existing line generates no mutants either way)."""
    honoured, rejected = set(), []
    for i, line in enumerate((text or "").split("\n"), 1):
        if not RELEVANCE_OPT_OUT.search(line):
            continue
        kind = behavioural_optout_kind(line)
        if kind is None:
            honoured.add(i)
        elif added is None or i in added:
            rejected.append({"line": i, "kind": kind, "text": line.strip()[:160]})
    return honoured, rejected

CMP_FLIPS = {
    ast.Lt: ["<=", ">="], ast.LtE: ["<", ">"],
    ast.Gt: [">=", "<="], ast.GtE: [">", "<"],
    ast.Eq: ["!="], ast.NotEq: ["=="],
    ast.Is: ["is not"], ast.IsNot: ["is"],
    ast.In: ["not in"], ast.NotIn: ["in"],
}
CMP_TEXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
            ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not",
            ast.In: "in", ast.NotIn: "not in"}
BIN_FLIPS = {
    ast.Add: ["-"], ast.Sub: ["+"], ast.Mult: ["+", "//"], ast.Div: ["*"],
    ast.FloorDiv: ["*"], ast.Mod: ["//"], ast.Pow: ["*"],
}
BIN_TEXT = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
            ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}


# --------------------------------------------------------------------------
# REAL-I/O ADAPTERS (2026-10-05, chat-frontend-plan s5-model-call)
# --------------------------------------------------------------------------
# s5's `call_model(request, http_func=None)` used `(http_func or _default_http)`,
# where `_default_http` is a 6-line urllib.request.urlopen wrapper. A HERMETIC
# fixture injects a fake http_func, so by design it never runs `_default_http`;
# every mutant there survived, the site read UNEXERCISED -> LOW -> NO-GO, and the
# author/refine loop burned 14 jobs trying to kill mutants no hermetic case can
# reach (the only way is a real network call -- which a verify must not make).
#
# The exemption is FUNCTION-level, and it is neither silent nor name-based:
#   1. EXPLICIT: the function must be declared, by name, under a
#      `## Real-I/O adapters` heading in TASK.md (bullets of `name`).
#   2. VALIDATED, structurally -- a declaration that fails any rule is REJECTED
#      (its mutants stay counted) and reported:
#        a. its body calls a real-I/O primitive (IO_PRIMITIVE_CALLS: urlopen,
#           requests/httpx verbs, socket, subprocess, os.system ...);
#        b. it is reached ONLY as an injectable default -- every reference to it
#           elsewhere in the module is `x or F`, `F if .. else ..`, a parameter
#           default, `x = F` under an `if`, or a keyword argument value; NEVER
#           a direct call `F(...)`, and at least one such reference exists;
#        c. it is THIN: at most ADAPTER_MAX_STMTS statements. Logic does not
#           hide in an adapter -- a fat function fails (c) and stays mutated.
#   3. LOGGED: excluded mutants are generated, reported (io_adapter on each
#      mutant, rec["io_adapters"], a suffix on the reason) and dropped from the
#      evidence -- the same treatment as literal-breaking mutants.
# The CALL SITE (`http_func or _default_http`) is not inside the adapter and stays
# mutated: `or` -> `and` there routes an injected call to the real adapter and a
# hermetic fixture kills it.
IO_ADAPTER_HEADING = re.compile(r"##+\s*Real[- ]I/?O adapters?[^\n]*\n(.*?)(?=\n##\s|\Z)",
                                re.S | re.I)
ADAPTER_MAX_STMTS = 12
IO_PRIMITIVE_CALLS = {
    "urlopen", "urllib.request.urlopen", "request.urlopen",
    "http.client.HTTPConnection", "http.client.HTTPSConnection",
    "HTTPConnection", "HTTPSConnection",
    "socket.socket", "socket.create_connection", "create_connection",
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output", "Popen",
    "check_output", "check_call", "os.system", "os.popen",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "aiohttp.ClientSession", "smtplib.SMTP", "smtplib.SMTP_SSL",
    "httpx.Client", "httpx.AsyncClient", "requests.Session",
} | {f"{lib}.{verb}" for lib in ("requests", "httpx")
     for verb in ("get", "post", "put", "patch", "delete", "head", "request")}


def declared_io_adapters(task_text: str) -> list[str]:
    """Function names declared under `## Real-I/O adapters` in TASK.md."""
    m = IO_ADAPTER_HEADING.search(task_text or "")
    if not m:
        return []
    return list(dict.fromkeys(
        b.group(1) for b in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)`", m.group(1))))


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _module_defs(tree) -> dict:
    """name -> FunctionDef for module-level and class-level defs (first wins)."""
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, n)
        elif isinstance(n, ast.ClassDef):
            for c in n.body:
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.setdefault(c.name, c)
    return out


def _adapter_problem(tree, fn, parents) -> str:
    """'' when `fn` is a thin, injected-default real-I/O adapter, else why not."""
    calls = {_dotted(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    if not (calls & IO_PRIMITIVE_CALLS):
        return "its body calls no real-I/O primitive (urlopen/requests/socket/subprocess...)"
    n_stmts = sum(1 for s in ast.walk(fn) if isinstance(s, ast.stmt)) - 1
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(
            getattr(fn.body[0], "value", None), ast.Constant):
        n_stmts -= 1     # docstring
    if n_stmts > ADAPTER_MAX_STMTS:
        return (f"it is not thin ({n_stmts} statements > {ADAPTER_MAX_STMTS}); "
                f"logic in an adapter must stay under mutation")
    span = (fn.lineno, fn.end_lineno or fn.lineno)
    refs = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == fn.name
                and isinstance(node.ctx, ast.Load)):
            continue
        if span[0] <= node.lineno <= span[1]:
            continue
        par = parents.get(id(node))
        ok = False
        if isinstance(par, ast.BoolOp) and isinstance(par.op, ast.Or) \
                and par.values and par.values[0] is not node:
            ok = True                                     # x or F
        elif isinstance(par, ast.IfExp) and par.test is not node:
            ok = True                                     # F if x is None else x
        elif isinstance(par, ast.arguments):
            ok = True                                     # def g(f=F)
        elif isinstance(par, ast.keyword):
            ok = True                                     # g(http_func=F)
        elif isinstance(par, ast.Assign) and par.value is node \
                and isinstance(parents.get(id(par)), ast.If):
            ok = True                                     # if f is None: f = F
        if not ok:
            where = ("a DIRECT call" if isinstance(par, ast.Call) and par.func is node
                     else type(par).__name__ if par is not None else "?")
            return (f"line {node.lineno} references it as {where}, not as an "
                    f"injectable default -- a hermetic test can reach that path")
        refs += 1
    if not refs:
        return "nothing references it as an injectable default (x or F / f=F)"
    return ""


def python_io_adapters(fixed_text: str, declared) -> dict:
    """{'accepted': {name: (start, end)}, 'rejected': {name: why},
        'candidates': {name: (start, end)}} for one Python file. Candidates are
    UNDECLARED functions that pass every rule -- reported as a hint, never excluded."""
    res = {"accepted": {}, "rejected": {}, "candidates": {}}
    try:
        tree = ast.parse(fixed_text)
    except SyntaxError:
        return res
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    defs = _module_defs(tree)
    declared = set(declared or ())
    for name, fn in defs.items():
        span = (fn.lineno, fn.end_lineno or fn.lineno)
        why = _adapter_problem(tree, fn, parents)
        if name in declared:
            if why:
                res["rejected"][name] = why
            else:
                res["accepted"][name] = span
        elif not why:
            res["candidates"][name] = span
    return res


def _span_name(spans: dict, lo: int, hi: int):
    for name, (a, b) in spans.items():
        if a <= lo and hi <= b:
            return name
    return None


# --------------------------------------------------------------------------
# diff parsing
# --------------------------------------------------------------------------
def parse_unified_diff(diff_text: str) -> dict[str, dict]:
    """Per file: added line numbers (in the NEW file) and the hunks.

    Each hunk is {new_start, new_len, old_lines, new_lines} so a hunk can be
    reverted on its own (the language-agnostic partial revert).
    """
    files: dict[str, dict] = {}
    cur = None
    hunk = None
    for ln in diff_text.splitlines():
        if ln.startswith("+++ "):
            name = ln[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if name == "/dev/null":
                cur = None
                continue
            cur = files.setdefault(name, {"added": set(), "hunks": []})
            hunk = None
            continue
        if cur is None:
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m:
            hunk = {"old_start": int(m.group(1)),
                    "new_start": int(m.group(3)),
                    "old_lines": [], "new_lines": [],
                    "plus": [], "minus": []}
            cur["hunks"].append(hunk)
            new_no = int(m.group(3))
            continue
        if hunk is None or ln.startswith("\\"):
            continue
        if ln.startswith("+"):
            hunk["new_lines"].append(ln[1:])
            hunk["plus"].append((new_no, ln[1:]))
            cur["added"].add(new_no)
            new_no += 1
        elif ln.startswith("-"):
            hunk["old_lines"].append(ln[1:])
            hunk["minus"].append(ln[1:])
        else:
            # context line (only present when the diff was not -U0)
            hunk["old_lines"].append(ln[1:] if ln.startswith(" ") else ln)
            hunk["new_lines"].append(ln[1:] if ln.startswith(" ") else ln)
            new_no += 1
    return files


def _is_comment_only(lines) -> bool:
    for l in lines:
        s = l.strip()
        if s and not s.startswith(COMMENT_PREFIXES):
            return False
    return True


def reindent_only_added(hunks: list[dict]) -> set[int]:
    """New-file line numbers of ADDED lines that are only a re-indent of a line
    the SAME hunk removed (identical once stripped). For whitespace-insensitive
    languages only (the caller never applies this to Python, where indentation
    IS the block structure).

    WHY (2026-10-06, rt-giftcard-copy-to-remaining b5c740c54701): wrapping two
    existing <input> elements in a new <div> with a button re-indented their
    unchanged `onChange={e => updateRow(...)}` lines; the mutator treated them as
    changed sites, every mutant on them survived (the fixture tests the new pure
    helper, not pre-existing JSX), and relevance read LOW naming lines the
    dispatch never changed.

    Guard against weakening: a hunk whose added lines are ALL re-indents (a
    pure reshuffle / un-wrap -- e.g. an `if` removed around an existing call) is
    left alone, because there the re-indented lines are the only place the
    behaviour change can be evidenced. Only a hunk that ALSO adds genuinely new
    lines (the wrapper / guard / new element, which carry their own mutants)
    drops its re-indented copies."""
    out: set[int] = set()
    for h in hunks:
        plus = h.get("plus") or []
        if not plus:
            continue
        pool: dict[str, int] = {}
        for t in h.get("minus") or []:
            s = t.strip()
            if s:
                pool[s] = pool.get(s, 0) + 1
        moved = []
        for no, t in plus:
            s = t.strip()
            if s and pool.get(s, 0) > 0:
                pool[s] -= 1
                moved.append(no)
        fresh = [no for no, t in plus if t.strip() and no not in moved]
        if moved and fresh:
            out.update(moved)
    return out


_TS_TYPE_ALIAS_RE = re.compile(r"^(export\s+)?(declare\s+)?type\s+[A-Za-z_$][\w$]*"
                               r"(<[^=]*>)?\s*=.*;\s*$")


def _is_ts_type_only(lines) -> bool:
    """Every non-blank, non-comment line is a ONE-LINE TS type alias
    (`type X = {...};`). Type aliases are erased at runtime, so moving or
    reverting such a hunk cannot change behaviour -- a hunk-revert mutant of it
    is equivalent by construction (b5c740c54701: reverting the move of
    `type DraftRow = ...` out of the component "survived" and read as an
    unexercised site). Deliberately narrow: multi-line interfaces, enums (which
    DO emit code) and anything else fall through as material."""
    seen = False
    for l in lines:
        s = l.strip()
        if not s or s.startswith(COMMENT_PREFIXES):
            continue
        if not _TS_TYPE_ALIAS_RE.match(s):
            return False
        seen = True
    return seen


# --------------------------------------------------------------------------
# span-based source editing (preserves every byte the mutation does not touch)
# --------------------------------------------------------------------------
class Src:
    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        # ast col offsets are UTF-8 BYTE offsets.
        self.blines = [l.encode("utf-8") for l in self.lines]

    def offset(self, lineno: int, col: int) -> int:
        """Character offset into self.text for (1-based line, byte col)."""
        off = 0
        for i in range(lineno - 1):
            off += len(self.lines[i]) + 1
        return off + len(self.blines[lineno - 1][:col].decode("utf-8", "replace"))

    def span(self, node) -> tuple[int, int]:
        return (self.offset(node.lineno, node.col_offset),
                self.offset(node.end_lineno, node.end_col_offset))

    def get(self, a: int, b: int) -> str:
        return self.text[a:b]

    def replace(self, a: int, b: int, new: str) -> str:
        return self.text[:a] + new + self.text[b:]


def _call_name(node) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_innocuous_stmt(stmt) -> bool:
    if isinstance(stmt, ast.Expr):
        v = stmt.value
        if isinstance(v, ast.Constant):          # docstring / bare literal
            return True
        if isinstance(v, ast.Call):
            name = _call_name(v)
            root = v.func
            while isinstance(root, ast.Attribute):
                root = root.value
            rootname = root.id if isinstance(root, ast.Name) else ""
            if (name in INNOCUOUS_CALL_NAMES or rootname in INNOCUOUS_CALL_NAMES
                    or name.startswith("log")
                    or name in CLEANUP_CALL_NAMES):
                # CLEANUP as a bare statement only (this branch is ast.Expr):
                # deleting `conn.close()` leaks a handle no in-process fixture
                # can observe. A cleanup call whose return is USED goes through
                # the Assign branch below, which does NOT exempt it.
                return True
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
        if _call_name(stmt.value) in INNOCUOUS_CALL_NAMES:
            return True
    if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Assert, ast.Pass,
                         ast.Global, ast.Nonlocal, ast.FunctionDef,
                         ast.AsyncFunctionDef, ast.ClassDef)):
        return True
    return False


def _inside_innocuous_call(node, parents) -> bool:
    for p in parents:
        if isinstance(p, ast.Call):
            name = _call_name(p)
            if name in INNOCUOUS_CALL_NAMES or name.startswith("log"):
                return True
    return False


def _inside_formatting_call(node, parents) -> bool:
    p = parents[-1] if parents else None
    return isinstance(p, ast.Call) and _call_name(p) in FORMATTING_CALL_NAMES


def python_code_lines(text: str) -> set[int] | None:
    """Line numbers carrying CODE: every statement's span minus docstrings.
    None if the file does not parse. A refimpl hunk whose added lines carry
    no code (a docstring edit, a comment block) is an equivalent mutant by
    construction when reverted -- the first real dispatch produced two such
    survivors from its own docstring changes."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            # a compound statement's span covers its body; take only the header
            end = node.end_lineno
            if isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try,
                                 ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                first = node.body[0].lineno if getattr(node, "body", None) else node.lineno
                end = max(node.lineno, first - 1)
            for ln in range(node.lineno, end + 1):
                lines.add(ln)
    # The header approximation above (lineno .. first body stmt - 1) swallows any
    # comment or blank line sitting between a compound statement's colon and its
    # first statement -- the AST has no node for a comment, so nothing else
    # excludes it. Live 2026-09-24 (arr-webhook-yearly-upgrade-batches s11): a
    # refimpl that reworded the comment under `while True:` produced a 1-line
    # hunk, that line read as "code", its hunk-revert became the lone surviving
    # "property-breaking mutant" (11/12 killed, but the site scored 0.0 and so
    # was reported UNEXERCISED -> LOW), and a sound verify was NO-GO'd for a
    # week. A comment-only line carries no code whatever
    # span it falls in; nor does a line the author opted out of relevance with
    # `# relevance: unobservable` -- for hunk materiality, that is the same claim.
    text_lines = text.split("\n")
    honoured, _ = optout_lines(text)
    out: set[int] = set()
    for ln in lines:
        if not (1 <= ln <= len(text_lines)):
            continue
        s = text_lines[ln - 1].strip()
        # a BEHAVIOURAL opt-out (branch/return/comparison/response) stays code
        if not s or s.startswith("#") or ln in honoured:
            continue
        out.add(ln)
    return out


def _line_of(text: str, lineno: int) -> str:
    ls = (text or "").split("\n")
    return ls[lineno - 1].strip()[:120] if 0 < lineno <= len(ls) else ""


class Mutant:
    def __init__(self, path: str, klass: str, desc: str, source: str,
                 lineno: int):
        self.path, self.klass, self.desc, self.source, self.lineno = (
            path, klass, desc, source, lineno)
        self.id = hashlib.sha256(
            f"{path}:{klass}:{lineno}:{desc}:{source}".encode()).hexdigest()[:10]
        self.literal_preserving = True
        self.killed = None
        self.crash = False
        self.seconds = None
        self.snippet = ""       # the MUTATED line
        self.original = ""      # the same line in the refimpl, unmutated
        self.end_lineno = lineno   # last line the mutation touches (hunk-revert: >1)
        self.func = None           # innermost enclosing def (Python), for reports
        self.io_adapter = None     # declared+validated real-I/O adapter -> excluded
        self.io_adapter_candidate = None   # UNDECLARED adapter-shaped fn (hint only)
        self.bank = None           # wrong-solution-bank category (wrong_solution_bank)

    def as_dict(self):
        return {"id": self.id, "file": self.path, "class": self.klass,
                "line": self.lineno, "mutation": self.desc,
                "literal_preserving": self.literal_preserving,
                "killed": self.killed, "crash": self.crash,
                "seconds": self.seconds, "snippet": self.snippet,
                "original": self.original, "func": self.func,
                "io_adapter": self.io_adapter,
                "io_adapter_candidate": self.io_adapter_candidate,
                "bank": self.bank}


# --------------------------------------------------------------------------
# Python AST mutants, restricted to the ADDED lines
# --------------------------------------------------------------------------
def python_mutants(rel: str, fixed_text: str, added: set[int]) -> list[Mutant]:
    try:
        tree = ast.parse(fixed_text)
    except SyntaxError:
        return []
    src = Src(fixed_text)
    out: list[Mutant] = []
    # Lines the author opted out of relevance mutation (allowlist class 3) --
    # minus BEHAVIOURAL lines, whose opt-out is rejected (optout_lines).
    annotated, _ = optout_lines(fixed_text)

    def emit(klass, desc, a, b, new, lineno):
        if lineno in annotated:
            # `# relevance: unobservable` on this line -- author-declared benign.
            return
        text = src.replace(a, b, new)
        if text == fixed_text:
            return
        try:
            compile(text, rel, "exec")
        except SyntaxError:
            return
        m = Mutant(rel, klass, desc, text, lineno)
        lines = text.split("\n")
        m.snippet = lines[lineno - 1].strip()[:120] if lineno - 1 < len(lines) else ""
        m.original = _line_of(fixed_text, lineno)
        out.append(m)

    def in_added(node) -> bool:
        return getattr(node, "lineno", None) in added

    # parent chain for the innocuous-call filter
    parents_of: dict[int, list] = {}

    def walk(node, parents):
        for child in ast.iter_child_nodes(node):
            parents_of[id(child)] = parents + [node]
            walk(child, parents + [node])
    walk(tree, [])

    # ---- statements -------------------------------------------------------
    for node in ast.walk(tree):
        body_lists = []
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                body_lists.append(lst)
        for lst in body_lists:
            for stmt in lst:
                if not in_added(stmt) or _is_innocuous_stmt(stmt):
                    continue
                a, b = src.span(stmt)
                # partial revert: delete the added statement
                if len(lst) > 1 or not isinstance(stmt, (ast.Return,)):
                    emit("stmt-delete", f"delete `{src.get(a, b).splitlines()[0][:60]}`",
                         a, b, "pass", stmt.lineno)
                if isinstance(stmt, (ast.If, ast.While)):
                    ta, tb = src.span(stmt.test)
                    t = src.get(ta, tb)
                    emit("cond-negate", f"negate `{t[:60]}`", ta, tb,
                         f"not ({t})", stmt.lineno)
                    emit("cond-force-true", f"force `{t[:60]}` True", ta, tb,
                         "True", stmt.lineno)
                    emit("cond-force-false", f"force `{t[:60]}` False", ta, tb,
                         "False", stmt.lineno)
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    va, vb = src.span(stmt.value)
                    v = src.get(va, vb)
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                            stmt.value.value, bool):
                        emit("return-flip", f"return {not stmt.value.value}",
                             va, vb, str(not stmt.value.value), stmt.lineno)
                    else:
                        emit("return-none", f"return None instead of `{v[:60]}`",
                             va, vb, "None", stmt.lineno)
                        if isinstance(stmt.value, (ast.Compare, ast.BoolOp,
                                                   ast.UnaryOp, ast.Call)):
                            emit("return-negate", f"return not ({v[:60]})",
                                 va, vb, f"not ({v})", stmt.lineno)
                if isinstance(stmt, ast.AugAssign):
                    op = type(stmt.op)
                    if op in BIN_FLIPS:
                        ta, tb = src.span(stmt.target)
                        va, vb = src.span(stmt.value)
                        between = src.get(tb, va)
                        cur = BIN_TEXT[op] + "="
                        if cur in between:
                            new = between.replace(cur, BIN_FLIPS[op][0] + "=", 1)
                            emit("augassign-op", f"{cur} -> {BIN_FLIPS[op][0]}=",
                                 tb, va, new, stmt.lineno)

    # ---- expressions ------------------------------------------------------
    for node in ast.walk(tree):
        if not in_added(node):
            continue
        parents = parents_of.get(id(node), [])
        if _inside_innocuous_call(node, parents):
            continue
        if isinstance(node, ast.Compare):
            prev = node.left
            for op, comp in zip(node.ops, node.comparators):
                pa, pb = src.span(prev)
                ca, cb = src.span(comp)
                between = src.get(pb, ca)
                cur = CMP_TEXT[type(op)]
                if cur in between:
                    for new_op in CMP_FLIPS[type(op)]:
                        new = between.replace(cur, new_op, 1)
                        emit("compare-flip", f"{cur} -> {new_op}", pb, ca, new,
                             node.lineno)
                prev = comp
        elif isinstance(node, ast.BoolOp):
            cur = "and" if isinstance(node.op, ast.And) else "or"
            new_op = "or" if cur == "and" else "and"
            a0, b0 = src.span(node.values[0])
            a1, b1 = src.span(node.values[1])
            between = src.get(b0, a1)
            if re.search(rf"\b{cur}\b", between):
                emit("boolop-swap", f"{cur} -> {new_op}", b0, a1,
                     re.sub(rf"\b{cur}\b", new_op, between, count=1), node.lineno)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            a, b = src.span(node)
            t = src.get(a, b)
            if t.startswith("not "):
                emit("not-drop", f"drop `not` in `{t[:60]}`", a, b, t[4:],
                     node.lineno)
        elif isinstance(node, ast.BinOp) and type(node.op) in BIN_FLIPS:
            la, lb = src.span(node.left)
            ra, rb = src.span(node.right)
            between = src.get(lb, ra)
            cur = BIN_TEXT[type(node.op)]
            if cur in between:
                for new_op in BIN_FLIPS[type(node.op)]:
                    emit("binop-swap", f"{cur} -> {new_op}", lb, ra,
                         between.replace(cur, new_op, 1), node.lineno)
        elif isinstance(node, ast.Constant):
            if _inside_formatting_call(node, parents):
                continue
            v = node.value
            a, b = src.span(node)
            parent = parents[-1] if parents else None
            if isinstance(v, bool):
                emit("const-bool", f"{v} -> {not v}", a, b, str(not v), node.lineno)
            elif isinstance(v, int) and not isinstance(parent, ast.JoinedStr):
                if isinstance(parent, ast.BoolOp) and parent.values[-1] is node:
                    # A FALLBACK constant (`item.get(k) or 0`). Nudging it by
                    # one is almost always an equivalent mutant: 0 -> 1 is
                    # still under any threshold. The property-breaking form
                    # is "a wrong default", so push it far enough to cross
                    # whatever boundary the code compares against.
                    nv = v + 1000003
                    emit("const-default", f"fallback {v} -> {nv}", a, b, str(nv),
                         node.lineno)
                elif (isinstance(parent, ast.Call)
                        and isinstance(parent.func, ast.Attribute)
                        and parent.func.attr == "get"
                        and len(parent.args) == 2 and parent.args[-1] is node):
                    # A dict `.get(key, DEFAULT)` fallback -- same class as the
                    # `or 0` case above: nudging the default by one is an
                    # equivalent mutant (a missing key defaulting to 0 vs 1 is
                    # still under any real threshold). The property-breaking
                    # form is a wrong default, so push it across the boundary.
                    nv = v + 1000003
                    emit("const-default", f"get-default {v} -> {nv}", a, b,
                         str(nv), node.lineno)
                else:
                    for nv in (v + 1, v - 1):
                        emit("const-int", f"{v} -> {nv}", a, b, str(nv), node.lineno)
            elif isinstance(v, float):
                for nv in (v + 1.0, v * 2 if v else 1.0):
                    emit("const-float", f"{v} -> {nv}", a, b, repr(nv), node.lineno)
            elif isinstance(v, str) and isinstance(
                    parent, (ast.Compare, ast.Subscript, ast.Return, ast.Assign,
                             ast.Dict, ast.keyword)):
                # Only where a string plausibly carries behaviour (a key, a
                # compared value, a returned value) -- never inside messages.
                # Allowlist class 2: a prose kwarg (`detail=`, `message=`) is a
                # human message, not the property -- skip its const-str even
                # though the enclosing call (e.g. HTTPException) is behavioural.
                if isinstance(parent, ast.keyword) and parent.arg in MESSAGE_KWARGS:
                    continue
                q = src.get(a, b)[0]
                if q in ("'", '"') and "\n" not in v:
                    emit("const-str", f"{v!r} -> {v + '_X'!r}", a, b,
                         q + v + "_X" + q, node.lineno)
    return out


# --------------------------------------------------------------------------
# TypeScript / JavaScript AST-span mutants (via the ts-mutator.mjs sidecar)
# --------------------------------------------------------------------------
# The Node sidecar walks the TS compiler-API AST and returns, for the ADDED
# lines only, the same class of property-breaking mutants python_mutants makes
# (compare-flip / boolop-swap / const-* / cond-* / stmt-delete / call-delete),
# each already validated to re-parse. It returns the FULL mutated file text per
# mutant, so this side just wraps it in a Mutant exactly as the Python path does
# -- no cross-language offset math. A single-hunk TS/JS refimpl now yields real
# span mutants (the old "non-Python single hunk -> UNPROVEN" limit is gone for
# these extensions). If Node or the sidecar is unavailable the function returns
# [] and the run falls back to hunk reverts (i.e. UNPROVEN on one hunk) -- the
# safe direction, never a spurious pass.
TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _ts_mutator_path() -> Path | None:
    env = os.environ.get("TS_MUTATOR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    here = Path(__file__).resolve().parent
    for cand in (here / "ts-mutator.mjs", here / "ts-mutator" / "ts-mutator.mjs"):
        if cand.is_file():
            return cand
    return None


def ts_mutants(worktree: Path, rel: str, fixed_text: str,
               added: set[int]) -> list[Mutant]:
    """Mirror of python_mutants for .ts/.tsx/.js/.jsx/.mjs/.cjs via the sidecar.

    The mutated file text is produced by the sidecar (it holds the AST); this
    function only re-homes each mutant into a Mutant with the Python snippet
    convention so scoring, literal-preserving filtering and reporting are byte
    identical to the Python path.
    """
    script = _ts_mutator_path()
    if script is None:
        return []
    target = worktree / rel
    payload = json.dumps({"path": str(target), "addedLines": sorted(added)})
    try:
        p = subprocess.run(["node", str(script)], input=payload,
                           capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if p.returncode != 0 or not p.stdout.strip():
        return []
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return []
    # Lines the author opted out of relevance mutation (allowlist class 3) --
    # same filter as python_mutants, so `// relevance: unobservable` on a JS/TS
    # line drops its mutants exactly like the hash form does in Python.
    # BEHAVIOURAL lines' opt-outs are rejected (optout_lines), as in Python.
    annotated, _ = optout_lines(fixed_text)

    out: list[Mutant] = []
    for md in data.get("mutants", []):
        source = md.get("source")
        lineno = md.get("line")
        if source is None or source == fixed_text or lineno is None:
            continue
        if lineno in annotated:
            # `// relevance: unobservable` on this line -- author-declared benign.
            continue
        m = Mutant(rel, md.get("class", "ts-mutant"), md.get("desc", ""),
                   source, lineno)
        lines = source.split("\n")
        m.snippet = lines[lineno - 1].strip()[:120] if lineno - 1 < len(lines) else ""
        m.original = _line_of(fixed_text, lineno)
        out.append(m)
    return out


# --------------------------------------------------------------------------
# C# token-level mutants (2026-09-27)
# --------------------------------------------------------------------------
# No Roslyn on the Python side, so this is a TOKEN mutator over the ADDED lines,
# with string/char literals and comments masked so nothing inside them is ever
# touched. Every class is chosen to keep the file COMPILING in the common case;
# a mutant that does not compile anyway is detected after its verify run (the
# C# verify prints "FAIL: dotnet build") and dropped from the evidence set --
# a build error is not a behavioural kill.
_CS_SKIP_LINE = re.compile(
    r"^\s*(?://|/\*|\*|using\b|namespace\b|#|\[|"
    r"(?:System\.)?(?:Console|Debug|Trace|Log|Logger|_log|log)\s*\.)")
_CS_TOKEN_MUTS = (
    # (class, regex on the MASKED line, replacement, description)
    ("cmp-flip", re.compile(r"=="), "!=", "== -> !="),
    ("cmp-flip", re.compile(r"!="), "==", "!= -> =="),
    ("boolop-swap", re.compile(r"&&"), "||", "&& -> ||"),
    ("boolop-swap", re.compile(r"\|\|"), "&&", "|| -> &&"),
    # < and > only when spaced: `List<int>` / `a => b` / `x->y` / `<<` stay put
    ("cmp-bound", re.compile(r"(?<=\s)<(?=\s)"), "<=", "< -> <="),
    ("cmp-bound", re.compile(r"(?<=\s)<=(?=\s)"), "<", "<= -> <"),
    ("cmp-bound", re.compile(r"(?<=\s)>(?=\s)"), ">=", "> -> >="),
    ("cmp-bound", re.compile(r"(?<=\s)>=(?=\s)"), ">", ">= -> >"),
    ("const-bool", re.compile(r"\btrue\b"), "false", "true -> false"),
    ("const-bool", re.compile(r"\bfalse\b"), "true", "false -> true"),
)
_CS_INT = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
_CS_RETURN = re.compile(r"\breturn\s+(?!default\b)([^;]+);")
_CS_IF = re.compile(r"\b(if|while)\s*\(")


def _cs_mask(line: str, in_block: bool):
    """(masked line, still-in-block-comment). String/char literals and comments
    become spaces of the same length, so column offsets stay valid."""
    out = list(line)
    i, n = 0, len(line)
    while i < n:
        if in_block:
            j = line.find("*/", i)
            end = n if j < 0 else j + 2
            for k in range(i, end):
                out[k] = " "
            in_block = j < 0
            i = end
            continue
        c = line[i]
        if line.startswith("//", i):
            for k in range(i, n):
                out[k] = " "
            break
        if line.startswith("/*", i):
            in_block = True
            continue
        if c in "\"'":
            verbatim = i > 0 and line[i - 1] == "@"
            j = i + 1
            while j < n:
                if line[j] == "\\" and not verbatim:
                    j += 2
                    continue
                if line[j] == c:
                    if verbatim and j + 1 < n and line[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out), in_block


def _cs_paren_end(masked: str, open_idx: int):
    depth = 0
    for k in range(open_idx, len(masked)):
        if masked[k] == "(":
            depth += 1
        elif masked[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def csharp_mutants(rel: str, fixed_text: str, added: set[int]) -> list[Mutant]:
    lines = fixed_text.split("\n")
    out: list[Mutant] = []
    in_block = False
    for idx, line in enumerate(lines):
        lineno = idx + 1
        masked, in_block_next = _cs_mask(line, in_block)
        was_block, in_block = in_block, in_block_next
        if lineno not in added or was_block:
            continue
        if not masked.strip() or _CS_SKIP_LINE.match(line) \
                or RELEVANCE_OPT_OUT.search(line):
            continue
        cands = []
        for klass, rx, rep_, desc in _CS_TOKEN_MUTS:
            for m in rx.finditer(masked):
                cands.append((klass, desc, m.start(), m.end(), rep_))
        for m in _CS_INT.finditer(masked):
            v = int(m.group(1))
            cands.append(("const-int", f"{v} -> {v + 1}", m.start(1), m.end(1), str(v + 1)))
        for m in _CS_RETURN.finditer(masked):
            if m.group(1).strip():
                cands.append(("return-default", "return <expr> -> return default",
                              m.start(1), m.end(1), "default"))
        for m in _CS_IF.finditer(masked):
            op = m.end() - 1
            cl = _cs_paren_end(masked, op)
            if cl is not None and cl > op + 1:
                cond = line[op + 1:cl]
                cands.append(("cond-negate", f"negate `{m.group(1)}` condition",
                              op + 1, cl, f"!({cond})"))
                cands.append(("cond-force-true", f"force `{m.group(1)}` condition true",
                              op + 1, cl, "true"))
                cands.append(("cond-force-false", f"force `{m.group(1)}` condition false",
                              op + 1, cl, "false"))
        # partial revert: delete ONE added statement line (Python's stmt-delete).
        # A deletion that breaks the build (a declaration, the method's only
        # return) is dropped as uncompilable by the run loop, never counted.
        if masked.rstrip().endswith(";"):
            ind = len(line) - len(line.lstrip())
            cands.append(("stmt-delete", f"delete `{line.strip()[:60]}`",
                          ind, len(line), ""))
        # a non-empty string literal gets a suffix (const-str, like the TS path)
        for m in re.finditer(r'(?<!@)"((?:[^"\\]|\\.)+)"', line):
            if masked[m.start()] == '"':
                cands.append(("const-str", "string -> append _X", m.end() - 1,
                              m.end() - 1, "_X"))
        for klass, desc, a, b, rep_ in cands:
            new_line = line[:a] + rep_ + line[b:]
            if new_line == line:
                continue
            text = "\n".join(lines[:idx] + [new_line] + lines[idx + 1:])
            mu = Mutant(rel, klass, desc, text, lineno)
            mu.snippet = new_line.strip()[:120]
            mu.original = line.strip()[:120]
            out.append(mu)
    return out


# --------------------------------------------------------------------------
# language-agnostic hunk reverts
# --------------------------------------------------------------------------
def hunk_revert_mutants(rel: str, fixed_text: str, hunks: list[dict],
                        code_lines: set[int] | None = None,
                        report: list | None = None) -> list[Mutant]:
    """Revert ONE hunk of the refimpl while keeping the others: the partial
    revert. Needs >= 2 material hunks, otherwise it is the full revert (already
    proven by baseline-fails) and says nothing new."""
    def material_hunk(h):
        if code_lines is not None:
            new_nos = range(h["new_start"], h["new_start"] + len(h["new_lines"]))
            return any(n in code_lines for n in new_nos)
        if (rel.endswith((".ts", ".tsx", ".mts", ".cts"))
                and _is_ts_type_only(list(h["new_lines"]) + list(h["old_lines"]))):
            return False   # type aliases erase at runtime: revert is equivalent
        return (not _is_comment_only(h["new_lines"])
                or not _is_comment_only(h["old_lines"]))
    material = [h for h in hunks if material_hunk(h)]
    if report is not None:
        # PER-HUNK NECESSITY (necessity_pass): every hunk that is NOT subject to
        # the gate is recorded with its reason -- the exemptions are explicit.
        for h in hunks:
            if h not in material:
                report.append({"file": rel, "hunk": h["new_start"],
                               "reason": "non-material hunk (comment-only / "
                                         "type-only / no executable line): erases "
                                         "at runtime, reverting it is equivalent"})
        if len(material) < 2 and material:
            report.append({"file": rel, "hunk": material[0]["new_start"],
                           "reason": "single material hunk (whole-file creation "
                                     "or one-hunk fix): its full revert is the "
                                     "baseline-fails proof"})
    if len(material) < 2:
        return []
    out = []
    lines = fixed_text.split("\n")
    for h in material:
        start = h["new_start"] - 1
        n_new = len(h["new_lines"])
        # sanity: the fixed text must carry the hunk where the diff says
        if lines[start:start + n_new] != h["new_lines"]:
            continue
        new_lines = lines[:start] + h["old_lines"] + lines[start + n_new:]
        text = "\n".join(new_lines)
        m = Mutant(rel, "hunk-revert",
                   f"revert hunk @{h['new_start']} ({n_new} added line(s))",
                   text, h["new_start"])
        m.snippet = (h["new_lines"][0].strip()[:120] if h["new_lines"] else "")
        m.original = m.snippet
        m.end_lineno = h["new_start"] + max(n_new, 1) - 1
        out.append(m)
    return out


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
def _pgrun():
    """~/bin/pgrun.py (own process group, whole-group kill on timeout), else None."""
    import importlib.util as _ilu
    f = Path(__file__).resolve().parent / "pgrun.py"
    try:
        spec = _ilu.spec_from_file_location("pgrun", f)
        m = _ilu.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


_PGRUN = _pgrun()


def _run_verify(cmd: str, cwd: Path, timeout: int) -> tuple[int, str, float]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DISPATCH_VERIFY_SANDBOX"] = "1"   # VERIFY-SANDBOX: never the real queue
    t0 = time.time()
    if _PGRUN is not None:
        # A plain subprocess.run timeout killed only `sh -c`; an infinite-loop
        # mutant's node/tsx grandchildren then ran at 100% CPU for hours (Rivian s3,
        # 2026-10-02: five orphans, 4.5h). The whole group dies on timeout now.
        rc, so, se, to = _PGRUN.run_group(cmd, cwd=cwd, env=env, timeout=timeout)
        return rc, so + se + ("\n[timeout]" if to else ""), time.time() - t0
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace")
               if isinstance(e.stdout, bytes) else (e.stdout or "")) + "\n[timeout]"
        rc = 124
    return rc, out, time.time() - t0


# Per-MUTANT timeout: a mutant that hangs is already "killed" (not green), so
# waiting the full verify_timeout (900s) for it only burns the budget. Bound it by
# the measured green run instead: MUTANT_TIMEOUT_FACTOR x the green verify's
# seconds, floored at MUTANT_TIMEOUT_FLOOR_S and never above verify_timeout.
MUTANT_TIMEOUT_FACTOR = 10
MUTANT_TIMEOUT_FLOOR_S = 120


def mutant_timeout(green_secs, verify_timeout):
    """PURE. None green_secs (the green run was skipped) -> verify_timeout."""
    if not green_secs:
        return verify_timeout
    return int(min(verify_timeout, max(MUTANT_TIMEOUT_FLOOR_S,
                                       MUTANT_TIMEOUT_FACTOR * green_secs)))


def _clear_pycache(root: Path):
    for d in root.rglob("__pycache__"):
        if ".venv" in d.parts or "venv" in d.parts:
            continue
        shutil.rmtree(d, ignore_errors=True)


def _green(rc: int, out: str) -> bool:
    return rc == 0 and "VERIFY_OK" in out


def must_contain_literals(task_text: str) -> list[str]:
    """Same extraction as ollama-dispatch-preflight.must_contain_literals."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)",
                  task_text, re.S | re.I)
    if not m:
        return []
    return [b.group(1) for b in re.finditer(r"`([^`\n]{4,200})`", m.group(1))]


_GREP_RE = re.compile(r"""\bgrep\b(?:\s+-[A-Za-z]+)*\s+(?:-e\s+)?(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")
_RUNS_RE = re.compile(r"""(?:^|[|;&(]\s*)(?:bash|sh|python3?|"?\$\{?\w+\}?"?)\s+(?:-\S+\s+)*([\w./-]+\.(?:sh|py))""", re.M)


def verify_grep_literals(worktree: Path, verify_cmd: str) -> list[str]:
    """Literals the verify itself GREPS for, in verify.sh and scripts it runs.

    Why these join the literal-preserving filter: a mutant that deletes the
    exact line a proxy verify greps for is "killed" -- by textual coincidence,
    not by any behaviour. Measured before this extractor existed, a pure
    grep-two-lines verify scored 0.45 because half the mutants happened to
    rewrite one of its two grepped lines. Excluding grep-asserted text from the
    evidence set is what makes a grep proxy score what it deserves. It cannot
    see every proxy shape (a Python `"x" in open(f).read()` is not extracted);
    those still score low because most mutants leave their literal intact.
    Unescapes the common grep escapes (\\* \\. \\[) so the text matches source.
    """
    texts = []
    frontier = [verify_cmd]
    seen = set()
    for _ in range(3):
        nxt = []
        for chunk in frontier:
            for m in _RUNS_RE.finditer(chunk):
                rel = m.group(1)
                if rel in seen:
                    continue
                seen.add(rel)
                f = worktree / rel
                if f.is_file():
                    try:
                        t = f.read_text(errors="replace")
                    except Exception:
                        continue
                    texts.append(t)
                    nxt.append(t)
        frontier = nxt
    out = []
    for t in texts:
        for m in _GREP_RE.finditer(t):
            lit = m.group(1) if m.group(1) is not None else m.group(2)
            lit = re.sub(r"\\([*.\[\]()+?^$|])", r"\1", lit)
            if lit and len(lit) >= 3 and not lit.startswith("-"):
                out.append(lit)
    return sorted(set(out))


def _python_func_spans(text: str) -> list:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [(n.lineno, n.end_lineno or n.lineno, n.name) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _innermost_func(spans, lineno):
    best = None
    for a, b, name in spans:
        if a <= lineno <= b and (best is None or (b - a) < (best[1] - best[0])):
            best = (a, b, name)
    return best[2] if best else None


def tag_io_adapters(rel: str, fixed: str, ms: list, declared, notes: dict) -> None:
    """Mark mutants inside a DECLARED + validated real-I/O adapter (excluded from
    evidence) and inside an undeclared adapter-shaped function (hint only).
    Records every decision in notes['io_adapters'] -- never silent."""
    spans = _python_func_spans(fixed)
    for m in ms:
        m.func = _innermost_func(spans, m.lineno)
    ad = python_io_adapters(fixed, declared)
    rep = notes.setdefault("io_adapters", {"declared": list(declared or ()),
                                           "excluded": [], "rejected": [],
                                           "candidates": []})
    for name, why in ad["rejected"].items():
        rep["rejected"].append({"name": name, "file": rel, "reason": why})
    for name, (a, b) in ad["accepted"].items():
        n = 0
        for m in ms:
            if a <= m.lineno and m.end_lineno <= b:
                m.io_adapter = name
                n += 1
        rep["excluded"].append({"name": name, "file": rel, "lines": f"{a}-{b}",
                                "excluded_mutants": n})
    for name, (a, b) in ad["candidates"].items():
        for m in ms:
            if a <= m.lineno and m.end_lineno <= b:
                m.io_adapter_candidate = name
        rep["candidates"].append({"name": name, "file": rel, "lines": f"{a}-{b}"})


def generate(worktree: Path, diff_text: str, literals,
             io_adapters=()) -> tuple[list[Mutant], dict]:
    """All candidate mutants for the applied refimpl, plus generation notes."""
    files = parse_unified_diff(diff_text)
    mutants: list[Mutant] = []
    notes = {"files": {}, "skipped_files": []}
    if io_adapters:
        notes["io_adapters"] = {"declared": list(io_adapters), "excluded": [],
                                "rejected": [], "candidates": []}
    for rel, info in files.items():
        p = worktree / rel
        if not p.is_file():
            notes["skipped_files"].append(f"{rel} (not a file)")
            continue
        try:
            fixed = p.read_text()
        except Exception as e:
            notes["skipped_files"].append(f"{rel} ({type(e).__name__})")
            continue
        ms = []
        code_lines = None
        if rel.endswith((".py",) + TS_EXTS):
            _, _rej = optout_lines(fixed, info["added"])
            for r in _rej:
                notes.setdefault("optout_rejected", []).append(dict(r, file=rel))
        if rel.endswith(".py"):
            ms += python_mutants(rel, fixed, info["added"])
            code_lines = python_code_lines(fixed)
        elif rel.endswith(TS_EXTS):
            _reind = reindent_only_added(info["hunks"])
            if _reind:
                notes.setdefault("reindent_only", {})[rel] = sorted(_reind)
            ms += ts_mutants(worktree, rel, fixed, info["added"] - _reind)
        elif rel.endswith(".cs"):
            ms += csharp_mutants(rel, fixed, info["added"])
        ms += hunk_revert_mutants(rel, fixed, info["hunks"], code_lines)
        if rel.endswith(".py"):
            tag_io_adapters(rel, fixed, ms, io_adapters, notes)
        # filter 2: a mutant that drops a Must-contain literal is not evidence.
        # Only literals the FIXED file actually contains can be BROKEN by a
        # mutant of it. A literal absent from the target -- e.g. a target PATH or
        # a log-pattern the verify greps out of tsc/stderr OUTPUT rather than out
        # of source (the TS/JS verifies do exactly this: `grep -F <target>` and
        # an ERR_MODULE regex against a build log) -- is otherwise "absent from
        # every mutant" and marks the WHOLE set literal-breaking, starving the
        # evidence set to zero and forcing a spurious UNPROVEN. Restricting to
        # file-present literals is strictly safe: it can only ADD evidence
        # mutants (raising the kill bar), never license a spurious "relevant".
        file_lits = [l for l in literals if l in fixed]
        for m in ms:
            m.literal_preserving = all(l in m.source for l in file_lits)
        # dedupe identical sources
        seen = set()
        uniq = []
        dead = python_unreachable_lines(fixed) if rel.endswith(".py") else set()
        for m in ms:
            if m.source in seen:
                continue
            seen.add(m.source)
            # EQUIVALENT-MUTANT FILTER, deterministic (equivalent_reason): an
            # identical / AST-equal / comment-or-whitespace-only / dead-branch
            # mutant cannot be killed by ANY test, so it is not evidence.
            why = equivalent_reason(m, fixed, dead)
            if why:
                notes.setdefault("equivalent_filtered", []).append(
                    {"file": rel, "line": m.lineno, "class": m.klass,
                     "mutation": m.desc, "reason": why})
                continue
            uniq.append(m)
        notes["files"][rel] = {"added_lines": len(info["added"]),
                               "hunks": len(info["hunks"]),
                               "mutants": len(uniq)}
        mutants += uniq
    return mutants, notes


def _site_of(m: Mutant) -> tuple:
    return (m.path, m.lineno)


def _balanced_sample(mutants: list[Mutant], cap: int) -> list[Mutant]:
    """Order mutants so that EVERY SITE is tried before any site is tried
    twice, rotating classes within a site; then truncate to cap.

    The first version balanced across CLASSES only. Measured (2026-09-03,
    test-verify-relevance's own relevant fixture, 11 mutants over 3 sites):
    with --max-mutants 3 or 5 it tried sites L7 and L9 seven times between
    them and never touched L8 (`return False`), and still said RELEVANT with
    truncated=true. On the real wt-tokens dispatch the cap of 40 spent seven
    mutants on one line and left three whole hunks untried. A verdict over a
    sample that skipped a site is a verdict about the verify's coverage of
    SOME of the change; measure_applied() now reports the untried sites and
    refuses to call such a run relevant.

    Ordering is deterministic (sorted sites, sorted classes) so a re-run is
    the same run. Always returns the FULL ordering when nothing is dropped.
    """
    by_site: dict[tuple, dict[str, list[Mutant]]] = {}
    for m in mutants:
        by_site.setdefault(_site_of(m), {}).setdefault(m.klass, []).append(m)
    # per-site round-robin over classes
    per_site: dict[tuple, list[Mutant]] = {}
    for s, by_k in by_site.items():
        order, seq, i = sorted(by_k), [], 0
        while True:
            progressed = False
            for k in order:
                if i < len(by_k[k]):
                    seq.append(by_k[k][i]); progressed = True
            if not progressed:
                break
            i += 1
        per_site[s] = seq
    sites = sorted(per_site)
    out: list[Mutant] = []
    i = 0
    while len(out) < len(mutants):
        progressed = False
        for s in sites:
            if i < len(per_site[s]):
                out.append(per_site[s][i]); progressed = True
        if not progressed:
            break
        i += 1
    return out[:cap]


def _source_text_scan(worktree, verify_cmd, diff_text):
    """source_text_harness.scan_worktree, loaded from next to this file.
    Fail-OPEN (None) when the module is absent or errors: the mutation
    measurement still runs, exactly as before this check existed."""
    try:
        import importlib.util as _ilu
        p = Path(__file__).resolve().parent / "source_text_harness.py"
        if not p.is_file():
            return None
        sp = _ilu.spec_from_file_location("source_text_harness", str(p))
        m = _ilu.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m.scan_worktree(worktree, verify_cmd, diff_text)
    except Exception:
        return None


def measure_applied(worktree: Path, verify_cmd: str, diff_text: str, **kw) -> dict:
    """measure_applied_core + the REAL-I/O ADAPTER report folded into `reason`, so
    every consumer (preflight line, gate.json, escalation text) shows an exclusion,
    a rejected declaration, or an undeclared adapter-shaped survivor. Never silent.

    SOURCE-TEXT HARNESS (2026-10-05, rt-bfmr-link-sync-feedback): a fixture that
    readFileSync/open()s the target and asserts on its TEXT kills every mutant
    of a line by regexing that line -- discrimination without relevance. That
    is decided STATICALLY first (source_text_harness.py); a dominated/never-
    executes fixture is LOW before any mutant runs, with no survivors, so the
    refine loop is handed the fix ("execute the code") rather than survivors to
    "kill" with more regexes. A `mixed` fixture is measured and annotated."""
    sth = _source_text_scan(worktree, verify_cmd, diff_text)
    if sth and sth.get("verdict") == "source-text":
        return {
            "verdict": "low", "score": 0.0,
            "threshold": kw.get("threshold", DEFAULT_THRESHOLD),
            "min_mutants": kw.get("min_mutants", DEFAULT_MIN_MUTANTS),
            "evidence_mutants": 0, "killed": 0, "survived": 0, "crash_kills": 0,
            "literal_breaking": 0, "generated": 0, "tried": 0, "truncated": False,
            "survivors": [], "mutants": [], "seconds": 0.0,
            "io_adapter_candidate_survivors": [],
            "source_text_harness": {k: sth.get(k) for k in
                                    ("verdict", "reason", "fix", "examples", "targets")},
            "reason": ("SOURCE-TEXT HARNESS: " + str(sth.get("reason") or "")
                       + " Mutation relevance is meaningless here (a regex over a "
                         "line kills its mutants by textual coincidence), so it was "
                         "not measured. FIX: " + str(sth.get("fix") or "")),
        }
    kw = dict(kw)
    post_bs = kw.pop("post_budget_s", None)
    do_nec = kw.pop("necessity", True)
    do_bank = kw.pop("wrong_bank", True)
    llm_equiv_cmd = kw.pop("llm_equivalence_cmd", None)
    llm_wrong_cmd = kw.pop("llm_wrong_solutions_cmd", None)
    rec = _measure_applied_core(worktree, verify_cmd, diff_text, **kw)
    objs = rec.pop("_objs", None)
    if objs is not None:
        try:
            if post_bs is not None:
                kw["post_budget_s"] = post_bs
            apply_post_gates(worktree, verify_cmd, diff_text, rec, objs, kw,
                             necessity=do_nec, bank=do_bank,
                             llm_equiv_cmd=llm_equiv_cmd, llm_wrong_cmd=llm_wrong_cmd)
        except Exception as e:     # fail CLOSED: an unrunnable gate is UNPROVEN, never a pass
            rec["post_gate_error"] = f"{type(e).__name__}: {e}"
            if rec.get("verdict") == "relevant":
                rec["verdict"] = "unproven"
                rec["reason"] = ("necessity / wrong-solution post-gate crashed ("
                                 + rec["post_gate_error"] + ") -- relevance not proven")
    # BEHAVIOURAL OPT-OUTS (optout_lines): never honoured, always reported -- on
    # a PASS too, so the coordinator sees the author tried to exempt a decision.
    _rej = (rec.get("generation") or {}).get("optout_rejected") or []
    rec["optout_rejected"] = _rej
    if _rej:
        rec["reason"] = ((rec.get("reason") or "") + " [IGNORED " + str(len(_rej))
                         + " `relevance: unobservable` opt-out(s) on BEHAVIOURAL lines "
                         "(their mutants were counted): "
                         + "; ".join(f"{r['file']}:{r['line']} ({r['kind']}) `{r['text'][:80]}`"
                                     for r in _rej[:6])
                         + " -- a branch/return/comparison/response line is the property; "
                           "kill its mutants with a case, or DELETE the line if redundant]")
    if sth and sth.get("verdict") == "mixed":
        rec["source_text_harness"] = {k: sth.get(k) for k in
                                      ("verdict", "reason", "fix", "examples", "targets")}
        rec["reason"] = ((rec.get("reason") or "") + " [SOURCE-TEXT ASSERTIONS (minority, "
                         "advisory): " + str(sth.get("reason") or "")[:400]
                         + " -- kills by these are textual, not behavioural]")
    ad = rec.get("io_adapters") or {}
    notes = []
    if ad.get("excluded"):
        names = ", ".join(f"{e['name']} ({e['file']}:{e['lines']})" for e in ad["excluded"])
        notes.append(f"real-I/O adapter(s) declared in TASK.md: {names} -- "
                     f"{rec.get('io_adapter_excluded', 0)} mutant(s) EXCLUDED from "
                     f"evidence (see io_adapters)")
    if ad.get("rejected"):
        notes.append("REJECTED `## Real-I/O adapters` declaration(s) -- their mutants "
                     "still count: " + "; ".join(f"{r['name']}: {r['reason']}"
                                                  for r in ad["rejected"]))
    cand = sorted({sv.get("io_adapter_candidate") for sv in rec.get("survivors") or []
                   if sv.get("io_adapter_candidate")})
    rec["io_adapter_candidate_survivors"] = cand
    if cand:
        notes.append("survivors inside " + ", ".join(f"`{c}`" for c in cand)
                     + ": a thin real-I/O adapter reached ONLY when no fake is "
                     "injected -- a hermetic case cannot kill these. If that is what "
                     "it is, declare it under `## Real-I/O adapters` in TASK.md "
                     "(validated + reported, never silent); do NOT add network cases")
    if notes:
        rec["reason"] = (rec.get("reason") or "") + " [" + "] [".join(notes) + "]"
    return rec


def _measure_applied_core(worktree: Path, verify_cmd: str, diff_text: str, *,
                    literals=(), threshold=DEFAULT_THRESHOLD,
                    min_mutants=DEFAULT_MIN_MUTANTS, max_mutants=DEFAULT_MAX_MUTANTS,
                    budget_s=DEFAULT_BUDGET_S, verify_timeout=VERIFY_TIMEOUT_S,
                    skip_green_check=False, progress=None, green_secs=None,
                    io_adapters=()) -> dict:
    """Measure relevance on a tree that ALREADY carries the reference impl.

    `io_adapters`: function names TASK.md declares under `## Real-I/O adapters`
    (declared_io_adapters). Only declarations that pass the structural rules
    (python_io_adapters) exclude anything; every decision is in rec["io_adapters"].

    Every mutated file is restored to its fixed content after each mutant, so
    the tree leaves exactly as it came (still carrying the fix). The caller
    owns applying and reverting the refimpl -- the preflight gate already does
    both around this call.
    """
    worktree = Path(worktree)
    t_start = time.time()
    rec = {
        "verdict": "unproven", "score": None, "threshold": threshold,
        "min_mutants": min_mutants, "evidence_mutants": 0, "killed": 0,
        "survived": 0, "crash_kills": 0, "literal_breaking": 0,
        "generated": 0, "tried": 0, "truncated": False, "survivors": [],
        "mutants": [], "reason": "", "seconds": 0.0,
    }
    if not skip_green_check:
        rc, out, secs = _run_verify(verify_cmd, worktree, verify_timeout)
        green_secs = secs
        if not _green(rc, out):
            rec["reason"] = (f"the reference impl does not turn the verify green "
                             f"(exit {rc}); nothing to mutate against")
            rec["seconds"] = time.time() - t_start
            return rec
    grep_lits = verify_grep_literals(worktree, verify_cmd)
    all_lits = list(dict.fromkeys(list(literals) + grep_lits))
    mutants, notes = generate(worktree, diff_text, all_lits, io_adapters=io_adapters)
    notes["literals_from_task"] = list(literals)
    notes["literals_from_verify_grep"] = grep_lits
    rec["generation"] = notes
    rec["generated"] = len(mutants)
    rec["_green_secs"] = green_secs
    rec["_objs"] = mutants          # popped by measure_applied (post-gates reuse them)
    # Declared + validated real-I/O adapter mutants: generated, reported, NOT
    # evidence (a hermetic verify cannot reach them by design) -- see the
    # REAL-I/O ADAPTERS block at the top of this file.
    adapter_ex = [m for m in mutants if m.io_adapter]
    rec["io_adapter_excluded"] = len(adapter_ex)
    rec["io_adapters"] = notes.get("io_adapters") or {}
    evidence = [m for m in mutants if m.literal_preserving and not m.io_adapter]
    breaking = [m for m in mutants if not m.literal_preserving]
    rec["literal_breaking"] = len(breaking)
    if not evidence:
        _has_ast = any(f.endswith(".py") or f.endswith(TS_EXTS) or f.endswith(".cs")
                       for f in notes["files"])
        rec["reason"] = ("no literal-preserving mutant could be generated for the "
                         "added lines" + ("" if _has_ast else
                                          " (target with no AST mutator and < 2 "
                                          "hunks; a TS/JS target here means the "
                                          "ts-mutator sidecar or Node was "
                                          "unavailable)"))
        rec["seconds"] = time.time() - t_start
        return rec
    sample = _balanced_sample(evidence, max_mutants)
    rec["truncated"] = len(sample) < len(evidence)
    originals: dict[str, str] = {}
    # LIVE PROGRESS (the owner 2026-09-27, "so it doesn't look hung"): this loop is
    # minutes of CPU with nothing on the GPU; the queue status / dashboard read
    # this record to show "verify-relevance 18/40 mutants". Display only.
    try:
        import dispatch_progress as _dp
    except Exception:
        _dp = None

    def _prog(done):
        if _dp is not None:
            _dp.write(worktree, "verify-relevance", "verify-relevance",
                      "mutants", done=done, total=len(sample))
    _prog(0)
    try:
        for i, m in enumerate(sample):
            if time.time() - t_start > budget_s:
                rec["truncated"] = True
                rec["reason"] = f"time budget {budget_s}s exhausted after {i} mutant(s)"
                break
            p = worktree / m.path
            if m.path not in originals:
                originals[m.path] = p.read_text()
            p.write_text(m.source)
            _clear_pycache(p.parent)
            rc, out, secs = _run_verify(verify_cmd, worktree,
                                        mutant_timeout(green_secs, verify_timeout))
            p.write_text(originals[m.path])
            _clear_pycache(p.parent)
            m.killed = not _green(rc, out)
            m.crash = m.killed and ("Traceback (most recent call last)" in out)
            if m.killed and m.path.endswith(".cs") and "FAIL: dotnet build" in out:
                # A C# token mutant that does not COMPILE is not behavioural
                # evidence: the verify died on the compiler, not on a case.
                # Dropped from the score (killed=None == never tried).
                m.killed = None
                rec["uncompilable"] = rec.get("uncompilable", 0) + 1
            m.seconds = round(secs, 2)
            if rc == 124 and out.endswith("[timeout]"):
                rec["mutant_timeouts"] = rec.get("mutant_timeouts", 0) + 1
            rec["tried"] += 1 if m.killed is not None else 0
            if progress:
                progress(m)
            _prog(i + 1)
    finally:
        if _dp is not None:
            _dp.clear(worktree, "verify-relevance")
        for rel, text in originals.items():
            try:
                (worktree / rel).write_text(text)
            except Exception:
                pass
        _clear_pycache(worktree)
    tried = [m for m in sample if m.killed is not None]
    rec["mutants"] = [m.as_dict() for m in mutants]
    rec["evidence_mutants"] = len(tried)
    rec["killed"] = sum(1 for m in tried if m.killed)
    rec["crash_kills"] = sum(1 for m in tried if m.crash)
    rec["survived"] = len(tried) - rec["killed"]
    rec["survivors"] = [m.as_dict() for m in tried if not m.killed]
    rec["seconds"] = round(time.time() - t_start, 2)
    # THREE SCORES, and the verdict takes the LOWEST.
    #   mutant_score   killed / tried. Overweights a complex line: one untested
    #                  `if a + b == 0:` yields eight mutants and sinks the score
    #                  eight times for one hole.
    #   site_score     mean over changed lines of (killed / tried on that line).
    #                  One hole counts once; one equivalent mutant on a line of
    #                  three only costs a third of a site.
    #   site_coverage  fraction of tried sites with AT LEAST ONE kill. This is
    #                  the one that sees a single-mutant hole: on wt-tokens
    #                  with only the `claude_tokens` alias case removed -- a
    #                  "must NOT change" item in the task -- L247's one mutant
    #                  (const-str) survived and the other two scores still
    #                  said RELEVANT 0.9 / 0.925 (measured 2026-09-03).
    # Neither of the first two is "the" truth, so the check reports all three
    # and gates on min() -- the direction that flags.
    #
    # An UNEXERCISED SITE -- every mutant tried on a line survived -- is the
    # most actionable finding this tool makes (the verify never runs that
    # path), and it is now a verdict, not just a report: any unexercised site
    # makes the run LOW whatever the averages say, because at twenty sites a
    # whole untested property is a 5% dent in every mean. The cost is a false
    # LOW on a line whose only mutant is equivalent (a dead store, say); the
    # site is named, so that is a ten-second human look. Threshold: >= 1
    # tried mutant (was 2, which is how L247 above slipped).
    #
    # UNTRIED SITES -- generated evidence but the cap or the time budget ran
    # out before the site was reached -- make the run UNPROVEN, never
    # relevant: the sample says nothing about those lines. Site-first ordering
    # (_balanced_sample) means this only happens when cap < number of sites
    # or the budget is exhausted inside the first pass.
    sites: dict[tuple, list] = {}
    for m in tried:
        sites.setdefault(_site_of(m), []).append(m)
    all_sites = {_site_of(m) for m in evidence}
    untried = sorted(f"{k[0]}:{k[1]}" for k in all_sites - set(sites))
    site_scores = {f"{k[0]}:{k[1]}": round(sum(1 for m in v if m.killed) / len(v), 3)
                   for k, v in sites.items()}
    rec["site_scores"] = site_scores
    rec["untried_sites"] = untried
    rec["unexercised_sites"] = sorted(
        f"{k[0]}:{k[1]}" for k, v in sites.items()
        if not any(m.killed for m in v))
    if tried and len(tried) < min_mutants and rec["killed"] == 0:
        # ZERO KILLS on a small sample (2026-10-05, rt-bg-commitments-fix-sync-guard):
        # 0/1 killed read as `unproven` (too few mutants for a verdict), and unproven
        # does not block GO -- so a fixture that tested an INLINED COPY of the guard
        # (never importing the target) reached GO with score 0.0. Too few mutants
        # cannot prove relevance, but every tried mutant surviving is positive
        # evidence the verify never runs the changed code: LOW, not unproven.
        rec["verdict"] = "low"
        rec["score"] = 0.0
        u = rec["unexercised_sites"]
        rec["reason"] = (f"every tried property-breaking mutant survived (killed 0/"
                         f"{len(tried)}; {', '.join(u[:4])} never exercised) -- the "
                         f"verify does not run the changed code (an inlined copy, a "
                         f"proxy, or a stub of the target?)")
        return rec
    if len(tried) < min_mutants:
        rec["reason"] = rec["reason"] or (
            f"only {len(tried)} evidence mutant(s) were tried; {min_mutants} "
            f"needed before a verdict means anything")
        rec["verdict"] = "unproven"
        rec["score"] = (rec["killed"] / len(tried)) if tried else None
        return rec
    rec["mutant_score"] = round(rec["killed"] / len(tried), 3)
    rec["site_score"] = round(sum(site_scores.values()) / len(site_scores), 3)
    rec["site_coverage"] = round(
        sum(1 for v in sites.values() if any(m.killed for m in v)) / len(sites), 3)
    rec["score"] = min(rec["mutant_score"], rec["site_score"], rec["site_coverage"])
    if untried:
        rec["verdict"] = "unproven"
        rec["reason"] = (f"{len(untried)} changed site(s) were never tried "
                         f"({', '.join(untried[:4])}{'...' if len(untried) > 4 else ''}) "
                         f"-- the cap or the time budget ran out before them, so "
                         f"the sample says nothing about those lines. Raise "
                         f"--max-mutants/--budget-s.")
        return rec
    if rec["unexercised_sites"]:
        rec["verdict"] = "low"
        u = rec["unexercised_sites"]
        rec["reason"] = (f"{len(u)} changed site(s) are UNEXERCISED -- every mutant "
                         f"on {', '.join(u[:4])}{'...' if len(u) > 4 else ''} survived, "
                         f"so the verify never runs that path (killed "
                         f"{rec['killed']}/{len(tried)} elsewhere)")
        return rec
    if rec["score"] >= threshold:
        rec["verdict"] = "relevant"
        rec["reason"] = (f"verify killed {rec['killed']}/{len(tried)} property-"
                         f"breaking mutants of the reference impl")
    else:
        rec["verdict"] = "low"
        rec["reason"] = (f"verify let {rec['survived']}/{len(tried)} property-"
                         f"breaking mutants of the reference impl pass -- it is "
                         f"testing a proxy, or a benign slice, of the property")
    return rec


# --------------------------------------------------------------------------
# DETERMINISTIC EQUIVALENT-MUTANT FILTER (2026-10-09)
# --------------------------------------------------------------------------
# A mutant no test CAN kill is not evidence and must not be fed back to the
# fixture author as a "hole" (research: equivalent-mutant noise is what makes
# survivor-feedback loops burn rounds). Filtered DETERMINISTICALLY, before any
# LLM is consulted: identical text, identical AST (Python: comment / whitespace /
# formatting only), comment-or-whitespace-only text change (other languages), and
# DEAD code (Python: statements after return/raise/continue/break, bodies of
# `if False` / `while 0`, the `else` of `if True`). Every drop is recorded in
# generation.equivalent_filtered -- never silent.
def python_unreachable_lines(text: str) -> set[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    dead: set[int] = set()

    def mark(stmts):
        for st in stmts:
            for n in range(st.lineno, (getattr(st, "end_lineno", None) or st.lineno) + 1):
                dead.add(n)

    def truth(t):
        if isinstance(t, ast.Constant) and not isinstance(t.value, (str, bytes)):
            return bool(t.value)
        return None
    for node in ast.walk(tree):
        for f in ("body", "orelse", "finalbody"):
            lst = getattr(node, f, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                for i, st in enumerate(lst):
                    if isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        mark(lst[i + 1:])
                        break
        if isinstance(node, ast.If):
            t = truth(node.test)
            if t is False:
                mark(node.body)
            elif t is True:
                mark(node.orelse)
        elif isinstance(node, ast.While) and truth(node.test) is False:
            mark(node.body)
    return dead


def _strip_line_comment(line: str, py: bool) -> str:
    """Drop a trailing `//` (or `#` for Python) comment, string-aware."""
    q = None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif py and c == "#":
            return line[:i]
        elif not py and line.startswith("//", i):
            return line[:i]
        i += 1
    return line


def _normalised_code_lines(text: str, py: bool) -> list[str]:
    out = []
    for l in text.split("\n"):
        s = l.strip()
        if not s or s.startswith(("//", "/*", "*", "*/")) or (py and s.startswith("#")):
            continue
        out.append(" ".join(_strip_line_comment(s, py).split()))
    return [l for l in out if l]


def equivalent_reason(m: Mutant, fixed: str, dead: set | None = None) -> str | None:
    """PURE. Why `m` is provably equivalent to the fixed file, or None."""
    if m.source == fixed:
        return "identical text"
    py = m.path.endswith(".py")
    if py:
        try:
            if ast.dump(ast.parse(m.source)) == ast.dump(ast.parse(fixed)):
                return "AST-identical (comment / whitespace / formatting only)"
        except SyntaxError:
            pass
        if dead and all(n in dead for n in range(m.lineno, (m.end_lineno or m.lineno) + 1)):
            return "dead code (unreachable statement / constant-false branch)"
    elif _normalised_code_lines(m.source, False) == _normalised_code_lines(fixed, False):
        return "comment / whitespace-only change"
    return None


# --------------------------------------------------------------------------
# POST-GATES on the measured record (2026-10-09): per-hunk NECESSITY and the
# WRONG-SOLUTION BANK. Both reuse the mutants the core already ran (a result is
# never re-measured), run only what is missing, and are CHEAP: hunk-reverts are
# one verify run per hunk, the bank is capped at BANK_PER_CATEGORY x categories.
# Both FAIL CLOSED: a surviving variant is LOW (a hard NO-GO at preflight); a
# variant the budget did not reach turns a would-be RELEVANT into UNPROVEN.
# --------------------------------------------------------------------------
NECESSITY_DOC_EXTS = (".md", ".rst", ".txt", ".adoc", ".markdown")
BANK_PER_CATEGORY = 3
BANK_CATEGORIES = ("boundary", "swapped-compare", "dropped-guard",
                   "wrong-default", "early-return")
_BOUNDARY_PAIRS = {(">=", ">"), (">", ">="), ("<=", "<"), ("<", "<=")}
_GUARDISH = re.compile(r"\bnot\b|\bNone\b|\bnull\b|\bundefined\b|!|\blen\(|\.length\b"
                       r"|==\s*0\b|\bempty\b|\bis_empty\b")
_COMPAREISH = re.compile(r"<|>|==|!=|\brange\(|\blen\(|\[|\bslice\(|\.length\b")


def bank_category(m: Mutant) -> str | None:
    """PURE. The wrong-solution category of an existing mutant, or None."""
    k, d = m.klass, m.desc or ""
    orig = m.original or ""
    if k in ("compare-flip", "cmp-bound"):
        mt = re.match(r"^(.+?) -> (.+?)$", d.strip())
        if mt and (mt.group(1).strip(), mt.group(2).strip()) in _BOUNDARY_PAIRS:
            return "boundary"
        return "swapped-compare"
    if k == "const-int" and _COMPAREISH.search(orig):
        return "boundary"
    if k == "const-default":
        return "wrong-default"
    if k == "early-return":
        return "early-return"
    if k in ("stmt-delete", "cond-force-false") and orig.strip().startswith("if") \
            and _GUARDISH.search(orig):
        return "dropped-guard"
    return None


def wrong_solution_extras(rel: str, fixed: str, added: set) -> list[Mutant]:
    """Python-only plausible-wrong variants the generic mutator has no class for:
    an EARLY `return None` at the top of an added function, and a WRONG parameter
    DEFAULT. (TS/JS get the categories its sidecar mutator already emits.)"""
    if not rel.endswith(".py"):
        return []
    try:
        tree = ast.parse(fixed)
    except SyntaxError:
        return []
    annotated, _ = optout_lines(fixed)
    src = Src(fixed)
    lines = fixed.split("\n")
    out: list[Mutant] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fn_end = getattr(fn, "end_lineno", None) or fn.lineno
        if not any(n in added for n in range(fn.lineno, fn_end + 1)):
            continue            # the refimpl did not touch this function
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn))
        if len(body) >= 2 and not has_yield and body[0].lineno > fn.lineno \
                and body[0].lineno not in annotated:
            first = body[0]
            ind = " " * first.col_offset
            text = "\n".join(lines[:first.lineno - 1] + [ind + "return None"]
                             + lines[first.lineno - 1:])
            try:
                compile(text, rel, "exec")
            except SyntaxError:
                text = None
            if text:
                m = Mutant(rel, "early-return",
                           f"early `return None` at the top of `{fn.name}`", text, first.lineno)
                m.snippet = "return None"
                m.original = _line_of(fixed, first.lineno)
                out.append(m)
        a = fn.args
        for d in list(a.defaults) + [x for x in a.kw_defaults if x is not None]:
            if not isinstance(d, ast.Constant) or d.lineno in annotated or d.lineno not in added:
                continue
            v = d.value
            if isinstance(v, bool):
                nv = str(not v)
            elif isinstance(v, int):
                nv = str(v + 1000003)
            else:
                continue
            x, y = src.span(d)
            text = src.replace(x, y, nv)
            try:
                compile(text, rel, "exec")
            except SyntaxError:
                continue
            m = Mutant(rel, "const-default", f"parameter default {v!r} -> {nv}", text, d.lineno)
            m.snippet = (text.split("\n")[d.lineno - 1].strip()[:120])
            m.original = _line_of(fixed, d.lineno)
            out.append(m)
    return out


def _run_missing(worktree: Path, verify_cmd: str, ms: list, green_secs, verify_timeout,
                 deadline: float) -> None:
    """Run every mutant in `ms` whose .killed is None; restore the tree after each."""
    originals: dict[str, str] = {}
    try:
        for m in ms:
            if m.killed is not None:
                continue
            if time.time() > deadline:
                break
            p = worktree / m.path
            if m.path not in originals:
                originals[m.path] = p.read_text()
            p.write_text(m.source)
            _clear_pycache(p.parent)
            rc, out, secs = _run_verify(verify_cmd, worktree,
                                        mutant_timeout(green_secs, verify_timeout))
            p.write_text(originals[m.path])
            _clear_pycache(p.parent)
            m.killed = not _green(rc, out)
            m.crash = m.killed and ("Traceback (most recent call last)" in out)
            if m.killed and m.path.endswith(".cs") and "FAIL: dotnet build" in out:
                m.killed = None      # uncompilable: not behavioural evidence
                m.uncompilable = True
            m.seconds = round(secs, 2)
    finally:
        for rel, text in originals.items():
            try:
                (worktree / rel).write_text(text)
            except Exception:
                pass
        _clear_pycache(worktree)


def _lit_ok(m: Mutant, file_lits: list) -> bool:
    return all(l in m.source for l in file_lits)


def _llm_json(cmd: str, payload: dict, cwd: Path, timeout: int = 300):
    cp = subprocess.run(cmd, shell=True, cwd=str(cwd), input=json.dumps(payload),
                        capture_output=True, text=True, timeout=timeout)
    txt = cp.stdout.strip()
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b < a:
        return None
    return json.loads(txt[a:b + 1])


def _sweep_exclude_enabled() -> bool:
    return os.environ.get("VR_SWEEP_EQUIV_EXCLUDE", "on").strip().lower() not in ("off", "0", "no", "false")


def apply_sweep_equivalence(worktree: Path, rec: dict, by_id: dict) -> None:
    """Classify each SURVIVING mutant with mutant_sweep (differential input sweep of the refimpl-applied
    module vs the mutant): "equivalent" (no input tells them apart) is excluded from the score and
    REPORTED in rec["equivalent_survivors"]; "differs" gets a concrete WITNESS (call, real result,
    mutant result) on the survivor so the refine prompt can show the exact case to add. "unknown" changes
    nothing (fail-closed). A mutant the verify KILLED is never touched, so a converged run's outcome only
    moves when it had survivors. Kill switches: MUTANT_SWEEP=off (no classification),
    VR_SWEEP_EQUIV_EXCLUDE=off (classify + witness, but keep counting equivalents)."""
    survs = rec.get("survivors") or []
    if not survs:
        return
    try:
        _d = str(Path(__file__).resolve().parent)
        if _d not in sys.path:
            sys.path.insert(0, _d)
        import mutant_sweep as ms
    except Exception as e:
        rec["sweep_error"] = f"import: {e}"[:200]
        return
    if not ms.enabled():
        return
    sources = {}
    for sv in survs:
        m = by_id.get(sv.get("id"))
        if m is not None and not sv.get("unnecessary_hunk") and m.klass != "hunk-revert" and isinstance(m.source, str):
            sources[sv["id"]] = m.source
    if not sources:
        return
    res = ms.classify_survivors(worktree, survs, sources)
    if not res:
        return
    counts = {"equivalent": 0, "differs": 0, "unknown": 0}
    excl = set()
    for sv in survs:
        r = res.get(sv.get("id"))
        if not r:
            continue
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        sv["sweep"] = {k: r.get(k) for k in ("status", "calls", "reason") if r.get(k) is not None}
        if r["status"] == "differs":
            sv["witness"] = r.get("witness")
        elif r["status"] == "equivalent":
            sv["sweep_equivalent"] = True
            if _sweep_exclude_enabled():
                excl.add(sv["id"])
    rec["sweep"] = {"version": ms.SWEEP_VERSION, **counts, "excluded": len(excl)}
    if not excl:
        return
    rec["equivalent_survivors"] = [s for s in survs if s.get("id") in excl]
    rec["survivors"] = [s for s in survs if s.get("id") not in excl]
    rec["survived"] = max(0, rec.get("survived", 0) - len(excl))
    rec["evidence_mutants"] = max(0, rec.get("evidence_mutants", 0) - len(excl))
    wb = rec.get("wrong_bank")
    if isinstance(wb, dict):
        for v in wb.get("variants") or []:
            if any((e.get("file"), e.get("line"), e.get("bank")) == (v.get("file"), v.get("line"), v.get("category"))
                   and e.get("mutation", "").find(str(v.get("mutation"))) >= 0 for e in rec["equivalent_survivors"]):
                v["equivalent"] = True
        eq_keys = {(e.get("file"), e.get("line")) for e in rec["equivalent_survivors"]}
        wb["survivors"] = [t for t in wb.get("survivors") or []
                           if not any(f" survived at {f}:{ln}:" in t for f, ln in eq_keys)]
    evidence = [m for m in rec.get("mutants") or [] if m.get("killed") is not None and m.get("id") not in excl]
    if rec.get("mutant_score") is None or rec["evidence_mutants"] < rec.get("min_mutants", 3) or not evidence:
        return
    sites: dict = {}
    for m in evidence:
        sites.setdefault((m.get("file"), m.get("line")), []).append(m)
    site_scores = {f"{k[0]}:{k[1]}": round(sum(1 for x in v if x.get("killed")) / len(v), 3) for k, v in sites.items()}
    rec["site_scores"] = site_scores
    rec["unexercised_sites"] = sorted(k for k, v in site_scores.items() if v == 0)
    killed = rec.get("killed", 0)
    rec["mutant_score"] = round(killed / rec["evidence_mutants"], 3) if rec["evidence_mutants"] else rec["mutant_score"]
    rec["site_score"] = round(sum(site_scores.values()) / len(site_scores), 3) if site_scores else rec.get("site_score")
    rec["site_coverage"] = round(sum(1 for v in site_scores.values() if v > 0) / len(site_scores), 3) if site_scores else rec.get("site_coverage")
    rec["score"] = min(x for x in (rec["mutant_score"], rec["site_score"], rec["site_coverage"]) if x is not None)
    nec_bad = bool(((rec.get("necessity") or {}).get("unnecessary")) or ((rec.get("necessity") or {}).get("untested")))
    open_bank = any((s.get("gate") == "wrong-solution-bank") for s in rec["survivors"])
    if (rec.get("verdict") == "low" and not nec_bad and not open_bank and not rec.get("untried_sites")
            and not rec["unexercised_sites"] and rec["score"] >= rec.get("threshold", 0.8)):
        rec["verdict"] = "relevant"
        rec["reason"] = (f"verify killed {killed}/{rec['evidence_mutants']} property-breaking mutants of the "
                         f"reference impl ({len(excl)} survivor(s) proven sweep-equivalent and excluded: "
                         f"no input distinguishes them from the original)")
    else:
        rec["sweep_note"] = (f"{len(excl)} survivor(s) proven sweep-equivalent and excluded from the score; "
                             f"verdict unchanged ({rec.get('verdict')})")


def apply_post_gates(worktree: Path, verify_cmd: str, diff_text: str, rec: dict,
                     objs: list, kw: dict, *, necessity=True, bank=True,
                     llm_equiv_cmd=None, llm_wrong_cmd=None) -> None:
    """Mutates `rec` in place. See the section header above."""
    t0 = time.time()
    green_secs = rec.pop("_green_secs", None)
    verify_timeout = kw.get("verify_timeout", VERIFY_TIMEOUT_S)
    budget = kw.get("budget_s", DEFAULT_BUDGET_S)
    deadline = t0 + kw.get("post_budget_s", max(60, budget // 2))        # its own allowance on top of the core's
    io_adapters = kw.get("io_adapters") or ()
    gen = rec.get("generation") or {}
    all_lits = list(dict.fromkeys(list(gen.get("literals_from_task") or [])
                                  + list(gen.get("literals_from_verify_grep") or [])))
    files = parse_unified_diff(diff_text)
    by_id = {m.id: m for m in objs}
    new_surv: list[dict] = []
    reasons: list[str] = []
    untested_msgs: list[str] = []
    extra_tried = 0

    # ---------------- 1. PER-HUNK NECESSITY ----------------
    nec = {"enabled": bool(necessity), "checked": 0, "necessary": 0,
           "unnecessary": [], "exempt": [], "untested": []}
    if necessity:
        cands: list[Mutant] = []
        for rel, info in files.items():
            if rel.endswith(NECESSITY_DOC_EXTS):
                nec["exempt"].append({"file": rel, "hunk": None,
                                      "reason": "doc-only file: prose has no behaviour to revert"})
                continue
            p = worktree / rel
            if not p.is_file():
                continue
            try:
                fixed = p.read_text()
            except Exception:
                continue
            code_lines = python_code_lines(fixed) if rel.endswith(".py") else None
            rep: list = []
            hms = hunk_revert_mutants(rel, fixed, info["hunks"], code_lines, rep)
            nec["exempt"].extend(rep)
            annotated, _ = optout_lines(fixed)
            dead = python_unreachable_lines(fixed) if rel.endswith(".py") else set()
            file_lits = [l for l in all_lits if l in fixed]
            for hm in hms:
                m = by_id.get(hm.id) or hm
                label = {"file": rel, "hunk": m.lineno}
                why = equivalent_reason(m, fixed, dead)
                if why:
                    nec["exempt"].append(dict(label, reason="equivalent revert: " + why))
                    continue
                if not _lit_ok(m, file_lits):
                    nec["exempt"].append(dict(label, reason="reverting it removes a `## Must contain` "
                                              "literal: the spec itself requires this hunk"))
                    continue
                if rel.endswith(".py"):
                    tag_io_adapters(rel, fixed, [m], io_adapters, {})
                    if m.io_adapter:
                        nec["exempt"].append(dict(label, reason=f"declared real-I/O adapter `{m.io_adapter}`"))
                        continue
                span = range(m.lineno, (m.end_lineno or m.lineno) + 1)
                body = [n for n in span if code_lines is None or n in code_lines]
                if body and all(n in annotated for n in body):
                    nec["exempt"].append(dict(label, reason="every code line carries a (honoured, "
                                              "non-behavioural) `relevance: unobservable` opt-out"))
                    continue
                cands.append(m)
        _run_missing(worktree, verify_cmd, cands, green_secs, verify_timeout, deadline)
        for m in cands:
            if m.killed is None:
                nec["untested"].append({"file": m.path, "hunk": m.lineno})
                untested_msgs.append(f"hunk {m.path}@{m.lineno}")
                continue
            nec["checked"] += 1
            if m.killed:
                nec["necessary"] += 1
            else:
                end = m.end_lineno or m.lineno
                n_lines = end - m.lineno + 1
                msg = (f"UNNECESSARY HUNK {m.path}:{m.lineno}-{end} ({n_lines} line(s)): reverting "
                       f"it leaves the verify GREEN, so nothing tests it -- `{(m.snippet or '')[:90]}`")
                d = m.as_dict()
                d["mutation"] = msg
                d["gate"] = "necessity"
                d["unnecessary_hunk"] = True
                nec["unnecessary"].append({"file": m.path, "start": m.lineno, "end": end,
                                           "message": msg})
                new_surv.append(d)
        if nec["unnecessary"]:
            reasons.append("; ".join(u["message"] for u in nec["unnecessary"][:3])
                           + " -- DELETE the hunk from the reference solution if the spec does "
                             "not need it, or add a case that fails without it")
    rec["necessity"] = nec

    # ---------------- 2. WRONG-SOLUTION BANK ----------------
    bk = {"enabled": bool(bank), "variants": [], "survivors": [],
          "unavailable": [], "llm": bool(llm_wrong_cmd)}
    if bank:
        pool = [m for m in objs if m.literal_preserving and not m.io_adapter
                and m.klass != "hunk-revert"]
        for rel, info in files.items():
            p = worktree / rel
            if not p.is_file() or not rel.endswith(".py"):
                continue
            try:
                fixed = p.read_text()
            except Exception:
                continue
            file_lits = [l for l in all_lits if l in fixed]
            dead = python_unreachable_lines(fixed)
            ex = [m for m in wrong_solution_extras(rel, fixed, info["added"])
                  if not equivalent_reason(m, fixed, dead) and _lit_ok(m, file_lits)]
            tag_io_adapters(rel, fixed, ex, io_adapters, {})   # declared adapters are exempt
            pool.extend(m for m in ex if not m.io_adapter)
        picked: list[Mutant] = []
        have = set()
        for cat in BANK_CATEGORIES:
            # the operator-level variants first (compare-flip IS the off-by-one /
            # swapped comparison; a +/-1 constant is the weaker form of it)
            cand = sorted((m for m in pool if bank_category(m) == cat),
                          key=lambda m: (m.klass not in ("compare-flip", "cmp-bound"),
                                         m.path, m.lineno, m.klass, m.id))
            n, sites = 0, set()
            for m in cand:
                if n >= BANK_PER_CATEGORY:
                    break
                if (m.path, m.lineno, m.klass) in sites or m.id in have:
                    continue
                sites.add((m.path, m.lineno, m.klass))
                have.add(m.id)
                m.bank = cat
                picked.append(m)
                n += 1
            if n == 0:
                bk["unavailable"].append(cat)
        if llm_wrong_cmd:
            try:
                texts = {rel: (worktree / rel).read_text() for rel in files
                         if (worktree / rel).is_file()}
                tfile = worktree / "TASK.md"
                props = _llm_json(llm_wrong_cmd, {"files": texts,
                                                  "task": tfile.read_text() if tfile.is_file() else ""},
                                  worktree) or []
                for pr in props[:4]:
                    rel, srcx = pr.get("file"), pr.get("source")
                    if rel not in texts or not isinstance(srcx, str) or srcx == texts[rel]:
                        continue
                    if rel.endswith(".py"):
                        try:
                            compile(srcx, rel, "exec")
                        except SyntaxError:
                            continue
                    m = Mutant(rel, "llm-wrong-solution", str(pr.get("why") or "LLM wrong solution")[:160],
                               srcx, int(pr.get("line") or 1))
                    m.literal_preserving = _lit_ok(m, [l for l in all_lits if l in texts[rel]])
                    if (not m.literal_preserving
                            or equivalent_reason(m, texts[rel], python_unreachable_lines(texts[rel])
                                                 if rel.endswith(".py") else set())):
                        continue
                    m.bank = "llm"
                    picked.append(m)
            except Exception as e:
                bk["llm_error"] = f"{type(e).__name__}: {e}"
        _run_missing(worktree, verify_cmd, picked, green_secs, verify_timeout, deadline)
        for m in picked:
            row = {"category": m.bank, "file": m.path, "line": m.lineno, "mutation": m.desc,
                   "killed": m.killed}
            bk["variants"].append(row)
            if m.killed is None:
                untested_msgs.append(f"bank variant {m.bank} {m.path}:{m.lineno}")
            elif not m.killed:
                msg = (f"WRONG-SOLUTION [{m.bank}] survived at {m.path}:{m.lineno}: {m.desc} "
                       f"(`{(m.original or '').strip()[:70]}` ==> `{(m.snippet or '').strip()[:70]}`) "
                       f"-- a plausible wrong implementation passes the fixture")
                d = m.as_dict()
                d["mutation"] = msg
                d["gate"] = "wrong-solution-bank"
                bk["survivors"].append(msg)
                if not any(s.get("id") == d["id"] for s in new_surv):
                    new_surv.append(d)
        if bk["survivors"]:
            reasons.append("; ".join(bk["survivors"][:3]))
    rec["wrong_bank"] = bk

    # ---------------- fold into the record ----------------
    if new_surv:
        ids = {d["id"] for d in new_surv}
        old = [s for s in rec.get("survivors") or [] if s.get("id") not in ids]
        counted = {s.get("id") for s in rec.get("survivors") or []} | \
                  {m["id"] for m in rec.get("mutants") or [] if m.get("killed") is not None}
        fresh = [d for d in new_surv if d["id"] not in counted]
        rec["survivors"] = new_surv + old
        rec["survived"] = rec.get("survived", 0) + len(fresh)
        rec["evidence_mutants"] = rec.get("evidence_mutants", 0) + len(fresh)
    if reasons:
        rec["verdict"] = "low"
        rec["reason"] = " | ".join(reasons) + ((" || " + rec["reason"]) if rec.get("reason") else "")
    elif untested_msgs and rec.get("verdict") == "relevant":
        rec["verdict"] = "unproven"
        rec["reason"] = ("could not finish the necessity / wrong-solution checks within the "
                         "time budget (" + ", ".join(untested_msgs[:4]) + ") -- raise --budget-s")

    # ---------------- deterministic SWEEP classifier: equivalent vs real hole (+ witness) ----------------
    try:
        apply_sweep_equivalence(worktree, rec, by_id)
    except Exception as e:                       # never let the classifier break the gate
        rec["sweep_error"] = f"{type(e).__name__}: {e}"[:200]

    # ---------------- optional LLM equivalence classifier (default OFF) ----------------
    if llm_equiv_cmd and rec.get("survivors"):
        try:
            rows = [{k: s.get(k) for k in ("id", "file", "line", "mutation", "original", "snippet")}
                    for s in rec["survivors"]]
            ids = _llm_json(llm_equiv_cmd, {"survivors": rows}, worktree) or []
            ids = {str(i) for i in ids}
            n = 0
            for s in rec["survivors"]:
                # ADVISORY ONLY: flags the survivor so the refine loop does not spend a
                # round on it. The verdict and score still COUNT it (fail-closed).
                if s.get("id") in ids and not s.get("unnecessary_hunk"):
                    s["llm_equivalent"] = True
                    n += 1
            rec["llm_equivalent"] = n
        except Exception as e:
            rec["llm_equivalent_error"] = f"{type(e).__name__}: {e}"
    rec["post_gate_seconds"] = round(time.time() - t0, 2)


# --------------------------------------------------------------------------
# CLI: apply / revert around measure_applied
# --------------------------------------------------------------------------
def _load_harness_basenames() -> set:
    """Dispatch-harness basenames that are NEVER the fix under measurement.
    Single source of truth is ollama-queue.py::_SCAFFOLD_BASENAMES (the same
    set gate-on-complete strips from the reviewed diff); fail-OPEN to the
    built-in list so an import failure can only ever strip LESS, never treat a
    target file as harness."""
    base = {"TASK.md", "task.md", "AUTO-TASK.md", "verify.sh", "verify.test.ts",
            "verify.test.js", "verify_impl.mts", "verify-impl.mts", "verify_impl.ts",
            "check_literals.py", "test_fixture.py", "refimpl.py",
            "auto-harness-check.py", ".dispatch-harness.json", ".preflight-state.json"}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_olq_scaffold_vr", Path(__file__).resolve().parent / "ollama-queue.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        s = getattr(m, "_SCAFFOLD_BASENAMES", None)
        if s:
            base |= set(s)
    except Exception:
        pass
    return base


_HARNESS_BASENAMES: set | None = None


def strip_harness_files(diff_text: str, basenames: set | None = None) -> tuple[str, list[str]]:
    """PURE. Drop every per-file section of a unified diff whose path basename is a
    dispatch-harness file. Returns (kept_diff, dropped_paths).

    WHY (2026-09-23, job e6cc67d029cc rt-bfmr-mytrackerid-preserve): a TASK.md
    hand-edited AFTER the launch baseline was sealed shows up in `git diff`
    alongside the fix. Every TASK.md line then became a mutation SITE; nothing
    tests prose, so all those hunk-revert mutants "survived", TASK.md:5/22/29/40/
    ... were reported as unexercised sites, and a correct one-line fix scored
    0.404 (LOW) and FAILED its gate. gate-on-complete already strips these
    basenames from the diff it reviews; the relevance measurement must see the
    same diff, or the two halves of the gate disagree about what the fix IS.
    Only the FIX's own files are evidence of the fix's relevance."""
    names = basenames if basenames is not None else _harness_basenames()
    lc = {n.lower() for n in names}
    kept, dropped = [], []
    # Sections start at `diff --git a/<p> b/<p>`; anything before the first one
    # (normally nothing) is kept verbatim.
    parts = re.split(r"(?m)^(?=diff --git )", diff_text)
    for part in parts:
        if not part.startswith("diff --git "):
            kept.append(part)
            continue
        m = re.match(r"diff --git a/(.+?) b/(.+?)\n", part)
        path = (m.group(2) if m else "").strip()
        if path and Path(path).name.lower() in lc:
            dropped.append(path)
            continue
        kept.append(part)
    return "".join(kept), dropped


def _harness_basenames() -> set:
    global _HARNESS_BASENAMES
    if _HARNESS_BASENAMES is None:
        _HARNESS_BASENAMES = _load_harness_basenames()
    return _HARNESS_BASENAMES


def _git(wt: Path, *a) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _apply(wt: Path, patch: str | None, cmd: str | None) -> tuple[bool, str]:
    if patch:
        pp = Path(patch).expanduser().resolve()
        rc, out = _git(wt, "apply", str(pp))
        if rc != 0:
            p2 = subprocess.run(["patch", "-p1", "-i", str(pp)], cwd=str(wt),
                                capture_output=True, text=True)
            if p2.returncode != 0:
                return False, out[-300:]
        return True, ""
    rc, out, _ = _run_verify(cmd, wt, 600)
    return rc == 0, out[-300:]


def _untracked(wt: Path) -> set[str]:
    rc, out = _git(wt, "ls-files", "--others", "--exclude-standard")
    return {l.strip() for l in out.splitlines() if l.strip()}


# --------------------------------------------------------------------------
# Relevance SIGN-OFF: a line -> killing-case proposal the coordinator CHECKS
#
# The mutation gate above proves, mechanically, that the verify NOTICES when a
# mutatable line's behaviour breaks (killed=relevant). Two things it leaves to
# the coordinator's unaided judgement, which this layer turns into a checkable
# artifact:
#   1. WHICH fixture case does the killing -- recorded nowhere, so the human
#      re-reads the fixture to trust the linkage. The proposer has the local
#      model NAME the case per covered line; the coordinator CHECKS the claim.
#   2. Lines with NO possible behavioural mutant (pure schema DDL, a constant
#      table, a bare re-export) -- these are the UNPROVEN case; each needs an
#      explicit, reviewable WAIVER, not a silent pass.
# check_signoff() is the load-bearing part: it is a MECHANICAL completeness
# gate, not a model. It refuses to call a sign-off complete if any provably-
# behavioural line (one with a killed mutant) is unmapped, and it refuses to
# let such a line be WAIVED -- a waiver is valid only where the engine could
# generate no behavioural mutant. It never approves; it reports holes.
# --------------------------------------------------------------------------

def _applied_creation_target(wt: Path) -> list:
    """[target] when `.dispatch-harness.json` declares a creation_task whose target
    is present on disk and UNTRACKED (not at HEAD, not ignored); else []."""
    try:
        d = json.loads((wt / ".dispatch-harness.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict) or not d.get("creation_task"):
        return []
    t = d.get("target")
    if not isinstance(t, str) or not t or not (wt / t).is_file():
        return []
    rc, _ = _git(wt, "ls-files", "--error-unmatch", "--", t)
    if rc == 0:
        return []                       # tracked: the normal diff already has it
    rc, out = _git(wt, "status", "--porcelain", "--untracked-files=all", "--", t)
    if rc != 0 or not any(l[:2] == "??" for l in out.splitlines()):
        return []                       # ignored or otherwise not a plain untracked
    return [t]


_HARNESS_NAMES = {"TASK.md", "verify.sh", "check_literals.py", "refimpl.py",
                  ".dispatch-harness.json", "relevance-signoff.json", "fix.patch"}


def _scope_line_files(task_text: str) -> list:
    """Backticked edit-ME tokens of every "Only edit" line (the part before any
    "do not edit" clause) -- the same reading ollama-dispatch-preflight uses."""
    out = []
    for m in re.finditer(r"[Oo]nly edit\b([^\n]*)", task_text or ""):
        tail = re.split(r"(?i)\bdo\s*n(?:['\u2019]t|ot)\s+edit\b", m.group(1), maxsplit=1)[0]
        for group in re.findall(r"`([^`\n]+)`", tail):
            for tok in re.split(r"[,\s]+(?:and\s+)?", group):
                tok = tok.strip("`,.; ")
                if tok and tok not in out:
                    out.append(tok)
    return out


def _applied_scope_new_files(wt: Path, task: Path) -> list:
    """Declared-scope files that are on disk and plain UNTRACKED (new in the fix)."""
    try:
        toks = _scope_line_files(task.read_text())
    except OSError:
        return []
    out = []
    for t in toks:
        if t in _HARNESS_NAMES or t.startswith("/") or ".." in Path(t).parts:
            continue
        if not (wt / t).is_file():
            continue
        rc, so = _git(wt, "status", "--porcelain", "--untracked-files=all", "--", t)
        if rc == 0 and any(l[:2] == "??" for l in so.splitlines()):
            out.append(t)
    return out


def behavioral_added_lines(worktree: Path, diff_text: str) -> dict:
    """{file: set(lineno) | None} of ADDED lines that carry behaviour, using the
    same AST notion the Python mutator uses. None for a file we cannot parse
    (no AST): the caller then relies on mutated sites alone for that file, which
    is the safe direction -- it never demands a waiver it cannot justify."""
    files = parse_unified_diff(diff_text)
    out: dict = {}
    for f, info in files.items():
        added = info["added"]
        p = Path(worktree) / f
        if f.endswith(".py") and p.exists():
            try:
                code = python_code_lines(p.read_text())
            except Exception:
                code = None
            out[f] = (added & code) if code is not None else None
        else:
            out[f] = None
    return out


def signoff_requirements(rec: dict, worktree: Path, diff_text: str) -> dict:
    """Partition the changed lines into the three sign-off classes:
      covered  -- >=1 killed mutant: provably behavioural, needs a killing-case
      survivor -- mutant(s) tried, none killed: a NO-GO, cannot be signed off
      no_mutant-- a behavioural added line the engine made no mutant for: WAIVER
    Keys are "file:line" strings so the artifact is stable/serialisable."""
    by_site: dict = {}
    for m in rec.get("mutants", []):
        if m.get("killed") is None:            # never tried (cap/budget)
            continue
        by_site.setdefault((m["file"], m["line"]), []).append(m)
    covered, survivor = [], []
    snippet_of = {}
    for k, ms in by_site.items():
        # The CHANGED line as written, not a mutant of it: a call-delete's
        # snippet reads `return this;` where the code says
        # `return origOpen.apply(this, arguments);` (sidecar-bfmr s2, 2026-09-27
        # -- read as "a call-delete on a line with no call").
        snippet_of[k] = next((m.get("original") or m.get("snippet", "") for m in ms
                              if m.get("original") or m.get("snippet")), "")
        (covered if any(m["killed"] for m in ms) else survivor).append(k)
    behavioral = behavioral_added_lines(worktree, diff_text)
    no_mutant = []
    for f, lns in behavioral.items():
        if lns is None:
            continue
        for ln in sorted(lns):
            if (f, ln) not in by_site:
                no_mutant.append((f, ln))
    fmt = lambda ks: [f"{f}:{ln}" for (f, ln) in sorted(ks)]
    return {
        "covered": fmt(covered),
        "survivor": fmt(survivor),
        "no_mutant": fmt(no_mutant),
        "snippets": {f"{f}:{ln}": s for (f, ln), s in snippet_of.items()},
    }


def check_signoff(reqs: dict, signoff: dict) -> dict:
    """MECHANICAL gate over a proposed sign-off. No model, no I/O.

    signoff = {"mappings":[{"line":"f:ln","killing_case":"...","reason":"..."}],
               "waivers": [{"line":"f:ln","reason":"..."}]}
    Returns {"ok", "problems", "required", "extra"}. ok is True only when every
    covered line has a non-empty killing_case, every no_mutant line has a
    waiver with a reason, no covered line is waived, and no survivor exists."""
    def norm(entries, field):
        out = {}
        for e in entries or []:
            k = str(e.get("line", "")).strip()
            if k:
                out[k] = e
        return out
    mapped = norm(signoff.get("mappings"), "killing_case")
    waived = norm(signoff.get("waivers"), "reason")
    problems = []
    for k in reqs["survivor"]:
        problems.append(f"{k}: only surviving mutants -- NO-GO, cannot be signed off "
                        "(prove relevance first)")
    for k in reqs["covered"]:
        if k in waived:
            problems.append(f"{k}: WAIVED but has a killed mutant -- a provably "
                            "behavioural line cannot be waived, map it to its case")
        m = mapped.get(k)
        if not m or not str(m.get("killing_case", "")).strip():
            problems.append(f"{k}: behavioural line with no proposed killing case")
    for k in reqs["no_mutant"]:
        w = waived.get(k)
        if not w or not str(w.get("reason", "")).strip():
            problems.append(f"{k}: no behavioural mutant and no waiver -- waive it "
                            "with a reason (why no mutant is possible) or add a case")
    req_all = set(reqs["covered"]) | set(reqs["survivor"]) | set(reqs["no_mutant"])
    extra = sorted((set(mapped) | set(waived)) - req_all)
    return {"ok": not problems, "problems": problems,
            "required": len(req_all), "extra": extra}


def render_signoff_prompt(reqs: dict, task_text: str, fixture_text: str) -> str:
    """Prompt for the local model to PROPOSE the line->killing-case mapping and
    the waivers. Strict-JSON contract; the model proposes, the gate checks."""
    lines = [f"- {k}   `{reqs['snippets'].get(k,'').strip()}`" for k in reqs["covered"]]
    nomut = [f"- {k}" for k in reqs["no_mutant"]]
    return (
        "You are proposing a RELEVANCE SIGN-OFF for a code change. For each "
        "CHANGED behavioural line below, name the ONE fixture test case that "
        "would FAIL if that line's behaviour were broken (the case that 'kills' "
        "a mutation of it). For each NO-MUTANT line, state why no behavioural "
        "mutation is possible (e.g. pure schema DDL, a constant table, a bare "
        "re-export) -- that is a waiver.\n\n"
        "Reply with STRICT JSON only, no prose, this exact shape:\n"
        '{"mappings":[{"line":"file:ln","killing_case":"<case name/desc from the '
        'fixture>","reason":"<one line>"}],'
        '"waivers":[{"line":"file:ln","reason":"<why no behavioural mutant is possible>"}]}\n\n'
        "== TASK ==\n" + task_text.strip() + "\n\n"
        "== FIXTURE (the cases you must cite by name) ==\n" + fixture_text.strip() + "\n\n"
        "== CHANGED BEHAVIOURAL LINES (map each to a killing case) ==\n"
        + ("\n".join(lines) if lines else "(none)") + "\n\n"
        "== NO-MUTANT LINES (waive each with a reason) ==\n"
        + ("\n".join(nomut) if nomut else "(none)") + "\n")


def parse_signoff_json(text: str) -> dict:
    """Pull the strict-JSON object out of a model reply (tolerates a code fence
    or leading prose). Raises ValueError if no object parses."""
    import json as _json
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    obj = _json.loads(t)
    obj.setdefault("mappings", [])
    obj.setdefault("waivers", [])
    return obj


def propose_signoff(reqs: dict, task_text: str, fixture_text: str, drafter) -> dict:
    """Call `drafter(prompt)->str` (a local model or a test stub) and parse its
    proposal. Never decides GO -- returns the proposal for check_signoff()."""
    prompt = render_signoff_prompt(reqs, task_text, fixture_text)
    return parse_signoff_json(drafter(prompt))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("worktree")
    ap.add_argument("--verify", default="bash verify.sh")
    ap.add_argument("--task-file", default="TASK.md")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refimpl", help="reference-impl patch")
    g.add_argument("--refimpl-cmd", help="command that writes the reference impl")
    g.add_argument("--applied", action="store_true",
                   help="the tree already carries the fix as uncommitted "
                        "changes; measure in place and leave it so")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--min-mutants", type=int, default=DEFAULT_MIN_MUTANTS)
    ap.add_argument("--max-mutants", type=int, default=DEFAULT_MAX_MUTANTS)
    ap.add_argument("--budget-s", type=int, default=DEFAULT_BUDGET_S)
    ap.add_argument("--base", default="",
                    help="diff the fix against this commit instead of HEAD/index "
                         "(gate: an auto-fix round's chain root, whose HEAD is a "
                         "round seal holding the previous attempt)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-necessity", action="store_true",
                    help="skip the per-hunk NECESSITY hard gate (diagnostics only)")
    ap.add_argument("--no-wrong-bank", action="store_true",
                    help="skip the WRONG-SOLUTION BANK hard gate (diagnostics only)")
    ap.add_argument("--llm-equivalence-cmd", default="",
                    help="OPT-IN (default off): command reading {survivors:[...]} JSON on "
                         "stdin, printing a JSON list of survivor ids it judges equivalent; "
                         "advisory flag only, the verdict still counts them")
    ap.add_argument("--llm-wrong-solutions-cmd", default="",
                    help="OPT-IN (default off): command reading {files, task} JSON on stdin, "
                         "printing [{file, source, why, line}] plausible-wrong full-file "
                         "variants to add to the bank (validated, equivalent-filtered)")
    ap.add_argument("-v", action="store_true", help="print each mutant as it runs")
    ap.add_argument("--signoff", action="store_true",
                    help="after measuring, emit a relevance SIGN-OFF artifact "
                         "(line->killing-case proposal + waivers) for the "
                         "coordinator to CHECK; never auto-approves")
    ap.add_argument("--signoff-drafter-cmd",
                    help="command whose stdout is the proposal JSON (a local "
                         "model, or a test stub); omit to emit the requirements "
                         "with an empty proposal for a later proposer/human pass")
    ap.add_argument("--fixture-file", default="",
                    help="fixture the proposer cites cases from (default: "
                         "auto-detect verify.test.ts / test_fixture.py / *.test.*)")
    ap.add_argument("--signoff-out", default="relevance-signoff.json",
                    help="artifact path, relative to the worktree")
    a = ap.parse_args()
    if not (a.refimpl or a.refimpl_cmd or a.applied):
        ap.error("need --refimpl, --refimpl-cmd or --applied")

    wt = Path(a.worktree).expanduser().resolve()
    task = wt / a.task_file
    literals = must_contain_literals(task.read_text()) if task.is_file() else []
    io_adapters = declared_io_adapters(task.read_text()) if task.is_file() else []

    before = _untracked(wt)
    if not a.applied:
        rc, out = _git(wt, "status", "--porcelain")
        if any(l[:2] != "??" for l in out.splitlines() if l.strip()):
            print("tree is not at baseline (tracked modifications); refusing to "
                  "apply a refimpl on top", file=sys.stderr)
            return 2
        ok, why = _apply(wt, a.refimpl, a.refimpl_cmd)
        if not ok:
            print(f"could not apply the reference impl: {why}", file=sys.stderr)
            return 2

    # New/untracked files the refimpl CREATED do not appear in `git diff` at
    # all, so their lines would never be counted as "added" and never mutated
    # (the new-file blind spot). Stage them intent-to-add (`git add -N`) so the
    # diff reports their full contents as added lines -- scoped to EXACTLY the
    # files the refimpl added, i.e. those that appeared since `before`. This is
    # only decidable in the --refimpl / --refimpl-cmd paths, where `before` is a
    # clean pre-apply snapshot; in --applied mode the fix is already present, so
    # there is no baseline to tell a fix-created file from unrelated untracked
    # scaffolding -- staging there would mutate files the fix never touched, so
    # we do not (the safe direction). Pre-existing tracked code the diff did not
    # touch is never pulled in either way.
    new_untracked = [] if a.applied else sorted(_untracked(wt) - before)
    # --applied CREATION TARGET (2026-10-03, idle-test-repo-duration2 41f36e7a653e):
    # in --repo mode the creation target is never sealed, so on the finished job
    # tree it is UNTRACKED and the gate's relevance step exited 3 "no tracked
    # diff". The one untracked file that IS decidable here is the target the
    # scaffold DECLARED in .dispatch-harness.json (creation_task): it did not
    # exist at HEAD, so its whole content is the fix. Stage exactly that file.
    if a.applied:
        new_untracked = _applied_creation_target(wt)
        # + NEW files the task's "Only edit" line DECLARES (2026-10-05,
        # rt-egift-link-s1-s0): a creation task's new migration.sql is product by
        # declaration, and leaving it untracked hid every FK/CASCADE/column line
        # from the mutator. Only declared, plain-untracked files -- never stray
        # scaffolding.
        new_untracked = sorted(set(new_untracked) | set(_applied_scope_new_files(wt, task)))
    staged_ita: list[str] = []
    if new_untracked:
        rc, _ = _git(wt, "add", "-N", "--", *new_untracked)
        if rc == 0:
            staged_ita = new_untracked

    def _unstage_ita():
        # undo the intent-to-add so the files return to untracked (leaving the
        # working-tree bytes intact); the finally block then removes the ones
        # the refimpl created, or, in --applied mode, leaves the fix in place.
        if staged_ita:
            _git(wt, "reset", "-q", "--", *staged_ita)

    # --base (2026-10-05, rt-egift-link-s1-s0 01fdda651fba): an auto-fix round runs on
    # a tree whose HEAD seals the PREVIOUS attempt, so the plain diff was only that
    # round's delta (a rename) -- its mutants were equivalent (a constraint NAME) and
    # relevance read LOW on a correct, fully tested fix. Measure the cumulative work.
    _base_ok = False
    if a.base:
        rc_b, _ = _git(wt, "merge-base", "--is-ancestor", a.base, "HEAD")
        _base_ok = rc_b == 0
        if not _base_ok:
            print(f"verify-relevance: --base {a.base} is not an ancestor of HEAD -- "
                  f"ignoring it (diffing against HEAD)", file=sys.stderr)
    rc, diff_text = (_git(wt, "diff", "-U0", a.base) if _base_ok
                     else _git(wt, "diff", "-U0"))
    diff_text, _dropped_harness = strip_harness_files(diff_text)
    if _dropped_harness:
        print("verify-relevance: ignoring harness file(s) in the diff (not the fix): "
              + ", ".join(_dropped_harness), file=sys.stderr)
    if not diff_text.strip():
        # nothing tracked AND no new files staged above: truly nothing to mutate
        print("the reference impl produced no tracked diff; nothing to mutate",
              file=sys.stderr)
        _unstage_ita()
        if not a.applied:
            _git(wt, "checkout", "--", ".")
        return 3

    def prog(m):
        if a.v:
            print(f"  {'KILLED ' if m.killed else 'SURVIVE'} {m.klass:<16} "
                  f"{m.path}:{m.lineno} {m.desc}"
                  f"{' [crash]' if m.crash else ''}")

    signoff_reqs = None
    try:
        rec = measure_applied(wt, a.verify, diff_text, literals=literals,
                              threshold=a.threshold, min_mutants=a.min_mutants,
                              max_mutants=a.max_mutants, budget_s=a.budget_s,
                              progress=prog, io_adapters=io_adapters,
                              necessity=not a.no_necessity,
                              wrong_bank=not a.no_wrong_bank,
                              llm_equivalence_cmd=a.llm_equivalence_cmd or None,
                              llm_wrong_solutions_cmd=a.llm_wrong_solutions_cmd or None)
        if a.signoff:
            # Build requirements while the FIX is still applied -- the finally
            # below reverts the tree, and behavioral_added_lines reads the fixed
            # file content to decide which added lines carry behaviour.
            signoff_reqs = signoff_requirements(rec, wt, diff_text)
    finally:
        _unstage_ita()
        if not a.applied:
            _git(wt, "checkout", "--", ".")
            for rel in sorted(_untracked(wt) - before):
                p = wt / rel
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                except Exception:
                    pass
    if a.json:
        print(json.dumps(rec, indent=2))
    else:
        print(f"verify-relevance: {rec['verdict'].upper()}  score="
              f"{rec['score'] if rec['score'] is not None else '-'} "
              f"(killed {rec['killed']}/{rec['evidence_mutants']} evidence mutants; "
              f"{rec['literal_breaking']} literal-breaking excluded; "
              f"{rec['crash_kills']} crash kills; {rec['seconds']}s)")
        print(f"  {rec['reason']}")
        if rec.get("site_score") is not None:
            print(f"  mutant_score={rec['mutant_score']} site_score={rec['site_score']} "
                  f"site_coverage={rec.get('site_coverage')} (verdict uses the lowest)")
        for u in rec.get("untried_sites", []):
            print(f"  UNTRIED {u}: the cap/budget ran out before this line was "
                  f"mutated -- nothing is known about it")
        for u in rec.get("unexercised_sites", []):
            print(f"  UNEXERCISED {u}: every mutant on this line survived -- the "
                  f"verify never runs this path")
        for s in rec["survivors"]:
            print(f"  SURVIVOR {s['class']:<16} {s['file']}:{s['line']} "
                  f"{s['mutation']}:  {s.get('original') or '?'}  ==>  {s['snippet']}")
    base_rc = {"relevant": 0, "low": 1, "unproven": 3}[rec["verdict"]]

    if a.signoff and signoff_reqs is not None:
        # Locate the fixture the proposer should cite cases from.
        fx = wt / a.fixture_file if a.fixture_file else None
        if fx is None or not fx.is_file():
            for cand in ("verify.test.ts", "test_fixture.py", "verify_impl.mts",
                         "verify_impl.mjs", "verify_impl.js"):
                if (wt / cand).is_file():
                    fx = wt / cand
                    break
            else:
                globbed = sorted(list(wt.glob("*.test.*")) + list(wt.glob("*_fixture.*")))
                fx = globbed[0] if globbed else None
        fixture_text = fx.read_text() if (fx and fx.is_file()) else ""
        task_text = task.read_text() if task.is_file() else ""

        proposal = {"mappings": [], "waivers": []}
        proposer_note = ("no --signoff-drafter-cmd: emitted requirements with an "
                         "empty proposal for a proposer/human pass")
        if a.signoff_drafter_cmd:
            try:
                cp = subprocess.run(a.signoff_drafter_cmd, shell=True, cwd=str(wt),
                                    capture_output=True, text=True, timeout=900)
                proposal = parse_signoff_json(cp.stdout)
                proposer_note = f"proposal from --signoff-drafter-cmd (rc={cp.returncode})"
            except Exception as e:
                proposer_note = f"proposer failed ({type(e).__name__}: {e}); empty proposal"

        chk = check_signoff(signoff_reqs, proposal)
        artifact = {
            "verdict": rec["verdict"],
            "relevance_score": rec.get("score"),
            "fixture": (fx.name if fx else None),
            "requirements": signoff_reqs,
            "proposal": proposal,
            "check": chk,
            "proposer_note": proposer_note,
            "approved": False,   # NEVER auto-approved: the coordinator checks this
        }
        out_path = wt / a.signoff_out
        out_path.write_text(json.dumps(artifact, indent=2))
        if not a.json:
            state = "COMPLETE" if chk["ok"] else "INCOMPLETE"
            print(f"\nrelevance-signoff: {state}  "
                  f"({len(signoff_reqs['covered'])} behavioural line(s) to map, "
                  f"{len(signoff_reqs['no_mutant'])} to waive, "
                  f"{len(signoff_reqs['survivor'])} survivor(s)) -> {out_path}")
            print(f"  {proposer_note}")
            for p in chk["problems"][:12]:
                print(f"  HOLE {p}")
            if chk["ok"]:
                print("  every changed line is mapped-or-waived; READ the artifact "
                      "and confirm the proposed cases before enqueue (not auto-approved)")
        # An incomplete sign-off is not a GO: surface rc=4 unless relevance is
        # already worse (low=1 stays, unproven=3 stays).
        if not chk["ok"]:
            return max(base_rc, 4)
    return base_rc


def _self_test() -> int:
    """Offline checks for the pure helpers (same check(name, got, want) convention
    as the other bin/ tools). Run: verify-relevance.py --self-test"""
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f": got {got!r} want {want!r}"))

    # --- python_code_lines: comments inside a compound header gap are NOT code -----
    # (2026-09-24, arr-webhook s11: the comment under `while True:` read as code and
    # its comment-only hunk-revert survived as the one "property-breaking" mutant.)
    src = "\n".join([
        "def f(state):",                                  # 1  header (code)
        '    """doc',                                     # 2  docstring (not code)
        '    string."""',                                 # 3
        "    while True:",                                # 4  header (code)
        "        # a comment between the colon and the first statement",  # 5 NOT code
        "",                                               # 6  blank NOT code
        "        now = 1",                                # 7  code
        "        x = 2  # relevance: unobservable",       # 8  opted out -> not code
        "        if x:",                                  # 9  header (code)
        "            # another gap comment",              # 10 NOT code
        "            return now",                         # 11 code
        "        break",                                  # 12 code
    ])
    cl = python_code_lines(src)
    check("code_lines: def header is code", 1 in cl, True)
    check("code_lines: docstring is not code", cl & {2, 3}, set())
    check("code_lines: while header is code", 4 in cl, True)
    check("code_lines: THE BUG -- comment between `while True:` and its body is NOT code", 5 in cl, False)
    check("code_lines: blank line in the header gap is NOT code", 6 in cl, False)
    check("code_lines: first body statement is code", 7 in cl, True)
    check("code_lines: `# relevance: unobservable` line is not code (author opt-out honored)", 8 in cl, False)
    check("code_lines: nested if header is code, its gap comment is not", (9 in cl, 10 in cl), (True, False))
    check("code_lines: return / break are code", cl >= {11, 12}, True)
    check("code_lines: unparseable -> None", python_code_lines("def (:"), None)

    # --- RELEVANCE_OPT_OUT: hash AND double-slash forms -------------------------
    check("opt_out: hash relevance unobservable matches",
          bool(RELEVANCE_OPT_OUT.search("x = 1  # relevance: unobservable")), True)
    check("opt_out: double-slash relevance ignore matches",
          bool(RELEVANCE_OPT_OUT.search("const x = 1; // relevance: ignore")), True)
    check("opt_out: `//relevance:benign` (no spaces) matches",
          bool(RELEVANCE_OPT_OUT.search("//relevance:benign")), True)
    check("opt_out: plain comment without the marker does NOT match",
          bool(RELEVANCE_OPT_OUT.search("# just a note")), False)
    check("opt_out: `// TODO cleanup` does NOT match",
          bool(RELEVANCE_OPT_OUT.search("// TODO cleanup")), False)
    check("opt_out: `relevance:` with an unknown word does NOT match",
          bool(RELEVANCE_OPT_OUT.search("# relevance: something-else")), False)

    # --- hunk_revert_mutants: a comment-only hunk is not material ---------------
    hunks = [
        {"new_start": 4, "new_lines": ["    while True:"], "old_lines": ["    while 1:"]},
        {"new_start": 5, "new_lines": ["        # a comment between the colon and the first statement"],
         "old_lines": ["        # old comment"]},
        {"new_start": 7, "new_lines": ["        now = 1"], "old_lines": ["        now = 0"]},
    ]
    ms = hunk_revert_mutants("t.py", src, hunks, cl)
    check("hunk-revert: comment-only hunk in a header gap produces NO mutant",
          any(m.lineno == 5 for m in ms), False)
    check("hunk-revert: the two code hunks still produce mutants", sorted(m.lineno for m in ms), [4, 7])
    # Revert-test: with the OLD code_lines (header gap included) the comment hunk WAS material.
    ms_old = hunk_revert_mutants("t.py", src, hunks, cl | {5})
    check("hunk-revert: (revert-test) the old classification did emit the equivalent mutant",
          any(m.lineno == 5 for m in ms_old), True)

    # --- a survivor names its ORIGINAL line, not only the mutant --------------
    # (2026-09-27, sidecar-bfmr s2: `return origOpen.apply(this, arguments);`
    # call-deleted to `return this;` was reported with only the mutated text and
    # read as "a call-delete on a line with no call".)
    fx = "def f(a):\n    return g(a)\n"
    pm = [m for m in python_mutants("t.py", fx, {2}) if "g(a)" not in m.snippet]
    check("mutant: records the unmutated original line",
          bool(pm) and all(m.original == "return g(a)" for m in pm), True)
    check("mutant: as_dict carries `original`",
          bool(pm) and pm[0].as_dict().get("original"), "return g(a)")
    # --- C# token mutants (2026-09-27) ----------------------------------------
    cs = "\n".join([
        "namespace N;",                                            # 1
        "public static class C {",                                 # 2
        "    public static int F(int x, int lo) {",                # 3
        "        if (x < lo && lo != 0) return lo; // x == 1",     # 4
        '        var s = "a == b && true";',                       # 5
        "        List<int> xs = new List<int>();",                 # 6
        "        Console.WriteLine(x == 2);",                      # 7
        "        return x + 1;",                                   # 8
        "    }",
        "}"])
    cm = csharp_mutants("C.cs", cs, {3, 4, 5, 6, 7, 8})
    by = {}
    for m in cm:
        by.setdefault(m.lineno, []).append(m.klass)
    check("cs: comparison / boolean-op / if-negate / return mutants on a real line",
          set(by.get(4, [])) >= {"cmp-bound", "boolop-swap", "cmp-flip", "cond-negate",
                                 "return-default"}, True)
    check("cs: nothing inside a // comment is mutated (the `== 1` after //)",
          any("x != 1" in m.source.split("\n")[3] for m in cm), False)
    check("cs: nothing inside a string literal is mutated, only the _X suffix",
          sorted(set(by.get(5, [])) - {"stmt-delete"}), ["const-str"])
    check("cs: generic angle brackets are NOT comparisons",
          sorted(k for k in by.get(6, []) if k.startswith("cmp")), [])
    check("cs: a logging line is innocuous (never mutated)", 7 in by, False)
    check("cs: literal ints are bumped and returns defaulted on line 8",
          set(by.get(8, [])) >= {"const-int", "return-default"}, True)
    check("cs: only ADDED lines are mutated",
          csharp_mutants("C.cs", cs, {8}) and
          {m.lineno for m in csharp_mutants("C.cs", cs, {8})} == {8}, True)
    check("cs: an if gets force-true/false and a statement line gets deleted",
          set(by.get(4, [])) >= {"cond-force-true", "cond-force-false", "stmt-delete"}, True)
    check("cs: every mutant records its original line",
          all(m.original for m in cm), True)

    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _r = signoff_requirements({"mutants": [
            {"file": "t.js", "line": 3, "killed": True, "snippet": "return this;",
             "original": "return origOpen.apply(this, arguments);"}]}, Path(_td), "")
    check("sign-off: the changed line shown to the proposer is the ORIGINAL",
          _r["snippets"].get("t.js:3"), "return origOpen.apply(this, arguments);")

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
