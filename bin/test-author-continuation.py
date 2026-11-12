#!/usr/bin/env python3
"""Regression test for the AUTHORING continuation loop (2026-09-22).

ROOT CAUSE it guards. do_auto() gave the authoring dispatch exactly ONE shot at a
flat --author-max-iters (14) and treated DID-NOT-CONVERGE as terminal, while
--retry-slice responds by DELETING the worktree and re-authoring from scratch.
Measured over the 60 most recent worker transcripts: 23 of the 26
non-convergences ended at EXACTLY iteration 14, every one of them an
authoring/refine job. esim-global/s3-global-section burned four full attempts
(e90653da2c02, 07d861c5d690, 9600f81e7880, f7397f97b6b0) rediscovering the same
nested-escaping bug; the last one worked it out on its FINAL iteration and died
one edit short of green.

_author_with_continuations() now hands the same worktree back to the model with
the live self-check output, bounded by --author-continue-rounds AND by a
no-progress check so a genuinely stuck model still fails fast.

What this proves (each assertion is red if the guard it covers is reverted):
  1. a converging first round never triggers a continuation at all;
  2. a non-converging round that KEEPS CHANGING the harness gets continuation
     rounds, and a later round's success is reported as success;
  3. a continuation round that leaves the harness BYTE-IDENTICAL stops the loop;
  3b. a THRASHING round (bytes change, self-check output does not) also stops;
  3c. the byte-signature guard IN ISOLATION -- see the correction below;
  4. --author-continue-rounds 0 restores the old single-shot behaviour;
  5. _harness_signature depends on the harness file bytes;
  6. ...and follows the LANG's fixture GLOBS, not one canonical filename;
  7. _harness_check_output can never leave the TARGET contaminated.

CORRECTION (2026-09-22, found by review, not by this file). The commit that
added scenario 3 claimed deleting the `if new_sig == sig:` early return "lets a
stuck run burn every remaining round". That claim was FALSE and the suite was
19/19 green with the guard deleted: scenarios 3 and 3b stub
_harness_check_output to a CONSTANT, so the separate self-check-output guard
always stopped the loop first and the byte-signature guard had zero
discriminating coverage. Scenario 3c exists to isolate it -- output VARIES every
round (output guard can never fire) while harness bytes stay FROZEN (only the
byte guard can stop it). The two guards are kept because they fail differently:
the output guard needs the check to RUN and to be deterministic; the byte guard
is cheap, deterministic, and fires even when the check output is noisy.

The general lesson: a mutation test only proves what it ISOLATES. Two guards
covering one scenario means the suite measures whichever fires first.

Run: python3 test-author-continuation.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

AUTO = Path(__file__).resolve().parent / "ollama-dispatch-auto"


def _load():
    # HOME LEAK (2026-10-06): run_auto() scenarios wrote ~/.ollama-dispatch/auto-runs/
    # t.json + t.attempts.json into the REAL config dir (label "t", model "m"), which
    # self-heal hygiene then reported as a dead driver record. Point the module's
    # config + auto-runs dirs at a private temp dir BEFORE it is loaded.
    import atexit, shutil
    _h = tempfile.mkdtemp(prefix="authcont-home-")
    atexit.register(shutil.rmtree, _h, True)
    os.environ["OLLAMA_DISPATCH_HOME"] = _h
    os.environ["OLLAMA_DISPATCH_AUTO_RUNS_DIR"] = os.path.join(_h, "auto-runs")
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("oda_authcont", str(AUTO))
    spec = importlib.util.spec_from_loader("oda_authcont", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


failures = []


def ok(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  -- {detail}"))
    if not cond:
        failures.append(name)


def _args(rounds=2, lang="python"):
    return SimpleNamespace(
        label="t", intent="do the thing", lang=lang, model="m", host="h",
        author_max_iters=24, author_continue_rounds=rounds, num_ctx=1, max_tokens=None,
        drafter_cmd=None, timeout=60, slice_id=None,
    )


def _wt(tmp, task="TASK", ref="REF"):
    wt = Path(tmp)
    (wt / "TASK.md").write_text(task)
    (wt / "refimpl.py").write_text(ref)
    (wt / "test_fixture.py").write_text("FX")
    return wt


@contextmanager
def sandbox(m):
    """Snapshot the module's attributes and restore them on exit.

    THE STUB-LEAK CLASS, killed at the root rather than per instance.
    patch() monkeypatches module-level names (dispatch_model,
    _harness_check_output, author_prompt) with no restore, so a stub set in
    one scenario leaked into every later one. That produced FOUR false greens
    in this series, and the cost is worse than a hidden regression: scenario
    13 was BORN green and meaningless because it asserted against a leaked
    stub instead of the real detector. A leak does not only hide regressions,
    it silently converts NEW tests into no-ops at the moment they are written.

    Every scenario now runs inside this, so a stub cannot outlive its block
    and no future scenario needs to remember to restore anything.
    """
    before = dict(m.__dict__)
    try:
        yield
    finally:
        for k in list(m.__dict__):
            if k not in before:
                del m.__dict__[k]
            elif m.__dict__[k] is not before[k]:
                m.__dict__[k] = before[k]


def run():
    m = _load()
    # Kept only for the explicit restore in scenario 7; sandbox() now makes
    # that restore redundant rather than load-bearing.
    _real_check_output = m._harness_check_output

    # ---- 5. the signature is really a function of the harness bytes ----------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        s1 = m._harness_signature(wt, "python")
        s_same = m._harness_signature(wt, "python")
        (wt / "refimpl.py").write_text("REF-CHANGED")
        s2 = m._harness_signature(wt, "python")
        ok("signature is stable when nothing changes", s1 == s_same)
        ok("signature changes when refimpl.py changes", s1 != s2,
           "a constant signature makes every round look stuck")
        (wt / "TASK.md").write_text("TASK-CHANGED")
        ok("signature changes when TASK.md changes",
           s2 != m._harness_signature(wt, "python"))

    # A recording stub for dispatch_model. `script` is the (ok, why) each call
    # returns; `mutate` is what that call writes into the harness.
    class _Rec(list):
        """a list that can also carry the recorded _harness_check_output args"""

    def patch(m, wt, script, mutate):
        calls = _Rec()
        checks = []

        def fake_dispatch(wt_, prompt, label, verify_cmd, a, max_iters=None):
            i = len(calls)
            calls.append(SimpleNamespace(label=label, prompt=prompt,
                                         max_iters=max_iters))
            mut = mutate[i] if i < len(mutate) else None
            if mut is not None:
                (wt_ / "refimpl.py").write_text(mut)
            return script[i] if i < len(script) else (False, "out of script")

        # NOTE the missing `=None` on tgt, deliberately (reviewer catch,
        # 2026-09-22). With a default here, mutating the CALL SITE back to
        # `_harness_check_output(wt, verify_cmd)` -- the exact regression that
        # silently disables the whole crash-safety fix in production -- left the
        # suite fully green: the default absorbed the missing argument. A
        # required parameter makes the call site's wiring load-bearing, and
        # `checks` lets scenario 2 assert WHICH target was threaded through.
        def fake_check(wt_, vc, tgt, bundle=None):
            checks.append(tgt)
            return ("  FAIL: SyntaxError somewhere", None)

        m.dispatch_model = fake_dispatch
        m._harness_check_output = fake_check
        m.author_prompt = lambda a, t: "AUTHOR"
        calls.checks = checks
        return calls

    # ---- 1. a converging first round does not continue -----------------------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        calls = patch(m, wt, [(True, "converged")], [None])
        okk, why = m._author_with_continuations(_args(), wt, "build.py", "vc")
        ok("converging first round returns ok", okk is True, why)
        ok("converging first round dispatches exactly once", len(calls) == 1,
           f"dispatched {len(calls)}x")

    # ---- 2. progress between rounds buys continuations, and a later win wins --
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        calls = patch(m, wt,
                      [(False, "DID NOT CONVERGE"), (True, "converged")],
                      ["A", "B"])
        # The stubs deliberately take `tgt` with NO default, so dropping the
        # target at the call site is a hard TypeError rather than a silent
        # pass. Catch it here so that regression reports a NAMED failure -- an
        # unhandled traceback reads to the next person like a broken test file
        # rather than like the guard doing its job.
        try:
            okk, why = m._author_with_continuations(_args(), wt, "build.py", "vc")
        except TypeError as e:
            if "tgt" in str(e):
                ok("the call site passes the TARGET to _harness_check_output",
                   False, f"call site dropped the target argument: {e}")
                print()
                print("FAILED (1): the call site passes the TARGET to "
                      "_harness_check_output")
                return 1
            raise
        ok("a continuation round can rescue a capped authoring run", okk is True, why)
        ok("it dispatched a continuation", len(calls) == 2, f"{len(calls)} calls")
        ok("the continuation uses a DISTINCT label (dupe-label guard)",
           len({c.label for c in calls}) == 2, [c.label for c in calls])
        ok("the continuation prompt says do not start over",
           "DO NOT start over" in calls[1].prompt)
        ok("the continuation prompt carries the live self-check output",
           "SyntaxError" in calls[1].prompt)
        ok("the continuation prompt carries the escaping rule",
           "RAW" in calls[1].prompt or "raw" in calls[1].prompt)
        # WIRING (reviewer catch): scenario 7 proves _harness_check_output
        # restores the target, but nothing proved the CALL SITE passes one.
        # Dropping the arg at the call site left the suite green. This asserts
        # the connection, not just the mechanism.
        ok("the call site threads the TARGET into _harness_check_output",
           calls.checks == ["build.py"], calls.checks)

    # ---- 3. a no-progress continuation stops the loop IMMEDIATELY ------------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        # round 1 fails and writes "A"; the continuation fails and writes "A"
        # again -> byte-identical harness -> must stop without a 2nd continuation
        calls = patch(m, wt,
                      [(False, "DID NOT CONVERGE"), (False, "again"), (False, "3rd")],
                      ["A", "A", "A"])
        okk, why = m._author_with_continuations(_args(rounds=2), wt, "build.py", "vc")
        ok("a stuck run still fails", okk is False)
        ok("a byte-identical continuation stops before round 2",
           len(calls) == 2, f"burned {len(calls)} dispatches; expected 2")
        ok("the failure says it is stuck, not out of runway",
           "genuinely stuck" in why, why)

    # ---- 3b. a THRASHING model (keeps editing, nothing improves) also stops --
    # The byte signature alone does not catch this: every round changes the
    # files, so the run would burn all its continuations. The self-check output
    # is the second, independent progress signal.
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        calls = patch(m, wt,
                      [(False, "no"), (False, "no"), (False, "no"), (False, "no")],
                      ["A", "B", "C", "D"])   # harness bytes DIFFER every round
        m._harness_check_output = lambda wt_, vc, tgt, bundle=None: ("  FAIL: 9/10 case(s) passed", None)
        okk, why = m._author_with_continuations(_args(rounds=3), wt, "build.py", "vc")
        ok("a thrashing run still fails", okk is False)
        ok("identical self-check output stops the loop early",
           len(calls) == 2, f"burned {len(calls)} dispatches; expected 2")
        ok("the thrash failure names the self-check output",
           "self-check output byte-identical" in why, why)

    # ---- 3c. ISOLATE the byte-signature guard --------------------------------
    # Reviewer catch (2026-09-22): scenarios 3 and 3b do NOT prove the
    # byte-signature guard, because b13e9f1's self-check-output guard stops the
    # loop first in both -- deleting `if new_sig == sig:` left the suite 19/19
    # GREEN. To isolate it, the output must VARY every round (so the output
    # guard can never fire) while the harness bytes stay IDENTICAL (so only the
    # byte guard can stop the loop). That is a real shape: a model that narrates
    # and re-runs the check without ever writing a file, against a check whose
    # output carries a timestamp/ordering/duration that differs per run.
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        # mutate=[None,...] -> no round writes anything, harness bytes frozen
        calls = patch(m, wt, [(False, "no")] * 4, [None, None, None, None])
        _seq = iter(["run-1", "run-2", "run-3", "run-4"])
        m._harness_check_output = lambda wt_, vc, tgt, bundle=None: (next(_seq), None)
        okk, why = m._author_with_continuations(_args(rounds=3), wt, "build.py", "vc")
        ok("frozen-harness run still fails", okk is False)
        # calls[0] is the INITIAL authoring dispatch, so "stopped after one
        # continuation" is 2 total. Without the byte guard the varying output
        # never trips the other guard and all 3 rounds run -> 4 calls.
        ok("byte-signature guard alone stops the loop after ONE continuation",
           len(calls) == 2, f"burned {len(calls)} dispatches; expected 2")
        ok("the failure names the byte-identical harness",
           "byte-identical" in why and "harness" in why, why)

    # ---- 4. --author-continue-rounds 0 restores single-shot ------------------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        calls = patch(m, wt, [(False, "DID NOT CONVERGE")], ["A"])
        okk, why = m._author_with_continuations(_args(rounds=0), wt, "build.py", "vc")
        ok("rounds=0 fails after one dispatch", okk is False and len(calls) == 1,
           f"{len(calls)} calls")

    # ---- 6. the signature follows the LANG's fixture GLOBS, not one name -----
    # Reviewer catch: _FIXTURE_GLOBS_BY_LANG["ts"] accepts `*.test.ts`, but the
    # signature hashed only fixture_name_for(lang). A TS round that edited a
    # legitimately-named non-canonical fixture hashed `<missing>` on both sides
    # and could false-positive as "genuinely stuck".
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "TASK.md").write_text("TASK")
        (wt / "refimpl.py").write_text("REF")
        (wt / "selectResults.test.ts").write_text("case A")
        s1 = m._harness_signature(wt, "ts")
        (wt / "selectResults.test.ts").write_text("case A + case B")
        ok("ts signature notices a *.test.ts fixture that is not the canonical name",
           s1 != m._harness_signature(wt, "ts"),
           "a non-canonically-named TS fixture hashes as <missing> either way")
        s2 = m._harness_signature(wt, "ts")
        (wt / "extra.test.ts").write_text("new file")
        ok("ts signature notices a NEW matching fixture file appearing",
           s2 != m._harness_signature(wt, "ts"))
        ok("python signature is unaffected by the glob widening",
           m._harness_signature(wt, "python")
           == m._harness_signature(wt, "python"))

    m._harness_check_output = _real_check_output   # scenario 7 tests the REAL one
    # ---- 7. the self-check can never leave the TARGET contaminated -----------
    # auto-harness-check.py applies refimpl to the target then reverts it, with
    # no try/finally between. A kill in that window leaves the reference
    # solution in the target and the next round dispatches into a dirty tree
    # (the 'dirty launch baseline -> UNTRUSTED' cascade). _harness_check_output
    # snapshots and restores in a finally so the guarantee does not depend on
    # the check surviving.
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "build.py").write_text("ORIGINAL TARGET\n")
        # a 'check' that contaminates the target and dies before reverting
        (wt / "boom.sh").write_text(
            "echo 'REFERENCE SOLUTION' > build.py\n"
            "echo '  FAIL: something'\n"
            "exit 1\n")
        out, dirty = m._harness_check_output(wt, "bash boom.sh", "build.py")
        ok("the self-check output is still returned",
           "FAIL: something" in out, out)
        ok("a check killed before its revert cannot contaminate the target",
           (wt / "build.py").read_text() == "ORIGINAL TARGET\n",
           repr((wt / "build.py").read_text()))

    # a target that did NOT exist before must not be left behind either
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "boom.sh").write_text("echo 'CREATED' > build.py\nexit 1\n")
        m._harness_check_output(wt, "bash boom.sh", "build.py")
        ok("a target created by a crashed check is removed again",
           not (wt / "build.py").exists())

    # and the no-target call path still works (nothing to snapshot)
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "ok.sh").write_text("echo hello\n")
        ok("target=None still returns output",
           "hello" in m._harness_check_output(wt, "bash ok.sh", None)[0])

    # ---- 8. the ORPHAN path: grandchildren must not outlive the timeout -----
    # Reviewer catch, reproduced. subprocess.run(timeout=) kills only the direct
    # shell child; auto-harness-check.py runs `python3 refimpl.py` and
    # `bash verify.sh` as ITS subprocesses, so on a timeout those grandchildren
    # survived, and re-contaminated the target AFTER the restore ran. Observed:
    # restore OK at t+0, target overwritten again at t+4. The fix runs the check
    # in its own session and kills the whole process group.
    # Driven through _run_in_own_process_group directly with a SHORT timeout:
    # _harness_check_output hardcodes 900s, and the point here is the kill path,
    # not the wait. The old subprocess.run(timeout=) behaviour fails this.
    import time as _time
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "build.py").write_text("ORIGINAL\n")
        (wt / "slow.sh").write_text(
            "python3 -c \"import time;time.sleep(3);"
            "open('build.py','w').write('ORPHAN WON\\n')\" &\n"
            "sleep 30\n")
        try:
            m._run_in_own_process_group("bash slow.sh", str(wt), 1)
            raised = False
        except Exception:
            raised = True
        ok("the process-group runner raises on timeout", raised)
        _time.sleep(5)
        ok("the whole process GROUP is killed, not just the shell",
           (wt / "build.py").read_text() == "ORIGINAL\n",
           repr((wt / "build.py").read_text()))

    # ---- 9. a contaminated tree REFUSES the continuation --------------------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = _wt(tmp)
        calls = patch(m, wt, [(False, "DID NOT CONVERGE"), (False, "2nd")],
                      ["A", "B"])
        m._harness_check_output = lambda wt_, vc, tgt, bundle=None: ("out", "target left dirty")
        okk, why = m._author_with_continuations(_args(rounds=2), wt, "build.py", "vc")
        ok("a contaminated tree fails the run", okk is False)
        ok("it refuses to DISPATCH into a dirty tree",
           len(calls) == 1, f"dispatched {len(calls)}x after contamination")
        ok("the reason names the contamination",
           "unattributable" in why and "target left dirty" in why, why)
        ok("contamination is tagged so do_auto can route it away from slicing",
           why.startswith(m.CONTAMINATED_PREFIX), why)
    # Scenario 9's stub is module-global and silently leaked into every later
    # scenario, so scenario 13 was asserting against the stub instead of the
    # real detector. Same leak class as the one fixed at line 269 -- restore it.
    m._harness_check_output = _real_check_output

    # ---- 10. output normalisation: noise ignored, COUNTS preserved ----------
    # The output guard almost never fired in practice because pytest/node:test
    # print a duration and tracebacks carry tmp paths. Normalise that noise --
    # but NOT counts: "5/7 passed" -> "6/7" is real progress and must stay
    # visible, so a blanket digit strip would cause false "stuck" aborts.
    ok("run duration is normalised away",
       m._normalise_check("1 failed in 0.12s")
       == m._normalise_check("1 failed in 9.87s"))
    ok("tmp paths are normalised away",
       m._normalise_check("File \"/tmp/abc123/x.py\", line 4")
       == m._normalise_check("File \"/tmp/zzz999/x.py\", line 4"))
    ok("CASE COUNTS are preserved (progress must stay visible)",
       m._normalise_check("5/7 case(s) passed")
       != m._normalise_check("6/7 case(s) passed"))

    # ---- 11. the TARGET never counts as harness progress (swift globs) ------
    with tempfile.TemporaryDirectory() as tmp, sandbox(m):
        wt = Path(tmp)
        (wt / "TASK.md").write_text("T")
        (wt / "refimpl.py").write_text("R")
        src = wt / "Sources" / "App"
        src.mkdir(parents=True)
        (src / "main.swift").write_text("original")
        tgt = "Sources/App/main.swift"
        s1 = m._harness_signature(wt, "swift", tgt)
        (src / "main.swift").write_text("EDITED TARGET ONLY")
        ok("a target-only edit does NOT read as harness progress",
           s1 == m._harness_signature(wt, "swift", tgt),
           "Sources/**/main.swift matches the target; it must be excluded")
        ok("without the target arg the glob would have counted it",
           m._harness_signature(wt, "swift")
           != m._harness_signature(wt, "swift", tgt))

    # ---- 12. duration normalisation must not eat DOMAIN values -------------
    # The unanchored pattern normalised ANY number+unit, so a task ABOUT
    # timeouts saw "expected 5s, got 7s" and "expected 5s, got 9s" collapse to
    # the same string -> false "stuck" abort on exactly the tasks whose output
    # legitimately contains durations. Anchor on the runner's own timing words.
    ok("a DOMAIN duration is preserved (not treated as runner noise)",
       m._normalise_check("expected 5s, got 7s")
       != m._normalise_check("expected 5s, got 9s"))
    ok("the runner's own 'in 0.12s' is still normalised",
       m._normalise_check("1 failed in 0.12s")
       == m._normalise_check("1 failed in 9.87s"))
    ok("a pytest trailing summary duration is still normalised",
       m._normalise_check("=== 3 passed in 0.12s ===")
       == m._normalise_check("=== 3 passed in 4.55s ==="))

    # ---- 13. the CONTAMINATION DETECTOR itself, against a REAL git tree -----
    # Scenario 9 stubs _harness_check_output wholesale, so it proves the caller's
    # refusal branch and NOTHING about the `git status --porcelain` comparison
    # that produces the signal. Reviewer confirmed both `_tree_state -> None`
    # and neutering the comparison left the suite fully green. This closes it
    # with a real worktree and a check that really does dirty the tree.
    if shutil.which("git"):
        with tempfile.TemporaryDirectory() as tmp, sandbox(m):
            wt = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "commit", "-qm", "base"]):
                if cmd[1] == "add":
                    (wt / "build.py").write_text("original\n")
                subprocess.run(cmd, cwd=wt, env=env, capture_output=True)
            ok("_tree_state reads a real worktree as clean",
               (m._tree_state(wt) or "").strip() == "")
            # a check that leaves junk behind, exactly like a killed one would
            (wt / "auto-harness-check.py").write_text(
                "open('LEFTOVER.txt','w').write('x')\n")
            # Pin the LOCAL lane: this scenario proves the local `git status --porcelain`
            # comparison. With the CPU lane live the check can be shipped to the Unraid
            # runner (a copy), so the leftover never lands in this tree -- an environmental
            # flip unrelated to the detector.
            _cl = os.environ.get("CPU_LANE")
            os.environ["CPU_LANE"] = "off"
            try:
                out, reason = m._harness_check_output(
                    wt, "python3 auto-harness-check.py", "build.py")
            finally:
                if _cl is None:
                    os.environ.pop("CPU_LANE", None)
                else:
                    os.environ["CPU_LANE"] = _cl
            ok("a check that DIRTIES the tree is reported as contamination",
               reason is not None,
               "the porcelain comparison must actually produce the signal")
            ok("the contamination reason names the leftover path",
               reason is not None and "LEFTOVER.txt" in reason)
            ok("_tree_state returns None outside a git worktree",
               m._tree_state(Path(tempfile.gettempdir()) / "definitely-not-a-repo-x")
               is None)

    # ---- 14. killpg must never target the CALLER's own process group -------
    # start_new_session=True makes pgid == p.pid, so the self-kill is
    # unreachable today -- but nothing enforced that, and a reviewer reproduced
    # a suite-wide rc=-9 (taking their shell with it) by flipping that one flag.
    _real_getpgid, _real_killpg = os.getpgid, os.killpg
    killed = []
    try:
        os.getpgid = lambda pid: 4242          # child "shares" our group
        os.killpg = lambda pgid, sig: killed.append(pgid)
        try:
            m._run_in_own_process_group("sleep 30", str(Path(tempfile.gettempdir())), 1)
        except subprocess.TimeoutExpired:
            pass
    finally:
        os.getpgid, os.killpg = _real_getpgid, _real_killpg
    ok("killpg is NOT called when the child shares the caller's group",
       killed == [],
       "os.killpg(our own pgid) SIGKILLs ollama-dispatch-auto and its parent")

    # ---- 15. contamination must NOT route into auto-slicing ----------------
    # do_auto hands every falsy return to _autoslice_on_failure, which on a
    # top-level run decomposes the intent and enqueues a NEW slice chain. A
    # dirty worktree is not evidence that the intent is too big: overnight that
    # turned one killed self-check into a cascade of fresh GPU dispatches.
    src = AUTO.read_text()
    ok("the contamination refusal is tagged with the sentinel prefix",
       "CONTAMINATED_PREFIX = " in src
       and "f\"{CONTAMINATED_PREFIX}{why}" in src)
    # BEHAVIOURAL, not a grep. The previous version asserted that the literal
    # text `if why.startswith(CONTAMINATED_PREFIX):` appeared in the file,
    # which proves those characters exist -- not that the routing happens.
    # Rewording `why` upstream would silently restore auto-slicing while the
    # suite stayed green: the exact failure the sentinel exists to prevent.
    ok("a contaminated reason routes to die(), not the slicer",
       m._failure_route(m.CONTAMINATED_PREFIX + "tree left dirty") == "die")
    ok("an ordinary non-convergence still routes to the slicer",
       m._failure_route("DID NOT CONVERGE") == "autoslice")
    ok("an empty/None reason does not accidentally route to die()",
       m._failure_route("") == "autoslice" and m._failure_route(None) == "autoslice")
    ok("the routing tag is stripped from the operator-facing reason",
       m._strip_route_tag(m.CONTAMINATED_PREFIX + "tree left dirty")
       == "tree left dirty"
       and m._strip_route_tag("DID NOT CONVERGE") == "DID NOT CONVERGE")
    # The HANDLER, behaviourally. Asserting on _failure_route alone left the
    # gap one level up: a source-text assertion fires when the call is deleted
    # but is satisfied by any line containing it -- including an INVERTED one.
    # Reviewer reproduced it: flipping `== "die"` to `== "autoslice"` at the
    # call site re-introduced the pass-3 blocker verbatim and the whole suite
    # stayed green. _handle_author_failure holds the decision AND both
    # destinations, so an inversion flips which stub fires.
    with sandbox(m):
        fired = []
        m.die = lambda reason, code=1: fired.append(("die", reason, code))
        m._autoslice_on_failure = lambda a, t, r: fired.append(("autoslice", r))
        m._handle_author_failure(_args(), "build.py",
                                 m.CONTAMINATED_PREFIX + "tree left dirty")
        # The stub RETURNS instead of exiting, deliberately. The real die()
        # exits, but relying on a callee's non-return to keep contamination
        # away from the slicer is exactly the implicit contract that produced
        # the earlier bugs here -- so this asserts there is no fall-through
        # even when die() returns.
        ok("a CONTAMINATED failure calls die(), never the slicer",
           [f[0] for f in fired] == ["die"], fired)
        ok("die() gets the stripped reason and a non-zero exit code",
           fired and fired[0][1] == "tree left dirty" and fired[0][2] == 1, fired)
    with sandbox(m):
        fired = []
        m.die = lambda reason, code=1: fired.append(("die", reason, code))
        m._autoslice_on_failure = lambda a, t, r: fired.append(("autoslice", r))
        m._handle_author_failure(_args(), "build.py", "DID NOT CONVERGE")
        ok("a plain non-convergence still reaches the slicer, not die()",
           [f[0] for f in fired] == ["autoslice"], fired)
        ok("the slicer is told it was a non-convergence",
           fired and "did not converge" in fired[0][1], fired)

    # ---- 15c. do_auto's USE of the handler, end to end ----------------------
    # Extracting _handle_author_failure closed the INVERSION hole but opened
    # the same hole one level up: replacing the delegation in do_auto with a
    # direct _autoslice_on_failure(...) call left the suite green (`nocall`).
    # A grep would have "closed" that too, and then the next level would be
    # open. do_auto is the top-level entry point -- there is nothing above it
    # -- so driving it for real is what actually terminates the recursion.
    for reason, expect in ((m.CONTAMINATED_PREFIX + "tree left dirty", "die"),
                           ("DID NOT CONVERGE", "autoslice")):
        with tempfile.TemporaryDirectory() as tmp, sandbox(m):
            fired = []
            m.load_defaults = lambda: ("model", "host")
            m.do_scaffold = lambda *a, **k: None
            m.write_harness_check = lambda *a, **k: None
            m._author_with_continuations = lambda *a, **k: (False, reason)
            m.die = lambda r, code=1: fired.append(("die", r, code))
            m._autoslice_on_failure = lambda a_, t, r: fired.append(("autoslice", r))
            args = _args()
            args.__dict__.update(
                target="build.py", new_project=tmp, dest=None, repo=None,
                num_ctx=1, no_auto_slice=True, auto_slice=False,
                auto_slice_threshold=2, interface="iface", slice_plan=None,
                max_rounds=1, require=[], max_tokens=None, drafter_cmd=None,
            )
            m.do_auto(args)
            ok(f"do_auto sends a {expect}-routed failure to {expect}()",
               [f[0] for f in fired] == [expect], fired)

    # ---- 16. the sandbox itself: prove the leak class is really dead --------
    # This harness fix must not be the fifth untested guard in this series.
    _probe = m._harness_check_output
    with sandbox(m):
        m._harness_check_output = lambda *a, **k: ("stub", None)
        m._brand_new_attr_for_test = 1
    ok("sandbox restores a stubbed module attribute",
       m._harness_check_output is _probe)
    ok("sandbox removes an attribute the scenario added",
       not hasattr(m, "_brand_new_attr_for_test"))

    # ---- the escaping rule is actually wired into the AUTHOR prompt ----------
    ok("ESCAPING_RULE is interpolated into the author prompt",
       "{ESCAPING_RULE}" in src and "ESCAPING_RULE = " in src)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        return 1
    print("all author-continuation assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
