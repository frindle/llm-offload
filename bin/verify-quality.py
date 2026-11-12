#!/usr/bin/env python3
"""verify-quality.py -- decide whether a --verify command is actually a test.

WHY
---
A dispatch shipped an INVERTED boolean this week and passed verify cleanly. The
spec literally handed the model `fresh.online === true`; it wrote
`_telemetryDegraded = !fresh.online`. The verify grepped for
`_telemetryDegraded = ...online`, which matches BOTH forms. The run did not even
converge -- it shipped only because an accept-on-verify-pass net trusted a check
that could not fail for the reason it existed.

An unvalidated test is worse than no test: it manufactures the confidence that
lets a bad run through.

TWO INDEPENDENT PROPERTIES, and conflating them is what went wrong
-----------------------------------------------------------------
1. DISCRIMINATES CHANGE  -- does verify FAIL on the untouched tree?
   If it passes before any work is done, it cannot tell done from not-done.
2. DISCRIMINATES CORRECTNESS -- can verify tell a CORRECT change from an
   INCORRECT one?

Property 1 is necessary and NOT sufficient, which is precisely the trap. In the
inverted-flag case baseline was `_telemetryDegraded = true` -- no "online" -- so
the grep DID fail at baseline and property 1 was satisfied. Both the correct and
the inverted form are equally "different from baseline", so no baseline
comparison can separate them. Only property 2 catches it, and the cheap way to
get property 2 is a WHOLE-LITERAL assertion taken from the spec: a literal
cannot match its own negation.

Exit codes: 0 = verify looks sound; 1 = verify is NOT discriminating (refuse the
dispatch); 2 = usage/error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _finding(kind: str, fid: str, reason: str, literal: str | None = None) -> dict:
    """One machine-readable finding.

    `kind` keeps the two populations this tool reports DISTINCT in JSON too:
    "problem" (makes the verdict fail) vs "note" (abstain / advisory -- a check
    that could not run, or ran and found nothing). Collapsing them is exactly
    what loses the difference between "could not run" and "ran clean".
    `id` is stable across runs on the same input; consumers key off it.
    `literal` carries the offending literal where one exists.
    """
    d = {"id": fid, "kind": kind, "reason": reason}
    if literal is not None:
        d["literal"] = literal
    return d

# Code-shaped literals worth asserting on: comparisons, assignments, calls.
# SINGLE SOURCE OF TRUTH for literal extraction. This file used to carry its own
# copy of the patterns, and they DRIFTED: completeness.py learned to reject
# referenced file paths and prose-with-an-equals-sign, this copy did not, so the
# same false positives kept firing here after being fixed there. Same class as
# DEFAULT_HOST not matching KNOWN_OLLAMA_HOSTS -- two records of one fact.
def _load_completeness():
    import importlib.util
    sp = importlib.util.spec_from_file_location(
        "completeness", str(Path(__file__).parent / "completeness.py"))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def _source_text_scan(cwd: Path, verify_cmd: str, spec: str):
    """source_text_harness.scan_worktree (next to this file). Fail-OPEN (None)
    when absent/erroring -- never a false problem from a broken checker."""
    try:
        import importlib.util
        p = Path(__file__).resolve().parent / "source_text_harness.py"
        if not p.is_file():
            return None
        sp = importlib.util.spec_from_file_location("source_text_harness", str(p))
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m.scan_worktree(cwd, verify_cmd, task_text=spec or None)
    except Exception:
        return None


def spec_literals(spec: str) -> list[str]:
    """Delegates to completeness.py so the two tools cannot disagree."""
    try:
        return _load_completeness().spec_literals(spec)
    except Exception:
        return []


def verify_text(cmd: str, cwd: Path) -> str:
    """The text to inspect: the command PLUS any script it actually runs.

    Found 2026-08-31 while testing this tool against the real shadow-gate case.
    Every text property here scanned `args.verify` -- but in practice that string
    is `bash verify.sh`, and all the assertions live in the FILE. So the literal
    checks, the compiler check and the wildcard check were all searching an empty
    haystack and firing spuriously on every file-based verify: it reported "the
    verify asserts no spec literals" and "runs no compiler" about a script that
    did both. A checker that inspects the wrong artifact produces confident
    nonsense -- the same failure I keep finding in other people's harnesses.
    """
    # Follow scripts TRANSITIVELY. The first version followed `bash verify.sh`
    # but not the `python3 test_dedupe.py` INSIDE it -- so the assertions, which
    # lived one hop further down, were invisible. That produced a false
    # "constraint not tested" against a verify that did test it. Same
    # wrong-artifact bug as the original --verify-string one, one level deeper:
    # fixing the instance and not the class is why it recurred.
    # THE INTERPRETER CAN BE A VARIABLE, and in practice it usually is. The
    # ollama-dispatch skill mandates a venv for env parity:
    #     PY=.venv/bin/python
    #     "$PY" test_fixture.py
    # A literal alternation (bash|python3|...) never matches `"$PY"`, so the
    # follow stopped at verify.sh and every assertion one hop down was invisible
    # -- producing "the verify asserts none of the spec literals" against a
    # verify that asserts all of them. A FALSE PROBLEM against a sound verify is
    # the worst failure this tool has: it trains people to discount the verdict.
    #
    # This is the SAME CLASS as the bug the comment above describes, resurfacing
    # through a different spelling of the same hop -- which is exactly what that
    # comment warned about ("fixing the instance and not the class is why it
    # recurred"). So the fix is the class: any variable in interpreter position,
    # quoted or braced or bare.
    #
    # Widening here is safe against false positives because a match is only
    # followed when the path EXISTS on disk (see f.is_file() below) -- an
    # `echo "$MSG" foo` matches the pattern and is then silently dropped.
    _RUNNER = re.compile(
        r"(?:^|[|;&(]\s*)"
        r"(?:bash|sh|zsh|source|\.|python3?|node|deno|ruby|perl|pytest"
        r'|"?\$\{?\w+\}?"?)'
        r"\s+(?:-\S+\s+)*([\w./-]+\.[A-Za-z0-9_]+|[\w./-]+)|(?:^|\s)(\./[\w./-]+)",
        re.M)
    text = cmd
    seen: set[str] = set()
    frontier = [cmd]
    for _ in range(3):                      # bounded: verify -> script -> script
        nxt = []
        for chunk in frontier:
            for m in _RUNNER.finditer(chunk):
                rel = (m.group(1) or m.group(2) or "").strip()
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                f = (cwd / rel.lstrip("./")).expanduser()
                if not f.exists():
                    f = Path(rel).expanduser()
                try:
                    if f.is_file() and f.stat().st_size < 400_000:
                        body = f.read_text()
                        text += "\n" + body
                        nxt.append(body)
                except Exception:
                    pass
        if not nxt:
            break
        frontier = nxt
    return text


def _norm(t: str) -> str:
    """Drop shell/regex escaping so a literal matches its escaped form.

    `grep -Eq "syncedOk === false \\|\\| limitReached === true"` contains the
    spec literal, but only after the backslashes come out.
    """
    return re.sub(r"\\(.)", r"\1", t)


def run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout,
                           env={**os.environ, "DISPATCH_VERIFY_SANDBOX": "1"})  # VERIFY-SANDBOX
        return p.returncode, (p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return 124, "verify timed out"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", required=True, help="the verify command")
    ap.add_argument("--cwd", required=True, help="repo/worktree at BASELINE state")
    ap.add_argument("--task-file", help="task spec, for literal extraction")
    ap.add_argument("--task", default="", help="task spec inline")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--no-baseline", action="store_true",
                    help="tree is NOT at baseline (model already edited it): "
                         "skip the baseline-fail property rather than inverting it")
    ap.add_argument("--typed", action="store_true",
                    help="change is in a typed language; require a compile step")
    ap.add_argument("--json", action="store_true",
                    help="emit exactly one JSON object on stdout (stable verdict "
                         "string + findings list); exit code unchanged, human "
                         "stdout suppressed")
    args = ap.parse_args()

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.exists():
        print(f"ERROR: cwd {cwd} does not exist", file=sys.stderr)
        return 2
    spec = args.task
    if args.task_file:
        spec = Path(args.task_file).expanduser().read_text()

    problems: list[str] = []
    notes: list[str] = []
    vtext = _norm(verify_text(args.verify, cwd))
    if vtext.strip() != args.verify.strip():
        notes.append(_finding("note", "script-followed",
                              f"inspecting the verify command plus {len(vtext)} "
                              f"chars of script it runs"))

    # --- property 1: must FAIL at baseline -------------------------------
    # ONLY meaningful on an untouched tree. Run against a tree the model has
    # already edited, this INVERTS: the verify passes BECAUSE the work was done,
    # and it then reports "verify passes on the untouched tree" -- a
    # high-severity false alarm. Found 2026-08-31 writing the gate runner, which
    # by construction always runs AFTER the edits; it would have produced a false
    # FAIL on essentially every real dispatch.
    rc = None
    if args.no_baseline:
        notes.append(_finding(
            "note", "baseline-skipped",
            "baseline check SKIPPED (--no-baseline): this tree is not at "
            "baseline, so a passing verify proves nothing either way"))
    else:
        rc, _out = run(args.verify, cwd, args.timeout)
    if rc == 0:
        problems.append(_finding(
            "problem", "passes-at-baseline",
            "verify PASSES on the untouched tree, so it cannot distinguish a "
            "completed task from an untouched one. A run that changes nothing "
            "would be certified as done."))
    elif rc is not None:
        notes.append(_finding("note", "baseline-failed-as-expected",
                              f"baseline: verify exits {rc} (fails as it should)"))

    # --- property 2: must be able to separate correct from incorrect -----
    lits = spec_literals(spec)
    if lits:
        asserted = [l for l in lits if l in vtext]
        if not asserted:
            problems.append(_finding(
                "problem", "no-literal-asserted",
                "the spec hands the model exact code literals but the verify "
                "asserts none of them, so it cannot tell a correct "
                "implementation from an inverted or altered one. Add a "
                "whole-literal check -- a literal cannot match its own negation.",
                literal=lits[0]))
            notes.append(_finding(
                "note", "literals-available",
                "literals available: " + "; ".join(repr(l) for l in lits[:4]),
                literal=lits[0]))
        else:
            notes.append(_finding("note", "literals-asserted",
                                  f"asserts {len(asserted)} spec literal(s)",
                                  literal=asserted[0]))

    # A substring grep on an identifier matches the negation too -- the exact
    # shape of the shipped bug.
    if re.search(r"grep[^|]*\b\w+\s*=\s*\.\.\.|grep[^|]*=\s*[^'\"]*\.\.\.", vtext):
        problems.append(_finding(
            "problem", "wildcard-grep",
            "verify greps a wildcard around an assignment; that matches both "
            "a value and its negation"))

    # --- property 3: report ALL failures, not just the first -------------
    # Found live 2026-08-31 (shadow-gate case 1). The dispatch's verify.sh used
    # `fail(){ echo "$1"; exit 1; }` and aborted at check 2 of 6 -- so the
    # flag-repoint check, both removal checks and `tsc` NEVER RAN, and a real
    # control-flow regression sat in code no later assertion ever evaluated.
    # A first-failure-exit verify reports one problem and hides the state of
    # everything after it, which also makes the worker's "N new failures
    # attributable to this diff" count systematically wrong.
    if re.search(r"\bexit\s+1\b", vtext) or re.search(r"\bfail\s*\(\)", vtext):
        notes.append(_finding(
            "note", "first-failure-exit",
            "verify appears to exit on first failure -- prefer running every "
            "assertion and reporting all failures, or the model fixes one thing "
            "at a time and later checks never run"))

    # --- property 4: do not gate on FORM the spec did not mean to fix ----
    # Also from shadow-gate case 1. The spec said "Parse `{ syncedOk, limitReached }`"
    # and the verify asserted that literal expression; the model wrote the
    # semantically identical `health.syncedOk === false || health.limitReached
    # === true` and verify failed CORRECT code. The literal was faithful to the
    # spec -- the defect is a spec dictating surface syntax it does not care
    # about. A literal whose operands are bare identifiers will reject the same
    # expression written with an object prefix.
    for lit in lits:
        if re.search(r"[=!<>]=|&&|\|\|", lit) and lit in vtext:
            bare = re.findall(r"\b([a-z_][A-Za-z0-9_]*)\s*[=!<>]=", lit)
            if bare:
                notes.append(_finding(
                    "note", "exact-expression-asserted",
                    f"verify asserts the expression {lit!r} as an exact string. "
                    f"A correct implementation that qualifies {bare[0]!r} (e.g. "
                    f"`obj.{bare[0]}`) or reorders commutative operands will FAIL. "
                    f"Either allow a qualifier, or intend the form and say so.",
                    literal=lit))
            break

    # --- property 5: a pass must PROVE THE TOOL RAN -------------------------
    # From a real dispatch (resell #310-hooks, 2026-08-31): the verify was
    #     npx eslint . 2>&1 | ! grep rules-of-hooks
    # qwen edited the WRONG FILE, the real violation survived, and the verify
    # PASSED -- because eslint did not report, the grep matched nothing, and
    # "no output" was treated as "no violations". A check whose success condition
    # is the ABSENCE of output passes identically when the tool crashes, is not
    # installed, or is silently misconfigured. That is the most dangerous verify
    # shape there is: it certifies work that was never done.
    # WIDENED after the first version failed on the REAL artifact. I wrote the
    # regex against my own paraphrase (`cmd | ! grep x`) and it did not match the
    # actual verify, which was:
    #     out=$(npx eslint . 2>&1); ! echo "$out" | grep -qiE "..." || exit 1
    # The `!` negates `echo`, not `grep`. Testing a checker against a
    # reconstruction instead of the artifact is exactly the failure this whole
    # project keeps rediscovering -- it would have handed them false assurance.
    _NEGGREP = re.compile(r"""(?x)
        !\s*[^|\n]*\|\s*grep          # ! anything | grep      (their real form)
      | \|\s*!\s*grep                  # cmd | ! grep
      | ^\s*!\s*grep                    # ! grep ...
      | \|\s*grep\s+-[a-zA-Z]*v        # | grep -v
    """, re.M)
    if _NEGGREP.search(vtext):
        proves_ran = re.search(
            r"\bset\s+-o\s+pipefail|PIPESTATUS|\$\?|--version\b|"
            r"\bcommand\s+-v\b|\bwhich\s+\w+|\btest\s+-x\b|\|\s*wc\s+-l|"
            r"\brc=\$\?|\bstatus=\$\?|-s\s+\S+|\[\s+-n\s+",
            vtext)
        msg = ("a check PASSES ON THE ABSENCE OF OUTPUT (negated grep on a "
               "pipeline). It passes identically if the tool crashes, is missing, "
               "or is misconfigured -- 'no output' is not 'no violations'. Assert "
               "the tool actually RAN: check its exit status (set -o pipefail / "
               "PIPESTATUS), or that it produced a non-empty report.")
        if proves_ran:
            notes.append(_finding(
                "note", "negated-grep-covered",
                "negated-grep check present, but the verify does assert the tool "
                "ran -- confirm that assertion covers THIS check"))
        else:
            problems.append(_finding("problem", "passes-on-absence-of-output", msg))

    # --- property 6: does the verify EXERCISE the task's stated constraints? --
    # Three real cases in one day, all the same shape: the task stated a
    # constraint, the verify never tested it, the dispatch passed, the work was
    # wrong.
    #   - a task said harness findings "must never be merged away"; the verify
    #     only tested model findings, so an implementation that dropped a
    #     computed finding passed
    #   - a verify asserted eslint output was clean but never that eslint RAN
    #   - a baseline-fail property run against a post-edit tree
    # Not fully decidable -- "did this assertion test that sentence" is a
    # judgement. But a MUST/NEVER clause whose distinctive identifiers appear
    # NOWHERE in the verify is a checkable proxy, and a cheap one.
    _IMPERATIVE = re.compile(
        r"(?im)^[^\n]*\b(must not|must never|must always|must|never|always|do not|don't)\b[^\n]*$")
    _IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
    _STOP = {"must", "never", "always", "should", "that", "this", "with", "from",
             "they", "them", "their", "than", "then", "when", "which", "while",
             "there", "here", "into", "onto", "does", "doesn", "will", "would",
             "have", "been", "being", "same", "each", "every", "only", "also",
             "more", "most", "other", "another", "keep", "make", "sure", "note",
             "code", "file", "files", "line", "lines", "change", "changes"}
    uncovered = []
    for m in _IMPERATIVE.finditer(spec):
        clause = m.group(0).strip()
        if len(clause) < 12:
            continue
        # ONLY clauses naming a CODE IDENTIFIER in backticks. A verify can assert
        # code, not prose -- "do not weaken the existing behaviour" is a real
        # constraint but nothing mechanical can check that it was tested, and
        # flagging it is noise. Earlier attempts ranked bare words by rarity and
        # picked grammatical terms ('neither', 'behaviour') as the supposed key,
        # which is a plausible-looking signal aimed at nothing.
        backticked = re.findall(r"`([^`\n]{2,60})`", clause)
        idents = {w for b in backticked for w in _IDENT.findall(b)
                  if w.lower() not in _STOP}
        if not idents:
            continue
        # Coverage must hinge on the clause's MOST DISTINCTIVE term, not on any
        # term. First version used any-match and reported the harness clause as
        # covered because the generic word "finding" appeared somewhere in the
        # test file -- a word that appears in every clause of that task. Rank by
        # rarity in the spec and require the rarest to be present.
        key = sorted(idents, key=lambda w: (spec.lower().count(w.lower()), -len(w)))[0]
        if key not in vtext:
            uncovered.append((clause[:110], key))
    if uncovered:
        notes.append(_finding(
            "note", "untested-constraint",
            f"{len(uncovered)} stated constraint(s) that NO assertion appears to "
            f"test -- a verify that does not exercise a MUST/NEVER lets a dispatch "
            f"pass while violating it (measured 3x in one day):"))
        for cl, key in uncovered[:4]:
            notes.append(_finding(
                "note", "untested-constraint-detail",
                f"    - {cl}   [its key term {key!r} appears nowhere in the verify]",
                literal=key))

    # --- property 7: the fixture must EXECUTE the target, not read its text --
    # 2026-10-05, rt-bfmr-link-sync-feedback: verify.test.ts readFileSync'd the
    # target and asserted content.includes(...) / regexes over its lines, never
    # importing it. It passed preflight, regate and second opinion. A fixture
    # that tests the SOURCE SHAPE fails a correct fix written differently and
    # passes a wrong one containing the strings. Literal presence belongs ONLY
    # in TASK.md `## Must contain` -> check_literals.py (never scanned here).
    _sth = _source_text_scan(cwd, args.verify, spec)
    if _sth and _sth.get("verdict") == "source-text":
        problems.append(_finding(
            "problem", "source-text-harness",
            "SOURCE-TEXT HARNESS: " + str(_sth.get("reason") or "")
            + " FIX: " + str(_sth.get("fix") or "")))
    elif _sth and _sth.get("verdict") == "mixed":
        notes.append(_finding(
            "note", "source-text-assertions",
            "some fixture assertions test the target's SOURCE TEXT (minority; "
            "advisory): " + str(_sth.get("reason") or "")[:400]
            + " -- move literal checks to `## Must contain`; assert behaviour."))

    if args.typed and not re.search(r"\btsc\b|--noEmit|\bmypy\b|\bcargo (check|build)\b|"
                                    r"\bgo build\b|\bjavac\b", vtext):
        problems.append(_finding(
            "problem", "typed-no-compiler",
            "typed change but the verify runs no compiler (tsc --noEmit / mypy / "
            "cargo check); a type error would ship"))

    # Every finding the human path prints appears in the JSON too, and vice
    # versa: notes (abstains / advisories) and problems (verdict-failers) are
    # one list here, kept distinguishable by each finding's "kind". A fact
    # visible to a human but missing from the machine output -- or wrong there
    # -- is a bug this codebase has hit repeatedly.
    findings = notes + problems
    if args.json:
        doc = {
            "verdict": "pass" if not problems else "fail",
            "findings": findings,
        }
        print(json.dumps(doc))
    else:
        print("=== verify-quality ===")
        for n in notes:
            print(f"  note: {n['reason']}")
        if not problems:
            print("  VERDICT: verify looks discriminating.")
        else:
            for p in problems:
                print(f"  PROBLEM: {p['reason']}")
            print("  VERDICT: verify is NOT discriminating -- refuse the dispatch.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
