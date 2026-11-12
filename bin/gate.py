#!/usr/bin/env python3
"""Studio INITIAL GATE: first-pass review of a qwen dispatch, before a human reviews.

Designed 2026-08-31 for the flow:
    task.md + baseline-failing verify.sh -> qwen edits -> verify exits 0
    -> [THIS GATE] -> human deep-review -> integrate -> CI

The gate's job is NOT to be right about everything. It is to spend cheap local
compute so the expensive reviewer's context is spent only on what needs it. That
makes PRECISION the property that matters: a gate that cries wolf inverts its own
purpose, because a human who stops trusting it reads everything anyway.

It composes checks that already exist and are separately measured, rather
than asking one model to do all four:

  1. scope-check.py       - files touched that the task never names   (DECIDABLE)
  2. completeness.py      - spec literals absent from the diff        (DECIDABLE)
  3. verify-quality.py    - is verify.sh meaningful or GAMEABLE       (DECIDABLE)
     verify EXIT CODE      - did the job's own verify pass at all?    (DECIDABLE)
  4. invariant-guard.py   - removed code a comment says must stay    (DECIDABLE)
  5. tw-class-check.mjs   - added CSS classes that compile to nothing (DECIDABLE)
  6. checker-probe.py     - target code references the grader's own artifacts (DECIDABLE)
  7. code-review-agent.py - correctness/edge-case defects             (model + harness rules)

All but the last are decidable and need no model at all. That is deliberate:
every durable gain in this project came from moving a check into the harness
where it is mechanical, never from prompting harder.

Emits a bounded, enum-constrained verdict; no free-text field anywhere.
"""
import argparse, importlib.util, json, re, subprocess, sys
from pathlib import Path

BIN = Path.home() / "bin"
MAX_ISSUES = 25          # bounded array: an unbounded list is an early harness bug


def _load_scaffold_basenames() -> set:
    """The SINGLE source of truth for dispatch-scaffold basenames:
    ollama-queue.py::_SCAFFOLD_BASENAMES. The auto flow (ollama-dispatch-auto)
    AUTHORS these harness files by design (AUTO-TASK.md, auto-harness-check.py,
    check_literals.py, test_fixture.py, verify.test.ts, refimpl.py, TASK.md,
    verify.sh, ...), so scope-check flags every one as "edited but never named"
    and every auto-author job lands CONCERNS on pure harness noise. Reuse the
    queue's set here rather than forking a second list that would drift.

    Fail-OPEN to an empty set: if the queue module cannot be loaded, suppress
    NOTHING -- a real scope concern must never be silently dropped because an
    import failed."""
    try:
        spec = importlib.util.spec_from_file_location(
            "_olq_scaffold", BIN / "ollama-queue.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        s = getattr(m, "_SCAFFOLD_BASENAMES", None)
        return set(s) if s else set()
    except Exception:
        return set()


_SCAFFOLD_BASENAMES = _load_scaffold_basenames()


def is_expected_scaffold(path: str, verify_failed: bool, task_text: str,
                         scaffold: set | None = None) -> bool:
    """True when a scope concern on `path` is EXPECTED authored-harness noise that
    should be suppressed. It is suppressed only when the basename is a known
    scaffold file AND the verify passed AND the task did not name it. A real FAIL,
    a non-scaffold file, or a task-named scaffold file is always judged (False).
    Module-level so both the gate and the pipeline CLEAN-GO predicate share it."""
    sset = _SCAFFOLD_BASENAMES if scaffold is None else scaffold
    bname = Path(path).name
    if bname not in sset:
        return False              # not scaffold -> always judged
    if verify_failed:
        return False              # a real FAIL surfaces everything
    if bname and bname in (task_text or ""):
        return False              # task named it -> real target
    if path and path in (task_text or ""):
        return False
    return True                   # expected authored scaffold, clean verify -> drop


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    # cwd matters for the Tailwind check: it resolves the project's own postcss
    # and tailwind from the working directory, so inheriting the GATE's cwd made
    # it abstain on a project that resolves them perfectly well.
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       cwd=str(cwd) if cwd else None)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def gate(task_file: Path, diff: Path, cwd: Path, verify: str | None,
         verify_exit: int | None, model: str, host: str, num_ctx: int,
         baseline_cwd: Path | None = None, no_review: bool = False,
         harness_authoring: bool = False) -> dict:
    # harness_authoring: this job is an ollama-dispatch-auto HARNESS-AUTHORING
    # dispatch (auto-author-*/auto-refine-*), where editing the harness IS the
    # work -- so its scaffold-file scope concerns are expected noise and are
    # suppressed (see is_expected_scaffold). For a normal CODING job it is False,
    # so a diff that edits the sealed verify.sh/check_literals.py/fixture stays a
    # blocking concern -- the guardrail that a repair may never weaken the check.
    issues: list[dict] = []

    # WHAT DID NOT RUN. A decidable check can abstain -- completeness with no
    # `## Must contain` section, scope with a task that anchors no edited file --
    # and an abstain produces an empty findings list that is indistinguishable
    # from a clean one unless it is carried explicitly. It was not carried, so
    # every legacy task read as completeness-clean; measured 2026-08-31, that was
    # 5 of 5 jobs in the live batch. Tracked here and surfaced in every rendering
    # of this result, because the single most dangerous thing this gate can do is
    # let "not checked" look like "checked and fine".
    not_checked: list[str] = []

    # 1. SCOPE -- decidable
    rc, out = run(["python3", str(BIN / "scope-check.py"),
                   "--task-file", str(task_file), "--diff", str(diff), "--json"]
                  + (["--verify", verify] if verify else []))
    scope = json.loads(out) if out.strip().startswith("{") else {}
    # SCOPE-CHECK DID NOT RUN. `scope` is {} when the subprocess crashed, was
    # killed, or emitted anything but JSON -- and every scope finding below is
    # then silently absent, which reads downstream as "scope clean". `scope_clean`
    # is a declared auto_approve requirement in signoff-rules.json, so an
    # unrecorded non-run made it unenforceable exactly when it mattered. rc was
    # discarded here, so nothing noticed. Same family as the launch-baseline
    # collapse and the inert basename_globs: an un-run check must SAY it did not
    # run. Recorded as not_checked, which auto_decide blocks on.
    if not scope:
        not_checked.append(
            f"scope-check (exit {rc}; emitted no JSON, so files the task never "
            f"named could not be ruled out -- this is NOT 'scope clean')")
    # EXPECTED-SCAFFOLD SUPPRESSION. ollama-dispatch-auto AUTHORS the harness
    # (AUTO-TASK.md, auto-harness-check.py, check_literals.py, test_fixture.py,
    # verify.test.ts, refimpl.py, TASK.md, verify.sh, ...) by design, so
    # scope-check flags every one as "unmentioned"/"selftest" and every clean
    # auto-author job lands CONCERNS on pure harness noise -- obscuring real
    # findings and blocking flow-through. Drop a scope concern whose basename is a
    # known scaffold file UNLESS (a) the job's verify actually FAILED (a real FAIL
    # must still surface everything for the human), or (b) the TASK explicitly
    # NAMED that file as its target (then it is real work, not scaffold noise).
    # Behavioural/code concerns and concerns on real target files are untouched.
    _verify_failed = isinstance(verify_exit, int) and verify_exit != 0
    try:
        _task_text = task_file.read_text()
    except Exception:
        _task_text = ""

    for p in scope.get("unmentioned", []):
        if harness_authoring and is_expected_scaffold(p, _verify_failed, _task_text):
            continue
        issues.append({"severity": "low", "file": p, "line": 0,
                       "what": "edited but never named by the task; confirm it was needed",
                       "source": "scope", "category": "input"})
    # The diff edited the check that gated it. MEDIUM, not low: this gate's whole
    # input is a verify that exited 0, and it is the one scope finding that is
    # about the trustworthiness of that input rather than about tidiness.
    for p in scope.get("selftest", []):
        if harness_authoring and is_expected_scaffold(p, _verify_failed, _task_text):
            continue
        issues.append({"severity": "medium", "file": p, "line": 0,
                       "what": "the diff edits the VERIFY/CHECK that gated it; the "
                               "pass certifies the model's own test -- read this "
                               "change before the code",
                       "source": "scope", "category": "input"})
    # THE DIFF MISSED EVERY FILE THE TASK NAMED. High, but category=input, so it
    # cannot fail the gate: a task can name a file as context and correctly edit
    # elsewhere. Loud without crying wolf.
    if scope.get("wrong_target"):
        _n = scope.get("task_named", [])
        issues.append({"severity": "high", "file": "", "line": 0,
                       "what": f"the task names {len(_n)} file(s) "
                               f"({', '.join(_n[:3])}) and the diff touched NONE "
                               f"of them -- the model may have worked in the "
                               f"wrong place",
                       "source": "scope", "category": "input"})
    if scope and not scope.get("asserted", True):
        not_checked.append(
            f"scope ({scope.get('not_asserted_reason') or 'no anchor'}; "
            f"{len(scope.get('unanchored', []))} edited file(s) unjudged)")

    # 2. COMPLETENESS -- decidable
    rc, out = run(["python3", str(BIN / "completeness.py"),
                   "--spec-file", str(task_file), "--diff", str(diff), "--json",
                   "--cwd", str(cwd)])
    comp = json.loads(out) if out.strip().startswith("{") else {}
    for lit in comp.get("missing", []):
        issues.append({"severity": "high", "file": "", "line": 0,
                       "what": f"spec names `{lit[:80]}` but it is absent from the diff",
                       "source": "completeness", "category": "input"})
    # A declared literal the baseline ALREADY had. Points at the task, not the
    # diff, so it is medium and worded as a spec defect -- reporting it as
    # missing work sends the reviewer to read correct code looking for a fault.
    #
    # NARROWED 2026-09-18 (job 890b48511629): completeness.py now satisfies a
    # `## Must contain` literal that is present in the POST-DIFF file, so a
    # slice-chain task that re-declares the contract an earlier slice landed no
    # longer produces this issue at all -- it used to, and two of them turned a
    # correct diff into concerns -> auto_fix undecidable -> human escalation.
    # spec_errors now fires ONLY as the fail-open path, when the result content
    # could not be read, so a medium issue here is still the right routing.
    for lit in comp.get("spec_errors", []):
        issues.append({"severity": "medium", "file": "", "line": 0,
                       "what": f"the task's `## Must contain` declares `{lit[:60]}`, which "
                               f"ALREADY existed in the baseline -- a diff cannot add what is "
                               f"already there. This is a spec defect, not missing work",
                       "source": "completeness", "category": "input"})
    if comp and not comp.get("asserted", True):
        not_checked.append(f"completeness ({comp.get('not_asserted_reason') or 'no literals'})")

    # 3. VERIFY QUALITY -- decidable. When the verify PASSED, "is that pass
    # meaningful" is the question that matters most here: a verify that passes
    # on an untouched tree certifies a no-op. Note the gate is NOT only run on
    # passing jobs -- the auto-gate fires on completion either way -- so a
    # FAILED verify is handled separately in _finish and is not this check.
    vq = None
    if verify:
        # The gate ALWAYS runs after the model's edits, so --cwd is NOT at
        # baseline. Without --baseline-cwd the baseline-fail property is skipped
        # rather than inverted -- otherwise every real dispatch gets a false
        # high-severity "verify passes on the untouched tree", i.e. a false FAIL.
        vq_cmd = ["python3", str(BIN / "verify-quality.py"),
                  "--verify", verify, "--task-file", str(task_file)]
        vq_cmd += (["--cwd", str(baseline_cwd)] if baseline_cwd
                   else ["--cwd", str(cwd), "--no-baseline"])
        rc, out = run(vq_cmd)
        vq = {"exit": rc, "text": out}
        for line in out.splitlines():
            if line.strip().startswith("PROBLEM:"):
                issues.append({"severity": "high", "file": "verify", "line": 0,
                               "what": line.strip()[8:].strip()[:180],
                               "source": "verify-quality", "category": "input"})

    try:
        _dtext = diff.read_text()
    except Exception:
        _dtext = ""

    # 4a. INVARIANT GUARD -- decidable. Did the diff delete code that an
    # adjacent comment says must stay? A dispatch removed a Prisma where-clause
    # guard whose comment above it read "never re-stamp overdueAt -- otherwise
    # the cleared badge re-appears", left that comment in place, and added its
    # own rationalisation underneath. Typecheck passed; the verify passed; no
    # gate check looked at whether a change contradicts a documented rule.
    # HIGH but category=input, so it routes a human without being able to
    # condemn code: an invariant can be genuinely obsolete.
    rc, out = run(["python3", str(BIN / "invariant-guard.py"),
                   "--diff", str(diff), "--json"])
    inv = json.loads(out) if out.strip().startswith("{") else {}
    for v in inv.get("violations", []):
        issues.append({"severity": "high", "file": v.get("file", ""), "line": 0,
                       "what": f"removed `{v.get('removed','')[:60]}` directly under a "
                               f"comment stating an invariant: "
                               f"\"{v.get('comment','')[:70]}\"",
                       "source": "invariant-guard", "category": "input"})

    # 4a-ii. UNEXPANDED `~` IN A PATH ARGUMENT -- decidable, and a runtime-only
    # bug that every static check misses. The handoff-panel dispatch shipped
    # subprocess.run(['python3', '~/bin/handoff-emit.py', ...]) at three sites.
    # subprocess does no shell expansion, so the endpoint returned exit 2 and
    # the panel was dead on arrival -- while the file parsed, imported, and
    # passed a verify asserting the endpoint EXISTED. "Present" passed; "works"
    # failed. Same vacuous-assertion family as a pre-existing literal, and the
    # fix is the same: make the real property decidable in the harness.
    for m in re.finditer(r"^\+(?!\+\+)(.*)$", _dtext, re.M):
        line = m.group(1)
        if re.search(r"""['"]~/""", line) and "expanduser" not in line and "(" in line:
            if line.strip().startswith("#"):
                continue
            issues.append({"severity": "high", "file": "", "line": 0,
                           "what": f"a literal `~/` path in code: {line.strip()[:90]} -- "
                                   f"subprocess/open do NOT expand `~`, so this resolves "
                                   f"to a directory named '~' at runtime. Wrap it in "
                                   f"os.path.expanduser() or Path.home()",
                           "source": "tilde-path", "category": "code"})

    # 4b. DEAD CSS CLASSES -- decidable, and version-accurate because it asks the
    # PROJECT'S OWN Tailwind what compiles rather than encoding a scale from
    # memory. Only meaningful when the diff touches markup and the project has
    # node_modules; abstains loudly otherwise.
    if re.search(r"^\+\+\+ b/.*\.(tsx|jsx)$", _dtext, re.M) and (cwd / "node_modules").exists():
        rc, out = run(["node", str(BIN / "tw-class-check.mjs"), str(diff)], cwd=cwd)
        for line in out.splitlines():
            if line.strip().startswith("DEAD:"):
                issues.append({"severity": "high", "file": "", "line": 0,
                               "what": f"added CSS class `{line.split(':',1)[1].strip()}` "
                                       f"generates NO CSS -- it silently does nothing, "
                                       f"and typecheck cannot see it",
                               "source": "tw-class-check", "category": "code"})
        if "NOT ASSERTED" in out:
            not_checked.append("tw-class-check (Tailwind did not resolve from the "
                               "project, so no class could be compiled)")

    # 4c. CHECKER-PROBE -- decidable. Does the TARGET code (not verify.sh/scaffold,
    # already scope's job) reference the grader's own artifacts -- the verify
    # sentinel, a scaffold basename, or one of our dispatch env vars? A model that
    # can see its own harness can special-case it instead of solving the real
    # task; this is undetectable from "the target's own test passes" because the
    # target is what's gaming the test. HIGH, category=input: a real target file
    # that legitimately processes strings shaped like these (a log parser, a CI
    # config generator) is a genuine, if rare, false-positive class this cannot
    # rule out from a regex alone.
    rc, out = run(["python3", str(BIN / "checker-probe.py"), "--diff", str(diff), "--json"])
    probe = json.loads(out) if out.strip().startswith("{") else {}
    for f in probe.get("findings", []):
        issues.append({"severity": "high", "file": f.get("file", ""), "line": f.get("line", 0),
                       "what": f"target code references the grader's own artifact "
                               f"`{f.get('artifact','')}` -- {f.get('text','')[:90]}",
                       "source": "checker-probe", "category": "input"})

    # 5. DEFECT REVIEW -- the only stage that needs a model.
    # Skipped entirely under --no-review: the auto-gate runs the decidable checks
    # inline and ENQUEUES this stage through the queue instead, so the GPU call
    # is scheduled against real dispatches rather than racing them.
    if no_review:
        return _finish(issues, "not-run (enqueued separately)", verify_exit, vq,
                       not_checked)
    out_dir = Path("/tmp/gate-review")
    rc, _ = run(["python3", str(BIN / "code-review-agent.py"), "--mode", "review",
                 "--diff", str(diff), "--intent", task_file.read_text()[:1500],
                 "--model", model, "--host", host, "--num-ctx", str(num_ctx),
                 "--out", str(out_dir)])
    report = out_dir / "report.md"
    review_verdict = "unknown"
    if report.exists():
        txt = report.read_text()
        m = re.search(r"^## VERDICT: (.+)$", txt, re.M)
        review_verdict = m.group(1).strip() if m else "unknown"
        for row in re.finditer(r"^\| \d+ \| (\w+) \| `([^`]+)` \| (.+?) \|$", txt, re.M):
            sev, where, what = row.group(1).lower(), row.group(2), row.group(3)
            f, _, ln = where.partition(":")
            issues.append({"severity": sev, "file": f,
                           "line": int(re.sub(r"\D", "", ln) or 0),
                           "what": what.strip()[:180], "source": "review",
                           "category": "code"})

    return _finish(issues, review_verdict, verify_exit, vq, not_checked)


def _finish(issues: list[dict], review_verdict: str,
            verify_exit=None, vq=None, not_checked=None) -> dict:
    # THE JOB'S OWN VERIFY FAILED. The strongest signal available, and until
    # 2026-08-31 this function accepted it as a parameter and did nothing with
    # it but echo it: resell-cc-edit-inline (ef842e8c36cd) exited 1 with 11 new
    # typecheck errors and gated as "pass-pending-review", because the arg was
    # never plumbed from the hook and the verdict never consulted it.
    #
    # It is category=CODE deliberately. The standing rule -- only a code high
    # fails -- exists so an imperfect TASK cannot condemn good code. This is the
    # opposite situation: the project's own check rejected the diff. A diff-only
    # reviewer finding nothing does not overturn tsc, so this must reach the
    # verdict through the normal path rather than as a special case.
    if verify_exit is not None and verify_exit != 0:
        issues = [{"severity": "high", "file": "verify", "line": 0,
                   "what": f"the job's own verify FAILED (exit {verify_exit}); "
                           f"the diff did not pass the project's check, and a "
                           f"clean diff review does not overturn that",
                   "source": "verify-exit", "category": "code"}] + issues
    issues = issues[:MAX_ISSUES]
    # INPUT-QUALITY findings judge the TASK and the VERIFY -- they are about what
    # the dispatcher wrote. CODE findings judge the diff -- what the model wrote.
    # Conflating them inverted a whole gate run on the first real case: two HIGH
    # input findings (a doc filename mis-read as a required literal; a verify with
    # no code literals to assert, because the task was behavioural) produced
    # "GATE: FAIL" while the code reviewer itself said PASS. A person reading FAIL
    # concludes the model's code failed. It had not.
    # So: only a CODE high can fail the gate. Input problems cap at "concerns" --
    # they are real and worth surfacing, but an imperfect task must never
    # condemn good code.
    code = [i for i in issues if i.get("category") == "code"]
    inputs = [i for i in issues if i.get("category") == "input"]
    high = [i for i in code if i["severity"] == "high"]
    # DELIBERATE mapping, not an accident: the reviewer's own verdict and the
    # gate's are different scales and will legitimately disagree. The reviewer
    # says FAIL when any defect survives adversarial verification, including a
    # medium one. The GATE says fail only for a high, because its output is
    # routing -- "fail" must mean "do not integrate", while "concerns" means
    # "a human should look here first", which is the gate's whole purpose. A
    # medium finding is exactly the concerns case. Printed side by side so the
    # divergence is visible rather than silently resolved.
    verdict = ("fail" if high
               else "concerns" if (issues or review_verdict.startswith("FAIL"))
               else "pass")
    # A clean result with NO REVIEW RUN is not a pass -- it is an unfinished
    # check, and it must not read as approval. Same rule as a visible skip:
    # the single most dangerous thing this gate can do is let "not checked"
    # look like "checked and fine".
    if verdict == "pass" and review_verdict.startswith("not-run"):
        verdict = "pass-pending-review"
    # An abstain does NOT move the verdict. The verdict is routing -- "concerns"
    # must mean "a human should look at this diff" -- and a dispatcher who did
    # not declare literals is not a reason to route a human to the code. It
    # would also fire on every legacy task, which is the crying-wolf failure
    # this gate is built to avoid. It rides alongside the verdict instead, and
    # every renderer prints it.
    return {"verdict": verdict, "issues": issues,
            "counts": {"code_high": len(high), "code": len(code),
                       "input": len(inputs), "total": len(issues)},
            "not_checked": list(not_checked or []),
            "review_verdict": review_verdict,
            "verify_exit_reported": verify_exit,
            "verify_quality_exit": (vq or {}).get("exit")}


def _self_test() -> int:
    """GPU-free tests for the expected-scaffold suppression + verdict mapping."""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            ok = False

    sset = {"AUTO-TASK.md", "auto-harness-check.py", "check_literals.py",
            "test_fixture.py", "verify.test.ts", "refimpl.py", "TASK.md",
            "verify.sh"}
    harness_paths = ["AUTO-TASK.md", "verify.sh", "refimpl.py", "check_literals.py",
                     "test_fixture.py", "verify.test.ts", "auto-harness-check.py",
                     "TASK.md", "a/b/refimpl.py"]

    # The suppression as gate() applies it: only for a HARNESS-AUTHORING job.
    def _suppress(p, harness_authoring, verify_failed, task_text):
        return harness_authoring and is_expected_scaffold(
            p, verify_failed, task_text, scaffold=sset)

    # (1) harness-authoring job + PASSING verify -> every scaffold concern
    # suppressed, so a gate with only those inputs reaches a clean verdict.
    _t1 = "Only edit `app/api/import/route.ts`.\n"
    supp = [p for p in harness_paths if _suppress(p, True, False, _t1)]
    check("(1) all harness paths suppressed for an auto-author job on a passing verify",
          len(supp) == len(harness_paths))
    v1 = _finish([], "not-run (enqueued separately)", verify_exit=0)
    check("(1) zero-issue harness-only gate is not 'concerns'",
          v1["verdict"] in ("pass", "pass-pending-review") and v1["counts"]["input"] == 0)

    # (2) a real (non-scaffold) target file with a bad change -> still judged.
    check("(2) real target file is NOT suppressed",
          is_expected_scaffold("app/api/import/route.ts", False, _t1, scaffold=sset) is False)
    v2 = _finish([{"severity": "high", "file": "app/api/import/route.ts",
                   "line": 3, "what": "bad change", "source": "review",
                   "category": "code"}], "FAIL", verify_exit=0)
    check("(2) a real code-high still FAILs the gate", v2["verdict"] == "fail")

    # (3) a scaffold file the task EXPLICITLY NAMED -> still checked (not suppressed).
    _t3 = "Rewrite the literal list in `check_literals.py` for the new spec.\n"
    check("(3) task-named scaffold file is NOT suppressed",
          is_expected_scaffold("check_literals.py", False, _t3, scaffold=sset) is False)

    # (4) a FAILed verify on a harness-only diff -> suppression is OFF (surfaces
    # everything) AND the verify-exit HIGH concern is added by _finish.
    check("(4) verify FAILED -> scaffold concern surfaces (not suppressed)",
          is_expected_scaffold("verify.sh", True, _t1, scaffold=sset) is False)
    v4 = _finish([], "not-run (enqueued separately)", verify_exit=1)
    check("(4) failed verify adds the verify-exit HIGH and FAILs the gate",
          v4["verdict"] == "fail"
          and any(i["source"] == "verify-exit" and i["severity"] == "high"
                  for i in v4["issues"]))

    # (5) THE REPAIR GUARDRAIL. A CODING job (harness_authoring=False) whose diff
    # edits verify.sh -- e.g. an auto-repair round weakening the check to force a
    # green -- must NOT be suppressed. Suppression is off for the whole job, so the
    # verify.sh edit stays a scope concern the gate surfaces. is_expected_scaffold
    # would return True in isolation (verify passed, task didn't name it); the
    # harness_authoring gate is what keeps the concern alive, so we assert on the
    # exact predicate gate() applies, both for verify.sh and for the fixture/grader.
    check("(5) GUARDRAIL: a coding job editing verify.sh is NOT suppressed",
          _suppress("verify.sh", False, False, _t1) is False
          and is_expected_scaffold("verify.sh", False, _t1, scaffold=sset) is True)
    check("(5) GUARDRAIL: a coding job editing check_literals.py is NOT suppressed",
          _suppress("check_literals.py", False, False, _t1) is False)
    check("(5) GUARDRAIL: a coding job editing test_fixture.py is NOT suppressed",
          _suppress("test_fixture.py", False, False, _t1) is False)
    # ...while the SAME edit on a harness-authoring job IS suppressed -- proving the
    # gate is what distinguishes authoring the check from weakening it.
    check("(5) authoring job DOES suppress verify.sh (the two paths differ)",
          _suppress("verify.sh", True, False, _t1) is True)

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--baseline-cwd", default=None,
                    help="a checkout at BASELINE (pre-edit). Without it the "
                         "baseline-fail check is skipped rather than inverted.")
    ap.add_argument("--verify", default=None)
    ap.add_argument("--verify-exit", type=int, default=None)
    ap.add_argument("--model", default="qwen3.8:27b-q4_K_M")  # coding default flipped 2026-09-02: bake-off 6/6 vs qwen3-coder 1/6
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-review", action="store_true",
                    help="run only the decidable checks; the model review is "
                         "enqueued separately by the auto-gate")
    ap.add_argument("--harness-authoring", action="store_true",
                    help="this job AUTHORS the dispatch harness (ollama-dispatch-auto "
                         "auto-author-*/auto-refine-*): its scaffold-file scope concerns "
                         "are expected and suppressed. NEVER set for a normal coding job "
                         "-- a coding diff that edits verify.sh/check_literals.py/fixture "
                         "must stay a blocking concern (the repair guardrail).")
    a = ap.parse_args()

    r = gate(Path(a.task_file).expanduser(), Path(a.diff).expanduser(),
             Path(a.cwd).expanduser(), a.verify, a.verify_exit,
             a.model, a.host, a.num_ctx,
             Path(a.baseline_cwd).expanduser() if a.baseline_cwd else None,
             a.no_review, a.harness_authoring)
    if a.json:
        print(json.dumps(r, indent=1)); return 0

    print(f"=== GATE: {r['verdict'].upper()} ===")
    c = r["counts"]
    print(f"  CODE: {c['code_high']} high of {c['code']}   "
          f"INPUT (task/verify quality): {c['input']}")
    print("  Only a CODE high can fail the gate; input problems cap at 'concerns' "
          "so an imperfect task cannot condemn good code.")
    for nc in r.get("not_checked", []):
        print(f"  NOT CHECKED: {nc}")
    print(f"  reviewer's own verdict: {r['review_verdict']}  "
          f"(different scale: reviewer FAILs on any surviving defect; the gate "
          f"reserves 'fail' for high severity, since its output is routing)")
    for i in r["issues"]:
        loc = f"{i['file']}:{i['line']}" if i["file"] and i["line"] else (i["file"] or "-")
        tag = "CODE " if i.get("category") == "code" else "INPUT"
        print(f"  [{tag} {i['severity'].upper():6s}] {loc:46s} {i['what'][:88]}")
    print("\n  ADVISORY by design -- see the writeup. This gate exists to spend cheap")
    print("  compute so the human reviewer spends context only where it is needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
