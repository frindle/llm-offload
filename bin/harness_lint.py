#!/usr/bin/env python3
"""harness-lint -- ONE pre-run validator for a dispatch harness worktree (2026-10-09).

A failing SPEC must never cost a model run (rt-walmart-cancel-import burned ~6 author rounds on a
Must-contain literal `normalize(orderNumber)` that the base file spells `normalize(r.orderNumber)`).
Every finding is a NAMED spec defect in the format the escalation / self-heal / slicer path already
parses:   SPEC_DEFECT: <reason>: <path> -- <message>

Checks (each a pure function returning findings; run_lint() composes them):
  literals      each `## Must contain` literal exists in its file at HEAD, or appears in the
                REFIMPL-APPLIED tree (refimpl run in a throwaway `git archive HEAD` copy); a literal
                that is absent from both gets a difflib "did you mean" against the nearest base line
  consistency   TASK.md scope line / forbidden-edit list / pinned-literal files / target agree
  authoring     refimpl swallowing broad exceptions, JS fixture load-crash traps, page.evaluate callbacks calling
                module-scope helpers, real-time polling sleeps in a fixture (harness_authoring_findings)
  stage_dryrun  the stage's own static check (`ollama-dispatch-auto --stage-check STAGE`) executes
                and prints STAGE_OK / STAGE_FAIL before any model is started
  stale_prompt  AUTO-TASK.md (the prompt a continuation/resume round would send) quotes no
                Must-contain literal that the CURRENT TASK.md no longer lists
  retry         the last two failed rounds in the chain's attempts file carry the identical
                missing-literal set (or identical failure signature) -> re-spec/park, no new round
  contract      (--full only) fail-before/pass-after via ollama-dispatch-preflight --json (not
                re-implemented here)

CLI:  harness-lint.py <worktree> [--stage task|fixture|refimpl] [--full] [--quick] [--json]
      exit 0 clean, 3 spec defect(s), 2 usage.   --quick skips the refimpl-applied run + dry-run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
_HG = []


def _hg():
    if not _HG:
        sp = importlib.util.spec_from_file_location("dispatch_harness_gates", BIN / "dispatch_harness_gates.py")
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        _HG.append(m)
    return _HG[0]


def _read(p):
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return ""


def _f(code, reason, path, message):
    return {"code": code, "reason": reason, "path": path, "message": message}


def _manifest(wt):
    try:
        return json.loads(_read(Path(wt) / ".dispatch-harness.json")) or {}
    except ValueError:
        return {}


def _is_real_task(task):
    hg = _hg()
    return bool(hg.must_contain_literals(task)) and not hg._TASK_PLACEHOLDER_RE.search(task)


# ---------------------------------------------------------------- literals
def refimpl_applied_texts(wt, files, timeout=90):
    """{path: text} of `files` after running refimpl.py in a throwaway copy of HEAD. None when
    the refimpl cannot be run there (missing / errors / timeout): the caller then skips (b)."""
    wt = Path(wt)
    ri = wt / "refimpl.py"
    if not ri.is_file() or "TODO" in _read(ri)[:400] and "STUB" in _read(ri).upper()[:600]:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="hlint-"))
    try:
        ar = subprocess.run(["git", "-C", str(wt), "archive", "HEAD"], capture_output=True, timeout=60)
        if ar.returncode != 0:
            return None
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=ar.stdout, check=True, capture_output=True, timeout=60)
        shutil.copy2(ri, tmp / "refimpl.py")
        r = subprocess.run([sys.executable, "refimpl.py", str(tmp)], cwd=str(tmp), capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return {p: _read(tmp / p) for p in files}
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_literals(wt, task, target, apply_refimpl=True):
    hg = _hg()
    wt = Path(wt)
    pairs = hg.must_contain_literals(task)
    if not pairs:
        return []
    refimpl_text = _read(wt / "refimpl.py")
    files = sorted({f or target for f, _l in pairs if (f or target)})
    applied = refimpl_applied_texts(wt, files) if apply_refimpl else None
    out = list(hg.literal_shape_findings(task))                 # (a) slash-wrapped identifier: static
    for f, lit in pairs:
        path = f or target or ""
        if not path:
            continue
        if applied is not None and hg.unnatural_literal_context(lit, applied.get(path, "")):
            out += hg.literal_context_findings([(f, lit)], {path: applied.get(path, "")})   # (b)
            continue
        base = hg.blob_at(wt, path, "HEAD")
        if base is None:                       # creation target: nothing at HEAD to compare
            base = ""
        if hg.count_occurrences(lit, base):
            continue
        if applied is not None and hg.count_occurrences(lit, applied.get(path, "")):
            continue
        nm = hg.literal_near_miss(lit, base)
        if nm:
            ln, line, win = nm
            out.append(_f(hg.LITERAL_NEAR_MISS, "Must-contain literal near-misses the base code", path,
                          f"`{lit}` is absent at HEAD" + (" and from the refimpl-applied tree" if applied is not None else "")
                          + f", but line {ln} has `{win}` -- did you mean `{win}`? (line: {line[:120]})"))
        elif applied is not None:
            out.append(_f(hg.REFIMPL_LITERAL_ABSENT, "Must-contain literal absent after the refimpl is applied", path,
                          f"`{lit}` is neither in {path} at HEAD nor written by refimpl.py -- unsatisfiable as specced"))
        elif apply_refimpl and refimpl_text and lit not in refimpl_text:
            # refimpl could not be run here: weaker, text-level evidence only. NEVER in quick mode
            # (stage entry): the refimpl is not authored yet at the task/fixture stages.
            out.append(_f("REFIMPL_LITERAL_UNPROVEN", "Must-contain literal not at HEAD and not in refimpl.py text", path,
                          f"`{lit}` is absent at HEAD and refimpl.py never spells it (refimpl not runnable here)"))
    return out


# ------------------------------------------------- authoring-defect lints (2026-10-11)
def check_authoring(wt, target, apply_refimpl=True):
    """REFIMPL_SWALLOWS_EXCEPTION / JS_FIXTURE_LOAD_TRAP / REAL_TIME_POLLING_FIXTURE (static) plus, when the refimpl
    can be applied in a throwaway tree, EVALUATE_NOT_SELF_CONTAINED on the refimpl-APPLIED target (a helper called from
    a page.evaluate-serialized function only exists after the refimpl wrote it)."""
    hg = _hg()
    tt = ""
    if apply_refimpl and target:
        ap = refimpl_applied_texts(wt, [target])
        tt = (ap or {}).get(target, "") if ap else ""
    return hg.harness_authoring_findings(wt, tt)


# ------------------------------------------------------------- consistency
_SCOPE_RE = re.compile(r"Only edit\s+((?:`[^`]+`(?:\s*(?:,|and)\s*)?)+)", re.I)
_FORBID_RE = re.compile(r"(?:do not|don't|never)\s+edit\s+((?:`[^`]+`(?:\s*(?:,|or|and)\s*)?)+)", re.I)


def _ticks(s):
    return re.findall(r"`([^`]+)`", s or "")


def check_consistency(task, target):
    hg = _hg()
    out = []
    scope, forbid = set(), set()
    for m in _SCOPE_RE.finditer(task):
        scope |= set(_ticks(m.group(1)))
    for m in _FORBID_RE.finditer(task):
        forbid |= set(_ticks(m.group(1)))
    pinned = {f for f, _l in hg.must_contain_literals(task) if f}
    if target and forbid and target in forbid:
        out.append(_f("SCOPE_FORBIDS_TARGET", "scope forbids editing the target", target,
                      f"the forbidden-edit list names the target `{target}` itself"))
    if scope and target and target not in scope:
        out.append(_f("TARGET_OUTSIDE_SCOPE", "target is outside the stated scope", target,
                      f"scope line allows {sorted(scope)} but the target is `{target}`"))
    for f in sorted(pinned):
        if f in forbid:
            out.append(_f("LITERAL_IN_FORBIDDEN_FILE", "required literal lives in a file the spec forbids editing", f,
                          f"a Must-contain literal is pinned to `{f}`, which the spec says not to edit"))
        elif scope and f not in scope and f != target:
            out.append(_f("LITERAL_OUTSIDE_SCOPE", "required literal pinned to a file outside the scope", f,
                          f"a Must-contain literal is pinned to `{f}` but the scope only allows {sorted(scope)}"))
    return out


# ------------------------------------------------------------- stage dry-run
def check_stage_dryrun(wt, stage, target, lang="typescript", timeout=60):
    auto = BIN / "ollama-dispatch-auto"
    if not auto.is_file():
        return []    # old auto tool retired (use mini-swe): no stage self-check to run
    try:
        r = subprocess.run([sys.executable, str(auto), "--stage-check", stage, "--lang", lang,
                            "--target", target or ""], cwd=str(wt), capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        return [_f("STAGE_CHECK_UNRUNNABLE", "the stage's self-check could not execute", str(wt),
                   f"{type(e).__name__}: {e}")]
    first = (r.stdout.strip().splitlines() or [""])[0]
    if not first.startswith(("STAGE_OK", "STAGE_FAIL")):
        return [_f("STAGE_CHECK_UNPARSEABLE", "the stage's self-check output is not parseable", str(wt),
                   f"rc={r.returncode}, first line {first[:120]!r}, stderr {r.stderr.strip()[-160:]!r}")]
    return []


# ------------------------------------------------------------ stale prompt
def check_stale_prompt(wt, task):
    hg = _hg()
    prompt = _read(Path(wt) / "AUTO-TASK.md")
    if not prompt or not task:
        return []
    cur = {l for _f_, l in hg.must_contain_literals(task)}
    stale = []
    for line in prompt.splitlines():
        # ONLY the check's own "MISSING literal ... >>> lit <<<" report lines count: the prompt's
        # boilerplate (refimpl.py, TODO, "git checkout -- <file>") is not a spec literal.
        if re.search(r"MISSING literal", line, re.I):
            for lit in re.findall(r">>>(.*?)<<<", line):
                lit = lit.strip()
                if lit and lit not in cur and lit not in task:
                    stale.append(lit)
    stale = sorted(set(stale))
    if not stale:
        return []
    return [_f("STALE_SPEC_IN_PROMPT", "the pending prompt quotes literal(s) the current TASK.md no longer lists",
               "AUTO-TASK.md",
               f"{stale[:4]} appear in AUTO-TASK.md's literal/verify text but not in TASK.md: rebuild the "
               f"round's prompt from the CURRENT TASK.md and check output (never a prior transcript)")]


# ------------------------------------------------------------------ retry
def attempts_for(wt):
    label = Path(wt).name[3:] if Path(wt).name.startswith("wt-") else Path(wt).name
    d = Path.home() / ".ollama-dispatch" / "auto-runs"
    best = []
    for p in d.glob(f"*{label}.attempts.json"):
        try:
            h = json.loads(p.read_text())
            if isinstance(h, list) and len(h) > len(best):
                best = h
        except (OSError, ValueError):
            pass
    return best


def _ts(s):
    import calendar, time
    try:
        return calendar.timegm(time.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def check_retry(hist, task_mtime=None):
    """Two failed rounds, identical signature -> spec defect. A TASK.md edited AFTER the last
    failed attempt is an operator/respec fix: the history no longer applies (sanctioned path)."""
    hg = _hg()
    h = [x for x in hist or [] if isinstance(x, dict)]
    if len(h) < 2:
        return []
    a, b = h[-2], h[-1]
    tb = _ts(b.get("at"))
    if task_mtime is not None and tb is not None and task_mtime > tb:
        return []
    ma, mb = a.get("missing_literals") or [], b.get("missing_literals") or []
    if hg.repeat_missing_literals(ma, mb):
        return [_f(hg.REPEAT_MISSING_LITERALS, "same missing-literal set two rounds running", "TASK.md",
                   hg.format_repeat_missing([tuple(x) for x in mb])[len(hg.SPEC_DEFECT_PREFIX):])]
    sa = hg.failure_signature(str(a.get("check_tail") or "") + str(a.get("reason") or ""))
    sb = hg.failure_signature(str(b.get("check_tail") or "") + str(b.get("reason") or ""))
    if sa and sa == sb:
        return [_f("REPEAT_FAILURE_SIGNATURE", "identical failure signature two rounds running", "TASK.md",
                   f"rounds {a.get('job')} and {b.get('job')} failed with the same signature {sa}: re-spec or park")]
    return []


# ------------------------------------------------- refimpl vs fixture (self-consistency)
def check_refimpl_vs_fixture(wt, timeout=420):
    """The refimpl, applied to a throwaway copy of HEAD with the authored harness files, must make
    the harness's own verify.sh print VERIFY_OK. harness-lint --full delegates fail-before/pass-after
    to preflight, which reports UNPR (and lint passed) whenever the worktree is not at baseline --
    exactly when a salvaged model diff sits in the tree. rt-walmart-cancel-import: a refimpl that
    counted `cancelledMarked` AFTER the update loop failed the fixture's aliasing fake prisma
    (0 !== 1) while lint said HARNESS_LINT_OK for 7 rounds. Fail-open on infrastructure errors."""
    wt = Path(wt)
    ri, vs = wt / "refimpl.py", wt / "verify.sh"
    if not ri.is_file() or not vs.is_file():
        return []
    rt = _read(ri)
    if "TODO" in rt[:400] and "STUB" in rt.upper()[:600]:
        return []
    if "SCAFFOLD: cases not yet authored" in _read(wt / "verify.test.ts"):
        return []
    tmp = Path(tempfile.mkdtemp(prefix="hlint-rv-"))
    try:
        ar = subprocess.run(["git", "-C", str(wt), "archive", "HEAD"], capture_output=True, timeout=60)
        if ar.returncode != 0:
            return []
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=ar.stdout, check=True, capture_output=True, timeout=60)
        for f in (man_authored(wt) or []):
            if (wt / f).is_file():
                (tmp / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wt / f, tmp / f)
        nm = wt / "node_modules"
        if nm.exists() and not (tmp / "node_modules").exists():
            os.symlink(os.path.realpath(nm), tmp / "node_modules")
        r = subprocess.run([sys.executable, "refimpl.py", str(tmp)], cwd=str(tmp), capture_output=True,
                           text=True, timeout=90)
        if r.returncode != 0:
            return []                       # refimpl unrunnable is the literals check's business
        v = subprocess.run(["bash", "verify.sh"], cwd=str(tmp), capture_output=True, text=True, timeout=timeout)
        if "VERIFY_OK" in (v.stdout or "") and v.returncode == 0:
            return []
        tail = " | ".join(l.strip() for l in (v.stdout or "").splitlines() if l.strip())[-400:]
        return [_f("REFIMPL_FAILS_FIXTURE", "the refimpl does not satisfy the harness's own verify.sh",
                   str(wt / "refimpl.py"),
                   "refimpl applied to HEAD does not print VERIFY_OK: the fixture and refimpl contradict "
                   "(fixture double aliasing/over-strict, or refimpl wrong). Tail: " + tail)]
    except (subprocess.SubprocessError, OSError):
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def man_authored(wt):
    return _manifest(wt).get("authored") or ["TASK.md", "check_literals.py", "verify.sh", "verify.test.ts"]


# ---------------------------------------------------------------- contract
def check_contract(wt):
    if not (BIN / "ollama-dispatch-preflight").is_file():
        return []    # old preflight tool retired (use mini-swe)
    try:
        r = subprocess.run([sys.executable, str(BIN / "ollama-dispatch-preflight"), str(wt), "--json",
                            "--refimpl-cmd", "python3 refimpl.py"], capture_output=True, text=True, timeout=1500)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return [_f("CONTRACT_UNRUN", "preflight could not be run", str(wt), str(e)[:200])]
    out = []
    for b in data.get("blockers") or []:
        if b.get("check") in ("baseline-fails", "refimpl-passes", "spec-defect", "refimpl-satisfies"):
            out.append(_f("CONTRACT_" + b["check"].upper().replace("-", "_"), "fail-before/pass-after not proven",
                          str(wt), (b.get("message") or "")[:200]))
    return out


# --------------------------------------------------------------------- run
def run_lint(wt, stage=None, target=None, full=False, quick=False, attempts=None, lang="typescript"):
    """-> [finding]. Fail-open on infrastructure errors (a lint bug must never strand a run)."""
    wt = Path(wt)
    task = _read(wt / "TASK.md")
    man = _manifest(wt)
    target = target or man.get("target")
    out = []
    if _is_real_task(task):
        for fn in (lambda: check_literals(wt, task, target, apply_refimpl=not quick),
                   lambda: check_consistency(task, target),
                   lambda: list(_hg().spec_test_parity_findings(wt, task)),
                   lambda: check_authoring(wt, target, apply_refimpl=not quick),
                   lambda: list(_hg().base_poison_findings(wt, target)),
                   lambda: check_stale_prompt(wt, task)):
            try:
                out += fn()
            except Exception as e:                       # noqa: BLE001
                print(f"[harness-lint] check crashed (ignored): {type(e).__name__}: {e}", file=sys.stderr)
    try:
        out += check_retry(attempts if attempts is not None else attempts_for(wt),
                           task_mtime=(wt / "TASK.md").stat().st_mtime if (wt / "TASK.md").is_file() else None)
    except Exception:                                    # noqa: BLE001
        pass
    if stage and not quick:
        out += check_stage_dryrun(wt, stage, target, lang)
    if not quick and _is_real_task(task):
        try:
            out += check_refimpl_vs_fixture(wt)
        except Exception as e:                           # noqa: BLE001
            print(f"[harness-lint] refimpl-vs-fixture crashed (ignored): {type(e).__name__}: {e}", file=sys.stderr)
    if full:
        out += check_contract(wt)
    return out


def format_findings(fs):
    hg = _hg()
    return "; ".join(f"{hg.SPEC_DEFECT_PREFIX}{f['reason']}: {f['path']} -- {f['message']}" for f in fs)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    wt = argv[0]
    stage = argv[argv.index("--stage") + 1] if "--stage" in argv else None
    fs = run_lint(wt, stage=stage, full="--full" in argv, quick="--quick" in argv)
    if "--json" in argv:
        print(json.dumps(fs, indent=1))
    else:
        print(format_findings(fs) if fs else "HARNESS_LINT_OK")
    return 3 if fs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
