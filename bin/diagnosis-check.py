#!/usr/bin/env python3
"""Decide whether a proposed DIAGNOSIS is acceptable, without a reference fix.

Normal verify-gated dispatch needs someone who already understands the bug well
enough to write the verify. That is the ceiling on offloading. This lifts it: a
model is given a symptom and returns a root cause plus a test that demonstrates
it, and we decide mechanically whether to believe it.

THE OBVIOUS RULE IS VACUOUS. "Accept if the repro fires" is satisfied by

    def test_bug(): assert False

which fires perfectly and proves nothing -- and unlike a normal verify there is
no reference solution to catch it. So firing is necessary and nowhere near
sufficient. Four criteria:

  1. FAILS_AT_HEAD   the test fails on the buggy code            (necessary only)
  2. PASSES_AT_ANCHOR the test passes on a pre-bug commit -- the load-bearing
     one. A test that fails at BOTH ends is not testing this bug. This is what
     kills assert-False, and it needs no fix to exist, only git history.
  3. DRIVES_SYMBOL   the test actually reaches the named cause, proven by AST,
     not grep -- a file merely mentioning the symbol in a comment fails this.
     The execution fallback (settrace) binds the name to its SITE: the frame
     must run in the diagnosed file AND be on the fault path (it raised, or the
     regression range changed its definition). A generic helper that merely
     runs somewhere in the call graph is not a cause.
  4. NOT_UNCONDITIONAL the test's failure is contingent on behaviour: a body
     that is a bare assert False / raise / pytest.fail with no call into the
     code under test is rejected outright.

Criterion 2 only exists for REGRESSIONS. With no anchor the verdict is
ANCHOR_UNAVAILABLE -- flagged for human review, never auto-accepted. Saying the
method has a restricted domain is better than pretending it generalises.

Usage:
  diagnosis-check.py --repo R --test t.py --symbol NAME [--anchor SHA] [--json]
  diagnosis-check.py --self-check      # proves it REJECTS, not just accepts
"""
import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_TIMEOUT = 300


def build_test_cmd(test_rel: str, test_cmd: str | None):
    """Resolve the command that RUNS a proposed test.

    Defaults to running it with this interpreter. Configurable because the tool
    was Python-only: `sys.executable <test>` cannot grade a TypeScript or JS
    target at all, which silently excluded whole repos from the candidate pool
    (measured 2026-09-02: 16 of 18 revert-signature candidates were in TS repos
    and therefore ungradeable, which reads as 'no candidates exist').
    """
    if not test_cmd:
        return [sys.executable, test_rel]
    import shlex
    return [w.replace("{test}", test_rel).replace("{python}", sys.executable)
            for w in shlex.split(test_cmd)]


def run_test(repo: Path, test_rel: str, at: str | None = None, test_cmd=None):
    """Run the test, optionally at a git ref, in a throwaway copy.

    Never mutates the caller's tree: the ancestor check happens in a temp clone,
    so a dirty worktree or a failed checkout cannot destroy the real one.
    """
    cmd = build_test_cmd(test_rel, test_cmd)
    if at is None:
        return subprocess.run(cmd, cwd=str(repo),
                              capture_output=True, text=True,
                              timeout=TEST_TIMEOUT).returncode
    tmp = Path(tempfile.mkdtemp())
    work = tmp / "at"
    try:
        subprocess.run(["git", "clone", "-q", str(repo), str(work)],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", at],
                       capture_output=True, check=True)
        # The TEST itself must come from the proposal, not from history -- the
        # ancestor predates the test, so checking out the ancestor would delete
        # it and we would be measuring "file missing", not "bug absent".
        # test_rel MUST be repo-relative. An absolute path makes
        # `work / test_rel` collapse to the original file (pathlib discards
        # the left side), which writes back into the source repo and puts the
        # ORIGINAL dir on sys.path[0] -- so the HEAD module is imported and
        # the anchor checkout is silently bypassed. main() rejects absolute
        # paths; this assert covers any other caller.
        assert not Path(test_rel).is_absolute(), \
            f"test_rel must be repo-relative, got absolute: {test_rel}"
        (work / test_rel).write_text((repo / test_rel).read_text())
        return subprocess.run(cmd, cwd=str(work),
                              capture_output=True, text=True,
                              timeout=TEST_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return None
    except subprocess.CalledProcessError:
        return None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_test_stable(repo, test_rel, at=None, n=7, test_cmd=None):
    """Run n times and require unanimous agreement. A FLAKY test can fake both ends.

    n=3 was NOT enough and a 50/50 flaky fixture slipped through on a lucky roll
    (3 agreeing runs has ~25% probability at p=0.5). n=7 puts that near 1.6%.
    This is inherently PROBABILISTIC -- it lowers the odds, it cannot eliminate
    them, and a test flaky at low frequency will still pass. Treat `stable` as
    "no instability observed", never as proof of determinism.

    Returns (rc, stable). Unstable results must never be read as evidence.
    """
    rcs = [run_test(repo, test_rel, at, test_cmd) for _ in range(n)]
    ok = [r for r in rcs if r is not None]
    if not ok:
        return None, False
    passed = [r == 0 for r in ok]
    return (0 if passed[0] else 1), (len(set(passed)) == 1 and len(ok) == n)


def mocks_target(test_src: str, symbol: str) -> bool:
    """True if the test patches/mocks the very symbol it claims to exercise.

    It then reaches the NAME while the real buggy body never runs -- the failure
    is about the mock. AST-level: any patch(...)/mock decorator or call whose
    argument text names the symbol.
    """
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = ""
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            # `patch.object(...)` presents as Attribute(attr="object"), NOT
            # "patch" -- the first version of this check missed it entirely and
            # a mock-over-target fixture slipped through. Match the whole dotted
            # path, not just the final attribute.
            dotted = fname
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                dotted = f"{f.value.id}.{f.attr}"
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute):
                dotted = f"{f.value.attr}.{f.attr}"
            if (fname in ("patch", "patch_object", "MagicMock", "Mock", "monkeypatch", "setattr")
                    or dotted in ("patch.object", "mock.patch", "mocker.patch",
                                  "mocker.patch_object", "monkeypatch.setattr")):
                for a in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and symbol in a.value:
                        return True
                    if isinstance(a, ast.Name) and a.id == symbol:
                        return True
                    if isinstance(a, ast.Attribute) and a.attr == symbol:
                        return True
    return False


def assertion_is_data_dependent(test_src: str, symbol: str) -> bool:
    """True if some assertion depends on a value derived from the target.

    Strengthens criterion 4 past "a call exists somewhere": `foo(); assert False`
    calls something and still proves nothing. We require that an asserted
    expression references a name bound from the target's return, or calls the
    target inline, or asserts on a raised exception from it.
    """
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return False
    # Resolve ALIASES first. `matches = mod._torrent_name_matches_file` then
    # `assert matches(...)` is the ordinary way to write this test, and matching
    # only on the symbol's own name silently REJECTED it -- measured on the first
    # real diagnosis run, where every other criterion passed and a correct
    # diagnosis was failed for how it spelled the call. A checker whose false
    # negatives look exactly like model incapacity is worse than no checker.
    # Fixpoint over one-hop rebindings (aliases of aliases included).
    aliases = {symbol}
    for _ in range(5):
        grew = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            v = node.value
            src_name = (v.attr if isinstance(v, ast.Attribute)
                        else v.id if isinstance(v, ast.Name) else None)
            if src_name in aliases:
                for t in node.targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name) and sub.id not in aliases:
                            aliases.add(sub.id)
                            grew = True
        if not grew:
            break
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            calls = [c for c in ast.walk(node.value) if isinstance(c, ast.Call)]
            for c in calls:
                f = c.func
                nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if nm in aliases or (isinstance(f, ast.Call)):
                    for t in node.targets:
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Name):
                                bound.add(sub.id)
        if isinstance(node, (ast.With, ast.Try)):
            pass
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    for a in asserts:
        for sub in ast.walk(a.test):
            if isinstance(sub, ast.Name) and (sub.id in bound or sub.id in aliases):
                return True
            if isinstance(sub, ast.Attribute) and sub.attr in aliases:
                return True
            if isinstance(sub, ast.Call):
                f = sub.func
                nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if nm in aliases:
                    return True
    return False


def anchor_direction(repo, anchor):
    """Is the reference BEFORE the bug (a true anchor) or AFTER the fix?

    Criterion 2's load-bearing property is that the reference is BUG-ABSENT, and
    a post-fix commit satisfies that just as well as a pre-bug one -- it still
    kills assert-False, which fails at both ends. But the two are not
    interchangeable to the tightness check, which measures `anchor..HEAD`: with a
    post-fix reference that range is BACKWARDS and reports 0 commits, which reads
    as "maximally tight" when it actually means "direction reversed". Distinguish
    them explicitly rather than letting a post-fix ref silently masquerade as the
    tightest possible anchor.

    Returns "pre-bug", "post-fix", or "unrelated" (neither is an ancestor of the
    other -- divergent branches, where the range is meaningless in either
    direction)."""
    def is_ancestor(a, b):
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                              capture_output=True).returncode == 0
    try:
        if is_ancestor(anchor, "HEAD"):
            return "pre-bug"
        if is_ancestor("HEAD", anchor):
            return "post-fix"
    except Exception:
        return "unrelated"
    return "unrelated"


def anchor_is_tight(repo, anchor, target_file, direction="pre-bug"):
    """How much unrelated change sits between anchor and HEAD.

    PASSES_AT_ANCHOR is only as strong as the anchor being close to the
    bug-introducing commit. Over a long range, unrelated changes also flip
    head-vs-anchor, so a test asserting an UNRELATED observable passes at anchor
    and fails at head for the WRONG REASON while reaching the named symbol only
    by coincidence (criterion 3 proves reach, never causation).

    Returns (commits_in_range, files_touched, tight) -- tight means the range
    touches only the target file.
    """
    try:
        # Walk from whichever end is the ancestor, so a post-fix reference is
        # measured over the SAME real distance rather than an empty backwards range.
        rng = f"HEAD..{anchor}" if direction == "post-fix" else f"{anchor}..HEAD"
        r = subprocess.run(["git", "-C", str(repo), "log", "--format=%H", rng],
                           capture_output=True, text=True, check=True)
        n_commits = len([x for x in r.stdout.split() if x])
        f = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", f"{anchor}..HEAD"],
                           capture_output=True, text=True, check=True)
        files = sorted({x for x in f.stdout.splitlines() if x.strip()})
    except subprocess.CalledProcessError:
        return None, None, False
    # WITHOUT a target file there is nothing to compare the range against, so
    # tightness is UNKNOWN -- return None, never True. The old expression made
    # `others` empty whenever target_file was None, so an unsupplied --target-file
    # reported tight=True over any range at all, and since ACCEPTED_LOOSE_ANCHOR
    # only fires on `is False`, a loose anchor was silently upgraded to a full
    # ACCEPTED. A check that is switched off must say so, not pass.
    if not target_file:
        return n_commits, files, None
    others = [x for x in files if Path(x).name != Path(target_file).name]
    return n_commits, files, (not others)


def drives_symbol(test_src: str, symbol: str) -> bool:
    """AST, not grep: the symbol must be imported or referenced as code.

    A comment or a docstring naming the symbol does not count, which is exactly
    the difference a grep would miss.
    """
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            return True
        if isinstance(node, ast.Attribute) and node.attr == symbol:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == symbol or (a.asname or "") == symbol:
                    return True
            if isinstance(node, ast.ImportFrom) and (node.module or "") == symbol:
                return True
    return False


def is_unconditional_failure(test_src: str) -> bool:
    """True if the test fails without ever calling into the code under test.

    Catches `assert False`, a bare `raise`, and `pytest.fail(...)` in a body that
    contains no Call into anything else -- the shapes that satisfy "the repro
    fires" while proving nothing.
    """
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return True
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        hard = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assert):
                c = n.test
                if isinstance(c, ast.Constant) and not c.value:
                    hard.append(n)
            elif isinstance(n, ast.Raise):
                hard.append(n)
        non_fail_calls = [c for c in calls
                          if not (isinstance(c.func, ast.Attribute) and c.func.attr == "fail")]
        if hard and not non_fail_calls:
            return True
    return False


CRITERIA = ("not_unconditional", "data_dependent", "drives_symbol",
            "no_mock_on_target", "fails_at_head", "passes_at_anchor", "stable")


def _same_file(repo: Path, probe_file: str, rel: str) -> bool:
    """Does the probe's absolute co_filename name `rel` (repo-relative)?"""
    import os as _os
    try:
        a = _os.path.realpath(probe_file)
        b = _os.path.realpath(str(repo / rel))
        if a == b:
            return True
    except Exception:  # noqa: BLE001
        pass
    # Copied/relocated trees: fall back to a path-component suffix match.
    parts = Path(rel).parts
    return Path(probe_file).parts[-len(parts):] == parts


def _def_spans(src: str, symbol: str):
    """(start, end) line spans of every def/class named `symbol` (any nesting).

    Decorators count as part of the definition: a route/decorator change is a
    change to the symbol."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.name == symbol:
            lo = min([n.lineno] + [d.lineno for d in n.decorator_list])
            spans.append((lo, n.end_lineno or n.lineno))

    # A MODULE-LEVEL CONSTANT HAS A DEFINITION TOO: its assignment. Without this,
    # `TIMEOUT = 30` -> `TIMEOUT = 0` gave TIMEOUT no span at all, so
    # symbol_changed_in_range said False -- "not what the range introduced" --
    # about the one symbol that WAS. Combined with correctly rejecting the
    # unchanged reader as a manifestation site, that made a constant-change
    # regression undiagnosable by ANY answer.
    #
    # Module level and class body only. NOT locals inside a function: a local of
    # a matching name is not the symbol a diagnosis would name, and counting it
    # would let any incidental variable satisfy the gate.
    def _assign_targets(node):
        if isinstance(node, ast.Assign):
            return node.targets
        if isinstance(node, ast.AnnAssign):
            return [node.target]
        return []

    _bodies = [tree.body] + [c.body for c in ast.walk(tree)
                             if isinstance(c, ast.ClassDef)]
    for body in _bodies:
        for n in body:
            for t in _assign_targets(n):
                names = ([el for el in t.elts] if isinstance(t, (ast.Tuple, ast.List))
                         else [t])
                for nm in names:
                    if isinstance(nm, ast.Name) and nm.id == symbol:
                        spans.append((n.lineno, n.end_lineno or n.lineno))
    return spans


def symbol_changed_in_range(repo: Path, anchor: str, rel: str, symbol: str):
    """Did anchor..HEAD touch the DEFINITION of `symbol` in `rel`?

    A regression diagnosis names the cause of a change in behaviour between the
    anchor and HEAD. If the named symbol's body is byte-identical at both ends,
    the symbol cannot be what the range introduced -- it is at best a symptom
    site or a bystander. This is the differential signal the execution probe
    lacked: it can see that a frame RAN, not that the frame is what CHANGED.

    True / False, or None when it cannot be decided (non-Python file, parse
    failure, git error). Missing at the anchor but present at HEAD counts as
    changed (a new function). Both diff sides are intersected, so a post-fix
    reference works the same way."""
    def show(ref):
        r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    head_src, anc_src = show("HEAD"), show(anchor)
    if head_src is None:
        return None
    head_spans = _def_spans(head_src, symbol)
    if head_spans is None:
        return None
    if not head_spans:
        return False            # not defined in this file at HEAD at all
    if anc_src is None:
        return True             # file (and so the symbol) is new in the range
    anc_spans = _def_spans(anc_src, symbol)
    if anc_spans is None:
        return None
    if not anc_spans:
        return True             # symbol is new in the range
    try:
        d = subprocess.run(["git", "-C", str(repo), "diff", "-U0", anchor, "HEAD", "--", rel],
                           capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None
    def hit(lo, n, spans):
        # n == 0 is a pure insertion/deletion "after line lo": no lines of this
        # side are inside the hunk, so it cannot intersect a span on this side.
        if n == 0:
            return False
        hi = lo + n - 1
        return any(not (hi < a or lo > b) for a, b in spans)
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", d, re.M):
        olo, on, nlo, nn = (int(m.group(1)), int(m.group(2) or 1),
                            int(m.group(3)), int(m.group(4) or 1))
        if hit(olo, on, anc_spans) or hit(nlo, nn, head_spans):
            return True
    return False


def symbol_executed(repo: Path, test_rel: str, symbol: str, test_cmd=None):
    """Per-FILE execution record for every frame named `symbol`. Never raises.

    The DECIDABLE form of "does this test drive the target". Runs the test under
    sys.settrace in a child process and reports, for each source file in which a
    frame named `symbol` was entered, whether it ran and whether it saw an
    exception. Used only as a fallback when the AST name checks say no -- so the
    cheap path stays cheap.

    Returns {abs_filename: {"ran": True, "raised": bool}}. The caller binds the
    bare name to a SITE: matching on co_name alone credited any same-named
    helper anywhere in the call graph (measured 2026-09-02: `_connect` in
    app/database.py was accepted as the cause of an AttributeError raised in
    app/api.py:containers, because it ran during the request).

    Fail-closed: any problem returns {}, which leaves the AST verdict standing
    rather than inventing a pass.
    """
    # `os` is NOT imported at module scope in this file. The first version of
    # this helper used os.path.basename and os.unlink, raised NameError, and the
    # broad `except Exception` below turned that coding error into (False, False)
    # -- a PROBE THAT NEVER RAN reported as "the symbol did not execute". Exactly
    # the failure this checker exists to catch, committed by the checker.
    import json as _json, os as _os, subprocess as _sp, tempfile as _tf
    probe = (
        "import sys, runpy, json, os\n"
        f"TARGET = {symbol!r}\n"
        "seen = {}\n"
        "def _tr(frame, event, arg):\n"
        # CO_OPTIMIZED (0x1) is set on functions and methods, NOT on class bodies
        # or module scope. Without this test a class body running at IMPORT
        # counted as the test driving the class -- vacuous, since importing is
        # unconditional. A class is matched instead by its METHODS' qualnames
        # below, so it must be genuinely exercised.
        "    _c = frame.f_code\n"
        "    _q = getattr(_c, 'co_qualname', _c.co_name)\n"
        "    _opt = bool(_c.co_flags & 0x1)\n"
        "    if _opt and (_c.co_name == TARGET or _q.startswith(TARGET + '.')):\n"
        "        f = os.path.realpath(_c.co_filename)\n"
        "        rec = seen.setdefault(f, {'ran': False, 'raised': False})\n"
        "        if event == 'call':\n"
        "            rec['ran'] = True\n"
        "            return _tr\n"
        "        if event == 'exception':\n"
        "            rec['raised'] = True\n"
        "    return None\n"
        "sys.settrace(_tr)\n"
        "try:\n"
        f"    runpy.run_path({test_rel!r}, run_name='__main__')\n"
        "except BaseException:\n"
        "    pass\n"
        "finally:\n"
        "    sys.settrace(None)\n"
        "    sys.stderr.write('__PROBE__' + json.dumps(seen))\n"
    )
    try:
        with _tf.NamedTemporaryFile('w', suffix='.py', delete=False,
                                    dir=str(repo)) as fh:
            fh.write(probe)
            probe_path = fh.name
        cmd = build_test_cmd(_os.path.basename(probe_path), test_cmd)
        r = _sp.run(cmd, cwd=str(repo), capture_output=True, text=True,
                    timeout=TEST_TIMEOUT)
        _os.unlink(probe_path)
        tag = (r.stderr or "").rsplit("__PROBE__", 1)
        if len(tag) == 2:
            d = _json.loads(tag[1].strip() or "{}")
            return {k: {"ran": bool(v.get("ran")), "raised": bool(v.get("raised"))}
                    for k, v in d.items() if isinstance(v, dict)}
    except Exception as e:                       # noqa: BLE001 -- reported, not hidden
        sys.stderr.write(f"[diagnosis-check] symbol_executed probe failed: "
                         f"{type(e).__name__}: {e}\n")
    return {}



def symbol_is_callable(repo: Path, rel: str | None, symbol: str) -> bool:
    """Is `symbol` a def/class at HEAD (vs a module-level assignment)?

    Fail-open: True when undecidable, so an unparseable or missing file keeps the
    ordinary REJECTED path rather than being routed to a human on a guess.
    """
    if not rel:
        return True
    try:
        src = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"],
                             capture_output=True, text=True)
        if src.returncode != 0:
            return True
        tree = ast.parse(src.stdout)
    except Exception:
        return True
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.name == symbol:
            return True
    return False


def check(repo, test_rel, symbol, anchor, target_file=None, test_cmd=None):
    repo = Path(repo).resolve()
    src = (repo / test_rel).read_text()
    out = {"test": test_rel, "symbol": symbol, "anchor": anchor}

    # Criteria 1, 3 and 4 are implemented with Python's ast module. On a test
    # written in another language they cannot run -- and an un-run check must
    # report UNKNOWN, never pass (the same trap as anchor_tight, hit twice).
    # A non-Python test therefore gets criteria 2 + stability only, and can
    # never reach a bare ACCEPTED.
    try:
        ast.parse(src)
        out["ast_supported"] = True
    except SyntaxError:
        out["ast_supported"] = False

    if out["ast_supported"]:
        out["not_unconditional"] = not is_unconditional_failure(src)
        _ast_dd = assertion_is_data_dependent(src, symbol)
        _ast_ds = drives_symbol(src, symbol)
        out["data_dependent"] = _ast_dd
        out["drives_symbol"] = _ast_ds
        out["drives_symbol_via"] = "ast" if _ast_ds else None
        out["data_dependent_via"] = "ast" if _ast_dd else None
        # FALLBACK: the symbol may be reached through framework dispatch (a Flask
        # route, a callback, a CLI entry point), where the test never names it.
        # Only pay for the trace when the cheap check already said no.
        if not (_ast_ds and _ast_dd):
            sites = symbol_executed(repo, test_rel, symbol, test_cmd)
            # BIND THE NAME TO A SITE. A bare co_name match credits any
            # same-named frame in the whole call graph. The diagnosis names a
            # cause in a FILE: with --target-file, only frames in that file
            # count; without it, the frame's own file stands in (and the
            # verdict is capped at ACCEPTED_UNVERIFIED_ANCHOR anyway).
            if target_file:
                at_site = {f: v for f, v in sites.items()
                           if _same_file(repo, f, target_file)}
                out["symbol_in_target_file"] = bool(at_site)
            else:
                at_site = dict(sites)
                out["symbol_in_target_file"] = None
            out["symbol_executed"] = any(v["ran"] for v in at_site.values())
            out["symbol_raised"] = any(v["raised"] for v in at_site.values())
            out["symbol_executed_elsewhere"] = sorted(
                str(Path(f).relative_to(repo)) if str(f).startswith(str(repo)) else f
                for f in sites if f not in at_site)
            # FAULT-PATH EVIDENCE. Running at the site is reach, not causation:
            # a helper on the request path runs every time and drives nothing.
            # Two independent ways to be on the fault path:
            #   raised            -- the frame itself saw the exception, or
            #   changed_in_range  -- anchor..HEAD touched the symbol's
            #                        definition, so it IS what the regression
            #                        introduced. This is what keeps a wrong-
            #                        VALUE cause (returns garbage, never raises,
            #                        consumed downstream) accepted: it never
            #                        raises, but it did change. Requiring a
            #                        raise alone rejected exactly that case.
            changed = None
            if anchor and out["symbol_executed"]:
                rels = [target_file] if target_file else [
                    str(Path(f).relative_to(repo)) for f in at_site
                    if str(f).startswith(str(repo))]
                vals = [symbol_changed_in_range(repo, anchor, r, symbol) for r in rels]
                changed = (True if any(v is True for v in vals)
                           else False if vals and all(v is False for v in vals)
                           else None)
            out["symbol_changed_in_range"] = changed
            on_fault_path = out["symbol_raised"] or changed is True
            if not _ast_ds and out["symbol_executed"] and (anchor is None or on_fault_path):
                out["drives_symbol"] = True
                out["drives_symbol_via"] = "execution"
                out["drives_symbol_evidence"] = (
                    "raised in target frame" if out["symbol_raised"]
                    else "definition changed in anchor..HEAD" if changed
                    else "executed (no anchor: fault path unassessed)")
            elif not _ast_ds:
                out["drives_symbol_evidence"] = (
                    "symbol never ran" if not sites
                    else f"ran only outside the target file: {out['symbol_executed_elsewhere']}"
                    if not out["symbol_executed"]
                    else "ran at site but did not raise and anchor..HEAD did not change its definition")
            # data_dependent is decided AFTER the anchor run -- see below.
        out["no_mock_on_target"] = not mocks_target(src, symbol)
    else:
        for k in ("not_unconditional", "data_dependent", "drives_symbol",
                  "no_mock_on_target"):
            out[k] = None

    rc_head, st_head = run_test_stable(repo, test_rel, test_cmd=test_cmd)
    out["fails_at_head"] = (rc_head is not None and rc_head != 0)

    if anchor:
        rc_anchor, st_anchor = run_test_stable(repo, test_rel, anchor, test_cmd=test_cmd)
        out["passes_at_anchor"] = (rc_anchor == 0)
        out["stable"] = bool(st_head and st_anchor)
        out["anchor_direction"] = anchor_direction(repo, anchor)
        n, files, tight = anchor_is_tight(repo, anchor, target_file,
                                          out["anchor_direction"])
        out["anchor_commits_in_range"] = n
        out["anchor_tight"] = tight
        out["anchor_range_files"] = files
        # FAULT PATH, REGARDLESS OF HOW THE NAME WAS RESOLVED (hole (a)).
        # 85c48fe required this for execution-established driving; an
        # AST-established one skipped it entirely, so a test that merely IMPORTS
        # the wrong symbol satisfied criterion 3 by name. Measured: a test naming
        # `helper`, a bystander unchanged across the range, graded ACCEPTED.
        # Computed unconditionally -- it used to run only inside the execution
        # branch, so it read None on every AST verdict and could not gate anything.
        if target_file and out.get("symbol_changed_in_range") is None:
            out["symbol_changed_in_range"] = symbol_changed_in_range(
                repo, anchor, target_file, symbol)
        _chg = out.get("symbol_changed_in_range")
        if out.get("drives_symbol") and _chg is False:
            out["drives_symbol"] = False
            out["drives_symbol_via"] = None
            out["drives_symbol_rejected_because"] = (
                f"{symbol}'s definition is byte-identical at the anchor and HEAD, "
                f"so it is not what this range introduced -- a bystander or a "
                f"manifestation site, not the cause")
        # None is NOT a rejection: it means undecidable (non-Python target, parse
        # failure, git error). Rejecting on it would be an un-run check reporting
        # a failure.

        # MEASURED DATA-DEPENDENCE. The AST check asks whether an assertion
        # references a value bound from the target -- which cannot see a target
        # reached through framework dispatch (a Flask route, a callback), where
        # the test never names it. But head-vs-anchor already MEASURES the thing
        # the AST check is proxying for: if the assertion outcome flips when the
        # target's behaviour changes, the assertion depends on the target. That
        # is stronger evidence than parsing names, not weaker.
        #
        # It cannot launder a vacuous test. `target(); assert False` executes the
        # target and still FAILS at the anchor, so passes_at_anchor is False and
        # this never fires -- verified as an arm of diag-exec-canary. Criterion 2
        # doing the load-bearing work is exactly what DESIGN.md says it is for.
        if (not out.get("data_dependent")) and out.get("symbol_executed") \
                and out.get("passes_at_anchor") and out.get("fails_at_head"):
            out["data_dependent"] = True
            out["data_dependent_via"] = "measured (head-vs-anchor flip)"
    else:
        out["passes_at_anchor"] = None
        out["stable"] = st_head
        out["anchor_tight"] = None

    if not out["ast_supported"]:
        # Criteria 1/3/4 never ran. The anchor result is still real and still
        # kills assert-False, but this is NOT the same evidence as a full pass.
        ok2 = bool(out["fails_at_head"] and out["passes_at_anchor"] and out["stable"])
        out["verdict"] = ("REJECTED" if not ok2 else "ACCEPTED_UNCHECKED_LANGUAGE")
        out["why"] = ("test is not Python, so the AST criteria (unconditional-failure, "
                      "data-dependence, symbol reach, mock-on-target) COULD NOT RUN. "
                      + ("Anchor discrimination holds, but three criteria are unassessed "
                         "-- review by hand, never auto-accept."
                         if ok2 else "Anchor discrimination also failed."))
    elif anchor is None:
        out["verdict"] = "ANCHOR_UNAVAILABLE"
        out["why"] = ("no pre-bug commit supplied: criterion 2 cannot run, so this "
                      "cannot be auto-accepted -- route to human review")
    elif all(out[k] for k in CRITERIA):
        if out["anchor_tight"] is False:
            # Reaching the symbol proves REACH, never CAUSATION. Over a loose
            # range an unrelated change can flip head-vs-anchor, so this is
            # surfaced for review rather than auto-accepted.
            out["verdict"] = "ACCEPTED_LOOSE_ANCHOR"
            out["why"] = (f"all criteria pass, but {out['anchor_commits_in_range']} commit(s) "
                          f"touching {len(out['anchor_range_files'] or [])} file(s) sit between "
                          f"anchor and HEAD -- the anchor is not the bug's immediate parent, so "
                          f"head-vs-anchor could flip for an unrelated reason. Confirm by hand.")
        elif out["anchor_tight"] is None:
            # Unknown is not the same as tight. Reported as its own verdict so a
            # run without --target-file can never be mistaken for one where the
            # range was actually checked and found clean.
            out["verdict"] = "ACCEPTED_UNVERIFIED_ANCHOR"
            out["why"] = (f"all criteria pass, but no --target-file was given, so the "
                          f"{out['anchor_commits_in_range']} commit(s) touching "
                          f"{len(out['anchor_range_files'] or [])} file(s) between anchor and "
                          f"HEAD were never checked for unrelated change. Re-run with "
                          f"--target-file to get a real tightness reading.")
        else:
            out["verdict"] = "ACCEPTED"
            out["why"] = "fails on the bug, passes before it, and drives the named cause"
    else:
        failed = [k for k in CRITERIA if not out[k]]
        # A NON-CALLABLE CAUSE CANNOT SATISFY criterion 3, and saying REJECTED
        # about it claims the diagnosis was wrong when the grader simply cannot
        # evaluate it. Only when drives_symbol is the ONLY thing missing, the
        # symbol IS what the range changed, and the head/anchor evidence holds.
        # BOTH function-shaped criteria fail for the same structural reason: a
        # non-callable symbol has no frame to trace and will not be named by a
        # symptom test, so neither drives_symbol NOR data_dependent can be
        # established. Allowing exactly that pair, and nothing else.
        if (set(failed) <= {"drives_symbol", "data_dependent"} and failed
                and out.get("symbol_changed_in_range") is True
                and out.get("fails_at_head") and out.get("passes_at_anchor")
                and out.get("not_unconditional") and out.get("no_mock_on_target")
                and not symbol_is_callable(repo, target_file, symbol)):
            out["verdict"] = "CAUSE_NOT_CALLABLE"
            out["why"] = (f"{symbol} is what changed in anchor..HEAD and the test "
                          f"fails at HEAD / passes at the anchor, but {symbol} is a "
                          f"module-level assignment rather than a def -- there is no "
                          f"frame to trace and a symptom test will not name it, so "
                          f"'drives the named cause' cannot be evaluated. NOT a "
                          f"rejection of the diagnosis: route a human.")
        else:
            out["verdict"] = "REJECTED"
            out["why"] = "failed: " + ", ".join(failed)
    return out


def self_check(repo, anchor):
    """Prove the checker REJECTS before anyone trusts it to accept.

    A checker that has only ever accepted is unproven -- the same reason the
    bake-off scorer needed a canary. Each fixture below is caught by a DIFFERENT
    criterion, which is what shows none of them is redundant:

      t_wrong_symbol passes criteria 1 AND 2 (fails at head, passes at anchor),
      so the "obvious" two-criterion checker would have ACCEPTED it.
      t_both_ends passes 1, 3 and 4 and is caught only by the anchor.

    Needs the fixture from build_fixture.sh; pass --repo and --anchor.
    """
    if not (repo and anchor):
        print("--self-check needs --repo <fixture> and --anchor <pre-bug sha>")
        return 2
    expect = {"t_good.py": "ACCEPTED", "t_assert_false.py": "REJECTED",
              "t_both_ends.py": "REJECTED",
            # WAS "REJECTED", changed 2026-09-02 when the execution fallback
            # landed. t_wrong_symbol reaches the real discount() via
            # getattr(lib, "disc"+"ount") and asserts on its return. It genuinely
            # drives the named cause and its assertion genuinely depends on that
            # cause -- the old expectation encoded an AST LIMITATION ("cannot
            # prove", per build_fixture.sh's own comment), not a defect in the
            # test. Now that driving is established by execution rather than by
            # name-resolution, ACCEPTED is the correct answer and keeping
            # REJECTED would pin a false negative in place.
            # Obscured naming is not a gaming vector: hiding the name made this
            # test HARDER to accept, never easier.
            "t_wrong_symbol.py": "ACCEPTED"}
    problems = []
    for test, want in expect.items():
        # --target-file is REQUIRED here, not optional. Since tightness reports
        # unknown rather than true when it cannot be assessed, omitting it turns
        # the good fixture into ACCEPTED_UNVERIFIED_ANCHOR and the canary fails on
        # correct behaviour -- and a canary that cries wolf is one people learn to
        # ignore, which costs more than having no canary at all.
        got = check(repo, test, "discount", anchor, target_file="lib.py")
        mark = "ok " if got["verdict"] == want else "BAD"
        print(f"  {mark} {test:20s} want={want:9s} got={got['verdict']:9s} {got['why']}")
        if got["verdict"] != want:
            problems.append(f"{test}: wanted {want}, got {got['verdict']}")
    # and the no-anchor path must refuse to auto-accept even a GOOD test
    na = check(repo, "t_good.py", "discount", None, target_file="lib.py")
    ok = na["verdict"] == "ANCHOR_UNAVAILABLE"
    print(f"  {'ok ' if ok else 'BAD'} no-anchor path      want=ANCHOR_UNAVAILABLE got={na['verdict']}")
    if not ok:
        problems.append("no-anchor path auto-accepted; it must never do that")
    for p in problems:
        print("FAIL:", p)
    print("--- " + ("CHECKER DISCRIMINATES" if not problems
                    else f"{len(problems)} problem(s)") + " ---")
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--test")
    ap.add_argument("--symbol")
    ap.add_argument("--anchor")
    ap.add_argument("--target-file", help="file the cause lives in; enables the anchor-tightness check")
    ap.add_argument("--test-cmd", default=None,
                    help=("how to RUN the proposed test, e.g. 'node {test}' or "
                          "'npx tsx {test}'. {test} is the test path, {python} this "
                          "interpreter. Default runs it with Python. A non-Python test "
                          "can never reach a bare ACCEPTED -- the AST criteria cannot "
                          "run on it, and they report unknown rather than passing."))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()

    # REFUSE an absolute or escaping --test rather than repairing it. Resolving
    # it against --repo would be a guess, and the failure mode it replaces is a
    # SILENT false REJECT (see run_test_at). Loud beats clever here.
    if a.test and not a.self_check:
        _t = Path(a.test)
        if _t.is_absolute():
            try:
                _t = _t.relative_to(Path(a.repo).resolve())
            except Exception:
                ap.error(f"--test must be REPO-RELATIVE (e.g. t_repro.py), not an "
                         f"absolute path: {a.test}. An absolute path bypasses the "
                         f"anchor checkout and silently reports passes_at_anchor=false.")
            a.test = str(_t)
        if ".." in Path(a.test).parts:
            ap.error(f"--test must not escape the repo: {a.test}")

    if a.self_check:
        return self_check(a.repo, a.anchor)

    if not (a.repo and a.test and a.symbol):
        print("need --repo --test --symbol (or --self-check)")
        return 2
    r = check(a.repo, a.test, a.symbol, a.anchor, a.target_file, a.test_cmd)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        for k in CRITERIA:
            print(f"  {k:20s} {r[k]}")
        if r.get("anchor_tight") is not None:
            print(f"  {'anchor_tight':20s} {r['anchor_tight']} "
                  f"({r.get('anchor_commits_in_range')} commit(s) in range)")
        print(f"  --- {r['verdict']}: {r['why']}")
    return 0 if r["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    sys.exit(main())
