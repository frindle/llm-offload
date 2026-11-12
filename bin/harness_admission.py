#!/usr/bin/env python3
"""harness_admission.py -- ONE shared HARNESS ADMISSION GATE (2026-10-10).

WHY. rt-costco-receipt-attach s3: 12 author jobs / ~3h / zero passes against a harness that
was defective from the first round (refimpl failed its own anchors; the fixture case
'date-only fallback MUST NOT link' failed even WITH the refimpl; the spec-traceability audit
only WARNed; the slice was 6 files / ~110KB of harness; the runaway stop fired late and the
driver authored again). No author/refine/continuation/coding job may be ENQUEUED against a
harness that fails this gate. Language agnostic: everything is read off the worktree
(fixture name from .dispatch-harness.json, verify from verify.sh), never off python/ts specifics.

GATE (admit()):
  fixture-untraceable   a string/number the fixture asserts on is in neither TASK.md nor the base
                        target (harness_audit_gates.untraceable_assertions, now BLOCKING). Allowlist:
                        `spec-ok` on the line, assertion-message prose ("expected ...", "should ..."),
                        interpolated / ISO-date / tiny-int computed values.
  baseline-not-red      verify.sh does not FAIL on the untouched baseline for an assertion reason.
  refimpl-unapplicable  the refimpl cannot apply to the base (anchor missing / apply error).
  refimpl-syntax        the refimpl WROTE source that does not parse (SyntaxError in the patched target).
  contract-broken       fail-before/pass-after not proven. The dynamic check runs the SAME self-check the
                        author jobs use as their verify (auto-harness-check.py: baseline red, refimpl
                        applied, verify green, tree restored); preflight is only the fallback when a
                        worktree has none. Anything that prevents the measurement (timeout, crash,
                        unparseable output) is itself a defect -- the dynamic path never fails open.
  flaky-fixture         with the refimpl applied, two runs of verify.sh give a different set of passing/failing
                        cases (skipped -- recorded as a NOTE, not a pass -- when the first run exceeds
                        HARNESS_FLAKE_MAX_S, default 90s).
  fixture-type-mismatch TS fixtures only (ha_tscheck.js, repo typescript, scratch copy with the refimpl applied):
                        the fixture imports something the real module does not export, or mock.module()s a
                        function whose returned tagged-union discriminant the real function cannot return
                        (e.g. {kind:'ambiguous'} vs the real {kind:'review',reason:'ambiguous'}). Message carries
                        the tsc-style error line. Other strict-mode diagnostics are NOTES. Non-TS fixtures: NOTE.
  oversize-reslice      the slice touches > MAX_TARGET_FILES files or its harness exceeds
                        MAX_HARNESS_BYTES: re-slice, do not author.
  reslice-required      a round-runaway / repeated-signature stop was recorded for THIS harness
                        (same fingerprint): no further identical round, route to re-slice/re-spec.
  repeated-defect       an AUTHORING-tier round (stage refimpl, continuation, escalation) is about to
                        run against the same defect set it already ran against once.
Policy by tier (tier_for_label): strict (coding enqueue) refuses on ANY defect; refine (auto-refine-*) refuses refimpl-side defects at once and fixture-side ones on recurrence;
authoring (-s3, -cN, -esc, -fresh) lets the model repair a NEW defect set ONCE (the defects are
appended to its prompt) and refuses a recurrence; oversize + reslice-required refuse everywhere.
Repair jobs (self-heal -r10N labels, repair=True) are exempt from refusal -- they ARE the repair
path -- but the harness changes fingerprint, so the next enqueue re-passes the whole gate.

FREEZE. A harness that passes with ZERO defects is admitted: its files are snapshotted and
enforce_freeze() (called after every auto-author job) restores any that the job modified.
STATE lives OUTSIDE the worktree: $HARNESS_ADMISSION_DIR or ~/.ollama-dispatch/admission/<sha1(path)>.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# --- causes (pipeline_cause_owners.referenced_causes reads this tuple) -------------------
ADMISSION_CAUSES = ("fixture-untraceable", "baseline-not-red", "refimpl-unapplicable",
                    "refimpl-syntax", "contract-broken", "flaky-fixture", "fixture-type-mismatch", "oversize-reslice", "reslice-required", "repeated-defect", "refine-no-change",
                    "frozen-harness-mutated",
                    "refusal-respec")        # slicer-side: a refusal's ONE bounded re-spec (owner: ollama-dispatch-slice.bound_stale_worktree_retry)
# OWN marker (NOT "SPEC_DEFECT: "): the auto driver treats it as a no-more-rounds stop, but the slicer's
# is_spec_defect substring match must not -- an admission refusal is routed by ONE owner
# (ollama-dispatch-slice.is_admission_refusal -> respec_on_cap, a single bounded re-spec), never twice.
ADMISSION_ROUTE_PREFIX = "ADMISSION_REFUSED: "
REFUSAL_PREFIX = ADMISSION_ROUTE_PREFIX + "HARNESS ADMISSION REFUSED"

FLAKE_MAX_S = float(os.environ.get("HARNESS_FLAKE_MAX_S", "90"))
TSC_TIMEOUT_S = int(os.environ.get("HARNESS_TSC_TIMEOUT_S", "180"))
MAX_TARGET_FILES = int(os.environ.get("HARNESS_MAX_TARGET_FILES", "3"))
CTX_CEILING_TOKENS = int(os.environ.get("HARNESS_CTX_CEILING", "131072"))
MAX_TARGET_LINES = int(os.environ.get("HARNESS_MAX_TARGET_LINES", "2500"))
MAX_TARGET_BYTES = int(os.environ.get("HARNESS_MAX_TARGET_BYTES", "120000"))
MAX_HARNESS_BYTES = int(os.environ.get("HARNESS_MAX_BYTES", "60000"))
# defects that are about the FIXTURE/spec side vs the REFIMPL side (authoring tiers may be mid-repair on the latter)
_FIXTURE_SIDE = {"fixture-untraceable", "baseline-not-red", "flaky-fixture", "fixture-type-mismatch"}
_REFIMPL_SIDE = {"refimpl-unapplicable", "refimpl-syntax", "contract-broken"}

_FIXTURE_NAMES = ("verify.test.ts", "verify.test.js", "verify.test.mjs", "verify_test.py",
                  "verify_fixture.py", "verify.test.py", "fixture.py")
_MSG_PROSE_RE = re.compile(r"^\s*(?:expected|should|must|got|unexpected|failed|fail:|ok:|case\b|test\b)", re.I)
_ISO_RE = re.compile(r"^\d{4}-\d\d-\d\d(?:[T ][\d:.]+Z?)?$")


def _state_dir() -> Path:
    return Path(os.environ.get("HARNESS_ADMISSION_DIR") or (Path.home() / ".ollama-dispatch" / "admission"))


def _key(wt) -> str:
    return hashlib.sha1(str(Path(wt).resolve()).encode()).hexdigest()[:16]


def state_path(wt) -> Path:
    return _state_dir() / f"{_key(wt)}.json"


def load_state(wt) -> dict:
    try:
        d = json.loads(state_path(wt).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(wt, st: dict) -> None:
    try:
        p = state_path(wt)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        os.replace(tmp, p)
    except OSError:
        pass


def _read(p) -> str:
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return ""


def _manifest(wt) -> dict:
    try:
        d = json.loads((Path(wt) / ".dispatch-harness.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def fixture_name(wt) -> str | None:
    wt = Path(wt)
    fx = _manifest(wt).get("fixture")
    if isinstance(fx, str) and fx and (wt / fx).is_file():
        return fx
    for n in _FIXTURE_NAMES:
        if (wt / n).is_file():
            return n
    return None


def harness_files(wt) -> list[str]:
    wt = Path(wt)
    out = []
    for n in ("TASK.md", fixture_name(wt), "refimpl.py"):
        if n and (wt / n).is_file():
            out.append(n)
    return out


_EXTRA_HARNESS = ("verify.sh", "check_literals.py")   # part of the harness: fingerprinted + frozen


def frozen_files(wt) -> list[str]:
    """Every file a harness is made of: TASK.md, fixture, refimpl.py, verify.sh, check_literals.py."""
    wt = Path(wt)
    return harness_files(wt) + [n for n in _EXTRA_HARNESS if (wt / n).is_file()]


def fingerprint(wt) -> str:
    h = hashlib.sha256()
    for n in frozen_files(wt):
        h.update(n.encode() + b"\0" + (Path(wt) / n).read_bytes() + b"\0")
    return h.hexdigest()[:20]


def _refimpl_is_stub(wt) -> bool:
    t = _read(Path(wt) / "refimpl.py")
    return (not t.strip()) or "TODO -- the exact text to replace" in t


# --- tiers --------------------------------------------------------------------------------
_REPAIR_RE = re.compile(r"-r10\d+$")


def tier_for_label(label: str):
    """None (nothing to gate beyond reslice state) | 'stage-refimpl' | 'authoring' | 'strict' | 'repair'."""
    l = str(label or "")
    if _REPAIR_RE.search(l):
        return "repair"
    if l.startswith("auto-refine-"):
        return "refine"
    if not l.startswith("auto-author-"):
        return "strict"                   # a coding enqueue (explicit admit call) is strict
    if re.search(r"-s[12](?:-r\d+)?$", l):
        return None                       # TASK / fixture stages: nothing to admit yet
    if re.search(r"-s3(?:-r\d+)?$", l):
        return "stage-refimpl"
    return "authoring"                    # -cN, -esc, -fresh, legacy


# --- static checks --------------------------------------------------------------------------
def _is_test_target(t: str) -> bool:
    return bool(re.search(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|tests?/[^/]*\.py|[^/]*\.(test|spec)\.[cm]?[jt]sx?|__tests__/[^/]+)$",
                          (t or "").replace("\\", "/")))


_HTTP_STATUS = {"200", "201", "202", "204", "301", "302", "304", "400", "401", "403", "404", "405",
                "409", "410", "415", "422", "429", "500", "502", "503", "504"}


def _allowlisted(f: dict, fx_lines=None) -> bool:
    tok = str(f.get("token") or "")
    if tok in _HTTP_STATUS and fx_lines:             # HTTP status asserted via a *status* expression
        ln = f.get("line") or 0
        if 0 < ln <= len(fx_lines) and re.search(r"status", fx_lines[ln - 1], re.I):
            return True
    if "SCAFFOLD" in tok:                 # the scaffold's own guard text (harness_complete owns placeholders)
        return True
    if _MSG_PROSE_RE.match(tok) or _ISO_RE.match(tok) or "${" in tok or "%s" in tok or "{}" in tok:
        return True
    if re.fullmatch(r"-?\d{1,2}", tok):
        return True
    # A bare DECIMAL constant (0.05, 0.1, 0.2) is a rate/threshold -- exactly what an author invents
    # (1a53991: 5%/10%/20% tier rates traced nowhere yet passed because the tiny-integer rule below
    # matched `.` as punctuation). It is NOT a tiny-integer worked example. Kill switch:
    # DISPATCH_TRACE_DECIMALS=off restores the old allowlisting.
    if (os.environ.get("DISPATCH_TRACE_DECIMALS", "on").strip().lower() not in ("off", "0", "no", "false")
            and re.fullmatch(r"-?\d*\.\d+", tok)):
        return False
    # a worked-example string made ONLY of tiny integers + punctuation/whitespace ('[1, 3]', '(-2, 0)', '4-4'):
    # the same arithmetic example the tiny-int rule already allows, just rendered by the code under test
    if re.fullmatch(r"[\s\[\](){},:;.\-+*/=<>|'\"\d]*", tok) and re.search(r"\d", tok) and not re.search(r"\d{3,}", tok):
        return True
    return False


def traceability_findings(wt, target, lang=None) -> list[dict]:
    wt = Path(wt)
    fx = fixture_name(wt)
    if not fx or not target or _is_test_target(target):
        return []
    import harness_audit_gates as hag
    import dispatch_harness_gates as hg
    base = hg.blob_at(wt, target) or ""
    if not lang:
        lang = "python" if fx.endswith(".py") else "ts"
    fs = hag.untraceable_assertions(_read(wt / fx), _read(wt / "TASK.md"), base, target, lang)
    fx_lines = _read(wt / fx).splitlines()
    return [f for f in fs if not _allowlisted(f, fx_lines)]


ANCHOR_MARGIN_BYTES = 16000     # one worker read page of slack around every window
_LINE_RANGE_RE = re.compile(r"\blines?\s+(\d+)\s*(?:-|to|through|\u2013)\s*(\d+)|\bL(\d+)\s*(?:-|\u2013)\s*L?(\d+)", re.I)
_SYM_ANCHOR_RE = re.compile(r"\b(?:near|around|after|before|inside|within)\s+(?:the\s+(?:function|def|class)\s+)?`([\w.$]+)\(?\)?`", re.I)


def anchored_bytes(blob: str, ctx: str):
    """Bytes the worker can touch when the intent/task names anchor windows, or None (= use the whole file).
    None unless _ANCHOR_RE matches AND at least one window resolves AND every extracted window resolves: a line
    range must lie inside the file, a named symbol must be defined in it. A bogus anchor never bypasses the cap."""
    if not blob or not _ANCHOR_RE.search(ctx or ""):
        return None
    lines = blob.splitlines(keepends=True)
    n = len(lines)
    wins, rngs, syms = [], set(), set()
    for m in _LINE_RANGE_RE.finditer(ctx):
        rngs.add((int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4))))
    for a, b in sorted(rngs):                     # the same window named twice is one window
        if not (1 <= a <= b <= n):
            return None
        wins.append(sum(len(l.encode("utf-8", "replace")) for l in lines[a - 1:b]))
    for m in _SYM_ANCHOR_RE.finditer(ctx):
        syms.add(m.group(1).rstrip("()"))
    for sym in sorted(syms):
        name = re.escape(sym)
        sm = re.search(rf"^[ \t]*(?:async\s+def|def|class|function|(?:export\s+)?(?:const|let|var))\s+{name}\b|^{name}\s*=", blob, re.M)
        if not sm:
            return None
        wins.append(MAX_TARGET_BYTES // 4)      # a symbol body: budget a fixed window
    if not wins:
        return None
    return min(len(blob), sum(w + ANCHOR_MARGIN_BYTES for w in wins))


def size_findings(wt, target) -> list[str]:
    wt = Path(wt)
    paths = set()
    task = _read(wt / "TASK.md")
    m = re.search(r"only edit([^\n]*)", task, re.I)
    if m:
        for p in re.findall(r"`([^`\s]+\.\w{1,6})`", m.group(1)):
            if not re.search(r"(^|/)(verify\.|TASK\.md|refimpl)", p):
                paths.add(p)
    if target:
        paths.add(target)
    try:
        import dispatch_harness_gates as hg
        tracked = set(hg._tracked_files(wt))
        for c in hg._path_constants(_read(wt / "refimpl.py")):
            for f in tracked:
                if f == c or f.endswith("/" + c):
                    if not re.search(r"(?:\.test\.|\.spec\.|_test\.|(?:^|/)test_)", f):
                        paths.add(f)
    except Exception:
        pass
    out = []
    if len(paths) > MAX_TARGET_FILES:
        out.append(f"{len(paths)} target files ({', '.join(sorted(paths))[:160]}) > cap {MAX_TARGET_FILES}")
    total = sum((wt / n).stat().st_size for n in harness_files(wt) if (wt / n).is_file())
    if total > MAX_HARNESS_BYTES:
        out.append(f"harness is {total} bytes (TASK+fixture+refimpl) > cap {MAX_HARNESS_BYTES}")
    # TOKEN estimate (aw-codec-floor-s2: a 330KB target + the harness blew the 131072-token ctx ceiling in the
    # queue's own gate, twice, before anyone re-sliced). Same shape as the queue's estimate: chars/3.5 tokens, +40%.
    try:
        import dispatch_harness_gates as hg
        ctx_txt = "\n".join([task, _read(wt / "AUTO-TASK.md")])
        tot = total
        for p in paths:
            blob = hg.blob_at(wt, p) or ""
            # ANCHORED target: the worker reads in capped windows (read_file page <= READ_FILE_MAX_CHARS), so a
            # slice that names resolvable anchor windows never holds the whole file; count only those windows.
            aw = anchored_bytes(blob, ctx_txt) if p == target else None
            tot += aw if aw is not None else len(blob)
        if (wt / "AUTO-TASK.md").is_file():
            tot += (wt / "AUTO-TASK.md").stat().st_size
        est = int(tot / 3.5 * 1.4)
        if est > CTX_CEILING_TOKENS:
            out.append(f"estimated task context ~{est} tokens (target file(s) + harness, +40%) > ceiling "
                       f"{CTX_CEILING_TOKENS} -- split the slice so the target region is small")
    except Exception:
        pass
    return out


# An explicit anchor window = the intent/task confines the model to a region it can find without paging the file.
_ANCHOR_RE = re.compile(r"\banchor(?:ed)?\b|\blines?\s+\d+\s*(?:-|to|through|\u2013)\s*\d+|\bL\d+\s*(?:-|\u2013)\s*L?\d+|"
                        r"\b(?:near|around|after|before|inside|within)\s+(?:line\s+\d+|the\s+(?:function|def|class)\b|`[\w.$]+\(?\)?`)",
                        re.I)


def oversize_target_findings(wt, target, text="") -> list[str]:
    """An AUTHORING stage (s1/s2: nothing else gates it) whose target file is huge exhausts its iterations paging
    the file (pa-supersede-on-import: 7062-line / 329KB arr-webhook.py, 3 rounds, TASK.md never written) and was
    filed as model incapacity. Refuse at enqueue unless the intent/task names an anchor window."""
    out = []
    if not target:
        return out
    try:
        import dispatch_harness_gates as hg
        blob = hg.blob_at(Path(wt), target) or ""
    except Exception:
        return out
    nlines, nbytes = blob.count("\n") + (1 if blob and not blob.endswith("\n") else 0), len(blob.encode("utf-8", "replace"))
    if nlines <= MAX_TARGET_LINES and nbytes <= MAX_TARGET_BYTES:
        return out
    ctx = "\n".join([str(text or ""), _read(Path(wt) / "TASK.md"), _read(Path(wt) / "AUTO-TASK.md")])
    if _ANCHOR_RE.search(ctx):
        return out
    out.append(f"target {target} is {nlines} lines / {nbytes} bytes (> {MAX_TARGET_LINES} lines or {MAX_TARGET_BYTES} bytes) "
               f"and the intent/task names no anchor window -- the model spends its iterations paging the file and never "
               f"writes the harness. Re-cut: put the new logic in a SMALL NEW module (own file + fixture) and add a "
               f"separate wiring slice that imports it into {target}; or name an anchor window (function/lines) in the intent")
    return out


def baseline_findings(wt, target, runner=None, timeout=300) -> list[str]:
    """verify.sh must be RED on the untouched baseline, for an assertion reason."""
    wt = Path(wt)
    if not (wt / "verify.sh").is_file():
        return []
    run = runner or _sh
    scratch = None
    if _refimpl_applied(wt, target):
        # The tree holds a previous round's edits (or a left-applied refimpl): measure the
        # BASELINE in a HEAD scratch copy (harness files overlaid, refimpl NOT applied) rather
        # than skipping or mis-judging the model's own work as the baseline.
        scratch, err = scratch_copy(wt, apply_refimpl=False)
        if not scratch:
            return [f"baseline could not be measured on a dirty tree (scratch copy: {err})"]
    try:
        rc, out = run("bash verify.sh", scratch or wt, timeout)
    finally:
        drop_scratch(scratch)
    if rc == 124:
        return ["verify.sh timed out on the untouched baseline (cannot be measured)"]
    if rc == 0 or "VERIFY_OK" in (out or ""):
        return ["verify.sh PASSES on the untouched baseline (cannot tell done from untouched)"]
    import dispatch_harness_gates as hg
    cls = hg.classify_baseline_red(out, creating=bool(_manifest(wt).get("creation_task")),
                                   spec_text=_read(wt / "TASK.md"))
    if not cls["ok"]:
        return ["verify.sh is red at baseline for the WRONG reason (crash/import/syntax): "
                + "; ".join(cls["signals"])[:200]]
    return []


def _refimpl_applied(wt, target) -> bool:
    """True when the tracked target already differs from HEAD (a refimpl left applied): the
    'baseline' run would measure the solution, so skip rather than mis-judge."""
    if not target:
        return False
    r = subprocess.run(["git", "-C", str(wt), "diff", "--quiet", "HEAD", "--", target],
                       capture_output=True)
    return r.returncode == 1


def _sh(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, ""


def refimpl_static_findings(wt, target) -> list[str]:
    wt = Path(wt)
    if not (wt / "refimpl.py").is_file() or _refimpl_is_stub(wt):
        return []
    import dispatch_harness_gates as hg
    # BASE-ONLY (HEAD blobs). The working-tree variant (hg.refimpl_anchor_findings) would read the
    # previous round's model edits to the target, which legitimately no longer contain the base
    # anchors -- a false refimpl-unapplicable on every round after the first.
    if _manifest(wt).get("creation_task") and target and hg.blob_at(wt, target) is None:
        return []                                    # refimpl writes the whole (new) target
    out = []
    out += [f["message"] if isinstance(f, dict) else str(f)
            for f in hg.refimpl_base_anchor_findings(wt, target, _read(wt / "refimpl.py"))]
    return out


_SYNTAX_RE = re.compile(r"does not parse|WROTE source that does not parse|SyntaxError in the TARGET|emitted a target|emitted unparseable", re.I)
_APPLY_RE = re.compile(r"anchor not found|refimpl\.py errored|refimpl import anchor|could not apply the reference", re.I)


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip())[:120]


def classify_selfcheck_failure(out: str):
    """(cause, detail) for a failing self-check/contract run -- PURE."""
    t = out or ""
    fail_lines = [l for l in t.splitlines() if "FAIL" in l or "Error" in l or "anchor" in l]
    first = _norm(fail_lines[0]) if fail_lines else _norm(t.strip().splitlines()[-1] if t.strip() else "no output")
    if _APPLY_RE.search(t):
        m = _APPLY_RE.search(t)
        return "refimpl-unapplicable", f"refimpl cannot apply: {m.group(0)} ({first})"
    if _SYNTAX_RE.search(t):
        return "refimpl-syntax", f"refimpl wrote source that does not parse ({first})"
    return "contract-broken", f"fail-before/pass-after not proven by the self-check ({first})"


REFINE_GUARD = ".refine-guard.json"        # == ollama-dispatch-auto.REFINE_GUARD
REFINE_NOOP_LIMIT = 2                      # a refine round that changed nothing may be re-admitted ONCE; the 2nd is a retry storm


def _guard_aside(wt):
    """Move an armed refine guard out of the way for the contract measurement. Returns the aside path or None."""
    g = Path(wt) / REFINE_GUARD
    if not g.is_file():
        return None
    a = Path(wt) / (REFINE_GUARD + ".admission-aside")
    try:
        os.replace(g, a)
        return a
    except OSError:
        return None


def _guard_restore(wt, aside):
    if aside is None:
        return
    g = Path(wt) / REFINE_GUARD
    try:
        if g.exists():                       # a fresh guard was written meanwhile: it wins
            Path(aside).unlink(missing_ok=True)
        else:
            os.replace(aside, g)
    except OSError:
        pass


def refine_state_hash(wt):
    """sha1 of the CURRENT bytes of every file the armed refine guard watches (None: no guard). PURE read."""
    g = Path(wt) / REFINE_GUARD
    try:
        files = sorted(json.loads(g.read_text()).get("files") or [])
    except (OSError, ValueError, AttributeError):
        return None
    h = hashlib.sha1()
    for f in files:
        p = Path(wt) / f
        h.update(f.encode() + b"\0" + (hashlib.sha256(p.read_bytes()).digest() if p.is_file() else b"-"))
    return h.hexdigest()[:20]


def note_refine_round(st, state_hash, label=None):
    """Record the state a refine round STARTS from; returns how many consecutive refine rounds have started
    from this identical state (0 = first sighting). A round that changed nothing leaves the state identical."""
    if state_hash is None:
        return 0
    if st.get("refine_state") == state_hash and label is not None and st.get("refine_label") == label:
        return int(st.get("refine_noop") or 0)      # the SAME round re-enqueued (infra retry): not a new no-change round
    st["refine_label"] = label
    if st.get("refine_state") == state_hash:
        st["refine_noop"] = int(st.get("refine_noop") or 0) + 1
    else:
        st["refine_state"], st["refine_noop"] = state_hash, 0
    return st["refine_noop"]


def _is_stale_refine_guard_refusal(st) -> bool:
    d = str(st.get("detail") or "")
    return d.startswith("contract-broken:") and "REFINE NOT DONE" in d


def contract_defects(wt, runner=None, preflight_runner=None, timeout=900) -> list[dict]:
    """[{cause, detail}] from the contract measurement. FAIL-CLOSED: cannot-measure is a defect."""
    wt = Path(wt)
    sc = wt / "auto-harness-check.py"
    if sc.is_file() and preflight_runner is None:
        run = runner or _sh
        # A REFINE round is admitted with .refine-guard.json already armed (write_refine_guard runs just
        # before the enqueue). The guard makes the self-check FAIL "REFINE NOT DONE" while the harness is
        # byte-identical to the round start -- i.e. at EXACTLY this moment -- which classified as
        # contract-broken and refused every refine round before it began (pa-supersede-policy x2, amazon,
        # sidecar-novnc-tunnel-url). The gate measures the CONTRACT (fail-before/pass-after); the guard is
        # a different question (did the model change anything), so it is set aside for the measurement.
        guard_aside = _guard_aside(wt)
        try:
            rc, out = run("python3 auto-harness-check.py", wt, timeout)
        except Exception as e:
            return [{"cause": "contract-broken", "detail": f"contract could not be measured: {type(e).__name__}: {e}"}]
        finally:
            _guard_restore(wt, guard_aside)
        if rc == 0 and "VERIFY_OK" in (out or ""):
            return []
        if rc == 124 or "SELFCHECK_HANG" in (out or ""):
            return [{"cause": "contract-broken", "detail": "contract could not be measured: self-check timed out/hung"}]
        c, d = classify_selfcheck_failure(out)
        return [{"cause": c, "detail": d}]
    if preflight_runner is None and not (HERE / "ollama-dispatch-preflight").is_file():
        return []    # old preflight tool retired (use mini-swe): contract cannot be measured, fail open
    cmd = [sys.executable, str(HERE / "ollama-dispatch-preflight"), str(wt), "--json", "--no-relevance",
           "--no-audit", "--refimpl-cmd", "python3 refimpl.py"]
    try:
        if preflight_runner is not None:
            data = preflight_runner(cmd, wt)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
            data = json.loads(p.stdout)
    except Exception as e:
        return [{"cause": "contract-broken", "detail": f"contract could not be measured (preflight): {type(e).__name__}: {e}"}]
    checks = (data or {}).get("checks") or []
    if not checks:
        return [{"cause": "contract-broken", "detail": "contract could not be measured: preflight returned no checks"}] \
            if preflight_runner is None else []
    by = {c.get("id") or c.get("check"): c for c in checks}
    out = []
    for cid in ("baseline-fails", "refimpl-passes", "refimpl-tests"):
        c = by.get(cid)
        if c and str(c.get("status")) == "FAIL":
            msg = str(c.get("message") or c.get("detail") or "")
            cause, d = classify_selfcheck_failure(msg)
            out.append({"cause": cause, "detail": f"{cid}: {msg[:160]}"})
    return out


def contract_findings(wt, runner=None, timeout=900) -> list[str]:
    return [d["detail"] for d in contract_defects(wt, preflight_runner=runner, timeout=timeout)]


# --- scratch copy (never touches the live worktree) ------------------------------------------
def scratch_copy(wt, apply_refimpl=True):
    """(path|None, err). HEAD tree + the untracked/ignored-not harness files + node_modules link, in a fresh
    standalone git repo; refimpl.py applied there. The live worktree (and its gitdir) is never written."""
    import tempfile
    wt = Path(wt)
    tmp = Path(tempfile.mkdtemp(prefix="ha-scratch-"))
    try:
        a = subprocess.run(["git", "-C", str(wt), "archive", "HEAD"], capture_output=True, timeout=120)
        if a.returncode != 0:
            return None, "git archive failed: " + a.stderr.decode("utf-8", "replace")[-200:]
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=a.stdout, check=True, timeout=120)
        for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(tmp), *c], check=True, capture_output=True, timeout=120)
        # untracked AND git-ignored files: the harness manifest (.dispatch-harness.json),
        # auto-harness-check.py and generated code (prisma client) are all ignored in real repos
        ls = subprocess.run(["git", "-C", str(wt), "ls-files", "-o", "--exclude-standard"], capture_output=True,
                            text=True, timeout=60).stdout.splitlines()
        ls += subprocess.run(["git", "-C", str(wt), "ls-files", "-o", "-i", "--exclude-standard"], capture_output=True,
                             text=True, timeout=60).stdout.splitlines()
        for f in ls:
            if f.startswith(("node_modules/", ".next/", ".git/")) or "/__pycache__/" in f or f.endswith(".tsbuildinfo"):
                continue
            try:
                if not (wt / f).is_file() or (wt / f).stat().st_size > 5_000_000:
                    continue
            except OSError:
                continue
            (tmp / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(wt / f, tmp / f)
        nm = wt / "node_modules"
        if nm.exists() and not (tmp / "node_modules").exists():
            os.symlink(os.path.realpath(nm), tmp / "node_modules")
        if apply_refimpl and (tmp / "refimpl.py").is_file():
            r = subprocess.run([sys.executable, "refimpl.py"], cwd=str(tmp), capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return tmp, "refimpl.py failed in the scratch copy: " + ((r.stderr or r.stdout)[-200:])
        return tmp, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def drop_scratch(path):
    if path:
        shutil.rmtree(path, ignore_errors=True)


_CASE_RES = (re.compile(r"^\s*(not ok|ok)\s+\d+\s*-\s*(.+?)(?:\s*#.*)?$", re.M),
             re.compile(r"^\s*(✔|✖|✓|✗|PASS|FAIL|PASSED|FAILED)[:\s]+(.+?)(?:\s+\([\d.]+ms\))?$", re.M))


def case_results(out: str) -> dict:
    """{case name: 'pass'|'fail'} read off TAP / spec-reporter / PASS|FAIL lines. {} when nothing parses."""
    res = {}
    for rx in _CASE_RES:
        for st, name in rx.findall(out or ""):
            res[name.strip()] = "pass" if st in ("ok", "✔", "✓", "PASS", "PASSED") else "fail"
    return res


def flake_defects(wt, notes, scratch=None, flake_runner=None) -> list[dict]:
    """Run verify.sh twice with the refimpl applied; different case results => flaky-fixture."""
    wt = Path(wt)
    try:
        if flake_runner is not None:
            secs, o1, o2 = flake_runner(wt)
        else:
            sc = scratch
            if not sc:
                notes.append("flakiness check skipped: no scratch copy available")
                return []
            t0 = time.time()
            r1 = _sh("bash verify.sh", sc, 600)
            secs = time.time() - t0
            if secs > FLAKE_MAX_S or r1[0] == 124:
                notes.append(f"flakiness check SKIPPED (not passed): first verify run took {secs:.0f}s > "
                             f"HARNESS_FLAKE_MAX_S={FLAKE_MAX_S:.0f}s")
                return []
            r2 = _sh("bash verify.sh", sc, 600)
            o1, o2 = r1[1], r2[1]
            rc1, rc2 = r1[0], r2[0]
            if rc1 != rc2:
                return [{"cause": "flaky-fixture", "detail": f"verify exit differs between two identical runs ({rc1} vs {rc2})"}]
        if secs > FLAKE_MAX_S:
            notes.append(f"flakiness check SKIPPED (not passed): first run took {secs:.0f}s > {FLAKE_MAX_S:.0f}s")
            return []
        c1, c2 = case_results(o1), case_results(o2)
        if not c1 and not c2:
            import dispatch_harness_gates as hg
            if hg.failure_signature(o1) != hg.failure_signature(o2):
                return [{"cause": "flaky-fixture", "detail": "verify output signature differs between two identical runs"}]
            notes.append("flakiness check weak: no per-case results parsed from verify output")
            return []
        diff = sorted(k for k in set(c1) | set(c2) if c1.get(k) != c2.get(k))
        if diff:
            return [{"cause": "flaky-fixture",
                     "detail": f"{len(diff)} case(s) differ between two identical runs with the refimpl applied: "
                               + "; ".join(f"{k!r}: {c1.get(k, 'absent')} -> {c2.get(k, 'absent')}" for k in diff[:4])}]
        return []
    except Exception as e:
        return [{"cause": "flaky-fixture", "detail": f"flakiness check could not run: {type(e).__name__}: {e}"}]


def typecheck_defects(wt, notes, scratch=None, tc_runner=None) -> list[dict]:
    wt = Path(wt)
    fx = fixture_name(wt)
    if not fx:
        return []
    if not fx.endswith((".ts", ".tsx", ".mts")):
        notes.append(f"fixture type-check skipped: {fx} is not TypeScript")
        return []
    try:
        if tc_runner is not None:
            data = tc_runner(wt, fx)
        else:
            if not scratch:
                notes.append("fixture type-check skipped: no scratch copy available")
                return []
            r = subprocess.run(["node", str(HERE / "ha_tscheck.js"), str(scratch), fx], capture_output=True,
                               text=True, errors="replace", timeout=TSC_TIMEOUT_S)
            try:
                data = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                return [{"cause": "fixture-type-mismatch",
                         "detail": "type-check could not be measured: " + ((r.stderr or r.stdout)[-200:] or f"rc={r.returncode}")}]
    except FileNotFoundError:
        notes.append("fixture type-check skipped: node not found")
        return []
    except subprocess.TimeoutExpired:
        return [{"cause": "fixture-type-mismatch", "detail": f"type-check timed out after {TSC_TIMEOUT_S}s (not measured)"}]
    except Exception as e:
        return [{"cause": "fixture-type-mismatch", "detail": f"type-check could not run: {type(e).__name__}: {e}"}]
    if data.get("skipped"):
        notes.append("fixture type-check skipped: " + str(data["skipped"]))
    for n in data.get("notes") or []:
        notes.append("typecheck: " + str(n)[:200])
    return [{"cause": "fixture-type-mismatch", "detail": e["line"][:300]} for e in (data.get("errors") or [])]


# --- the gate -------------------------------------------------------------------------------
def _sig(defects) -> str:
    return hashlib.sha1("\0".join(sorted(f"{d['cause']}:{d['detail'][:80]}" for d in defects)).encode()).hexdigest()[:12]


def _closed(cause, fn, *a, **k):
    """Run a check; an exception inside the gate is a DEFECT (never silently ok)."""
    try:
        return fn(*a, **k)
    except Exception as e:
        return [{"__err__": f"admission check could not run ({cause}): {type(e).__name__}: {e}"}]


def collect(wt, target=None, lang=None, dynamic=True, runner=None, preflight_runner=None, refimpl_checks=True,
            selfcheck_runner=None, notes=None, flake_runner=None, typecheck_runner=None, meta=None):
    notes = notes if notes is not None else []
    meta = meta if meta is not None else {}
    wt = Path(wt)
    target = target or _manifest(wt).get("target")
    defects = []

    def add(cause, items, key=None):
        for it in items:
            if isinstance(it, dict) and "__err__" in it:
                defects.append({"cause": cause, "detail": it["__err__"]})
            elif isinstance(it, dict) and "cause" in it:
                defects.append(it)
            else:
                defects.append({"cause": cause, "detail": (it.get(key) if key else it) if isinstance(it, dict) else str(it)})
    add("oversize-reslice", _closed("size", size_findings, wt, target))
    add("fixture-untraceable", _closed("traceability", traceability_findings, wt, target, lang), key="detail")
    if fixture_name(wt):
        add("baseline-not-red", _closed("baseline", baseline_findings, wt, target, runner=runner))
    if refimpl_checks and (wt / "refimpl.py").is_file() and not _refimpl_is_stub(wt):
        add("refimpl-unapplicable", _closed("refimpl-static", refimpl_static_findings, wt, target))
        if dynamic and not any(x["cause"] == "refimpl-unapplicable" for x in defects):
            _cd = _closed("contract", contract_defects, wt, runner=selfcheck_runner,
                          preflight_runner=preflight_runner)
            add("contract-broken", _cd)
            meta["contract_green"] = not _cd          # the contract was MEASURED and held
            if not any(x["cause"] in _REFIMPL_SIDE for x in defects):       # contract held: flakiness + types
                scratch, err = (None, "")
                injected = flake_runner is not None and typecheck_runner is not None
                if not injected:
                    scratch, err = scratch_copy(wt)
                    if err:
                        notes.append("scratch copy problem: " + err)
                        if "refimpl.py failed" in err:           # the refimpl would not even apply there
                            add("refimpl-unapplicable", [err])
                            drop_scratch(scratch)
                            return defects
                try:
                    add("flaky-fixture", _closed("flaky", flake_defects, wt, notes, scratch, flake_runner))
                    add("fixture-type-mismatch", _closed("typecheck", typecheck_defects, wt, notes, scratch,
                                                         typecheck_runner))
                finally:
                    drop_scratch(scratch)
    return defects


def admit(wt, label, target=None, lang=None, dynamic=True, runner=None, preflight_runner=None, text=None,
          repair=False, selfcheck_runner=None, flake_runner=None, typecheck_runner=None) -> dict:
    """The gate. Returns {ok, tier, cause, defects, message, advisory}.  ok=False => do NOT enqueue.
    `advisory` (list of defect strings) => enqueue but append them to the model's prompt."""
    wt = Path(wt)
    tier = "repair" if repair else tier_for_label(label)
    st = load_state(wt)
    fp = fingerprint(wt)
    res = {"notes": [], "ok": True, "tier": tier, "cause": None, "defects": [], "message": "", "advisory": [], "fp": fp}
    if st.get("fp") != fp:
        # status / frozen snapshot are bound to the harness bytes and reset with them; the defect-signature
        # HISTORY is keyed by signature and survives (a re-edited harness with the same defect is a recurrence).
        st = {"fp": fp, "history": st.get("history", []), "status": "new"}
    if st.get("status") == "reslice" and _is_stale_refine_guard_refusal(st):
        # Refusals recorded by the pre-fix gate: the armed refine guard's "REFINE NOT DONE" was measured as a
        # broken contract. That was never a harness defect (see contract_defects); the record is void.
        st = {"fp": fp, "history": [h for h in st.get("history", []) if h.get("tier") != "refine"], "status": "new"}
        save_state(wt, st)
        res["notes"].append("voided a stale refine-guard false-positive reslice record")
    if st.get("status") == "reslice" and tier != "repair":
        res.update(ok=False, cause="reslice-required",
                   message=f"{REFUSAL_PREFIX} [reslice-required]: {st.get('detail', 'a runaway/repeated-signature stop')} "
                           f"was recorded for this exact harness; another round cannot change it. Re-slice / re-spec "
                           f"(or repair the harness via --retry-slice/--regate) -- the harness must re-pass the gate.")
        return res
    if tier is None:
        if str(label or "").startswith("auto-author-"):
            ov = oversize_target_findings(wt, target, text)
            if ov:
                res.update(ok=False, cause="oversize-reslice",
                           message=f"{REFUSAL_PREFIX} [oversize-reslice]: {ov[0]} -- NOT enqueuing {label}.",
                           defects=[{"cause": "oversize-reslice", "detail": ov[0]}])
        return res
    if tier != "strict" and not harness_started(wt):
        # Round 0 of the legacy (unstaged) author flow: TASK.md still has its scaffold placeholders AND
        # refimpl.py is still the stub -- there is no harness to admit yet. Running the dynamic checks here
        # only delays the first enqueue (a foreign bundle then wins the lane) and judges the scaffold.
        res["notes"].append("harness not started (scaffold stubs): nothing to admit yet")
        return res
    notes = []
    meta = {}
    defects = collect(wt, target, lang, dynamic=dynamic, runner=runner, preflight_runner=preflight_runner,
                      selfcheck_runner=selfcheck_runner, notes=notes, flake_runner=flake_runner,
                      typecheck_runner=typecheck_runner, meta=meta)
    if tier != "strict":     # the context-token estimate models a CODING job that loads the target; authoring pages it
        defects = [d for d in defects if not str(d.get("detail", "")).startswith("estimated task context")]
    res["defects"] = defects
    res["notes"] = notes
    if tier == "repair":                  # the repair path never rewrites the refusal state it exists to clear
        res["advisory"] = [f"[{d['cause']}] {d['detail']}" for d in defects]
        return res
    if tier == "refine":
        # RETRY STORM: a refine round that ends with the harness byte-identical (nothing killed a survivor)
        # re-queues the same state. One re-admission is tolerated; the next is the runaway.
        noop = note_refine_round(st, refine_state_hash(wt), str(label))
        if noop >= REFINE_NOOP_LIMIT:
            st.update(status="reslice", detail=f"refine-no-change: {noop} refine rounds started from byte-identical state", fp=fp)
            save_state(wt, st)
            res.update(ok=False, cause="refine-no-change",
                       message=f"{REFUSAL_PREFIX} [refine-no-change]: the previous {noop} refine rounds changed nothing "
                               f"(fixture/refimpl/TASK/target byte-identical) -- NOT enqueuing another job; re-slice / "
                               f"re-spec, or repair the harness through the repair path and it must re-pass this gate.")
            return res
    sig = _sig(defects)
    prev_sigs = [h.get("sig") for h in st.get("history", [])]
    st.setdefault("history", []).append({"sig": sig, "tier": tier, "label": str(label), "at": time.time(),
                                         "causes": sorted({d["cause"] for d in defects})})
    st["history"] = st["history"][-20:]
    if not defects:
        st.update(status="admitted", fp=fp, sig=sig)
        st.pop("frozen", None); st.pop("frozen_fp", None)
        if meta.get("contract_green") and harness_complete_files(wt):
            _snapshot(wt, st)             # FREEZE only a COMPLETE harness whose contract was measured green
        else:
            res["notes"].append("admitted without freeze: "
                                + ("harness files incomplete/stub" if meta.get("contract_green") else "contract not measured"))
        save_state(wt, st)
        return res
    lines = [f"[{d['cause']}] {d['detail']}" for d in defects]
    over = [d for d in defects if d["cause"] == "oversize-reslice"]
    refuse = None
    if over and tier != "repair":
        refuse = ("oversize-reslice", "the slice is too big to author as one harness -- re-slice it")
    elif tier == "strict":
        refuse = (defects[0]["cause"], "the harness is defective")
    elif tier == "refine" and any(d["cause"] in _REFIMPL_SIDE for d in defects):
        refuse = (next(d["cause"] for d in defects if d["cause"] in _REFIMPL_SIDE),
                  "a refine round needs a converged refimpl (fail-before/pass-after proven)")
    elif tier in ("authoring", "stage-refimpl", "refine") and sig in prev_sigs:
        refuse = ("repeated-defect", "this round would run against the SAME defect set the previous round already ran against")
    if refuse and tier != "repair":
        st.update(status="reslice", detail=f"{refuse[0]}: {lines[0][:160]}", fp=fp)
        save_state(wt, st)
        res.update(ok=False, cause=refuse[0],
                   message=f"{REFUSAL_PREFIX} [{refuse[0]}]: {refuse[1]}. Defects: " + " | ".join(lines)[:700]
                           + " -- NOT enqueuing another job; fix the harness through the repair path "
                             "(heal_harness_repair / --retry-slice / --regate) and it must re-pass this gate.")
        return res
    st["status"] = "advisory"
    save_state(wt, st)
    res["advisory"] = lines
    return res


def advisory_prompt(res) -> str:
    if not res.get("advisory"):
        return ""
    return ("\n\n## HARNESS ADMISSION DEFECTS (fix these in THIS round; a recurrence stops the run)\n"
            + "\n".join(f"- {l}" for l in res["advisory"]) + "\n")


# --- freeze -----------------------------------------------------------------------------
def _snap_dir(wt) -> Path:
    return _state_dir() / f"{_key(wt)}.snap"


def harness_complete_files(wt) -> bool:
    """True only when TASK.md, the fixture AND refimpl.py all exist and none is a scaffold stub/placeholder."""
    wt = Path(wt)
    fx = fixture_name(wt)
    if not fx or not (wt / "TASK.md").is_file() or not (wt / "refimpl.py").is_file() or _refimpl_is_stub(wt):
        return False
    try:
        import dispatch_harness_gates as hg
        return not hg.placeholder_findings(_read(wt / "TASK.md"), _read(wt / "refimpl.py"), {fx: _read(wt / fx)})
    except Exception:
        return False


def harness_started(wt) -> bool:
    """False while the harness is still the untouched scaffold: refimpl.py is the stub AND TASK.md still
    carries its TODO placeholders."""
    wt = Path(wt)
    if not _refimpl_is_stub(wt):
        return True
    try:
        import dispatch_harness_gates as hg
        return not bool(hg._TASK_PLACEHOLDER_RE.search(_read(wt / "TASK.md")))
    except Exception:
        return True               # cannot tell -> treat as started (fail toward gating)


def clear_state(wt) -> None:
    """Forget ALL admission state (json + snapshot dir) for a worktree path: called when the worktree is
    removed or re-scaffolded, so a fresh scaffold at the same path starts clean."""
    try:
        state_path(wt).unlink(missing_ok=True)
        shutil.rmtree(_snap_dir(wt), ignore_errors=True)
    except OSError:
        pass


def _snapshot(wt, st):
    d = _snap_dir(wt)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for n in frozen_files(wt):
        shutil.copy2(Path(wt) / n, d / n)
        files[n] = hashlib.sha256((Path(wt) / n).read_bytes()).hexdigest()
    st["frozen"] = files
    st["frozen_fp"] = st.get("fp")


_UNFROZEN_LABEL_RE = re.compile(r"(?:-s[123](?:-r\d+)?$|^auto-refine-|-r10\d+$)")


def enforce_freeze(wt, label=None) -> list[str]:
    """After an auto-author job: restore any admitted-frozen harness file the job changed.
    Returns the restored names ([] when nothing was frozen / nothing changed).

    Guards (a freeze must never revert real authoring):
      * only a snapshot taken of a COMPLETE harness at a measured-green contract exists (see admit);
      * the state's current fingerprint must equal the fingerprint recorded AT SNAPSHOT time;
      * staged -s1/-s2/-s3 labels, refine rounds and repair jobs never get a freeze applied;
      * a re-scaffolded path (stub/placeholder files again) clears the stale state instead."""
    wt = Path(wt)
    st = load_state(wt)
    if st.get("status") != "admitted" or not st.get("frozen"):
        return []
    if label and _UNFROZEN_LABEL_RE.search(str(label)):
        return []
    if not harness_complete_files(wt):          # re-scaffold at the same path: the snapshot is stale
        clear_state(wt)
        return []
    if not st.get("frozen_fp") or st.get("fp") != st.get("frozen_fp"):
        return []
    restored = []
    for n, h in st["frozen"].items():
        p, src = wt / n, _snap_dir(wt) / n
        cur = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        if cur != h and src.is_file():
            shutil.copy2(src, p)
            restored.append(n)
    return restored


def note_stop(wt, stop_reason, detail="") -> None:
    """A runaway / repeated-signature style stop: this exact harness must not get another identical round."""
    try:
        st = load_state(wt)
        fp = fingerprint(wt)
        if st.get("fp") != fp:
            st = {"fp": fp, "history": st.get("history", [])}
        st.update(status="reslice", detail=f"{stop_reason}: {str(detail)[:160]}", fp=fp)
        save_state(wt, st)
    except Exception:
        pass


def backstop_refusal(cwd, label) -> str | None:
    """For ollama-queue.py enqueue (no subprocess, no verify run): refuse an author/refine job whose
    harness has a recorded reslice/refused state at the CURRENT fingerprint. None = allow."""
    try:
        l = str(label or "")
        if not (l.startswith("auto-author-") or l.startswith("auto-refine-")) or _REPAIR_RE.search(l):
            return None
        if not cwd or not Path(cwd).is_dir():
            return None
        st = load_state(cwd)
        if st.get("status") == "reslice" and st.get("fp") == fingerprint(cwd):
            return (f"{REFUSAL_PREFIX} [reslice-required]: {st.get('detail', '')} -- harness unchanged since the "
                    f"stop; re-slice / repair it (heal_harness_repair / --retry-slice) so it re-passes the gate")
    except Exception:
        return None
    return None


def main(argv=None):
    av = list(sys.argv[1:] if argv is None else argv)
    if len(av) >= 2 and av[0] == "check":
        r = admit(av[1], av[2] if len(av) > 2 else "auto-refine-cli", dynamic="--no-dynamic" not in av)
        print(json.dumps({k: r[k] for k in ("ok", "tier", "cause", "defects", "message")}, indent=1))
        return 0 if r["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
