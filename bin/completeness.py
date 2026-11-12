#!/usr/bin/env python3
"""Did this diff implement the WHOLE spec? -- the question a bug scan never asks.

Built 2026-08-31 after the worker-owner session ranked COMPLETENESS above recall
as the gap that matters for gating a dispatch:

    "Your reviewer checks whether what's IN the diff is buggy. It cannot tell
     whether qwen did the WHOLE task. Twice today qwen shipped a partial or
     wrong-shaped answer that was individually clean line-by-line. A review that
     doesn't ask 'what's MISSING vs what was asked' isn't a dispatch gate, it's
     a bug scan."

They are right, and I had this written down already -- a standing note says
"reviews must ask what is MISSING, not just what is wrong" -- and I still built a
reviewer that only sees the diff and never the spec.

The decidable core: a spec that hands the model exact code literals has told us
what must appear. Whether those strings are in the added lines is a fact, not a
judgement, so the harness decides it. Requirements phrased in prose are reported
separately as UNCHECKABLE rather than guessed at -- an honest "I could not check
these 3" beats a confident wrong verdict on all 7.
"""
import argparse, json, re, sys
from pathlib import Path

_LITERAL_PATTERNS = [
    r"`([^`\n]{6,120})`",                       # backticked code
    r"\b(\w+(?:\.\w+)+\s*[=!<>]=+\s*\S+)",      # a.b === c
    # Bare `x = y` OUTSIDE backticks catches English -- "qty = reservation qty",
    # "value = reservation" -- which then drove a HIGH "the verify asserts none
    # of the spec's literals" on a task whose verify was entirely appropriate.
    # Require the RHS to look like code (a call, member access, literal,
    # bracket, or terminator), not just a bare word.
    r"\b(\w+(?:\.\w+)*\s*=\s*[^\n,;]*[(){}\[\].;\"']+[^\n,;]{0,50})",
]
# A REFERENCED PATH is context, not an obligation. Covers docs AND source files:
# a task saying "look at raw-state/route.ts for the pattern" was having that path
# read as a required literal, because a path contains a dot and therefore passed
# the "looks like code" test. Extended 2026-08-31 after run 2 of the viability
# test flagged two .ts reference paths the same way AGENTS.md was flagged in run 1.
# Matches only when the literal is ENTIRELY a path -- a call like
# join(dir, 'telemetry-health.json') is still a real literal and must survive.
_FILENAME = re.compile(r"""(?xi)
    [\w./@-]+ \.
    (md|txt|json|ya?ml|toml|lock|cfg|ini|csv|png|jpe?g|svg|pdf|sh|env
     |tsx?|jsx?|mjs|cjs|py|rb|go|rs|java|cs|c|h|cpp|hpp|swift|kt|php|sql)
""")
# Extensionless path references -- Dockerfile, Makefile, LICENSE, or anything
# that is plainly a path (contains a directory separator and no spaces). The
# extension-based reject missed `./sidecar/Dockerfile` entirely, which then drove
# a HIGH "spec names X but it is absent from the diff" on a real dispatch.
_PATHREF = re.compile(r"""(?xi)
    \.{0,2}/?[\w.-]*/[\w./-]+          # any slash-bearing path
  | (\./)?(Dockerfile|Makefile|LICENSE|Procfile|Justfile|Gemfile|Rakefile)(\.\w+)?
""")
_NOISE = re.compile(r"^(the|this|that|and|or|if|is|are|to|for|with|from)\b", re.I)
# Prose that marks an obligation rather than a description.
_OBLIGATION = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s+(.{8,300})$|(?:^|\.\s+)((?:you\s+)?(?:must|should|need to|"
    r"add|create|remove|delete|replace|rename|repoint|wire|return|ensure|make sure)\b.{6,300}?)(?:\.|$)",
    re.I | re.M)


# EXPLICIT CONTRACT, preferred over inference.
#
# Inferring "required code literals" from prose does not converge. Seven distinct
# false positives in one day, each a correct fix for that instance and each
# followed by a slightly different one: AGENTS.md, route.ts, prose-with-an-equals,
# a second drifted copy of the extractor, ./sidecar/Dockerfile (extensionless),
# backticked English, and `bash verify.sh` (a command reference). The underlying
# test -- "does this string look like code" -- has no clean boundary, so each
# patch narrows the heuristic without ever making it right.
#
# So: let the task SAY what must appear. A spec containing a "Must contain"
# section (any heading level) uses ONLY that section's backticked spans, and the
# rest of the task stays prose the checker never touches. That converts a
# heuristic into a contract and makes completeness EXACT.
#
# Falls back to inference when no such section exists, so nothing breaks and
# adoption can be gradual.
# Contract-only mode: no `## Must contain` section means no literals asserted.
# Set False to restore the old inference fallback (kept for one release so a
# regression can be A/B'd rather than argued about).
_CONTRACT_ONLY = True

_MUST_SECTION = re.compile(
    r"^#{1,6}\s*must[ _-]?contain\s*$(.*?)(?=^#{1,6}\s|\Z)",
    re.I | re.M | re.S)


_BULLET = re.compile(r"^\s{0,4}[-*+]\s+(.*)$")


def _backticked(text: str) -> list[str]:
    return [l.strip() for l in re.findall(r"`([^`\n]{2,160})`", text) if l.strip()]


def declared_literals(spec: str) -> list[str] | None:
    """The `## Must contain` CONTRACT: backticked spans on its BULLET LINES.

    THE FALSE POSITIVE THIS FIXES (2026-09-19; confirmed on jobs 25ef57cbded5
    arr-codec-floor-s1, 890b48511629 bg-eraser-s1 and 4fcc5fdafba5
    rt-walmart555-tracking-fix -- three parked escalations, one cause).

    The old rule was "every backticked span anywhere in the section". But the
    scaffold WRITES prose into that section -- it appends a parenthetical
    explaining the optional file-pinning syntax:

        (A bare bullet checks the default target. To PIN a literal to a specific
        file ... prefix the bullet with `in <path>:`, e.g.
        `- in app/api/x/route.ts: ` followed by a backtick-quoted token.)

    so the checker read its OWN DOCUMENTATION EXAMPLE as two required literals,
    `in <path>:` and `- in app/api/x/route.ts: `, found them absent from the diff
    (of course -- no real diff contains the placeholder `<path>`), and emitted two
    HIGH "spec names X but it is absent" issues. That flipped a clean verdict to
    `concerns`, which made auto_fix `undecidable`, which escalated a job whose
    review was PASS with 17/17 mutants killed. Every such task is affected,
    because the scaffold writes that paragraph into every one of them: a
    self-inflicted, systemic false positive.

    The contract IS the bullet list -- that is what the template asks an author to
    write and what "- `def codec_rank`" means. Prose in the section, including the
    scaffold's own example, is explanation, and explanation is not a requirement.
    Note this also fixes it structurally rather than by blacklisting the current
    wording: reword the parenthetical and the fix still holds.

    FALLBACK. A section with bullets uses only those bullets. A section with NO
    bullet carrying a literal falls back to the whole-section scan, so a spec that
    states its contract some other way still asserts something rather than
    silently asserting nothing -- but the scaffold's example is fenced out by
    name there, because in that mode nothing else can distinguish it."""
    m = _MUST_SECTION.search(spec)
    if not m:
        return None
    body = m.group(1)
    out, seen = [], set()
    for line in body.splitlines():
        b = _BULLET.match(line)
        if not b:
            continue                      # prose / the scaffold's own example
        for lit in _backticked(b.group(1)):
            if lit not in seen:
                seen.add(lit); out.append(lit)
    if out:
        return out
    # No bulleted literal at all -> old whole-section behaviour, minus the
    # template example (which is the one string we know is never a requirement).
    for lit in _backticked(body):
        if _is_template_example(lit) or lit in seen:
            continue
        seen.add(lit); out.append(lit)
    return out


# The scaffold's file-pinning example, in the shapes it actually appears in. Used
# ONLY on the no-bullets fallback path; the bullet rule above needs no list.
_TEMPLATE_EXAMPLE = re.compile(
    r"^-?\s*in\s+(<path>|app/api/x/route\.ts)\s*:?\s*$", re.I)


def _is_template_example(lit: str) -> bool:
    return bool(_TEMPLATE_EXAMPLE.match(lit.strip()))


def spec_literals(spec: str) -> list[str]:
    declared = declared_literals(spec)
    if declared is not None:
        return declared          # explicit contract wins; no inference at all
    if _CONTRACT_ONLY:
        # No "## Must contain" section => assert NOTHING, and say so loudly rather
        # than inferring. Chosen with the queue owner 2026-08-31 after seven
        # false positives from inference. Completeness becomes OPT-IN: a task that
        # does not declare its literals gets no completeness signal, which the
        # reviewer and the verify still cover. The alternative (silent fallback to
        # inference) keeps the FP class alive on every legacy task.
        #
        # It is reported as a VISIBLE not-asserted state, never as a clean pass --
        # same rule as a skipped gate: "not checked" must never read as "checked
        # and fine".
        return []
    out: list[str] = []
    for pat in _LITERAL_PATTERNS:
        for m in re.finditer(pat, spec):
            lit = re.sub(r"^[\s`]+|[\s`.,;]+$", "", m.group(1))
            if len(lit) < 6 or _NOISE.match(lit):
                continue
            if not re.search(r"[=<>(\.]", lit):
                continue
            # PROSE WITH AN EQUALS SIGN IS NOT CODE. The assignment pattern was
            # matching English -- "qty = reservation qty", "value = reservation"
            # -- and those drove a HIGH "the verify asserts none of the spec's
            # literals" on a task whose verify was entirely appropriate. Third
            # variant of one root cause: a shallow "looks like code" test
            # (has a dot / has an equals) accepting things that merely resemble
            # code. Require the right-hand side to carry actual code punctuation
            # if it contains spaces.
            _rhs = lit.split("=")[-1].strip() if "=" in lit else lit
            if " " in _rhs and not re.search(r"[(){}\[\].;\"'`]|::|->|=>", _rhs):
                continue
            # A FILENAME is not a code literal. `AGENTS.md`, `README.md`,
            # `lib/foo.ts` all satisfy the "contains a dot, looks like code"
            # test, so a task saying "see AGENTS.md" for CONTEXT was being
            # treated as a requirement that AGENTS.md appear in the diff --
            # a HIGH-severity false positive that drove a whole gate run to
            # FAIL on correct code (reported from the first real viability run,
            # 2026-08-31). Referenced paths are context, not obligations.
            _l = lit.strip()
            if _FILENAME.fullmatch(_l) or _PATHREF.fullmatch(_l):
                continue
            # PROSE INSIDE BACKTICKS is still prose. The backtick pattern was
            # capturing English spans like `over HTTP via TRACKER_URL; shares
            # /data. Needs` and reporting them as required code literals -- they
            # contain a dot and a slash, so every shallow "looks like code" test
            # passes them. Seen live on a real auto-gate run. A literal with
            # several spaces and no code-shaped token is a sentence.
            if _l.count(" ") >= 3 and not re.search(
                    r"[(){}\[\]]|=>|->|::|===|!==|\breturn\b|\bconst\b|\bdef\b|;\s*\w+\s*=", _l):
                continue
            # Reject cross-boundary captures and truncated fragments. The
            # backtick pattern can span two adjacent code spans, yielding junk
            # like "old` assignment -> `new"; and an unbalanced paren means the
            # capture stopped mid-expression. Reporting either as MISSING is a
            # false alarm about the spec, not a finding about the diff -- and a
            # completeness checker that cries wolf will be ignored, which is
            # worse than not having one.
            if "`" in lit or "\u2192" in lit or "->" in lit:
                continue
            if lit.count("(") != lit.count(")") or lit.count("{") != lit.count("}"):
                continue
            if any(lit in o for o in out):
                continue
            out = [o for o in out if o not in lit]
            out.append(lit)
    return out


def added_lines(diff: str) -> list[str]:
    return [l[1:] for l in diff.splitlines()
            if l.startswith("+") and not l.startswith("+++")]


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t)


def _ident_core(lit: str) -> str:
    """Identifiers only -- tolerant of an object qualifier or a renamed string.

    A literal must be matched the way the guard-move check learned to match:
    on what identifies it, not byte-for-byte. `syncedOk === false || limitReached
    === true` is satisfied by `health.syncedOk === false || health.limitReached
    === true`, and asserting the exact string is the trap that failed correct
    work in the flag-repoint dispatch.
    """
    return "".join(sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", lit))))


_REMOVAL_CUE = re.compile(
    r"\b(remove|removed|removing|delete|deleted|deleting|drop|dropped|"
    r"no longer|must be gone|used to|previously|currently|old|replace|replaced|"
    r"instead of|rather than|stop )\b", re.I)


def _is_negative(spec: str, lit: str) -> bool:
    """Does the spec name this literal as something to GET RID OF?

    Without this the checker inverts the requirement. On the real flag-repoint
    spec it reported `._telemetryDegraded = fresh.online === true` as MISSING --
    a literal the spec explicitly says to DELETE, whose absence from the added
    lines is the task being done correctly. A completeness checker that demands
    the presence of code the spec asked you to remove is worse than no checker:
    it fails correct work, which is exactly the failure mode I diagnosed in
    someone else's verify hours earlier.
    """
    for m in re.finditer(re.escape(lit[:40]), spec):
        # STRUCTURAL cue first, and it is the reliable one: specs write a
        # replacement as `old` -> `new`, so a literal followed closely by an
        # arrow is the OLD side. The lexical cue alone missed this on the real
        # spec, because "Replace EVERY ..." sat on the list's PARENT line while
        # each literal lived on its own bullet.
        after = spec[m.end():m.end() + 60]
        if re.search(r"^[^\n]{0,40}(->|\u2192|\u21d2)", after):
            return True
        # lexical cue: scan back over the bullet AND its parent line
        back = spec[max(0, m.start() - 400):m.start()]
        tail = "\n".join(back.splitlines()[-3:])
        if _REMOVAL_CUE.search(tail):
            return True
    return False


def removed_lines(diff: str) -> list[str]:
    return [l[1:] for l in diff.splitlines()
            if l.startswith("-") and not l.startswith("---")]


def diff_paths(diff: str) -> set[str]:
    """Every file the diff touches, from its `+++ b/<path>` headers."""
    return {m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M)
            if m.group(1) != "/dev/null"}


def diff_is_substantive(diff: str) -> bool:
    """Did this diff actually CHANGE something? A slice that adds and removes
    nothing has done no work, and must never be credited with a literal that was
    already sitting in the tree before it ran."""
    return bool(added_lines(diff) or removed_lines(diff))


def final_scope_text(diff: str, cwd) -> str | None:
    """The POST-diff content of the files the diff touched -- or None if it
    could not be read, which means "unknown", never "absent".

    THE SLICE-CHAIN FALSE POSITIVE (job 890b48511629, 2026-09-18). A `## Must
    contain` literal was being matched against the diff's ADDED LINES only. In a
    slice chain the task-authoring step restates the target's whole current
    contract, so slice s3's `## Must contain` legitimately re-declares the
    signature and the guard that slices s1 and s2 already landed. The model did
    the right thing -- it reused those lines instead of duplicating them -- and
    the checker reported both as absent, which `_baseline_has` then relabelled
    "spec defect". A medium input issue is still an issue, so the gate landed
    CONCERNS -> auto_fix_class=undecidable -> escalate, and a completely correct
    diff burned a human review cycle.

    The reference implementation of the right rule already existed, in
    ollama-dispatch-preflight.refimpl_scope_text: "Not the diff's added lines: a
    'Must contain' literal is as often one that must be PRESERVED (a signature
    the fix must not change) as one that must be added, and diff-scoping fails
    those." This is the same rule, applied to the model's diff instead of the
    reference impl.

    SCOPE IS DELIBERATELY NARROW -- the files the diff TOUCHED, not the whole
    tree. Concatenating every tracked file re-inerts the check (preflight hit
    exactly that: `eval_arms_pending` already lived elsewhere in the repo and a
    benign verify was rendered GO). Combined with diff_is_substantive, a literal
    can only be credited as pre-existing when this slice actually edited the file
    that holds it.

    The gate runs against the job's own worktree after the model's edits and the
    diff is `git diff HEAD`, so the on-disk file IS the post-diff content.
    """
    if cwd is None:
        return None
    blob, read_any = [], False
    for path in sorted(diff_paths(diff)):
        f = Path(cwd) / path
        try:
            if f.is_file() and f.stat().st_size < 4_000_000:
                blob.append(f.read_text(errors="replace"))
                read_any = True
        except OSError:
            continue
    return "\n".join(blob) if read_any else None


def _baseline_has(lit: str, diff: str, cwd) -> bool:
    """Was this literal ALREADY in the file before the diff?

    A `## Must contain` literal that already exists in the baseline can never
    appear in the diff's ADDED lines, so declaring it guarantees a spurious
    MISSING no matter how correct the work is. First live use of the convention
    hit exactly this: a task declared `def do_POST`, the file already had one at
    line 761, qwen correctly EXTENDED it rather than adding a second, and
    completeness reported the spec literal absent.

    The diff alone cannot settle it -- the definition sat far from the change,
    so it appeared in no context line. The baseline can, via git, and the gate
    already runs in a git checkout.

    Fails OPEN (returns False, i.e. "genuinely missing") on any git problem: a
    missing baseline must never silently downgrade a real completeness finding
    into a spec nit.
    """
    if cwd is None:
        return False
    import subprocess
    for path in {m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M)
                 if m.group(1) != "/dev/null"}:
        try:
            r = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=str(cwd),
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and _norm(lit) in _norm(r.stdout):
                return True
        except Exception:
            continue
    return False


def check(spec: str, diff: str, cwd=None):
    adds = added_lines(diff)
    body = "\n".join(adds)
    nbody = _norm(body)
    lits = spec_literals(spec)

    rems = removed_lines(diff)
    nrem = _norm("\n".join(rems))
    satisfied, missing, negatives = [], [], []
    for lit in lits:
        if _is_negative(spec, lit):
            # a NEGATIVE requirement: this must be ABSENT from the added lines
            if _norm(lit) in nbody:
                negatives.append((lit, "STILL PRESENT -- spec said to remove it"))
            elif _norm(lit) in nrem:
                satisfied.append((lit, "correctly removed"))
            else:
                negatives.append((lit, "not found in the diff at all -- "
                                       "was it ever there? verify by hand"))
            continue
        if _norm(lit) in nbody:
            satisfied.append((lit, "exact"))
            continue
        core = _ident_core(lit)
        if core and any(_ident_core(a) and set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", lit))
                        <= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", a)) for a in adds):
            satisfied.append((lit, "present, differently spelled"))
            continue
        missing.append(lit)

    # PRESENT IN THE FINAL FILE = SATISFIED. A `## Must contain` literal is a
    # statement about the RESULT, not about the diff hunk: "after this change the
    # target must contain X". A slice-chain task restates the target's whole
    # current contract, so literals an earlier slice landed are re-declared and
    # correctly not re-added by this one. Checked against the post-diff content
    # of the files this diff touched (see final_scope_text), and only when the
    # diff actually changed something -- so a no-op diff can still never be
    # credited with what was already in the tree.
    final = final_scope_text(diff, cwd) if missing else None
    if final is not None and diff_is_substantive(diff):
        nfinal = _norm(final)
        retained = [l for l in missing if _norm(l) in nfinal]
        for l in retained:
            satisfied.append(
                (l, "already present in the target file; the diff correctly did "
                    "not re-add it"))
        missing = [l for l in missing if l not in retained]

    # Split genuine misses from SPEC ERRORS -- a literal the baseline already
    # had is a defect in the task, not in the diff, and reporting it as "the
    # model failed to implement this" points the reviewer at the wrong artifact.
    #
    # This is now the FAIL-OPEN path only. When the final file was readable, a
    # baseline literal still in the result was already credited as satisfied
    # above, and one that is NOT in the result was DELETED by this diff -- which
    # is genuinely missing work, high severity, not a spec nit. spec_errors
    # therefore fires only when the post-diff content could not be read at all
    # (no --cwd, file gone, unreadable), where "the baseline had it" is the best
    # evidence available and must not masquerade as missing work.
    spec_errors = ([l for l in missing if _baseline_has(l, diff, cwd)]
                   if final is None else [])
    missing = [l for l in missing if l not in spec_errors]

    obligations = []
    for m in _OBLIGATION.finditer(spec):
        t = (m.group(1) or m.group(2) or "").strip()
        t = re.sub(r"\s+", " ", t)
        if 8 <= len(t) <= 300 and t not in obligations:
            obligations.append(t)
    return lits, satisfied, missing, obligations, negatives, spec_errors


def _self_test() -> int:
    """GPU-free tests for the final-file literal rule. Run: completeness.py --self-test

    The case that motivated them is the SLICE-CHAIN false positive (job
    890b48511629): a `## Must contain` literal that the BASELINE already had, is
    still in the RESULT, and appears nowhere in the diff's added lines. It was
    reported as a spec error, which the gate renders as a medium input issue ->
    concerns -> escalate. Both directions are proven here: that case is now
    complete, and a literal genuinely absent from the result is still flagged.
    """
    import shutil, subprocess, tempfile
    ok = True

    def check_(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            ok = False

    BASE = '''"""Build the vendored eraser removal command for a broker."""


def build_eraser_cmd(broker_id: str, profile_path: str = "profile.local.json") -> list[str]:
    if not isinstance(broker_id, str) or not broker_id.strip():
        raise ValueError("broker_id must be a non-empty string")
    return ["eraser", "remove", "--profile", profile_path, "--broker", broker_id]
'''
    # s3's result: the signature and the guard are UNCHANGED (s1/s2 landed them);
    # this slice only normalises the id. Neither pre-existing literal appears in
    # an added line -- they are context lines.
    FINAL = '''"""Build the vendored eraser removal command for a broker."""
import re

_BROKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def build_eraser_cmd(broker_id: str, profile_path: str = "profile.local.json") -> list[str]:
    if not isinstance(broker_id, str) or not broker_id.strip():
        raise ValueError("broker_id must be a non-empty string")
    normalized = broker_id.strip().lower()
    return ["eraser", "remove", "--profile", profile_path, "--broker", normalized]
'''
    SIG = 'def build_eraser_cmd(broker_id: str, profile_path: str = "profile.local.json")'
    GUARD = 'raise ValueError("broker_id must be a non-empty string")'
    NEW = "_BROKER_ID_RE = re.compile"
    REL = "broker_guard/eraser.py"

    def fixture(base: str | None, final: str | None):
        """A real git worktree at HEAD=base with the working tree at final, plus
        the `git diff HEAD` between them. Returns (tmpdir, diff_text)."""
        d = Path(tempfile.mkdtemp(prefix="completeness-selftest-"))
        p = d / REL
        p.parent.mkdir(parents=True, exist_ok=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": "/usr/bin:/bin:/usr/local/bin"}
        def g(*a):
            return subprocess.run(["git", *a], cwd=str(d), env=env,
                                  capture_output=True, text=True)
        g("init", "-q")
        p.write_text(base if base is not None else "")
        g("add", "-A"); g("commit", "-qm", "baseline")
        if final is None:
            p.unlink()
        else:
            p.write_text(final)
        diff = g("diff", "HEAD").stdout
        return d, diff

    spec = (f"# TASK\n\nNormalise the broker id.\n\n## Must contain\n"
            f"- `{SIG}`\n- `{GUARD}`\n- `{NEW}`\n")
    tmps = []
    try:
        # (1) THE REPRO. Two declared literals live in the baseline AND the
        # result but in no added line; one literal is genuinely added.
        d, diff = fixture(BASE, FINAL); tmps.append(d)
        check_("(1) fixture really omits the pre-existing literals from the diff",
               GUARD not in "\n".join(added_lines(diff))
               and "def build_eraser_cmd" not in "\n".join(added_lines(diff)))
        lits, sat, missing, _o, _n, spec_err = check(spec, diff, d)
        check_("(1) REPRO: pre-existing-but-retained literals are COMPLETE "
               "(no missing, no spec_errors)", missing == [] and spec_err == [])
        check_("(1) they are credited as satisfied, with the reason stated",
               len(sat) == 3 and any("already present in the target file" in h
                                     for _l, h in sat))

        # (2) GUARDRAIL: a literal in NEITHER the baseline nor the result is
        # still MISSING -- the check is not weakened into a rubber stamp.
        spec_absent = spec + "- `def scrub_broker_profile(broker_id)`\n"
        lits, sat, missing, _o, _n, spec_err = check(spec_absent, diff, d)
        check_("(2) GUARDRAIL: a genuinely absent literal is still MISSING",
               missing == ["def scrub_broker_profile(broker_id)"] and spec_err == [])

        # (3) GUARDRAIL: a literal the diff DELETED is missing work, not a spec
        # nit. Before this change the baseline-had-it test relabelled it a spec
        # error (medium); it is a removal of required code (high).
        d3, diff3 = fixture(BASE, BASE.replace(
            '    if not isinstance(broker_id, str) or not broker_id.strip():\n'
            '        raise ValueError("broker_id must be a non-empty string")\n', ''))
        tmps.append(d3)
        _l, _s, missing3, _o, _n, spec_err3 = check(spec, diff3, d3)
        check_("(3) GUARDRAIL: a literal the diff DELETED is MISSING, not a spec error",
               GUARD in missing3 and spec_err3 == [])

        # (4) GUARDRAIL: a no-op diff earns nothing. The tree contains every
        # literal, but this slice changed nothing, so it may not be credited.
        d4, _ = fixture(FINAL, FINAL); tmps.append(d4)
        noop = f"diff --git a/{REL} b/{REL}\n--- a/{REL}\n+++ b/{REL}\n"
        check_("(4) a diff with no +/- lines is not substantive",
               diff_is_substantive(noop) is False)
        _l, _s, missing4, _o, _n, _se = check(spec, noop, d4)
        check_("(4) GUARDRAIL: a no-op diff is NOT credited with pre-existing literals",
               SIG in missing4 and GUARD in missing4)

        # (5) SCOPE: only files THIS diff touched count. The literal lives in an
        # untouched file, so it must not be credited (the inert-check failure
        # ollama-dispatch-preflight.refimpl_scope_text documents).
        (d / "other_module.py").write_text("def scrub_broker_profile(broker_id):\n    pass\n")
        _l, _s, missing5, _o, _n, _se = check(spec_absent, diff, d)
        check_("(5) SCOPE: a literal in an untouched file is NOT credited",
               "def scrub_broker_profile(broker_id)" in missing5)

        # (6) FAIL-OPEN: the post-diff content cannot be READ (permissions, an
        # oversized file, a worktree already reaped) -> abstain and fall back to
        # the baseline test, so a pre-existing literal is still reported as a
        # SPEC ERROR rather than silently escalating into missing work.
        d6, diff6 = fixture(BASE, FINAL); tmps.append(d6)
        (d6 / REL).chmod(0o000)
        check_("(6) final content unreadable -> final_scope_text abstains (None)",
               final_scope_text(diff6, d6) is None)
        _l, _s, missing6, _o, _n, spec_err6 = check(spec, diff6, d6)
        (d6 / REL).chmod(0o644)
        check_("(6) FAIL-OPEN: the baseline spec-error path still fires when the "
               "result cannot be read", GUARD in spec_err6 and GUARD not in missing6)

        # (6b) THE SCAFFOLD'S OWN DOCUMENTATION EXAMPLE IS NOT A REQUIREMENT
        # (2026-09-19). Verbatim text the scaffold writes into every TASK.md,
        # reproduced exactly as job 25ef57cbded5 carried it. Before the bullet
        # rule this yielded the two literals `in <path>:` and
        # `- in app/api/x/route.ts: `, neither of which any real diff can contain,
        # and two HIGH "absent from the diff" issues turned a 17/17-mutant PASS
        # into concerns -> undecidable -> a parked human escalation. Same cause on
        # 890b48511629 and 4fcc5fdafba5.
        TEMPLATED = (
            "# TASK\n\nAdd a codec rank helper.\n\n"
            "## Must contain\n\n"
            "- `def codec_rank`\n- `x265`\n- `x264`\n- `hevc`\n\n"
            "(The gate holds the reference impl against this list. If the verify goes green\n"
            "while one of these is absent from the changed files, the verify does not\n"
            "enforce the spec -- that is a benign verify, caught mechanically.)\n\n"
            "(A bare bullet checks the default target. To PIN a literal to a specific file --\n"
            "useful when a fix spans a helper file and the route/wiring that calls it --\n"
            "prefix the bullet with `in <path>:`, e.g.\n"
            "`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that\n"
            "token is required in THAT file, not the target.)\n\n"
            "## Scope\n\nOnly edit `arr-webhook.py`.\n")
        _decl = declared_literals(TEMPLATED)
        check_("(6b) the four AUTHORED bullets are the whole contract",
               _decl == ["def codec_rank", "x265", "x264", "hevc"])
        check_("(6b) REPRO: the template's own `in <path>:` example is NOT a literal",
               "in <path>:" not in (_decl or []))
        check_("(6b) REPRO: nor is `- in app/api/x/route.ts: `",
               not any("route.ts" in l for l in (_decl or [])))
        check_("(6b) nor is anything from the ## Scope section or the prose "
               "parenthetical (arr-webhook.py must not be a required literal)",
               not any("arr-webhook" in l or "benign verify" in l
                       for l in (_decl or [])))
        # ...and the bullet rule does not silently drop real contracts:
        check_("(6b) GUARDRAIL: a genuine PINNED bullet is still a literal",
               declared_literals(
                   "## Must contain\n- in lib/x.ts: `realToken`\n- `other`\n")
               == ["realToken", "other"]),
        check_("(6b) GUARDRAIL: indented and *-style bullets still count",
               declared_literals("## Must contain\n  * `aToken`\n  + `bToken`\n")
               == ["aToken", "bToken"])
        check_("(6b) GUARDRAIL: a section with NO bullets still asserts its "
               "literals (fallback), minus the template example",
               declared_literals(
                   "## Must contain\nThe file must define `someSymbol` and "
                   "`otherSymbol`; prefix the bullet with `in <path>:` to pin it.\n")
               == ["someSymbol", "otherSymbol"])
        check_("(6b) a section with neither bullets nor literals is still [] "
               "(declared, but empty) -- not None, which means 'no section'",
               declared_literals("## Must contain\n\njust prose.\n") == []
               and declared_literals("# TASK\n\nno section here\n") is None)

        # (7) no --cwd at all -> unchanged legacy behaviour (reported missing).
        _l, _s, missing7, _o, _n, spec_err7 = check(spec, diff, None)
        check_("(7) without --cwd nothing is credited and nothing is excused",
               GUARD in missing7 and spec_err7 == [])
    finally:
        for d in tmps:
            shutil.rmtree(d, ignore_errors=True)

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="spec text")
    ap.add_argument("--spec-file")
    ap.add_argument("--diff", required=True)
    ap.add_argument("--cwd", default=None,
                    help="git checkout the diff came from. Lets a declared literal "
                         "that ALREADY existed in the baseline be reported as a spec "
                         "error instead of as missing work.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    spec = a.spec or ""
    if a.spec_file:
        spec = Path(a.spec_file).expanduser().read_text()
    if not spec.strip():
        print("ERROR: need --spec or --spec-file", file=sys.stderr)
        return 2
    diff = Path(a.diff).expanduser().read_text()
    lits, satisfied, missing, obligations, negatives, spec_errors = check(
        spec, diff, Path(a.cwd).expanduser() if a.cwd else None)

    if a.json:
        # ASSERTED is part of the RESULT, not a footnote in the human output.
        # Under _CONTRACT_ONLY a task with no `## Must contain` section yields
        # literals=[] and missing=[], which any consumer reading only `missing`
        # renders as a clean completeness pass. It is not one -- nothing was
        # checked. The human path already says NOT ASSERTED loudly; the JSON
        # path said nothing at all, so the gate (which uses --json) reported
        # every legacy task as completeness-clean. Measured 2026-08-31: zero of
        # the five tasks in the live batch declare the section, so this was the
        # state of 100% of dispatches.
        # THREE STATES, not two. The fix above distinguished "no section" from
        # "section with literals" -- but `declared_literals` returns [] (not
        # None) for a section that EXISTS and yields no backticked spans, so
        # `is not None` reported asserted=True with literals=[] and missing=[].
        # gate.py:125 only records a not_checked entry when asserted is false,
        # so that rendered as completeness CHECKED AND CLEAN while nothing had
        # been asserted at all.
        #
        # It is the likelier authoring mistake, not an exotic one: literals must
        # be in `backticks`, and a task that writes the section as a plain bullet
        # list looks completely correct to its author. The human path already
        # said "the section is empty -- nothing asserted"; only the JSON path,
        # which is the one the GATE reads, was silent.
        #
        # asserted is now truthiness-based, so all three states are distinct:
        #   None -> no section          -> not asserted, "did not run"
        #   []   -> section, no literals-> not asserted, "empty section"
        #   [..] -> section with spans  -> asserted
        _decl = declared_literals(spec)
        asserted = bool(_decl)
        print(json.dumps({"literals": lits, "satisfied": [s[0] for s in satisfied],
                          "missing": missing, "spec_errors": spec_errors,
                          "obligations": obligations,
                          "asserted": asserted,
                          "not_asserted_reason": (
                              "" if asserted else
                              "task declares no `## Must contain` section, so no "
                              "literals were asserted -- completeness did not run"
                              if _decl is None else
                              "the `## Must contain` section is present but contains "
                              "no `backticked` literals, so nothing was asserted -- "
                              "completeness did not run (literals must be in "
                              "backticks)")},
                         indent=1))
        return 1 if missing else 0

    print("=== completeness: does the diff implement the whole spec? ===")
    if not lits:
        if declared_literals(spec) is None:
            print("  note: this task declares no `## Must contain` section, so NO literals are "
                  "asserted. That is NOT a pass -- completeness was not checked. Add the "
                  "section to get an exact check.")
        else:
            print("  note: the `## Must contain` section is empty -- nothing asserted.")
    for lit, how in satisfied:
        print(f"  [OK]      {lit[:88]}   ({how})")
    for lit in missing:
        print(f"  [MISSING] {lit[:88]}")
    for lit in spec_errors:
        print(f"  [SPEC ERROR] {lit[:80]}\n               already present in the baseline -- a diff "
              f"cannot add what is already there. Declare what the change should ADD.")
    for lit, why in negatives:
        print(f"  [REMOVAL?] {lit[:78]}\n             {why}")
    if obligations:
        print(f"\n  {len(obligations)} prose obligation(s) the harness CANNOT check -- "
              f"a human or the review model must:")
        for o in obligations[:12]:
            print(f"    - {o[:110]}")
    print()
    if missing:
        print(f"  VERDICT: INCOMPLETE -- {len(missing)} spec literal(s) absent from the diff.")
        return 1
    if not lits and declared_literals(spec) is None:
        # "0 of 0 present" is technically true and reads as a PASS. It is not one --
        # nothing was checked. Caught in my own output immediately after building
        # the opt-in path, which is the same mistake I had just flagged twice
        # elsewhere: not-checked must never render as checked-and-fine.
        print("  VERDICT: NOT ASSERTED -- this task declares no `## Must contain` "
              "section, so completeness was not checked at all. The reviewer and "
              "the verify still apply; this check abstained.")
        return 0
    print(f"  VERDICT: every spec literal is present ({len(satisfied)}/{len(lits)}).")
    print("  This is a FLOOR, not a pass: prose obligations above are unchecked, and")
    print("  a literal being present does not mean it is used correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
