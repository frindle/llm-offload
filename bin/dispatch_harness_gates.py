#!/usr/bin/env python3
"""dispatch_harness_gates.py -- PURE harness/spec correctness gates (Phase 2, 2026-10-08).

WHY
---
Of 158 diagnosed escalations, 46% were unsatisfiable specs, 37% harness defects, 15%
already-satisfied and 3% model. Almost every one was a defect our own tooling could see
BEFORE a GPU slot was spent. This module holds the decidable checks as small pure
functions so preflight, the slicer, auto and the gate share ONE definition:

  placeholder_findings / harness_complete   a harness still carrying scaffold
        placeholders (TODO -- ..., assert.fail('SCAFFOLD_INCOMPLETE'), refimpl
        OLD = "TODO ...") or lacking a real must_contain literal / a refimpl anchor
        that exists in the target must never reach a coding dispatch
        (rt-egift-link-s1-s4-api-route).
  is_stub_text                             `export {};`, an empty module or a comment-only
        file is a BASELINE, not a modification (5b6b052149a7).
  refimpl_declared_test_paths              product test files the refimpl writes (they are
        sealed by queue "seal round baseline" commits and must not read as dirt).
  literal_findings                         every spec literal must be present in the target
        or derivable (named in the intent / introduced by the refimpl) (8a4941aa8521,
        a580c8793aa7).
  contract_status                          fail-before/pass-after read off preflight's checks.
  spec_defects / format_spec_defects       PRE-DISPATCH CONTRADICTION GATE (2026-10-09): a
        spec that contradicts the BASE commit (creation target already exists, every
        literal already present and the verify already green, refimpl anchor matching
        nothing) is a SPEC_DEFECT -- no model can satisfy it, so no GPU job is enqueued.
  classify_baseline_red                    assertion-failure vs crash/import/syntax at the
        baseline: only an assertion (or, for a creation/new-symbol task, the spec-named
        missing module/export) counts as 'fails before'.
  failure_signature                        stable fingerprint of a failure (first assertion
        line + failing case names) for the abandon-after-3-identical-failures rule.
  RefineBudget helpers                     bounded harness-refine counting (relevance
        survivors with VERIFY_OK go back to test authoring, not to a park).
  log_auto_decision                        one clearly logged record per automatic decision.

Run `python3 dispatch_harness_gates.py check <worktree>` for a standalone harness-complete
report (exit 1 on findings).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. harness-complete
# ---------------------------------------------------------------------------
# A placeholder in the SPEC: the scaffold's `TODO -- ...` fields and its marker.
_TASK_PLACEHOLDER_RE = re.compile(r"\bTODO\s*(?:--|:|—)|SCAFFOLD_INCOMPLETE|<PLACEHOLDER>|\bTBD\s*--")
# A placeholder in CODE (refimpl): the scaffold's OLD/NEW strings.
_REFIMPL_PLACEHOLDER_RE = re.compile(
    r"TODO\s*(?:--|:|—)|SCAFFOLD_INCOMPLETE|the exact text to replace|<PLACEHOLDER>")
# A placeholder in a FIXTURE: the guard case the generator emits. The marker word also
# appears in the fixture's header COMMENT and in verify.sh's own error strings, so only
# NON-comment fixture lines count.
_FIXTURE_PLACEHOLDER_RE = re.compile(
    r"SCAFFOLD_INCOMPLETE|SCAFFOLD:\s*cases not yet authored|TODO\s*--\s*(?:add|author|write)")
_FIXTURE_NAMES = ("verify.test.ts", "verify.test.mts", "test_fixture.py", "verify_impl.mts",
                  "verify_impl.mjs", "verify_impl.js", "Tests/verifyTests.swift",
                  "DispatchTests/VerifyImpl.cs")
_COMMENT_LINE_RE = re.compile(r"^\s*(?://|#|\*|/\*)")
_ANCHOR_NAME_RE = re.compile(r"^(?:OLD\w*|\w*_OLD|old_\w+|ANCHOR\w*|anchor\w*|\w*_anchor)$")


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _manifest(wt: Path) -> dict:
    try:
        d = json.loads((wt / ".dispatch-harness.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def must_contain_literals(task_text: str):
    """[(file_or_None, literal)] of REAL literals from the `## Must contain` block.

    Same grammar as preflight.must_contain_pairs (only `- ` items; `in <path>:` pins), but
    placeholder literals (containing TODO) are dropped: they are not literals."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "", re.S | re.I)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        if not re.match(r"\s*-\s", line):
            continue
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        f = fm.group(1) if fm else None
        for lm in re.finditer(r"`([^`\n]{1,200})`", line):
            lit = lm.group(1)
            if _TASK_PLACEHOLDER_RE.search(lit) or not lit.strip():
                continue
            out.append((f, lit))
    return out


def placeholder_findings(task_text: str = "", refimpl_text: str = "",
                         fixture_texts: dict | None = None):
    """PURE. [str] -- one finding per placeholder family found. Empty = no placeholders."""
    out = []
    t = [ln.strip() for ln in (task_text or "").splitlines() if _TASK_PLACEHOLDER_RE.search(ln)]
    if t:
        out.append(f"TASK.md still carries {len(t)} scaffold placeholder line(s), e.g. {t[0][:100]!r}")
    r = [ln.strip() for ln in (refimpl_text or "").splitlines()
         if not _COMMENT_LINE_RE.match(ln) and _REFIMPL_PLACEHOLDER_RE.search(ln)]
    if r:
        out.append(f"refimpl.py still carries scaffold placeholder(s), e.g. {r[0][:100]!r}")
    for name, txt in (fixture_texts or {}).items():
        f = _fixture_guard_lines(txt)
        if f:
            out.append(f"{name} still carries the scaffold guard case, e.g. {f[0][:100]!r} "
                       f"(no adversarial cases authored)")
    return out


_CASES_FLAG_OFF_RE = re.compile(r"\bCASES_AUTHORED\b[^\n=]*=\s*(?:false|False|0|\"0\")\b")
_COND_RE = re.compile(r"^\s*(?:\}?\s*else\s+)?(?:if|elif|while|unless)\b|^\s*\[\[?\s")


def _fixture_guard_lines(txt: str):
    """Lines of a fixture that are a LIVE scaffold guard. The generator leaves a conditional
    guard (`if (!CASES_AUTHORED) {{ ...SCAFFOLD_INCOMPLETE... }}`, `if checks < 3`) in a
    finished fixture on purpose; that is not a placeholder. A marker is live when (a) the
    CASES_AUTHORED flag is still off, or (b) the marker line is not under any condition."""
    lines = (txt or "").splitlines()
    if any(not _COMMENT_LINE_RE.match(ln) and _CASES_FLAG_OFF_RE.search(ln) for ln in lines):
        return [ln.strip() for ln in lines
                if not _COMMENT_LINE_RE.match(ln) and (_CASES_FLAG_OFF_RE.search(ln)
                                                       or _FIXTURE_PLACEHOLDER_RE.search(ln))] or ["CASES_AUTHORED off"]
    out = []
    for i, ln in enumerate(lines):
        if _COMMENT_LINE_RE.match(ln) or not _FIXTURE_PLACEHOLDER_RE.search(ln):
            continue
        prev = [x for x in lines[max(0, i - 4):i] if x.strip()][-3:]
        if not any(_COND_RE.match(x) or "if " in x for x in prev):
            out.append(ln.strip())
    return out


def refimpl_anchors(refimpl_text: str):
    """[str] literal anchor strings the refimpl searches for (module-level `OLD = \"..\"`,
    `old_x = ..`, `ANCHOR = ..`). Parsed with ast; unparseable -> []."""
    try:
        tree = ast.parse(refimpl_text or "")
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tg in node.targets:
                if isinstance(tg, ast.Name) and _ANCHOR_NAME_RE.match(tg.id) \
                        and node.value.value.strip():
                    out.append(node.value.value)
    return out


def _path_constants(refimpl_text: str):
    try:
        tree = ast.parse(refimpl_text or "")
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if 0 < len(v) < 200 and not re.search(r"\s", v) and re.search(r"\.\w{1,6}$", v):
                out.append(v)
    return out


def _tracked_files(wt: Path):
    try:
        p = subprocess.run(["git", "-C", str(wt), "ls-files"], capture_output=True, text=True,
                           timeout=30)
        return [x for x in p.stdout.splitlines() if x] if p.returncode == 0 else []
    except Exception:
        return []


def refimpl_declared_test_paths(wt: Path):
    """Tracked TEST files (name contains .test./_test./test_) that refimpl.py names. The
    refimpl CREATES these; a queue 'seal round baseline' commit then tracks the previous
    attempt's version, and the refimpl's rewrite reads as a dirty tracked file."""
    consts = _path_constants(_read(wt / "refimpl.py"))
    if not consts:
        return []
    tracked = _tracked_files(wt)
    out = []
    for f in tracked:
        if not re.search(r"(?:\.test\.|\.spec\.|_test\.|(?:^|/)test_)", f):
            continue
        if any(f == c or f.endswith("/" + c) or c.endswith("/" + f) or Path(f).name == c
               for c in consts):
            out.append(f)
    return out


def refimpl_anchor_findings(wt: Path, target: str | None):
    """[str] anchors the refimpl needs that exist in NONE of the files it can touch.

    Candidate files: the target plus every tracked file named (by path or basename) by a
    path-like string constant in refimpl.py. Skipped for creation tasks (the refimpl writes
    the whole target) and when the refimpl declares no literal anchor."""
    man = _manifest(wt)
    if man.get("creation_task"):
        return []
    ref = _read(wt / "refimpl.py")
    anchors = refimpl_anchors(ref)
    if not anchors:
        return []
    texts = []
    if target:
        texts.append(_read(wt / target))
    consts = _path_constants(ref)
    if consts:
        for f in _tracked_files(wt):
            if any(f == c or f.endswith("/" + c) or Path(f).name == Path(c).name for c in consts):
                texts.append(_read(wt / f))
    blob = "\n".join(texts)
    out = []
    for a in anchors:
        if a not in blob:
            out.append(f"refimpl anchor {a[:80]!r} occurs in none of the files the refimpl edits "
                       f"(target {target!r}) -- 'assert OLD in t' can never pass")
    return out


def harness_complete(wt, target: str | None = None):
    """Whole static harness-complete decision for a worktree.

    Returns (ok, [findings]). Findings (each a blocker):
      * placeholders in TASK.md / refimpl.py / the fixture;
      * no REAL `## Must contain` literal (needs >= 1 non-TODO literal);
      * a refimpl anchor that is absent from every file it edits."""
    wt = Path(wt)
    man = _manifest(wt)
    target = target or man.get("target")
    fixtures = {}
    names = list(_FIXTURE_NAMES)
    fx = man.get("fixture")
    if isinstance(fx, str) and fx and fx not in names:
        names.append(fx)
    for n in names:
        p = wt / n
        if p.is_file():
            fixtures[n] = _read(p)
    task = _read(wt / "TASK.md")
    findings = []
    if not task.strip():
        findings.append("TASK.md is missing or empty")
    findings += placeholder_findings(task, _read(wt / "refimpl.py"), fixtures)
    if task.strip() and not must_contain_literals(task):
        findings.append("TASK.md has no REAL `## Must contain` literal (>= 1 non-placeholder "
                        "backtick literal in a `- ` bullet is required)")
    if not (wt / "refimpl.py").is_file() and not (wt / "fix.patch").is_file():
        findings.append("no refimpl.py / fix.patch: the task has never been shown satisfiable")
    findings += refimpl_anchor_findings(wt, target)
    return (not findings), findings


# ---------------------------------------------------------------------------
# 3. stubs
# ---------------------------------------------------------------------------
_STUB_BODIES = {"", "export{};", "export{}", "export{};export{};", "module.exports={};",
                "module.exports={}", "exportdefault{};", "'usestrict';", '"usestrict";',
                "pass", "package{}"}


def _strip_comments(text: str) -> str:
    t = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    t = re.sub(r"(?m)^\s*(?://|#).*$", "", t)
    t = re.sub(r'\A\s*(?:"""|\'\'\').*?(?:"""|\'\'\')', "", t, flags=re.S)
    t = re.sub(r"(?m)\s//.*$", "", t) if "export" in t else t
    return t


def is_stub_text(text: str) -> bool:
    """True when `text` has no behaviour: empty, whitespace/comment only, or only an
    `export {};` / `module.exports = {};` / `pass` body. These are what a scaffold or a
    creation task leaves as a target, and they count as BASELINE."""
    body = re.sub(r"\s+", "", _strip_comments(text))
    if body in _STUB_BODIES:
        return True
    return len(text or "") <= 600 and "implement per TASK.md" in (text or "") and len(body) < 40


# ---------------------------------------------------------------------------
# 4. spec satisfiability
# ---------------------------------------------------------------------------
def count_occurrences(lit: str, text: str) -> int:
    """Exact count, plus the opposite-quote spelling of a fully-quoted literal."""
    n = (text or "").count(lit)
    if len(lit) >= 2 and lit[0] in "\"'" and lit[-1] == lit[0] and lit[0] not in lit[1:-1]:
        alt = "'" if lit[0] == '"' else '"'
        n += (text or "").count(alt + lit[1:-1] + alt)
    return n


def literal_findings(lits, target_text: str, spec_text: str = "", introduced_text: str = ""):
    """PURE. Spec literals nobody can satisfy: [(lit, why)].

    A literal is SATISFIABLE when it (a) occurs in the target already, (b) is introduced by
    the reference impl, or (c) is NAMED in the spec text (intent / title / verify_shape /
    interface) the author works from -- so the author can know to write it. A literal that is
    in none of the three has 0 occurrences anywhere the author can see: unsatisfiable by
    construction (typo, wrong file, stale identifier)."""
    out = []
    for lit in lits or []:
        lit = str(lit)
        if not lit.strip():
            continue
        if count_occurrences(lit, target_text) or count_occurrences(lit, introduced_text) \
                or count_occurrences(lit, spec_text):
            continue
        out.append((lit, "0 occurrences in the target, not introduced by the refimpl, and not "
                         "named anywhere in the slice's intent/title/verify_shape"))
    return out


# ---------------------------------------------------------------------------
# 2. fail-before / pass-after contract
# ---------------------------------------------------------------------------
def contract_status(checks):
    """(status, why) over preflight check dicts/objects ({id,status}).

    PASS only when the verify FAILED on the untouched baseline (`baseline-fails` PASS) and
    PASSED with the refimpl applied (`refimpl-passes` PASS). FAIL when either is FAIL.
    UNPROVEN when either was never measured (waived refimpl, blocked earlier)."""
    by = {}
    for c in checks or []:
        cid = (c.get("id") or c.get("check")) if isinstance(c, dict) else getattr(c, "id", None)
        st = c.get("status") if isinstance(c, dict) else getattr(c, "status", None)
        by[cid] = st
    b, r = by.get("baseline-fails"), by.get("refimpl-passes")
    t = by.get("refimpl-tests")      # "no x->F": EVERY fixture case must pass under the refimpl
    if b == "FAIL" or r == "FAIL" or t == "FAIL":
        bad = [n for n, s in (("baseline-fails", b), ("refimpl-passes", r),
                              ("refimpl-tests", t)) if s == "FAIL"]
        return "FAIL", "fail-before/pass-after broken: " + ", ".join(bad)
    if b == "PASS" and r == "PASS":
        return "PASS", "verify FAILS on the untouched baseline and PASSES with the refimpl"
    missing = [n for n, s in (("baseline-fails", b), ("refimpl-passes", r)) if s != "PASS"]
    return "UNPROVEN", "never measured: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# 5. bounded refine budget
# ---------------------------------------------------------------------------
HARNESS_REFINE_CAP = 2


def refine_budget(count, cap: int = HARNESS_REFINE_CAP):
    """('refine'|'respec', n_next): below the cap another harness refine; at the cap re-spec."""
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    return ("refine", n + 1) if n < cap else ("respec", n)


# ---------------------------------------------------------------------------
# 8. PRE-DISPATCH CONTRADICTION GATE (2026-10-09, smoke-order-mismatch)
# ---------------------------------------------------------------------------
# The replay task's INTENT said "create lib/payoutMismatch.ts" but the file ALREADY EXISTED at
# the base commit (in a later-evolved form). No refimpl can satisfy "create X" over a real X;
# the author model spiralled (~120K-char turns) and ~3 authoring jobs burned GPU. Every fact
# needed to see that is in git at the base commit, so this is decided BEFORE any job exists.
SPEC_DEFECT_PREFIX = "SPEC_DEFECT: "
CREATION_TARGET_EXISTS = "CREATION_TARGET_EXISTS"
ALREADY_SATISFIED = "ALREADY_SATISFIED"
REFIMPL_ANCHOR_UNMATCHED = "REFIMPL_ANCHOR_UNMATCHED"

_PATH_EXT = r"(?:tsx|ts|jsx|js|mjs|cjs|py|swift|sh|cs|go|rs|kt|java|css|html|json|sql|prisma)"
# `create [a|the] [new] [file|module|...] <path>` -- the path must follow the verb directly
# (plus filler words), so "create a test for lib/foo.ts" does NOT name foo.ts as created.
_CREATE_PATH_RE = re.compile(
    r"\b(?:create[sd]?|creating|add(?:s|ed|ing)?\s+a\s+new\s+file|new\s+file)\s+"
    r"(?:(?:a|an|the|new)\s+)*(?:(?:file|module|script|target|helper|component|test\s+file)\s+)?"
    r"(?:called\s+|named\s+)?['\"`]?((?:[\w.\[\]-]+/)*[\w.\[\]-]+\.%s)(?![\w/])" % _PATH_EXT, re.I)
_MODIFY_WORDS = (r"modif(?:y|ies|ied|ying)|extend(?:s|ed|ing)?|updat(?:e|es|ed|ing)|edit(?:s|ed|ing)?|"
                 r"rewrit(?:e|es|ing)|rewritten|overwrit(?:e|es|ing)|replac(?:e|es|ed|ing)|"
                 r"refactor(?:s|ed|ing)?|amend(?:s|ed|ing)?|existing|already\s+exists?")


def creation_paths_in_text(text: str):
    """[path] files the INTENT/TASK text says to CREATE (`create lib/x.ts ...`)."""
    out = []
    for m in _CREATE_PATH_RE.finditer(text or ""):
        p = m.group(1).strip("'\"`.,;:)")
        if p and p not in out:
            out.append(p)
    return out


def intent_allows_existing(text: str, path: str) -> bool:
    """True when the text EXPLICITLY says to modify/extend/update `path` (or that it already
    exists): a modify word within ~60 chars before the path, or 'exists' within ~60 after."""
    t = text or ""
    q = re.escape(path)
    return bool(re.search(r"\b(?:%s)\b[^.\n]{0,60}%s" % (_MODIFY_WORDS, q), t, re.I)
                or re.search(r"%s[^.\n]{0,60}\b(?:already\s+exists?|exists\s+already|is\s+already|"
                             r"(?:to\s+be\s+)?(?:modified|extended|updated|rewritten|replaced))\b"
                             % q, t, re.I))


def _git_run(wt, *args):
    try:
        return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, timeout=60)
    except Exception:
        return None


def blob_at(wt, path: str, ref: str = "HEAD"):
    """text of `path` at commit `ref` (git cat-file -e / show), or None when absent there."""
    p = path or ""
    while p.startswith("./"):
        p = p[2:]
    e = _git_run(wt, "cat-file", "-e", f"{ref}:{p}")
    if e is None or e.returncode != 0:
        return None
    r = _git_run(wt, "show", f"{ref}:{p}")
    return r.stdout.decode("utf-8", "replace") if r is not None and r.returncode == 0 else None


def _required_change_text(task_text: str) -> str:
    """The section the INTENT lives in; falls back to the whole text."""
    m = re.search(r"##+\s*Required change[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "", re.S | re.I)
    return m.group(1) if m else (task_text or "")


def _finding(code, reason, path, message):
    return {"code": code, "reason": reason, "path": path, "message": message}


def creation_target_findings(wt, target, creation: bool, intent_text: str, ref: str = "HEAD"):
    """[finding] -- (a) a file the task CREATES (the creation-task target, plus every path the
    intent says `create ...`) that already exists, as a real implementation, at `ref`.
    A stub at base (what a creation scaffold seeds and seals) is a baseline, not a defect.
    The INTENT saying modify/extend/update/'already exists' for that path exempts it."""
    paths = []
    if creation and target:
        paths.append(str(target))
    for p in creation_paths_in_text(intent_text):
        if p not in paths:
            paths.append(p)
    out = []
    for p in paths:
        body = blob_at(wt, p, ref)
        if body is None or is_stub_text(body):
            continue
        if intent_allows_existing(intent_text, p):
            continue
        out.append(_finding(
            CREATION_TARGET_EXISTS, "creation target exists at base", p,
            f"the task CREATES {p!r} but it already exists at base {ref} as a real "
            f"implementation ({len(body)} bytes, not a stub). No reference impl can create a "
            f"file that is there, and the author model spirals trying (smoke-order-mismatch). "
            f"Re-spec: make the intent modify/extend {p!r} (say so explicitly), or drop the "
            f"slice/task if the file already does what it asks."))
    return out


def already_satisfied_findings(wt, task_text: str, target, verify_green, ref: str = "HEAD"):
    """[finding] -- (b) every REAL `## Must contain` literal is ALREADY present in the file it
    is pinned to (default: the target) at `ref` AND the verify already passes there.
    `verify_green` is the measured baseline verdict (True = VERIFY_OK on the untouched tree);
    None/False -> no finding (a literal that is present proves nothing without a green verify)."""
    if verify_green is not True:
        return []
    pairs = must_contain_literals(task_text)
    if not pairs:
        return []
    for f, lit in pairs:
        body = blob_at(wt, f or target or "", ref) if (f or target) else None
        if body is None or count_occurrences(lit, body) == 0:
            return []
    return [_finding(
        ALREADY_SATISFIED, "already satisfied at base", target or "",
        f"all {len(pairs)} 'Must contain' literal(s) are already present at base {ref} and the "
        f"verify is already GREEN on the untouched tree: there is nothing left to build. "
        f"Skip the task (the property already holds) or restate it so it names something "
        f"the base lacks.")]


def refimpl_base_anchor_findings(wt, target, refimpl_text: str, ref: str = "HEAD"):
    """[finding] -- (c) refimpl OLD anchors that match NOTHING in the base content of the
    files the refimpl edits (target + tracked files its path constants name). Reads `ref`,
    not the working copy, so a clobbered/edited tree cannot hide a dead anchor. (The
    working-tree variant, refimpl_anchor_findings, skips creation tasks; this one does not,
    because a creation task over an existing file still runs `assert OLD in t`.)"""
    anchors = refimpl_anchors(refimpl_text)
    if not anchors:
        return []
    texts = []
    if target:
        b = blob_at(wt, target, ref)
        if b is None:
            # UNSEALED CREATION STUB (pa-supersede-policy-rq, 2026-10-13): preflight runs this gate
            # BEFORE check_harness_sealed commits the scaffold's seeded creation stub, so the target
            # is absent at HEAD although the worktree holds the pristine stub the refimpl anchors on
            # and that is about to be sealed as the baseline. Treat an untouched stub (is_stub_text,
            # never a real implementation) as the base; a clobbered/real file is still not.
            try:
                _wtxt = (Path(wt) / target).read_text(errors="replace")
            except OSError:
                _wtxt = None
            if _wtxt is not None and is_stub_text(_wtxt):
                b = _wtxt
        if b is not None:
            texts.append(b)
    consts = _path_constants(refimpl_text)
    if consts:
        r = _git_run(wt, "ls-tree", "-r", "--name-only", ref)
        files = r.stdout.decode("utf-8", "replace").splitlines() if r is not None and r.returncode == 0 else []
        for f in files:
            if any(f == c or f.endswith("/" + c) or Path(f).name == Path(c).name for c in consts):
                b = blob_at(wt, f, ref)
                if b is not None:
                    texts.append(b)
    blob = "\n".join(texts)
    return [_finding(REFIMPL_ANCHOR_UNMATCHED, "refimpl anchor matches nothing at base", target or "",
                     f"refimpl anchor {a[:80]!r} occurs in none of the base files the refimpl "
                     f"edits (target {target!r} @ {ref}) -- 'assert OLD in t' can never pass")
            for a in anchors if a not in blob]


# ---------------------------------------------------------------------------
# LITERAL LINT (2026-10-09, rt-walmart-cancel-import)
# ---------------------------------------------------------------------------
# A `## Must contain` literal that the base file spells slightly differently
# (`normalize(orderNumber)` vs the real `normalize(r.orderNumber)` /
# `normalize(s.orderNumber)`) can never be satisfied by a refimpl that edits the real
# call sites, and the authoring model burns whole rounds chasing it. Three pure gates:
#   (a) literal_near_miss / literal_near_miss_findings -- at authoring (s1) and on stage
#       entry: a literal ABSENT from the file at HEAD whose nearest existing line equals it
#       up to INSERTED/DROPPED characters (a qualifier like `r.`) is a spec defect, with a
#       "did you mean" message. Substitutions (`= true` vs `= false`) are NOT near-misses:
#       that is exactly the shape of a legitimate new literal.
#   (b) refimpl_literal_gap_findings -- with the refimpl applied, every literal must be
#       present; otherwise the stage failure is a spec defect, not nonconvergence.
#   (c) missing_literals_in_text / repeat_missing_literals -- two consecutive failed rounds
#       carrying the IDENTICAL non-empty missing-literal set: re-spec/park, no third round.
LITERAL_NEAR_MISS = "LITERAL_NEAR_MISS"
REFIMPL_LITERAL_ABSENT = "REFIMPL_LITERAL_ABSENT"
REPEAT_MISSING_LITERALS = "REPEAT_MISSING_LITERALS"
_NEAR_MISS_MIN_LEN = 8
_NEAR_MISS_MIN_RATIO = 0.8
_NEAR_MISS_MAX_EDIT = 16        # inserted/dropped chars tolerated between literal and line


def literal_near_miss(lit: str, body: str):
    """PURE. (line_no, line_text, window) of the existing line that contains `lit` up to a
    pure insertion/deletion of characters (never a substitution), else None. A literal that
    occurs verbatim is not a near miss. Short literals (<8 chars) never qualify: a short
    token is too likely to be a coincidental subsequence."""
    import difflib
    lit = str(lit or "")
    if len(lit.strip()) < _NEAR_MISS_MIN_LEN or not body or count_occurrences(lit, body):
        return None
    best = None
    for i, line in enumerate(body.splitlines(), 1):
        if len(line) < _NEAR_MISS_MIN_LEN // 2 or len(line) > 4000:
            continue
        # cheap prefilter: the literal's first and last significant char must occur
        if lit[0] not in line or lit[-1] not in line:
            continue
        sm = difflib.SequenceMatcher(None, lit, line, autojunk=False)
        # candidate windows start where the literal's opening run matches
        for a0 in sorted({b.b for b in sm.get_matching_blocks() if b.size >= 3})[:12]:
            win = line[a0:a0 + len(lit) + _NEAR_MISS_MAX_EDIT]
            w = difflib.SequenceMatcher(None, lit, win, autojunk=False)
            ops = [o for o in w.get_opcodes() if o[0] != "equal"]
            if not ops or any(o[0] == "replace" for o in ops):
                continue
            # trailing text of the window beyond the last match is not part of the miss
            last_eq = max((o[4] for o in w.get_opcodes() if o[0] == "equal"), default=0)
            first_eq = min((o[3] for o in w.get_opcodes() if o[0] == "equal"), default=0)
            used = win[first_eq:last_eq]
            if lit[0] != used[:1] or lit[-1] != used[-1:]:
                continue
            ed = sum(max(o[2] - o[1], o[4] - o[3]) for o in w.get_opcodes()
                     if o[0] in ("insert", "delete") and o[3] >= first_eq and o[4] <= last_eq)
            if ed == 0 or ed > _NEAR_MISS_MAX_EDIT:
                continue
            ratio = difflib.SequenceMatcher(None, lit, used, autojunk=False).ratio()
            if ratio < _NEAR_MISS_MIN_RATIO:
                continue
            # the literal must be an ordered subsequence of the used window (insert-only)
            it = iter(used)
            if not all(ch in it for ch in lit):
                if not (len(used) < len(lit)):
                    continue
                # 2026-10-10 (aw-codec-floor s2): when the existing text is SHORTER than the
                # literal and is an ordered subsequence of it (`candidates` within `candidate_names`),
                # the literal merely EXTENDS a word that already exists -- that is exactly the
                # shape of a legitimate new identifier, not a misspelling of an existing line.
                it2 = iter(lit)
                if all(ch in it2 for ch in used):
                    continue
            cand = (ratio, i, line.strip(), used)
            if best is None or cand[0] > best[0]:
                best = cand
    if best is None:
        return None
    return best[1], best[2], best[3]


def literal_near_miss_findings(wt, task_text: str, target, refimpl_text: str = "", ref: str = "HEAD"):
    """[finding] -- (a) every REAL `## Must contain` literal absent from its file at `ref`
    whose nearest existing line is a near miss. A literal the refimpl text itself spells
    (it introduces it on purpose) is skipped."""
    out = []
    for f, lit in must_contain_literals(task_text):
        path = f or target or ""
        if not path:
            continue
        body = blob_at(wt, path, ref)
        if body is None or count_occurrences(lit, body):
            continue
        if refimpl_text and lit in refimpl_text:
            continue
        nm = literal_near_miss(lit, body)
        if nm is None:
            continue
        ln, line, win = nm
        out.append(_finding(
            LITERAL_NEAR_MISS, "Must-contain literal near-misses the base code", path,
            f"`{lit}` occurs 0 times in {path} @ {ref}, but line {ln} has `{win}` -- did you "
            f"mean `{win}`? Fix the literal in TASK.md (the real call sites are spelled that "
            f"way) or drop it; no refimpl that edits the real code can add the "
            f"misspelled form. Line {ln}: {line[:140]}"))
    return out


# UNNATURAL-LITERAL LINT (2026-10-11, rt-amazon / jobs 521e3b134333 b5fcdc7582dd): a Must-contain
# literal `/extractTrackingPageInBrowser/` (regex-literal shape around a plain identifier) was only
# satisfiable by a contrived log string in the refimpl; the model burned its whole output cap on it
# (prose_loop, zero edits). Two PURE gates, both admission-blocking SPEC_DEFECTs:
#   (a) literal_shape_findings   -- slash-wrapped plain code identifier (slashes mean nothing in code)
#   (b) literal_context_findings -- a plain-identifier literal whose ONLY occurrences (refimpl-applied
#       tree) sit inside a comment or a string on a log/throw line, never in real code.
# Both are deliberately narrow: explicit strings (URLs, error messages, aria-labels, CSS selectors)
# are not plain identifiers and are never inspected.
UNNATURAL_LITERAL = "UNNATURAL_LITERAL"
_SLASH_IDENT_RE = re.compile(r"^/([A-Za-z_$][A-Za-z0-9_$]{5,})/$")
_PLAIN_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$")
_LOGISH_RE = re.compile(r"console\.|\blog\w*\s*\(|\blogger\b|\bprint\s*\(|\bthrow\s+new\b|\bnew\s+\w*Error\s*\(|"
                        r"\.(?:warn|error|info|debug|trace)\s*\(|\bwarn\w*\s*\(", re.I)


def _codeish_ident(s: str) -> bool:
    """camelCase / PascalCase / snake_case identifier: reads as code, not as a URL path segment."""
    return bool(re.search(r"[a-z][A-Z]", s) or re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", s))


def unnatural_literal_shape(lit: str):
    """PURE. (a) message when `lit` is a slash-wrapped plain code identifier, else None.
    `/api/`, `/orders/` (URL path segments, lowercase) and `/\\d+/` (real regexes) pass."""
    m = _SLASH_IDENT_RE.match(lit or "")
    if not m or not _codeish_ident(m.group(1)):
        return None
    ident = m.group(1)
    return (f"`{lit}` has the shape of a regex literal around the plain identifier `{ident}`, but the "
            f"slashes mean nothing in code: only a contrived string/comment can ever contain them, and "
            f"the model will burn its output cap trying. Drop the slashes and require the identifier "
            f"`{ident}` (or drop the literal)")


def literal_shape_findings(task_text: str):
    """[finding] -- (a) for every REAL `## Must contain` literal."""
    out = []
    for f, lit in must_contain_literals(task_text):
        msg = unnatural_literal_shape(lit)
        if msg:
            out.append(_finding(UNNATURAL_LITERAL, "Must-contain literal is unnatural (slash-wrapped identifier)",
                                f or "TASK.md", msg))
    return out


def _occurrence_kinds(line: str, lit: str):
    """['code'|'string'|'comment', ...] one per occurrence of `lit` on a single line (per-line
    scanner; a multi-line template body starts in 'code' state, the SAFE direction)."""
    if _COMMENT_LINE_RE.match(line):
        return ["comment"] * line.count(lit)
    kinds, q, i, n = [], None, 0, len(line)
    while i < n:
        if line.startswith(lit, i):
            kinds.append("string" if q else "code")
            i += max(1, len(lit))
            continue
        c = line[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        else:
            if c in "'\"`":
                q = c
            elif line.startswith("//", i) or (c == "#" and not lit.startswith("#")):
                kinds += ["comment"] * line.count(lit, i)
                break
        i += 1
    return kinds


def unnatural_literal_context(lit: str, text: str):
    """PURE. True when `lit` (optionally slash-wrapped) is a plain identifier whose every occurrence
    in `text` is a comment, or a string on a log/throw line -- i.e. never real code."""
    core = lit[1:-1] if len(lit) > 2 and lit[0] == lit[-1] == "/" else lit
    if not _PLAIN_IDENT_RE.match(core) or len(core) < 6 or not text:
        return False
    seen = False
    for line in text.splitlines():
        if lit not in line:
            continue
        for k in _occurrence_kinds(line, lit):
            seen = True
            if k == "code":
                return False
            if k == "string" and not _LOGISH_RE.search(line):
                return False
    return seen


def literal_context_findings(pairs, texts_by_file, default_text: str = ""):
    """[finding] -- (b) literals present in the refimpl-applied tree ONLY as comment / log-message text."""
    bad = []
    for f, lit in pairs or []:
        body = texts_by_file.get(f, "") if f else default_text
        if unnatural_literal_context(lit, body or ""):
            bad.append(lit)
    if not bad:
        return []
    names = ", ".join(repr(l) for l in bad[:6])
    return [_finding(
        UNNATURAL_LITERAL, "Must-contain literal is satisfiable only by a comment/log string", "TASK.md",
        f"{len(bad)} literal(s) occur in the refimpl-applied tree only inside a comment or a log/throw "
        f"message, never in real code: {names}. A model must invent a contrived string to satisfy it. "
        f"Drop the literal, or require the real identifier / syntax (not its slash-wrapped or quoted form)")]


# SPEC/TEST PARITY LINT (2026-10-11, rt-amazon: three prose_loop runs, zero edits). The repaired test asserted EXACT
# evaluation counts and virtual-clock values (`detailEvals(), 1 + POLL_ATTEMPTS`, `clock.now, POLL_ATTEMPTS * POLL_MS`)
# that TASK.md never stated, so the model burned its output cap re-deriving them. A SPEC_DEFECT, admission-blocking:
# TASK.md must carry an "Exact behaviour contract" section that mentions every distinct non-trivial quantity the
# fixture asserts exactly. Deliberately conservative: only counter/time/length-shaped ACTUAL values compared to a
# NUMBER or a NAMED NUMERIC CONSTANT expression, and only when at least one is a named constant or a clock/elapsed value (bare `.length, 2` data asserts never fire); 0/1/true/false, string/data asserts, <=/>= bounds and ad-hoc
# expectations are ignored; fewer than SPEC_PARITY_MIN_QUANTITIES distinct quantities never fires.
SPEC_TEST_PARITY = "SPEC_TEST_PARITY"
SPEC_PARITY_MIN_QUANTITIES = 2
_PARITY_HEADING_RE = re.compile(r"exact (?:behaviou?r )?contract|expected counts|precise behaviou?r", re.I)
_PARITY_FIXTURES = ("verify.test.ts", "verify.test.mts", "verify.test.js", "verify.test.mjs", "verify.py", "test_fixture.py")
_PARITY_FN_RE = re.compile(r"(?<![\w$])(?:assert\.|t\.)?(strictEqual|deepStrictEqual|equal|deepEqual|assertEqual|assertEquals)\s*\(")
_PARITY_EXPECT_RE = re.compile(r"(?<![\w$])expect\s*\(")
_PARITY_COUNTERISH_RE = re.compile(
    r"length|count|calls?\b|evals?\b|evaluat|attempt|retr|elapsed|clock|\bnow\b|time|\bms\b|size|tries|hits|invocation|sleeps?\b|polls?\b|Evals", re.I)
_PARITY_TIMEISH_RE = re.compile(r"clock|\bnow\b|elapsed|\bms\b|Ms\b", re.I)
_PARITY_QEXPR_RE = re.compile(r"^[\s\d_.()+\-*/%A-Z]+$")
_PARITY_CONST_RE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)?\s*([A-Z][A-Z0-9_]{2,})\s*(?::\s*[\w<>\[\]]+)?=\s*([^;\n]+?)\s*;?\s*(?://.*|#.*)?$", re.M)


def _split_args(src, open_idx):
    """PURE. Top-level comma-separated args of the call whose '(' is at open_idx, plus the index just past ')'."""
    depth, args, cur, i, q = 0, [], [], open_idx, None
    while i < len(src):
        c = src[i]
        if q:
            cur.append(c)
            if c == "\\":
                cur.append(src[i + 1:i + 2]); i += 1
            elif c == q:
                q = None
        elif c in "'\"`":
            q = c; cur.append(c)
        elif c in "([{":
            depth += 1
            if depth > 1:
                cur.append(c)
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i + 1
            cur.append(c)
        elif c == "," and depth == 1:
            args.append("".join(cur).strip()); cur = []
        else:
            cur.append(c)
        i += 1
    return None, len(src)


def _numeric_consts(text):
    """PURE. {NAME: rhs} for UPPER_CASE constants whose right side is a numeric expression of numbers / other such names."""
    raw = {m.group(1): m.group(2).strip() for m in _PARITY_CONST_RE.finditer(text)}
    ok = {}
    for _ in range(4):
        for n, rhs in raw.items():
            if n in ok or not _PARITY_QEXPR_RE.match(rhs) or not re.search(r"\d", rhs):
                continue
            if all(t in ok for t in re.findall(r"[A-Z][A-Z0-9_]+", rhs)):
                ok[n] = rhs
    return ok


def _quantity_expr(expr, consts):
    """PURE. set of quantity keys ('16', 'POLL_ATTEMPTS', ...) when `expr` is a pure numeric expression of
    non-trivial numbers / known numeric constants; None when it is anything else; empty set when trivial (0/1)."""
    e = (expr or "").strip()
    if not e or not _PARITY_QEXPR_RE.match(e) or not re.search(r"[\dA-Z]", e):
        return None
    names = re.findall(r"[A-Z][A-Z0-9_]{2,}", e)
    if any(n not in consts for n in names):
        return None
    if re.search(r"[A-Z]", re.sub(r"[A-Z][A-Z0-9_]{2,}", "", e)):
        return None
    keys = set(names)
    for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", re.sub(r"[A-Z][A-Z0-9_]{2,}", " ", e)):
        if n not in ("0", "1"):
            keys.add(n)
    return keys


def parity_quantities(fixture_text):
    """PURE. {quantity: [example assertion text]} for the exact counter/time/length assertions of a fixture."""
    consts = _numeric_consts(fixture_text)
    found, strong = {}, set()
    pairs = []
    for m in _PARITY_FN_RE.finditer(fixture_text):
        args, _end = _split_args(fixture_text, m.end() - 1)
        if args and len(args) >= 2:
            pairs.append((args[0], args[1]))
    for m in _PARITY_EXPECT_RE.finditer(fixture_text):
        args, end = _split_args(fixture_text, m.end() - 1)
        mm = re.match(r"\s*\.\s*(?:not\s*\.\s*)?(toBe|toEqual|toStrictEqual|toHaveLength)\s*\(", fixture_text[end:])
        if args and mm:
            a2, _e2 = _split_args(fixture_text, end + mm.end() - 1)
            if a2:
                pairs.append((args[0] if mm.group(1) != "toHaveLength" else args[0] + ".length", a2[0]))
    for m in re.finditer(r"^\s*assert\s+(.+?)\s*==\s*(.+?)\s*(?:,.*)?$", fixture_text, re.M):
        pairs.append((m.group(1), m.group(2)))
    for a, b in pairs:
        for act, exp in ((a, b), (b, a)):
            if _quantity_expr(act, consts) is not None or not _PARITY_COUNTERISH_RE.search(act):
                continue
            keys = _quantity_expr(exp, consts)
            if keys:
                if any(k in consts for k in keys) or _PARITY_TIMEISH_RE.search(act):
                    strong.add(True)
                for k in keys:
                    found.setdefault(k, f"{act.strip()[:60]}, {exp.strip()[:60]}")
                break
    return (found if strong else {}), consts   # plain data-length asserts alone (`result.length, 2`) are example-spec'd: never enough


def _contract_sections(task_text):
    """PURE. Concatenated body text of every heading matching the contract regex (to the next heading of the same or higher level)."""
    lines, out, i = (task_text or "").splitlines(), [], 0
    heads = [(n, len(m.group(1)), m.group(2)) for n, l in enumerate(lines) if (m := re.match(r"^(#{1,6})\s+(.*)$", l))]
    for k, (n, lvl, title) in enumerate(heads):
        if not _PARITY_HEADING_RE.search(title):
            continue
        end = next((n2 for n2, l2, _t in heads[k + 1:] if l2 <= lvl), len(lines))
        out.append("\n".join(lines[n:end]))
    return "\n".join(out)


def _quantity_mentioned(q, consts, section):
    if re.search(rf"(?<![\w$]){re.escape(q)}(?![\w$])", section):
        return True
    if q.isdigit():
        if re.search(rf"(?<![\d,.]){int(q):,}(?![\d,])", section):
            return True
        if int(q) >= 1000 and int(q) % 1000 == 0 and re.search(rf"(?<![\d.]){int(q) // 1000}\s*(?:s|sec|seconds)\b", section):
            return True
        return False
    rhs = consts.get(q, "")
    return bool(re.fullmatch(r"\d+", rhs.strip())) and _quantity_mentioned(rhs.strip(), consts, section)


def _parity_quantity_findings(wt, task_text):
    """[finding] -- SPEC_TEST_PARITY: the fixture asserts exact counts/clock values TASK.md never states in an
    'Exact behaviour contract' section. Empty when below SPEC_PARITY_MIN_QUANTITIES or fully covered."""
    wt = Path(wt)
    for fx in _PARITY_FIXTURES:
        text = _read(wt / fx)
        if not text:
            continue
        found, consts = parity_quantities(text)
        if len(found) < SPEC_PARITY_MIN_QUANTITIES:
            continue
        section = _contract_sections(task_text)
        missing = [q for q in sorted(found) if not _quantity_mentioned(q, consts, section)]
        if missing:
            return [_finding(
                SPEC_TEST_PARITY, "Test asserts exact quantities the spec never states", fx,
                f"{fx} asserts exact values ({'; '.join(f'`{found[q]}`' for q in missing[:3])}) but TASK.md has "
                + ("no 'Exact behaviour contract' section" if not section else "an 'Exact behaviour contract' section missing them")
                + f". Add an 'Exact behaviour contract' table listing: {', '.join(missing)} "
                  f"(what each equals and why), so the model need not re-derive them from the tests.")]
    return []


# ---------------------------------------------------------------------------
# OVER-SPECIFICATION LINT (2026-10-11), an extension of SPEC_TEST_PARITY. Two real rounds were burned by fixtures that
# pinned things TASK.md never states:
#   cardlink s2   a counter matched every page.evaluate whose SOURCE contained 'payment method', so a valid
#                 extractDetailInBrowser change failed it; and 'exactly 16 reads' pinned refimpl internals.
#   arr-webhook   the fixture stubbed `get_all_torrents` (TASK.md never named the fetch method; the model used
#                 core.get_torrents, valid on live Deluge) and expected a (count, calls) tuple while TASK.md said int.
# Four static checks, all under the SPEC_TEST_PARITY code (one admission path, one canary seam):
#   (1) STUBBED SEAM    a function the fixture stubs (monkeypatch/setattr/patch.object/`target.f =`, mock.method /
#                       spyOn / mock.module namedExports, JSON-RPC `method == 'a.b'` dispatch) that the BASE target
#                       defines or imports must be NAMED in a spec section (Interface / Exact contract / Required
#                       change / Must contain / Entry point...). A model cannot route through a seam it was never told.
#   (2) RETURN SHAPE    the fixture unpacks the entry function's result into several names (a tuple) while TASK.md
#                       declares a scalar (`-> int`, "NOT a tuple") or never mentions a tuple/pair at all.
#   (3) EXACT COUNT     `== N` (N >= 2, a LITERAL number) on a call-log-shaped value (calls / posts / reads / evals /
#                       fetches ...) must appear in the spec sections; `>=`/`<=` and bare data lengths never fire.
#                       (Named-constant / clock quantities stay the older parity rule's job.)
#   (4) SOURCE MATCH    a counter/branch keyed on the SOURCE TEXT of the callback (`String(fn).includes('x')`,
#                       `fn.toString()`): any valid rewrite of the callback that drops the string breaks it.
# Deliberately conservative: comments blanked, 0/1 counts exempt, stdlib objects exempt, seam names must be defined or
# imported by the BASE target (a brand-new function the task adds is not a seam), near-misses never fire.
# Why this does not undo earlier rules: it only ADDS findings; older SPEC_TEST_PARITY quantities (named constants,
# clock values, >=2 quantity minimum) are untouched and not re-reported; JS fixtures' load-trap / polling / evaluate lints
# look at different shapes.
# ---------------------------------------------------------------------------
_OVERSPEC_SECTION_RE = re.compile(
    r"interface|exact (?:behaviou?r )?contract|expected counts|precise behaviou?r|required change|entry point|"
    r"signature|\bapi\b|must contain|must hold|exact behaviou?r|contract|behaviou?r", re.I)
_PY_STD_OBJS = {"os", "sys", "time", "random", "subprocess", "requests", "json", "re", "socket", "shutil", "builtins",
                "datetime", "threading", "logging", "urllib", "asyncio", "importlib", "pathlib", "tempfile", "uuid",
                "globalThis", "global", "process", "console", "Math", "Date", "JSON", "Object", "Promise", "fs"}
_PY_PATCH_OBJ_RE = re.compile(
    r"(?:monkeypatch\.setattr|(?<![\w.])setattr|(?:mock\.)?patch\.object)\s*\(\s*([\w.]+)\s*,\s*['\"](\w+)['\"]")
_PY_PATCH_STR_RE = re.compile(r"(?<![\w.])(?:mock\.)?patch\s*\(\s*['\"]([\w.]+)['\"]")
_PY_MODALIAS_RE = re.compile(r"^\s*(\w+)\s*=\s*(?:importlib\.util\.)?(?:module_from_spec|import_module|__import__)\b", re.M)
_JS_METHOD_RE = re.compile(r"(?<![\w$])(?:mock\.method|sinon\.stub|(?:jest|vi)\.spyOn)\s*\(\s*([\w$.]+)\s*,\s*['\"`]([\w$]+)['\"`]")
_JS_MOCKMOD_RE = re.compile(r"mock\.module\s*\(\s*['\"`](\.{1,2}/[^'\"`]+)['\"`]")
_RPC_EQ_RE = re.compile(r"\b(?:method|rpc|action|op|endpoint|cmd|command)\w*\s*(?:===?|\.startsWith\()\s*\(?\s*['\"`]([A-Za-z_]+(?:\.[A-Za-z_]+)+)['\"`]")
_CALLLOG_RE = re.compile(
    r"calls?\b|call_?count|callCount|\bposts?\b|\breads?\b|\bevals?\b|evaluat\w*|\bfetch\w*|invocations?|\brequests?\b|"
    r"\bhits\b|\bsends?\b|\bpolls?\b|\bsleeps?\b|\bwrites?\b|\bgets\b|\bqueries\b", re.I)
# Counts of 1-3 are structural (first page + the short page that ends paging, one retry, "once"): the spec's behaviour
# rules imply them. Magic counts (cardlink: 16 reads) are not derivable. Sweep 2026-10-11: min 2 flagged a healthy
# paging slice (sidecar-bfmr s4: `page.calls.length, 2` = "stop after the first short page").
OVERSPEC_MIN_COUNT = 4
_NUMWORDS = {"two": 2, "twice": 2, "three": 3, "thrice": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_CB = r"(?:fn|func|f|cb|callback|script|code|expr|expression|pageFunction|handler|source|src|evalFn|pageFn)\b"
_SRC_OF = r"(?:String\s*\(\s*" + _CB + r"\s*\)|" + _CB + r"\s*\.toString\s*\(\s*\)|Function\.prototype\.toString\.call\s*\(\s*" + _CB + r"\s*\))"
_SRC_MATCH_RE = re.compile(
    _SRC_OF + r"\s*\.\s*(?:includes|match|indexOf|search|startsWith)\s*\(\s*(['\"`/])([^'\"`/\n]{2,80})\1|"
    r"/([^/\n]{2,80})/[a-z]*\s*\.test\s*\(\s*" + _SRC_OF)
_SRC_ASSIGN_RE = re.compile(r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*" + _SRC_OF)


def _spec_name_sections(task_text):
    """PURE. Concatenated text of every heading whose title is an Interface / contract / required-change style
    section (to the next heading of the same or higher level). Empty when TASK.md has none."""
    lines = (task_text or "").splitlines()
    heads = [(n, len(m.group(1)), m.group(2)) for n, l in enumerate(lines) if (m := re.match(r"^(#{1,6})\s+(.*)$", l))]
    out = []
    for k, (n, lvl, title) in enumerate(heads):
        if not _OVERSPEC_SECTION_RE.search(title):
            continue
        end = next((n2 for n2, l2, _t in heads[k + 1:] if l2 <= lvl), len(lines))
        out.append("\n".join(lines[n:end]))
    return "\n".join(out)


def _name_in(name, text):
    return bool(re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text or ""))


def _defined_in_base(name, base_text):
    """True when the BASE target defines or imports `name` (so the task did not invent it)."""
    return bool(re.search(
        r"(?:\bdef\s+|\bclass\s+|\bfunction\s*\*?\s*|\b(?:const|let|var)\s+|\bimport\s+[^\n]*?|\bas\s+)" + re.escape(name) + r"\b|"
        r"\bfrom\s+\S+\s+import\s+[^\n]*\b" + re.escape(name) + r"\b|"
        r"\bimport\s*\{[^}]*\b" + re.escape(name) + r"\b[^}]*\}", base_text or ""))


def stubbed_seams(fixture_name, fixture_src, base_text=""):
    """PURE. {seam_name: example} the fixture stubs: functions the BASE target defines/imports, plus JSON-RPC style
    dotted method literals the fake dispatches on."""
    py = fixture_name.endswith(".py")
    src = fixture_src or ""
    if not py:
        src = _blank_js_comments(src)
    found = {}
    if py:
        aliases = {"target"} | set(_PY_MODALIAS_RE.findall(src))
        for m in _PY_PATCH_OBJ_RE.finditer(src):
            obj, nm = m.group(1), m.group(2)
            if obj.split(".")[0] not in _PY_STD_OBJS and _defined_in_base(nm, base_text):
                found.setdefault(nm, f"patches {obj}.{nm}")
        for m in _PY_PATCH_STR_RE.finditer(src):
            dotted = m.group(1)
            root, nm = dotted.split(".")[0], dotted.split(".")[-1]
            if "." in dotted and root not in _PY_STD_OBJS and _defined_in_base(nm, base_text):
                found.setdefault(nm, f"patches {dotted}")
        for a in aliases:
            for m in re.finditer(r"(?<![\w.])" + re.escape(a) + r"\.(\w+)\s*=(?!=)", src):
                if _defined_in_base(m.group(1), base_text):
                    found.setdefault(m.group(1), f"assigns {a}.{m.group(1)}")
    else:
        for m in _JS_METHOD_RE.finditer(src):
            obj, nm = m.group(1), m.group(2)
            if obj.split(".")[0] not in _PY_STD_OBJS and _defined_in_base(nm, base_text):
                found.setdefault(nm, f"stubs {obj}.{nm}")
        for m in _JS_MOCKMOD_RE.finditer(src):
            body = src[m.end():m.end() + 1500]
            ne = re.search(r"namedExports\s*:\s*\{", body)
            if not ne:
                continue
            j = body.find("{", ne.start())
            close = _match_close(body, j)
            blk = body[j + 1:close] if close > 0 else ""
            for km in re.finditer(r"(?:^|[,{\s])([A-Za-z_$][\w$]*)\s*(?::|\()", blk):
                if _defined_in_base(km.group(1), base_text):
                    found.setdefault(km.group(1), f"mock.module({m.group(1)}) export")
    for m in _RPC_EQ_RE.finditer(src):
        found.setdefault(m.group(1), f"dispatches on {m.group(1)!r}")
    return found


_DATA_SEAM_RE = re.compile(
    r"^_*(?:get|fetch|list|load|read|query|find|search|lookup|resolve|check|is|has|parse|build|compute|extract|select)(?:_|[A-Z0-9]|$)|"
    r"(?:_|[a-z])(?:info|status|url|client|state|rows?|data|torrents|hash|keys?)$|^[A-Z]\w*(?:Client|Session|Store)$|^\w+\.\w+", re.I)


def _seam_returns_data(name):
    """PURE. A stub that FEEDS DATA to the code under test (fetch/lookup/parse/client, or a dotted RPC method) is a route the
    model must know about; a side-effect-only stub (login, record_activity, send_*, ensure_*) is harmless when unused."""
    return bool(_DATA_SEAM_RE.search(name))


def _entry_calls(fixture_name, fixture_src):
    """PURE. Names of target functions the fixture CALLS directly (`target.f(...)` / a bare call of an imported name)."""
    src = fixture_src or ""
    if fixture_name.endswith(".py"):
        al = {"target"} | set(_PY_MODALIAS_RE.findall(src))
        return {m.group(2) for m in re.finditer(r"(?<![\w.])(" + "|".join(map(re.escape, al)) + r")\.(\w+)\s*\(", src)}
    s = _blank_js_comments(src)
    return {m.group(1) for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", s)} - _JS_SKIP


def _base_entry_reaches(base_text, entries, seam, py):
    """PURE. True when `seam` is referenced by the BASE body of a function the fixture calls, directly or through other
    base functions it calls (<= 4 hops): the existing code already routes through it, so the model edits a call it can
    see rather than choosing a route. A brand-new entry function (absent at base) never qualifies."""
    if not base_text or not entries:
        return False
    tail = seam.split(".")[-1]
    bodies = {}
    if py:
        try:
            tree = ast.parse(base_text)
        except SyntaxError:
            return False
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies.setdefault(n.name, ast.get_source_segment(base_text, n) or "")
    else:
        s = _blank_js_comments(base_text)
        for m in _TOP_FN_RE.finditer(s):
            nm = m.group(1) or m.group(2)
            sp = _fn_span(s, nm) if nm else None
            if sp:
                bodies.setdefault(nm, s[sp[0]:sp[1]])
    seen, frontier = set(), {e for e in entries if e in bodies}
    for _ in range(4):
        if any(_name_in(tail, bodies[f]) for f in frontier):
            return True
        seen |= frontier
        frontier = {g for g in bodies if g not in seen and any(_name_in(g, bodies[f]) for f in frontier)}
        if not frontier:
            break
    return False


def _seam_named(seam, section):
    if "." in seam and not seam.startswith("."):
        return _name_in(seam, section) or _name_in(seam.split(".")[-1], section)
    return _name_in(seam, section)


def _entry_shapes(fixture_name, fixture_src):
    """PURE. {fn_name: (shape, example)} -- result of a target call unpacked into >=2 names (a tuple/list)."""
    out = {}
    if fixture_name.endswith(".py"):
        for m in re.finditer(r"^\s*\(?\s*(\w+(?:\s*,\s*\w+)+)\s*\)?\s*=\s*(?:await\s+)?(?:target|mod|module|m)\.(\w+)\s*\(",
                             fixture_src or "", re.M):
            out.setdefault(m.group(2), ("tuple", m.group(0).strip()[:70]))
        for m in re.finditer(r"assert\s+(?:target|mod|module)\.(\w+)\s*\([^\n]*?\)\s*==\s*\(", fixture_src or ""):
            out.setdefault(m.group(1), ("tuple", m.group(0).strip()[:70]))
    else:
        s = _blank_js_comments(fixture_src or "")
        for m in re.finditer(r"\b(?:const|let|var)\s*\[\s*[\w$]+\s*,\s*[\w$]+[^\]]*\]\s*=\s*(?:await\s+)?[\w$.]*?([\w$]+)\s*\(", s):
            out.setdefault(m.group(1), ("tuple", m.group(0).strip()[:70]))
    return out


def _base_returns_tuple(base_text, fn):
    """PURE. The BASE definition of `fn` already returns a tuple (`return a, b`): the shape is visible in the code the
    task edits, so a fixture unpacking it is not over-specifying."""
    try:
        tree = ast.parse(base_text or "")
    except SyntaxError:
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn
               and any(isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple) for r in ast.walk(n))
               for n in ast.walk(tree))


def _declared_return(fn, section):
    """PURE. ('scalar'|'tuple'|None, text) from the spec lines that name `fn`."""
    lines = [l for l in (section or "").splitlines() if _name_in(fn, l)]
    joined = " ".join(lines)
    if not joined:
        return None, ""
    if re.search(r"tuple|pair|\(\s*\w+\s*,\s*\w+|\[\s*\w+\s*,\s*\w+", re.sub(r"(?i)\bnot\s+a\s+tuple", "", joined), re.I):
        return "tuple", joined
    if re.search(r"->\s*(?:a\s+|plain\s+)?`?(?:int|bool|str|float|None|number|string|boolean)\b|"
                 r"returns?\s+(?:a\s+)?(?:plain\s+)?`?(?:int|bool|str|float|number|string|boolean)\b|"
                 r"plain\s+`?(?:int|str|bool)\b|not\s+a\s+tuple", joined, re.I):
        return "scalar", joined
    return None, joined


def _is_count_expr(act):
    """PURE. The asserted value is a COUNT of a call log (`len(x.calls)`, `x.calls.length`, `x.callCount`, `evals`,
    `env.detailEvals()`), not a field of one entry (`updateCalls[0].data.buyerId`)."""
    a = act.strip()
    if re.search(r"\]\s*[.\[]", a) or re.fullmatch(r"[\d\s]+", a):
        return False
    return bool(re.match(r"len\s*\(", a) or re.search(r"\.length\s*$|\.(?:call_?count|callCount|count|size)\s*$|\(\s*\)\s*$", a)
                or re.fullmatch(r"[\w$.]+", a))


def _exact_call_counts(fixture_name, fixture_src):
    """PURE. {N: example} for `<call-log value> == N` assertions with a LITERAL N >= 2."""
    src = fixture_src or ""
    if not fixture_name.endswith(".py"):
        src = _blank_js_comments(src)
    pairs = []
    for m in _PARITY_FN_RE.finditer(src):
        args, _e = _split_args(src, m.end() - 1)
        if args and len(args) >= 2:
            pairs.append((args[0], args[1]))
    for m in _PARITY_EXPECT_RE.finditer(src):
        args, end = _split_args(src, m.end() - 1)
        mm = re.match(r"\s*\.\s*(toBe|toEqual|toStrictEqual|toHaveLength|toHaveBeenCalledTimes)\s*\(", src[end:])
        if args and mm:
            a2, _e2 = _split_args(src, end + mm.end() - 1)
            if a2:
                pairs.append((args[0] if mm.group(1) not in ("toHaveLength",) else "len(" + args[0] + ")", a2[0]))
    for m in re.finditer(r"^\s*assert\s+(.+?)\s*==\s*(.+?)\s*(?:,.*)?$", src, re.M):
        pairs.append((m.group(1), m.group(2)))
    out = {}
    for a, b in pairs:
        for act, exp in ((a, b), (b, a)):
            if re.fullmatch(r"\d+", exp.strip()) and int(exp.strip()) >= OVERSPEC_MIN_COUNT and _CALLLOG_RE.search(act) \
                    and _is_count_expr(act):
                out.setdefault(int(exp.strip()), f"{act.strip()[:60]}, {exp.strip()}")
                break
    return out


def _number_justified(n, section):
    if re.search(rf"(?<![\w$.]){n}(?![\w$])", section or "") or re.search(rf"(?<![\d,.]){n:,}(?![\d,])", section or ""):
        return True
    low = (section or "").lower()
    return any(w for w, v in _NUMWORDS.items() if v == n and re.search(rf"\b{w}\b", low))


def overspec_findings(wt, task_text, skip_quantities=()):
    """[finding] -- SPEC_TEST_PARITY over-specification: stubbed seams / return shapes / exact call counts / source-text
    matching in the fixture that TASK.md's Interface / contract sections do not state. See the block comment above."""
    wt = Path(wt)
    man = _manifest(wt)
    target = man.get("target")
    base_text = (blob_at(wt, target, "HEAD") or "") if target else ""
    section = _spec_name_sections(task_text)
    out = []
    for fx in _PARITY_FIXTURES:
        text = _read(wt / fx)
        if not text:
            continue
        # (1) stubbed seams
        miss = [(s, ex) for s, ex in sorted(stubbed_seams(fx, text, base_text).items())
                if _seam_returns_data(s) and not _seam_named(s, section)
                and not _base_entry_reaches(base_text, _entry_calls(fx, text), s, fx.endswith('.py'))]
        if miss:
            out.append(_finding(
                SPEC_TEST_PARITY, "Test stubs a seam the spec never names", fx,
                f"{fx} stubs {', '.join(f'`{s}` ({ex})' for s, ex in miss[:3])} but TASK.md's Interface / Required-change / "
                f"contract sections never name " + ("it" if len(miss) == 1 else "them") + ". A model that reaches the same "
                "result through another valid route (a different helper, a different RPC method) fails the fixture for "
                "nothing. Name the seam in an 'Interface' section (and say whether alternatives are allowed), or stub at a "
                "boundary the spec does name"))
        # (2) return shape
        for fn, (shape, ex) in sorted(_entry_shapes(fx, text).items()):
            decl, _j = _declared_return(fn, section)
            if decl == "tuple" or (fx.endswith(".py") and _base_returns_tuple(base_text, fn)) \
                    or (not base_text and not man.get("creation_task")):   # no base to read (non-git hand harness): cannot judge
                continue
            why = ("TASK.md declares a plain scalar return" if decl == "scalar"
                   else "TASK.md never states what it returns" if _name_in(fn, section) else "TASK.md never names it")
            out.append(_finding(
                SPEC_TEST_PARITY, "Test asserts a return shape the spec contradicts or omits", fx,
                f"{fx} unpacks `{fn}`'s result as a {shape} (`{ex}`) but {why}. State the exact return shape "
                f"(e.g. `{fn}(...) -> (count, calls)`) in the Interface section, or assert only what the spec says"))
        # (3) exact literal call counts (named-constant / clock quantities belong to the older rule)
        bad = [(n, ex) for n, ex in sorted(_exact_call_counts(fx, text).items())
               if str(n) not in skip_quantities and not _number_justified(n, section)]
        if bad:
            out.append(_finding(
                SPEC_TEST_PARITY, "Test pins an exact call count the spec never states", fx,
                f"{fx} asserts exact call counts ({'; '.join(f'`{ex}`' for _n, ex in bad[:3])}) that TASK.md never states. "
                f"A valid implementation with one more or one fewer harmless call fails. State the count ({', '.join(str(n) for n, _ in bad)}) "
                f"in the Exact behaviour contract with its reason, or assert an inequality (>= / <=)"))
        # (4) matching on the SOURCE TEXT of a callback
        if not fx.endswith(".py"):
            s = _blank_js_comments(text)
            hits = [(m.group(2) or m.group(3) or "").strip() for m in _SRC_MATCH_RE.finditer(s)]
            for am in _SRC_ASSIGN_RE.finditer(s):
                v = re.escape(am.group(1))
                hits += [m.group(2).strip() for m in re.finditer(
                    r"(?<![\w$])" + v + r"\s*\.\s*(?:includes|match|indexOf|search|startsWith)\s*\(\s*(['\"`])([^'\"`\n]{2,80})\1", s)]
            hits = [h for h in dict.fromkeys(hits) if h and not _name_in(h, section)]
            if hits:
                out.append(_finding(
                    SPEC_TEST_PARITY, "Test identifies calls by matching the callback's source text", fx,
                    f"{fx} keys a counter/branch on the SOURCE TEXT of a function ({', '.join(repr(h) for h in hits[:3])}): "
                    f"it matches every evaluate whose code merely contains that string, and any valid rewrite that drops it "
                    f"fails (cardlink s2). Identify calls by their arguments / results / call order instead"))
    return out


def spec_test_parity_findings(wt, task_text):
    """[finding] -- SPEC_TEST_PARITY: (a) the older exact-quantity rule (counts / clock values the contract never states)
    plus (b) the over-specification checks (stubbed seams, return shapes, literal call counts, source-text matching)."""
    old = _parity_quantity_findings(wt, task_text)
    skip = set(re.findall(r"\b\d+\b", " ".join(f["message"] for f in old)))
    return old + overspec_findings(wt, task_text, skip_quantities=skip)


# ---------------------------------------------------------------------------
# N-VERSION CHECK (2026-10-11), the dynamic half of the over-specification check. With the refimpl APPLIED and the
# fixture green, build a second, independently-shaped but equivalent implementation and run the SAME fixture on it. A
# fixture that fails an equivalent implementation pins refimpl internals the spec never states (cardlink s2: "exactly
# 16 reads"). IMPLEMENTED SUBSET: Python targets, one variant family -- "extra harmless read": a statement the refimpl
# ADDED that only reads (`x = get_*/fetch_*/list_*/load_*/read_*/query_*/find_*/search_*/lookup_*(...)` or a bare call of
# one) is duplicated in place. A correct implementation with one more idempotent read must still pass; an exact
# call-count assertion on that seam does not. NOT implemented (documented gap): JS/TS targets, call reordering, and
# alternate-helper substitution -- those need per-language equivalence knowledge and are covered only by the static lint.
# Result is advisory (WARN): an exact count the TASK states legitimately fails the variant.
# ---------------------------------------------------------------------------
NVERSION_VARIANT_FAILS = "NVERSION_VARIANT_FAILS"
# bare `load`/`read` (json.load(fh), fh.read()) CONSUME a stream: duplicating them is not an equivalent variant (sweep FPs)
_NV_READ_RE = re.compile(r"^_*(?!(?:load|read)$)(?:get|fetch|list|load|read|query|find|search|lookup)(?:_|[A-Z0-9]|$)")
NVERSION_MAX_VARIANTS = 2


def _added_lines(base_text, applied_text):
    """PURE. 1-based line numbers of `applied_text` that are not in `base_text` (difflib)."""
    import difflib
    sm = difflib.SequenceMatcher(None, (base_text or "").splitlines(), (applied_text or "").splitlines(), autojunk=False)
    out = set()
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def _stmt_read_callee(node):
    """PURE. callee name when `node` is `f(...)` / `x = f(...)` / `x = await f(...)` with a read-ish f, else None."""
    val = node.value if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
    if isinstance(val, ast.Await):
        val = val.value
    if not isinstance(val, ast.Call):
        return None
    f = val.func
    nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    return nm if _NV_READ_RE.match(nm or "") else None


def nversion_variants(base_text, applied_text):
    """PURE. [(description, variant_text)] -- up to NVERSION_MAX_VARIANTS Python variants of the refimpl-applied target, each
    duplicating one refimpl-ADDED read statement. [] when not Python / nothing qualifies."""
    try:
        tree = ast.parse(applied_text or "")
    except SyntaxError:
        return []
    added = _added_lines(base_text, applied_text)
    lines = (applied_text or "").splitlines(keepends=True)
    out, seen = [], set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for st in ast.walk(fn):
            if not isinstance(st, (ast.Expr, ast.Assign)) or st.lineno not in added or st.end_lineno is None:
                continue
            if any(l not in added for l in range(st.lineno, st.end_lineno + 1)):
                continue
            nm = _stmt_read_callee(st)
            if not nm or nm in seen:
                continue
            seen.add(nm)
            seg = lines[st.lineno - 1:st.end_lineno]
            if not seg[-1].endswith("\n"):
                seg[-1] += "\n"
            variant = "".join(lines[:st.end_lineno]) + "".join(seg) + "".join(lines[st.end_lineno:])
            try:
                ast.parse(variant)
            except SyntaxError:
                continue
            out.append((f"{nm}(...) called twice at line {st.lineno} of `{fn.name}` (extra idempotent read)", variant))
            if len(out) >= NVERSION_MAX_VARIANTS:
                return out
    return out


def nversion_findings(wt, target, base_text, applied_text, run_verify):
    """[finding] -- run `run_verify()` -> (ok: bool, output: str) once per variant with the variant written over `target`;
    the applied text is ALWAYS restored. A failing variant = the fixture is over-specified (see block comment)."""
    wt = Path(wt)
    p = wt / target
    out = []
    for desc, vtext in nversion_variants(base_text, applied_text):
        try:
            p.write_text(vtext)
            ok, log = run_verify()
        finally:
            p.write_text(applied_text)
        if not ok:
            first = next((l.strip() for l in (log or "").splitlines() if re.search(r"FAIL|Error|assert", l, re.I)), "")[:160]
            out.append(_finding(
                NVERSION_VARIANT_FAILS, "An equivalent implementation fails the fixture (over-specified)", target,
                f"variant: {desc}. The fixture is green on the refimpl but RED on this equivalent one ({first or 'see verify output'}): "
                f"it pins refimpl internals (an exact call count / call order on a read). If TASK.md states that count, ignore; "
                f"otherwise loosen the assertion to >=/<= or state the contract"))
    return out


def refimpl_literal_gap_findings(pairs, texts_by_file, default_text: str = ""):
    """[finding] -- (b) literals absent from the refimpl-APPLIED tree. `pairs` =
    must_contain_literals(); `texts_by_file` {path: applied text}; unpinned literals are
    checked against `default_text`."""
    gap = []
    for f, lit in pairs or []:
        body = texts_by_file.get(f, "") if f else default_text
        if not count_occurrences(lit, body or ""):
            gap.append((f, lit))
    if not gap:
        return []
    names = ", ".join(repr(l) for _f, l in gap[:6])
    return [_finding(
        REFIMPL_LITERAL_ABSENT, "Must-contain literal absent after the refimpl is applied", "TASK.md",
        f"{len(gap)} literal(s) are absent from the refimpl-applied tree: {names}. The spec and "
        f"the reference impl disagree; another model round cannot fix a literal the refimpl "
        f"never writes -- correct the spec (or the refimpl) deterministically.")]


_MISSING_LIT_RE = re.compile(r"MISSING literal in (\S+) \([^\n]*?\):\s*>>>(.*?)<<<")


def missing_literals_in_text(text: str):
    """PURE. frozenset of (file, literal) from check_literals.py's `MISSING literal in <file>
    (... >>>LIT<<<)` lines in a self-check / worker log."""
    return frozenset((m.group(1), m.group(2)) for m in _MISSING_LIT_RE.finditer(text or ""))


def repeat_missing_literals(prev, cur):
    """PURE. True when two consecutive failed rounds carry the identical NON-EMPTY missing-literal
    set: the same literal(s) defeated a whole model round twice -- stop spending rounds."""
    return bool(prev) and bool(cur) and frozenset(map(tuple, prev)) == frozenset(map(tuple, cur))


def format_repeat_missing(cur) -> str:
    names = ", ".join(repr(l) for _f, l in sorted(cur)[:6])
    return (f"{SPEC_DEFECT_PREFIX}Must-contain literal(s) never satisfied: two consecutive "
            f"rounds failed on the identical missing-literal set ({names}) -- re-spec or park; "
            f"another model round will not change it")


# ---------------------------------------------------------------------------
# HARNESS-AUTHORING DEFECT LINTS (2026-10-11). Four defects that each burned a model round this week:
#   REFIMPL_SWALLOWS_EXCEPTION   refimpl.py wraps the code under test in a broad `except:` / `except Exception: pass`
#                                (arr-webhook: `target.session` was a NameError, swallowed -> every case returned the
#                                same empty value and the fixture looked "wrong").
#   JS_FIXTURE_LOAD_TRAP         a JS/TS fixture that crashes at LOAD: require() of a .ts module beside mock.module,
#                                `globalThis.mock.module`, or `mock.module`/`test(` with no import of node:test.
#   EVALUATE_NOT_SELF_CONTAINED  a function handed to page.evaluate() (serialized, rebuilt without its closure) calls a
#                                module-scope helper -> ReferenceError in real Chromium (cardlink s2 extractDetailInBrowser).
#   REAL_TIME_POLLING_FIXTURE    verify.sh caps each test at 10s; a fixture that really sleeps (or drives polling code
#                                with no virtual/compressed timers) blows it.
# All are narrow static checks (comment-blanked); a near-miss never flags. Related existing checks and why they do not
# conflict: _mock_module_lint (ollama-dispatch-auto self-check) lints mock.module OPTIONS / static target import /
# re-mocking -- this one only the load-crash shapes it does not look at; the working recipe added to the authoring prompt
# (HARNESS_RECIPES_RULE) says "use mock.module (MOCK_MODULE_RULE) for ESM, Module._load stub for CJS require()" so no rule
# demands what another forbids.
REFIMPL_SWALLOWS_EXCEPTION = "REFIMPL_SWALLOWS_EXCEPTION"
JS_FIXTURE_LOAD_TRAP = "JS_FIXTURE_LOAD_TRAP"
EVALUATE_NOT_SELF_CONTAINED = "EVALUATE_NOT_SELF_CONTAINED"
REAL_TIME_POLLING_FIXTURE = "REAL_TIME_POLLING_FIXTURE"
_JS_FIXTURES = ("verify.test.ts", "verify.test.mts", "verify.test.js", "verify.test.mjs")
_BROAD_EXC = {"Exception", "BaseException", "NameError", "AttributeError"}


def _blank_js_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/|(?<![:'\"`\\])//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.S)


def _handler_names(h) -> set:
    t = h.type
    if t is None:
        return {"<bare>"}
    nodes = t.elts if isinstance(t, ast.Tuple) else [t]
    return {getattr(n, "id", getattr(n, "attr", "?")) for n in nodes}


def _handler_swallows(h) -> bool:
    """True when the handler body neither re-raises nor exits nor reports (pass / continue / break / return <const>)."""
    for n in ast.walk(ast.Module(body=h.body, type_ignores=[])):
        if isinstance(n, ast.Raise):
            return False
        if isinstance(n, ast.Call):
            f = n.func
            nm = getattr(f, "id", getattr(f, "attr", ""))
            if nm in ("exit", "_exit", "print", "write", "warn", "error", "exception", "fail", "abort"):
                return False
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            return False                   # records the failure / sets state: a decision, not a swallow
    for n in h.body:
        if isinstance(n, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(n, ast.Return) and (n.value is None or isinstance(n.value, ast.Constant)
                                          or isinstance(n.value, (ast.List, ast.Dict, ast.Tuple, ast.Set))):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue
        return False
    return True


def refimpl_swallow_findings(refimpl_text: str):
    """[finding] -- a broad except (bare / Exception / BaseException / NameError / AttributeError) that swallows, around a
    try body that does more than plain file/path I/O."""
    try:
        tree = ast.parse(refimpl_text or "")
    except SyntaxError:
        return []
    io_names = {"Path", "open", "read_text", "write_text", "exists", "is_file", "is_dir", "unlink", "remove", "rmtree", "mkdir",
                "makedirs", "chmod", "stat", "listdir", "glob", "rglob", "iterdir", "resolve", "copy", "copy2", "move",
                "rename", "replace", "write", "read", "close", "loads", "load", "dumps", "dump", "relative_to", "kill",
                "run", "check_output", "Popen", "getenv", "environ", "get", "decode", "encode", "int", "float", "str"}
    out, seen = [], set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            names = _handler_names(h)
            if not (names & ({"<bare>"} | _BROAD_EXC)) or not _handler_swallows(h):
                continue
            calls = {getattr(n.func, "id", getattr(n.func, "attr", "")) for b in node.body for n in ast.walk(b)
                     if isinstance(n, ast.Call)}
            def _root(a):
                while isinstance(a, (ast.Attribute, ast.Call, ast.Subscript)):
                    a = a.value if isinstance(a, (ast.Attribute, ast.Subscript)) else a.func
                return getattr(a, "id", "")
            attrs = [n for b in node.body for n in ast.walk(b) if isinstance(n, ast.Attribute)
                     and _root(n) not in ("os", "sys", "Path", "re", "json", "shutil", "subprocess", "time", "tempfile")]
            if not (calls - io_names) and not attrs:
                continue                  # try body is only file/path/json I/O: not "the code under test"
            if h.lineno in seen:
                continue
            seen.add(h.lineno)
            what = "bare `except:`" if "<bare>" in names else "`except " + "/".join(sorted(names)) + "`"
            out.append(_finding(
                REFIMPL_SWALLOWS_EXCEPTION, "refimpl swallows exceptions around the code it writes", "refimpl.py",
                f"line {h.lineno}: {what} with a pass/default body hides a NameError/AttributeError/typo in the refimpl "
                f"itself, so every case then returns the same empty value and the fixture looks wrong (arr-webhook: "
                f"`target.session` did not exist). Catch the one specific exception you expect, or let it raise"))
    return out[:4]


_TRAP_TEST_USE_RE = re.compile(r"(?<![\w$.])(?:test|describe|it)\s*(?:\.\w+)?\s*\(")
_TRAP_MOCK_USE_RE = re.compile(r"(?<![\w$.])mock\s*\.\s*(?:module|fn|method|timers)\b")


def _binds(src: str, name: str) -> bool:
    return bool(re.search(r"\bimport\b[^;]*?\b" + name + r"\b[^;]*?\bfrom\b", src, re.S)
                or re.search(r"\b(?:const|let|var)\s*\{[^}]*\b" + name + r"\b[^}]*\}\s*=", src)
                or re.search(r"\b(?:const|let|var)\s+" + name + r"\s*=", src)
                or re.search(r"\bfunction\s+" + name + r"\s*\(", src))


def js_fixture_trap_findings(name: str, src: str):
    """[finding] -- crash-at-load shapes in one JS/TS fixture's text."""
    s = _blank_js_comments(src or "")
    out, msgs = [], []
    has_mm = bool(re.search(r"(?<![\w$.])mock\s*\.\s*module\s*\(", s)) or "globalThis.mock" in s
    if re.search(r"\b(?:globalThis|global)\s*\.\s*mock\b", s):
        msgs.append("`globalThis.mock` does not exist (mock is exported by node:test, not a global): "
                    "`import { test, mock } from 'node:test'` and call `mock.module(...)`")
    if has_mm and re.search(r"\brequire\s*\(\s*['\"`][^'\"`]+\.m?ts['\"`]\s*\)", s):
        msgs.append("require() of a .ts module beside mock.module: a .ts ESM target cannot be require()d (ERR_REQUIRE_ESM / "
                    "undefined exports) and mock.module does not intercept it. Use `await import()` after module-scope "
                    "mocks, or for a CJS target stub the dependency with the Module._load recipe")
    for ident, use_re in (("mock", _TRAP_MOCK_USE_RE), ("test", _TRAP_TEST_USE_RE)):
        if use_re.search(s) and not _binds(s, ident) and not (ident == "test" and (_binds(s, "describe") and _binds(s, "it"))):
            if ident == "test" and re.search(r"\bfrom\s+['\"](?:vitest|@jest/globals|bun:test)['\"]", s):
                continue
            msgs.append(f"`{ident}` is used but never imported/declared (`import {{ test, mock }} from 'node:test'`): the "
                        f"fixture dies with ReferenceError before running a single case")
    for m in msgs:
        out.append(_finding(JS_FIXTURE_LOAD_TRAP, "JS/TS fixture crashes at load", name, m))
    return out


# --- page.evaluate self-containment ---------------------------------------------------------------------------
_EVAL_CALL_RE = re.compile(r"\.\s*(?:evaluate|evaluateHandle|\$eval|\$\$eval|waitForFunction)\s*\(\s*")
_TOP_FN_RE = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(|"
                        r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)\n]*\)\s*(?::[^=\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)", re.M)
_JS_SKIP = {"if", "for", "while", "switch", "catch", "return", "function", "typeof", "new", "await", "async", "super"}


def _match_close(s: str, i: int) -> int:
    """index of the bracket closing the one opening at s[i]; strings/templates skipped (regex literals not modelled)."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    depth, j, n, q = 0, i, len(s), None
    while j < n:
        c = s[j]
        if q:
            if c == "\\":
                j += 2
                continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c in pairs:
            depth += 1
        elif c in ")}]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _fn_span(s: str, name: str):
    m = re.search(r"(?:function\s*\*?\s*" + re.escape(name) + r"\s*\(|(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*(?:async\s*)?(?:function\b[^(]*\(|\())", s)
    if not m:
        return None
    p = m.end() - 1
    e = _match_close(s, p)
    if e < 0:
        return None
    b = re.compile(r"\s*(?::[^{=]+)?\s*(?:=>)?\s*").match(s, e + 1)
    k = b.end() if b else e + 1
    if k < len(s) and s[k] == "{":
        c = _match_close(s, k)
        return (p, c + 1) if c > 0 else None
    return (p, min(len(s), s.find("\n", k) if s.find("\n", k) > 0 else len(s)))


def _inline_span(s: str, i: int):
    """span of an inline function/arrow argument starting at s[i], else None."""
    m = re.compile(r"(?:async\s*)?(?:function\b[^(]*\(|\()").match(s, i)
    if not m:
        m2 = re.compile(r"(?:async\s*)?[A-Za-z_$][\w$]*\s*=>").match(s, i)
        if not m2:
            return None
        k = m2.end()
        while k < len(s) and s[k] in " \t\n":
            k += 1
        if k < len(s) and s[k] == "{":
            c = _match_close(s, k)
            return (i, c + 1) if c > 0 else None
        return None
    e = _match_close(s, m.end() - 1)
    if e < 0:
        return None
    b = re.compile(r"\s*(?:=>)?\s*").match(s, e + 1)
    k = b.end()
    if k < len(s) and s[k] == "{":
        c = _match_close(s, k)
        return (i, c + 1) if c > 0 else None
    d, j = 0, k
    while j < len(s):                      # expression body: ends at the evaluate call's `)` or a depth-0 `,`
        c = s[j]
        if c in "([{":
            d += 1
        elif c in ")]}":
            if d == 0:
                break
            d -= 1
        elif c == "," and d == 0:
            break
        j += 1
    return (i, j)


def evaluate_closure_findings(path: str, src: str):
    """[finding] -- a function serialized by page.evaluate()/$eval()/waitForFunction() that calls a module-scope helper
    function of the same file (not declared inside it): ReferenceError in real Chromium."""
    s = _blank_js_comments(src or "")
    helpers = {m.group(1) or m.group(2) for m in _TOP_FN_RE.finditer(s)}
    helpers.discard(None)
    if not helpers:
        return []
    out, seen = [], set()
    for m in _EVAL_CALL_RE.finditer(s):
        i = m.end()
        im = re.compile(r"([A-Za-z_$][\w$]*)\s*[,)]").match(s, i)
        if im and im.group(1) in helpers:
            span, label = _fn_span(s, im.group(1)), im.group(1)
        else:
            span, label = _inline_span(s, i), "(inline function)"
            if label == "(inline function)" and span is None:
                continue
        if not span:
            continue
        body = s[span[0]:span[1]]
        # names declared inside the serialized function shadow module helpers
        local = set(re.findall(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)", body[1:])) | \
            set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", body))
        for d in re.findall(r"\b(?:const|let|var)\s*[\[{]([^\]}=]*)[\]}]", body):
            local |= {x.strip().split(":")[-1].strip() for x in d.split(",")}
        head = body[:body.find(")") + 1] if ")" in body else ""
        local |= set(re.findall(r"[A-Za-z_$][\w$]*", head))
        for h in sorted(helpers - local - _JS_SKIP - ({im.group(1)} if im else set())):
            if re.search(r"(?<![\w$.])" + re.escape(h) + r"\s*\(", body) and (label, h) not in seen:
                seen.add((label, h))
                out.append(_finding(
                    EVALUATE_NOT_SELF_CONTAINED, "function passed to page.evaluate is not self-contained", path,
                    f"`{label if label != '(inline function)' else 'the inline evaluate callback'}` is serialized by "
                    f"page.evaluate and re-created in the browser WITHOUT its closure, but it calls module-scope helper "
                    f"`{h}(...)` -> ReferenceError in real Chromium (a fake page that calls it in-process hides this). "
                    f"Inline the helper's logic inside the function, or pass the data in as an argument"))
    return out[:4]


# --- real-time polling in fixtures -------------------------------------------------------------------------------
_RT_SLEEP_JS_RE = re.compile(r"setTimeout\s*\(\s*(?:[A-Za-z_$][\w$]*|\(\)\s*=>\s*[A-Za-z_$][\w$]*\(\)|[^,()]*)\s*,\s*(\d[\d_]*)\s*\)|"
                             r"\b(?:sleep|delay|wait|waitForTimeout)\s*\(\s*(\d[\d_]*)\s*\)")
_RT_VIRT_JS_RE = re.compile(r"mock\.timers|useFakeTimers|installFakeClock|fakeTimers|\b(?:globalThis|global)\s*\.\s*setTimeout\s*=|"
                            r"\bsetTimeout\s*=\s*|clock\b|virtual|sleep\s*:|sleepFn|\.tick\s*\(|compress", re.I)
_RT_VIRT_PY_RE = re.compile(r"monkeypatch|mock\.patch|patch\s*\(|freezegun|time\.sleep\s*=|\bsleep\s*=|fake_?sleep|virtual|clock|compress", re.I)
_RT_POLL_JS_RE = re.compile(r"(?:for|while)\s*\([^)]*\)\s*\{[^{}]{0,600}?(?:setTimeout|waitForTimeout|\bsleep\s*\(|\bdelay\s*\()|"
                            r"(?:setTimeout|waitForTimeout|\bsleep\s*\(|\bdelay\s*\()[^{}]{0,200}?\}\s*(?:while|for)\b", re.S)
_RT_POLL_PY_RE = re.compile(r"(?:for|while)\b[^\n]*:\n(?:[^\n]*\n){0,12}?[ \t]+(?:await\s+asyncio\.sleep|time\.sleep)\s*\(", re.M)


def real_time_polling_findings(fixture_name: str, fixture_src: str, target_text: str = ""):
    """[finding] -- (a) the fixture itself sleeps >= 1s of real time; (b) the code under test polls (loop around a
    sleep) and the fixture installs no virtual / compressed timers. verify.sh caps each test at 10s."""
    py = fixture_name.endswith(".py")
    fx = fixture_src or ""
    if not py:
        fx = _blank_js_comments(fx)
    msg = None
    if py:
        for m in re.finditer(r"\btime\.sleep\s*\(\s*(\d+(?:\.\d+)?)\s*\)", fx):
            if float(m.group(1)) >= 1 and not _RT_VIRT_PY_RE.search(fx):
                msg = f"the fixture calls time.sleep({m.group(1)}) in real time"
                break
        poll = _RT_POLL_PY_RE.search(target_text or "")
        virt = _RT_VIRT_PY_RE.search(fx)
    else:
        for m in _RT_SLEEP_JS_RE.finditer(fx):
            ms = int((m.group(1) or m.group(2)).replace("_", ""))
            if ms >= 1000 and not re.search(r"\btest\s*\(\s*['\"`][^\n]*timeout", fx):
                msg = f"the fixture sleeps {ms} ms of real time in one step"
                break
        poll = _RT_POLL_JS_RE.search(target_text or "")
        virt = _RT_VIRT_JS_RE.search(fx)
    if msg is None and poll and not virt:
        msg = ("the code under test polls (a loop around a sleep) and the fixture installs no virtual/compressed timers, "
               "so each case sleeps in real time")
    if not msg:
        return []
    return [_finding(REAL_TIME_POLLING_FIXTURE, "fixture uses real-time sleeps (verify.sh caps each test at 10s)", fixture_name,
                     msg + ". Compress or virtualize every timer (override globalThis.setTimeout to ms/50, or inject a "
                     "fake clock / patch time.sleep) keeping attempt COUNTS unchanged")]


def harness_authoring_findings(wt, target_text: str = ""):
    """[finding] -- the four lints above over one worktree's authored files. `target_text` is the target file text
    (ideally refimpl-applied) for the evaluate check; the polling check reads refimpl.py (the polling code the task
    INTRODUCES -- a whole-file scan would flag every daemon loop in a big target)."""
    wt = Path(wt)
    out = []
    ri = _read(wt / "refimpl.py")
    if ri and not _REFIMPL_PLACEHOLDER_RE.search(ri):
        out += refimpl_swallow_findings(ri)
    for fx in _JS_FIXTURES + ("test_fixture.py", "verify.py"):
        p = wt / fx
        if not p.is_file():
            continue
        src = _read(p)
        if fx in _JS_FIXTURES:
            out += js_fixture_trap_findings(fx, src)
        out += real_time_polling_findings(fx, src, ri)
    if target_text:
        out += evaluate_closure_findings("target", target_text)
    return out


def spec_defects(wt, target=None, creation=None, intent_text="", task_text="", refimpl_text=None,
                 verify_green=None, ref: str = "HEAD"):
    """THE CONTRADICTION GATE. [finding] where each finding is
    {code, reason, path, message}; empty = the spec does not contradict the base.

    Static (no execution): (a) CREATION_TARGET_EXISTS, (c) REFIMPL_ANCHOR_UNMATCHED (only
    when a refimpl is given AND the task is a creation task -- edit tasks' anchors are
    harness-complete's job). Needs the measured verdict: (b) ALREADY_SATISFIED.
    `creation=None` reads the worktree manifest; `intent_text` defaults to TASK.md's
    `## Required change`."""
    wt = Path(wt)
    man = _manifest(wt)
    task = task_text or _read(wt / "TASK.md")
    if creation is None:
        creation = bool(man.get("creation_task"))
    target = target or man.get("target")
    intent = intent_text or _required_change_text(task)
    out = list(base_drift_findings(wt))
    out += creation_target_findings(wt, target, creation, intent, ref)
    if refimpl_text is None:
        refimpl_text = _read(wt / "refimpl.py")
    if creation and refimpl_text:
        out += refimpl_base_anchor_findings(wt, target, refimpl_text, ref)
    out += already_satisfied_findings(wt, task, target, verify_green, ref)
    out += literal_near_miss_findings(wt, task, target, refimpl_text or "", ref)
    out += literal_shape_findings(task)
    out += harness_authoring_findings(wt)
    out += spec_test_parity_findings(wt, task)
    out += base_poison_findings(wt, target)
    return out


def format_spec_defects(findings) -> str:
    """One precise line a parked slice / log carries: `SPEC_DEFECT: <reason>: <path> -- <msg>`."""
    if not findings:
        return ""
    return "; ".join(f"{SPEC_DEFECT_PREFIX}{f['reason']}: {f['path']} -- {f['message']}"
                     for f in findings)


# ---------------------------------------------------------------------------
# 9. BASELINE-RED CLASSIFICATION (review steal #2)
# ---------------------------------------------------------------------------
# RED at the baseline must be red for the RIGHT reason. A verify that crashes on an import /
# syntax / unrelated TypeError "fails" for a reason that has nothing to do with the property,
# and would also fail for the refimpl-less model forever. What counts as red:
#   * an ASSERTION failure (AssertionError / ERR_ASSERTION / a verify.sh `FAIL:` line)  -> yes;
#   * a missing module / missing export / `x is not a function` -> yes ONLY when the task
#     CREATES the thing (a creation task, whose stub exports nothing) or the missing name is
#     NAMED in the spec text (the feature symbol the task adds);
#   * SyntaxError / TS compile error / ReferenceError / TypeError / ENOENT / command not found
#     / any other crash, or a missing name the spec never mentions                      -> NO;
#   * output with no recognisable signal at all (stderr discarded, bespoke runner)     -> unclassified, accepted
#     (never fail closed on text we cannot read).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ASSERT_RE = re.compile(r"AssertionError|ERR_ASSERTION|^\s*E\s+assert\b|^\s*(?:AssertionError|assertion failed)",
                        re.M | re.I)
_FAIL_LINE_RE = re.compile(r"^\s*FAIL(?:ED)?\s*[:\-]\s*(.+)$", re.M)
_FAIL_LINE_ENV = re.compile(r"npm |node_modules|install|bootstrap|not on PATH|PATH|tsc\b|prisma|"
                            r"command not found|No such file|cannot find|timed out", re.I)
_MISSING_EXPORT_RES = (
    re.compile(r"does not provide an export named ['\"`]?([\w$]+)"),
    re.compile(r"cannot import name ['\"`]?([\w$]+)", re.I),
    re.compile(r"has no attribute ['\"`]?([\w$]+)", re.I),
    re.compile(r"([\w$.]+) is not a function"),
    re.compile(r"([\w$.]+) is not (?:a constructor|callable|iterable)"),
    re.compile(r"TS(?:2305|2724|2614|2339)\b.*?['\"`]([\w$]+)['\"`]"),
    re.compile(r"No matching export in .*? for import ['\"`]([\w$]+)"),
)
_MISSING_MODULE_RES = (
    re.compile(r"ModuleNotFoundError: No module named ['\"`]([^'\"`]+)"),
    re.compile(r"Cannot find (?:module|package) ['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"ERR_MODULE_NOT_FOUND.*?['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"TS2307\b.*?['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"ImportError: No module named ['\"`]?([\w.]+)"),
    re.compile(r"MODULE_NOT_FOUND"),
)
_BAD_CRASH_RES = (
    (re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b"), "syntax error"),
    (re.compile(r"\berror TS\d{4}\b"), "TypeScript compile error"),
    (re.compile(r"\bReferenceError\b|\bNameError\b"), "reference error"),
    (re.compile(r"\bTypeError\b|\bRangeError\b|\bKeyError\b|\bIndexError\b|\bValueError\b|"
                r"\bAttributeError\b|\bZeroDivisionError\b"), "runtime error"),
    # NB: a bare "Traceback (most recent call last)" header is NOT a signal -- the exception
    # line under it is, and it is classified above (missing symbol / assertion / error class).
    (re.compile(r"\bENOENT\b|command not found|Segmentation fault|UnhandledPromiseRejection"), "crash"),
)


def _ident_in(name: str, spec_text: str) -> bool:
    base = (name or "").split(".")[-1]
    base = re.sub(r"^.*/", "", base)
    base = re.sub(r"\.\w{1,5}$", "", base) if "/" in (name or "") else base
    return bool(base) and bool(re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(base), spec_text or ""))


def classify_baseline_red(out: str, creating: bool = False, spec_text: str = ""):
    """PURE. Classify a non-zero baseline verify run.

    Returns {"ok": bool, "kind": "assertion"|"expected-missing"|"crash"|"unclassified",
             "signals": [str]}. ok=False means the red is for the WRONG reason and must not
    count as 'fails before' (see the policy block above)."""
    t = _ANSI.sub("", out or "")
    if _ASSERT_RE.search(t):
        return {"ok": True, "kind": "assertion", "signals": ["assertion failure"]}
    for m in _FAIL_LINE_RE.finditer(t):
        if not _FAIL_LINE_ENV.search(m.group(1)):
            return {"ok": True, "kind": "assertion", "signals": ["verify FAIL: " + m.group(1).strip()[:80]]}
    signals, good, bad = [], [], []
    for ln in t.splitlines():
        hit = False
        for rx in _MISSING_EXPORT_RES:
            m = rx.search(ln)
            if m:
                name = m.group(1)
                (good if creating or _ident_in(name, spec_text) else bad).append(
                    f"missing symbol {name!r}" + ("" if creating else " (not named in the spec)"))
                hit = True
                break
        if hit:
            continue
        for rx in _MISSING_MODULE_RES:
            m = rx.search(ln)
            if m:
                name = m.group(1) if m.groups() else "?"
                (good if creating or _ident_in(name, spec_text) else bad).append(
                    f"missing module {name!r}" + ("" if creating else " (not named in the spec)"))
                hit = True
                break
        if hit:
            continue
        for rx, label in _BAD_CRASH_RES:
            if rx.search(ln):
                bad.append(f"{label}: {ln.strip()[:100]}")
                break
    if bad:
        return {"ok": False, "kind": "crash", "signals": (bad + good)[:5]}
    if good:
        return {"ok": True, "kind": "expected-missing", "signals": good[:5]}
    return {"ok": True, "kind": "unclassified", "signals": []}


# ---------------------------------------------------------------------------
# 10. FAILURE SIGNATURE (review steal #4: abandon after 3 identical failures)
# ---------------------------------------------------------------------------
_CASE_NAME_RES = (re.compile(r"^\s*not ok \d+ - (.+?)\s*(?:#.*)?$", re.M),
                  re.compile(r"^\s*(?:FAIL|FAILED)\s*[:\-]\s*(.+)$", re.M),
                  re.compile(r"^\s*(?:✖|✗|×)\s+(.+?)(?:\s+\([\d.]+ms\))?$", re.M))


def failure_signature(text: str) -> str:
    """Stable fingerprint of a failure: first assertion line + sorted failing case names, with
    numbers/addresses/paths-timestamps normalised so run-to-run noise does not change it.
    '' when `text` carries no failure evidence."""
    import hashlib
    t = _ANSI.sub("", text or "")
    first = ""
    for ln in t.splitlines():
        if re.search(r"AssertionError|ERR_ASSERTION|\bExpected\b|\bexpected\b.*\b(?:got|received|to)\b|"
                     r"^\s*E\s+assert", ln):
            first = ln.strip()
            break
    cases = sorted({c.strip() for rx in _CASE_NAME_RES for c in rx.findall(t)})
    if not first and not cases:
        return ""
    norm = re.sub(r"0x[0-9a-f]+|\b\d+(?:\.\d+)?ms\b|\b\d{4}-\d\d-\d\dT[\d:.]+Z?|(?:/[\w.-]+){2,}|:\d+(?::\d+)?\b", "#", first)
    return hashlib.sha1((norm + "\0" + "\0".join(cases)).encode()).hexdigest()[:16]


ABANDON_AFTER = 3


def repeat_failure_state(prev_sig, sig, count, cap: int = ABANDON_AFTER):
    """PURE. ('continue'|'hint'|'abandon', new_count). `count` = how many CONSECUTIVE rounds
    (including the previous one) already carried `prev_sig`. An empty sig never counts."""
    if not sig:
        return "continue", 0
    n = count + 1 if sig == prev_sig else 1
    if n >= cap:
        return "abandon", n
    return ("hint" if n >= 2 else "continue"), n


# ---------------------------------------------------------------------------
# 11. DETERMINISTIC BASE RESOLUTION (smoke report defect 2, 2026-10-09)
# ---------------------------------------------------------------------------
# The scaffold's _fresh_base_ref silently re-bases to origin/main when HEAD is behind, and its
# unlocked `git fetch origin` collided when 3 dispatches started at once (ref-lock error ->
# "cutting from HEAD" fallback): the same call resolved to DIFFERENT bases. Auto now resolves
# the base itself, ONCE, and hands the sha to the scaffold as --baseline-ref:
#   * explicit --baseline-ref  -> that commit, NO network fetch;
#   * default                  -> HEAD, unless HEAD is strictly behind origin's default branch,
#     decided after a fetch taken under a per-repo flock (collisions serialise, none fall back).
# The resolved sha is recorded in .dispatch-harness.json (`base_sha`) so a later gate can
# compare it with the tree it is looking at (base_drift_findings).
def _rev(repo, ref):
    r = _git_run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.decode().strip() if r is not None and r.returncode == 0 else ""


def resolve_base_sha(repo, explicit=None, fetch_timeout=90, lock_timeout=240):
    """(sha, note). Raises ValueError for an explicit ref that is not a commit."""
    repo = Path(repo)
    if explicit:
        sha = _rev(repo, explicit)
        if not sha:
            raise ValueError(f"--baseline-ref {explicit!r} is not a commit in {repo}")
        return sha, f"explicit --baseline-ref {explicit} -> {sha[:12]} (no fetch)"
    head = _rev(repo, "HEAD")
    if not head:
        raise ValueError(f"{repo} has no HEAD commit")
    import fcntl
    gd = _git_run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    lockdir = Path(gd.stdout.decode().strip()) if gd is not None and gd.returncode == 0 else repo
    fh = None
    try:
        fh = open(lockdir / "ods-base-fetch.lock", "a+")
        deadline = time.time() + lock_timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    return head, f"fetch lock busy > {lock_timeout}s -- using HEAD {head[:12]}"
                time.sleep(0.2)
        try:
            f = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"],
                               capture_output=True, text=True, timeout=fetch_timeout)
            if f.returncode != 0:
                return head, f"fetch origin failed ({(f.stderr or '').strip()[:100]}) -- using HEAD {head[:12]}"
        except (OSError, subprocess.SubprocessError) as e:
            return head, f"fetch origin failed ({type(e).__name__}) -- using HEAD {head[:12]}"
        sym = _git_run(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        cands = [sym.stdout.decode().strip()] if sym is not None and sym.returncode == 0 and sym.stdout.strip() else []
        cands += ["origin/main", "origin/master"]
        remote = next((c for c in cands if _rev(repo, c)), None)
        if not remote:
            return head, f"no origin default branch -- using HEAD {head[:12]}"
        rsha = _rev(repo, remote)
        if rsha == head:
            return head, f"HEAD is {remote} ({head[:12]})"
        behind = _git_run(repo, "merge-base", "--is-ancestor", head, rsha)
        if behind is not None and behind.returncode == 0:
            return rsha, f"HEAD {head[:12]} is behind {remote} {rsha[:12]} -- base {rsha[:12]}"
        return head, f"HEAD {head[:12]} is ahead of/diverged from {remote} -- base HEAD"
    finally:
        if fh is not None:
            try:
                fh.close()           # releases the flock
            except OSError:
                pass


def record_base_sha(wt, sha, note="", probe=None):
    """Merge `base_sha` (+ note, optional baseline_probe) into the worktree's harness manifest."""
    p = Path(wt) / ".dispatch-harness.json"
    d = _manifest(Path(wt))
    d["base_sha"] = sha
    if note:
        d["base_note"] = note
    if probe is not None:
        d["baseline_probe"] = probe
    p.write_text(json.dumps(d, indent=2))


def base_drift_findings(wt):
    """[finding] -- the recorded base_sha must be an ANCESTOR of (or equal to) the tree's HEAD
    (seal commits legitimately advance HEAD; a re-base/reset does not). No base_sha = no
    finding (older harnesses)."""
    wt = Path(wt)
    base = _manifest(wt).get("base_sha")
    if not base:
        return []
    r = _git_run(wt, "merge-base", "--is-ancestor", base, "HEAD")
    if r is not None and r.returncode == 0:
        return []
    head = _rev(wt, "HEAD")
    return [_finding("BASE_DRIFT", "worktree HEAD is not descended from the recorded base", base[:12],
                     f"harness metadata says the spec was checked against base {base[:12]} but "
                     f"HEAD is {head[:12] or '?'}, which does not contain it: the tree was "
                     f"re-based/reset after the spec was checked, so every base-relative "
                     f"verdict is stale. Re-run from the resolved base.")]


# ---------------------------------------------------------------------------
# ORIGINAL-BASE RECORD + BASE POISON (2026-10-11). arr-webhook job 8557454d853b failed; auto-fix r1 (950dffa5697f)
# then SEALED the model's partial 96-line diff into HEAD (queue seal_prev_round_baseline, "auto: seal round baseline"),
# so every later round measured "baseline" against HEAD = base + the model's own partial work: failure counts were
# understated (cardlink s2: queue said 2 failures, real 3). Sealing a continuation round's work is deliberate (Cause 1:
# a dirty launch tree makes the verdict UNTRUSTED), so the seal stays -- but the ORIGINAL clean base is now
#   * recorded at the FIRST seal (idempotent, never overwritten) in `<git-dir>/dispatch-orig-base` -- outside the
#     worktree, so it can never dirty the tree or be edited by the model;
#   * the reference for "baseline" measurements of the target (clean_target_ref: the parent of the first seal commit that
#     touched the target) -- consumed by auto-harness-check's baseline reset;
#   * checked by BASE_POISONED_BY_SEAL at admission: seal commits that changed the declared target sit between the
#     recorded original base and HEAD.
# Detection keys on the seal commits themselves (subject prefix + the target in their diff), not on `diff orig..HEAD`,
# so a legitimate base refresh (refresh_stale_base / origin moved) never reads as poison.
# ---------------------------------------------------------------------------
BASE_POISONED_BY_SEAL = "BASE_POISONED_BY_SEAL"
SEAL_SUBJECT_PREFIX = "auto: seal round baseline"


def orig_base_path(wt):
    """git-dir/dispatch-orig-base for the worktree (per-worktree git dir for linked worktrees), or None."""
    r = _git_run(wt, "rev-parse", "--absolute-git-dir")
    if r is None or r.returncode != 0:
        return None
    return Path(r.stdout.decode().strip()) / "dispatch-orig-base"


def read_orig_base(wt):
    """The recorded original clean base SHA, or ''."""
    p = orig_base_path(wt)
    try:
        v = p.read_text().split()[0] if p and p.is_file() else ""
    except (OSError, IndexError):
        return ""
    return v if re.fullmatch(r"[0-9a-f]{7,64}", v or "") else ""


def record_orig_base(wt, sha=None):
    """Record the original clean base ONCE (first seal). Idempotent: an existing record is never overwritten. Returns
    the SHA now on record ('' when it could not be determined/written). Fail-open."""
    cur = read_orig_base(wt)
    if cur:
        return cur
    # no explicit sha: HEAD, unless seal commits already moved the target (legacy worktree) -> the pre-poison commit
    sha = sha or _rev(wt, clean_target_ref(wt, _manifest(Path(wt)).get("target")))
    p = orig_base_path(wt)
    if not sha or p is None:
        return ""
    try:
        p.write_text(sha + "\n")
    except OSError:
        return ""
    return sha


def seal_commits_touching(wt, target):
    """[(sha, +added, -deleted)] oldest first: queue seal commits reachable from HEAD that changed `target`."""
    if not target:
        return []
    r = _git_run(wt, "log", "--reverse", "--no-merges", "--format=%H%x00%s", "-n", "200", "--", str(target))
    out = []
    if r is None or r.returncode != 0:
        return out
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        sha, _, subj = line.partition("\x00")
        if not subj.startswith(SEAL_SUBJECT_PREFIX):
            continue
        ns = _git_run(wt, "show", "--numstat", "--format=", sha, "--", str(target))
        a = d = 0
        for l in (ns.stdout.decode("utf-8", "replace").splitlines() if ns is not None else []):
            m = re.match(r"^(\d+)\s+(\d+)\s+", l)
            if m:
                a += int(m.group(1)); d += int(m.group(2))
        out.append((sha, a, d))
    return out


def clean_target_ref(wt, target):
    """The commit whose `target` is the ORIGINAL (un-poisoned) baseline: the parent of the first seal commit that changed
    the target; 'HEAD' when none did (the common case -- nothing to correct)."""
    sc = seal_commits_touching(wt, target)
    if not sc:
        return "HEAD"
    par = _rev(wt, sc[0][0] + "^")
    return par or "HEAD"


def base_poison_findings(wt, target=None):
    """[finding] -- BASE_POISONED_BY_SEAL: the worktree's HEAD carries a prior round's sealed diff of the target, so a
    baseline measured at HEAD is not the original. Silent for an untouched target and for legacy worktrees."""
    wt = Path(wt)
    target = target or _manifest(wt).get("target")
    sc = seal_commits_touching(wt, target)
    if not sc:
        return []
    orig = read_orig_base(wt) or _manifest(wt).get("base_sha") or ""
    clean = clean_target_ref(wt, target)
    a = sum(x[1] for x in sc)
    d = sum(x[2] for x in sc)
    return [_finding(
        BASE_POISONED_BY_SEAL, "Worktree base carries a previous round's sealed diff of the target", str(target),
        f"{len(sc)} 'auto: seal round baseline' commit(s) changed {target} (+{a}/-{d} lines, first {sc[0][0][:12]}) so HEAD "
        f"{_rev(wt, 'HEAD')[:12]} is NOT the original base{(' ' + orig[:12]) if orig else ''}: a baseline / failure count measured "
        f"at HEAD includes the model's own partial work and understates what is still broken. Measure against "
        f"{clean[:12]} (clean_target_ref), or re-create the worktree from the original base before a fresh dispatch")]


def baseline_probe(wt, timeout=300):
    """Run the scaffold's verify.sh ONCE at the (still untouched) base, before any author job.
    Returns {"ran": bool, "rc": int|None, "green": bool, "tail": str}. Never raises."""
    wt = Path(wt)
    if not (wt / "verify.sh").is_file():
        return {"ran": False, "rc": None, "green": False, "tail": "no verify.sh"}
    try:
        p = subprocess.run(["bash", "verify.sh"], cwd=str(wt), capture_output=True, text=True,
                           timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return {"ran": True, "rc": p.returncode, "green": p.returncode == 0 and "VERIFY_OK" in out,
                "tail": out.strip()[-300:]}
    except subprocess.TimeoutExpired:
        return {"ran": True, "rc": 124, "green": False, "tail": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ran": False, "rc": None, "green": False, "tail": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 7. auto-decision log
# ---------------------------------------------------------------------------
def decisions_path() -> Path:
    home = os.environ.get("OLLAMA_DISPATCH_HOME")
    base = Path(home) if home else Path.home() / ".ollama-dispatch"
    return base / "auto-decisions.jsonl"


def log_auto_decision(kind: str, label: str, decision: str, detail: str = "", path=None,
                      **extra) -> dict:
    """Append one JSON line recording an automatic decision that used to be a human
    checkpoint. Never raises. Returns the record."""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind,
           "label": label, "decision": decision, "detail": str(detail)[:600], **extra}
    try:
        p = Path(path) if path else decisions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def main(argv=None):
    av = list(sys.argv[1:] if argv is None else argv)
    if len(av) == 2 and av[0] == "check":
        ok, fs = harness_complete(av[1])
        for f in fs:
            print("FINDING:", f)
        print("HARNESS COMPLETE" if ok else "HARNESS INCOMPLETE")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
