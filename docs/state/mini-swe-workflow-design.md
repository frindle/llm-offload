# mini-swe dispatch workflow v2 (design, 2026-10-24)

Status: DESIGN ONLY. Nothing in the runner, queue or repos has been changed. Anything marked "new" is a proposal.

## 0. What the evidence shows

One task family, `webhook-fix-u2`, went through 7 runs (u2, u2b x3, u2c, u2d; u2e is queued now) plus release-race-u1. None merged.

| run | where it died | real cause | class |
|---|---|---|---|
| release-race-u1 (38ff966512c9) | phase A: "fewer than 2 real assertions" (plus Darkbloom capacity retries) | qwen wrote a weak test | TEST (qwen-authored) + INFRA noise |
| u2 (b34a79ed55b6) | phase A: no test after 30 steps, TimeExceeded, "fixture never reaches auto_rescue_endpoint" | target_fn listed a function the task never calls; spec too big for 30 steps | SPEC |
| u2b (7531cd855697) | phase A: skeleton left unedited, provider capacity outage | Darkbloom outage | INFRA |
| u2b (a69a3f59e77b) | phase B: LimitsExceeded at 45 steps (default fix_steps), test_green/suite FAIL, MISSING | 5 to 7 features in one job, 41 module-level lines; the phase-A test **already had `UnboundLocalError: aw`** in test_malformed_state_loads_empty and was accepted | SIZE + TEST |
| u2c (a438f27017d4) | all gates PASS, Opus rejected | test_already_imported_clears_counter reads `r` it never binds (latent NameError, hidden at baseline because a FileNotFoundError hit first); qwen "fixed" it with `sys._getframe(2).f_globals['r'] = r` plus module-level `r = None`; test order came from `sorted(globals())`, so the "LAST" test was not last | TEST + GATE |
| u2d (f5da17d44f36) | test_unchanged FAIL, then fail-fast (B2 changed nothing in 35 steps) | phase-A test had `aw = reload_aw()` with no `global aw` (UnboundLocalError) and `reload_aw()` returned None (NoneType); red_without_fix accepted it because rc != 0; the only way to green was to edit the frozen test | TEST + GATE |
| u2e (30538c33e9a3, queued) | n/a | same shape again: qwen still writes the test, TASK.md just has more prose rules about `aw` | resubmitted without changing the workflow |

Holes in the runner, confirmed in `/Users/user/bin/mini-swe-runner.py`:
- `test_gate` (l.2161): `FAILASSERT_RE.search(out)` checks the **whole output**. One AssertionError anywhere makes the run count as "assertion red", so per-test UnboundLocalError, NameError and NoneType failures pass. `CRASH_RE` (l.1257) leaves out UnboundLocalError, NameError and TypeError.
- `fix_gate` (l.2316): `red_without_fix = rc2 != 0`, so any crash counts.
- `fn_scope` defaults to `"warn"` (l.2309, l.1966), so u2c and u2d were marked "ok" with WARN text about 14 to 41 outside lines.
- No check on the diff for frame or global hacks.
- No check that the test passes against a correct implementation. Nothing proves the test is satisfiable, so latent bugs like u2c's `r` only show up in phase B.

Tooling we already have and should reuse:
- **Supplied test.** If `test_file` already exists after `prepare()` (l.3419), the runner skips qwen phase A and gates the supplied file with `test_gate`.
- **Model-free dry run.** `--check-only --try-test T [--try-fix DST=SRC]` (l.3436) runs the phase-A gates on T, and with `--try-fix` the full phase-B `fix_gate` on a candidate fix.
- `steps` chains (l.939), where earlier steps' tests stay green (`_prior_tests`), plus `mutation_gate`, `symptom`, `reach_anchor`, `min_fn_cov`.

## (a) Who writes the frozen test

**Rule: Claude (Opus) writes the frozen test and a private reference implementation. qwen only writes the fix.** The two-phase "qwen writes the test" path is kept only for trivial single-function tasks, and its output must still pass (b).

Flow:
1. Opus confirms the bug or feature at baseline (existing rule: confirm the bug, verify the symptom).
2. Opus writes `test_<x>.py` from the template in (f): an explicit `TESTS = [...]` order, a per-test result line, no module alias reassignment, and a `reload_mod()` that rebinds through `globals()` in exactly one helper.
3. Opus writes `ref/<allowed file>`, a reference fix, in the job dir `~/.ollama-dispatch/<job>/ref/`. It never goes inside `baseline_from`, so qwen cannot see it.
4. Opus commits the test into the `baseline_from` dispatch worktree (`~/dispatch-worktrees/<job>-base`; never the real repo's main), so `prepare()` keeps it as tracked and the runner treats it as supplied. `test_file` must not be in `allowed`; precheck already enforces this.
5. Run validation (b). Only a job that passes it may go to `queue-submit`.

Cost: Opus spends about 10 to 20 minutes per job on the test and ref. Each u2 run cost 1 to 2 hours of queue time plus an Opus review, so this pays for itself on the first avoided failure. It fits "reasoning to Claude, bulk to qwen": the test is the spec, which is reasoning, and the fix is the bulk.

## (b) Pre-submit validation (new `mini-validate <spec.json>`, wraps existing pieces)

All of these must pass. The script writes `VALIDATION.md` into the job dir, and `queue-submit` refuses a spec without a fresh passing one (it compares the sha of the spec, test and TASK.md).

| # | check | how |
|---|---|---|
| V1 | spec sane | `mini-precheck spec.json` (existing) |
| V2 | test is statically clean | `pyflakes`/AST pass on the test: zero undefined names (F821; catches u2c's `r`), no assigning the module alias inside a function without `global` (catches u2d), plus the forbidden-pattern list in (c) G5 |
| V3 | red at baseline **for the right reason, per test** | `mini-swe-runner.py --check-only --cwd <scratch> --task-file spec.json --try-test test.py` with the hardened per-test classifier (c) G1. Every test must be FAIL, and every FAIL type must be AssertionError or a type listed in `red_ok` |
| V4 | green against the reference | same command plus `--try-fix <file>=ref/<file>`. All fix gates must be GREEN, including suite, strict fn_scope and the anti-gaming scan. This is what proves the test is satisfiable without hacks |
| V5 | each requirement is covered | for every hunk in the ref diff: apply the ref minus that hunk, and at least one test must go red (assertion-class). A hunk that no test catches is either unspecified or dead, so fix the test or drop the hunk. (Existing `mutation_gate` can stand in where it is configured) |
| V6 | sizing within limits | the ref diff stats are checked against (d). Over the limit means split into `steps` |
| V7 | order independence | run the test's `TESTS` list in reverse order (env `TEST_ORDER=reverse`, supported by the template) against the ref. It must still be green, or the order dependency must be declared with `ORDER_DEPENDENT = True` and a comment |

Phase B then gets `fix_steps` and `fix_wall` from (d), computed from the ref diff and not guessed.

## (c) Runner gate hardening (exact checks)

G1. **Per-test red classification** (`test_gate`, replacing the whole-output regex).
- Parse per-test outcomes. Python: the template prints `RESULT <name> PASS|FAIL <ExcType>: <msg>`, and `pytest -rA` lines are also parsed. Node: `not ok` blocks plus the error name.
- Reject at baseline if **any** test:
  - is PASS. Every frozen test must be red at baseline unless it is listed in `spec.baseline_green` (a regression guard);
  - fails with `UnboundLocalError`, `NameError`, `SyntaxError`, `IndentationError`, `ImportError`/`ModuleNotFoundError`, `RecursionError`, `TabError`;
  - fails with `TypeError` or `AttributeError` whose message matches `'NoneType' object|object is not subscriptable|object is not callable|takes \d+ positional|missing \d+ required`;
  - fails with any type not in {AssertionError} ∪ `spec.red_ok`. `red_ok` entries are `{type, match}`, e.g. `{"type":"AttributeError","match":"_rescue_gave_up"}` or `{"type":"FileNotFoundError","match":"rescue.json"}`, so a "missing feature" red has to name the new symbol or path.
- Reject if the parsed test count is not equal to `len(TESTS)`. That catches a harness that stops early.
- Add `UnboundLocalError|NameError: name` to `CRASH_RE` as a backstop.

G2. **red_without_fix uses G1 too** (`fix_gate`): the reverted run must fail the **same set of test names** as the baseline, and each with an allowed reason. `rc != 0` alone is no longer enough.

G3. **fn_scope fails instead of warning.** The default becomes `"fail"` (l.1966/l.2309), and `"warn"` needs an explicit spec key and a reason.
- Module-level edits are allowed only for the names in `spec.new_names` (e.g. `["_load_rescue_state","_save_rescue_state","_rescue_attempts","_rescue_gave_up"]`) and for imports.
- Any new module-level assignment or def not in `new_names` fails. That catches u2c's `r = None` and `_inject_r_into_caller`.

G4. **Anti-gaming diff scan** (new gate `no_hacks`, on `+` lines of the allowed files only; fails on any hit):
- `sys._getframe`, `inspect.(currentframe|stack|getouterframes|getframeinfo|trace)`, `\.f_back`, `\.f_globals`, `\.f_locals`, `\.f_code`;
- `globals\(\)\s*\[`, `globals\(\)\.(update|setdefault|__setitem__)`, `vars\(\)\s*\[`, `locals\(\)\s*\[`;
- `sys\.modules\[[^\]]*\]\.\w+\s*=` and `setattr\(sys\.modules`, `__builtins__`, `builtins\.\w+\s*=`, `__main__`;
- the test file's stem or name inside the allowed file (`test_auto_rescue`), `PYTEST_CURRENT_TEST`, `unittest` or `pytest` imports in non-test code, and the phrases `for test compat`/`test compatibility`;
- `except:`/`except Exception:` followed only by `pass` in added lines that wrap a target_fn call (soft: WARN, but listed in REVIEW).
- JS equivalents: `new Error().stack`, `arguments.callee`, `globalThis\.\w+\s*=`, `require.cache`.

G5. **Frozen-test static rules** (`py_static`; enforced for qwen-authored tests too):
- Test discovery may not use `globals()`/`dir()`/`sorted(globals())`. It must be an explicit `TESTS = [f1, f2, ...]` list, or pytest.
- No function may assign the module alias (`aw = ...`) unless the only assignment is inside a single helper that declares `global`.
- No undefined names (pyflakes F821), no bare `except` in the test, no `sys.exit(0)` before the summary.

G6. **Frozen-test tamper means test-suspect, not model failure.** The first `test_unchanged` violation inside a B attempt (detected by the existing step watch) stops that attempt and exits with `RESULT FAIL class=TEST-SUSPECT`, with the model's diff to the test saved as `test-proposal.diff`. With (a)+(b) this should never fire. If it does, the triage route is "Opus re-checks the test".

G7. **Report the class.** Every `RESULT FAIL` line gets `class=INFRA|SPEC|TEST|MODEL|SIZE`, using the signatures in (e), so qctl and triage do not have to scrape.

## (d) Sizing rules

Measured on the **reference diff** (V6):
- One job is **one behavior**: one target_fn plus at most 2 helpers listed in `new_names`.
- At most **40 changed lines**, at most **3 TASK requirements**, at most **5 test cases** (min 2).
- More than one cohesive behavior means a **`steps` chain**. Each step gets its own Opus-written test and ref slice, and earlier tests must stay green (already supported).
- u2 should have been 4 steps:
  1. library-path skip;
  2. `downloadClientId` in the command;
  3. persistence load/save plus malformed-loads-empty;
  4. cap 6 + give-up once + dry-run + already-imported clears.
- Step budget: `fix_steps = clamp(20 + 2 x ref_changed_lines, 30, 80)`, `fix_wall = 20 s x fix_steps`, `fix_attempts = 2`. If the formula wants more than 80, the job is too big: split it. (u2b died at the default 45 with about 70 changed lines in flight, and its peak prompt was 31.5k tokens.)
- Context guard: if B1's `peak_prompt` passes 60% of num_ctx, split before retrying.
- When qwen writes the test (trivial tasks only), `test_steps` is at most 30. If phase A cannot finish in 30, the test belongs to Opus.

## (e) Failure triage protocol

**No resubmit without `TRIAGE.md` in the job dir.** `queue-submit` (new check) refuses a spec whose task family (same `task` stem or `baseline_from`) has a FAILED run newer than its `TRIAGE.md`.

Steps:
1. Run `mini-triage <job-id>` (new, deterministic, no LLM). It greps the livelog, REVIEW.md and the trajectories for the signatures below, then writes the class, evidence lines and the trajectory step numbers where things went wrong. It reads the last 10 B-trajectory actions and any edits to the test file.
2. Opus confirms the class by reading the cited trajectory steps (Opus verifies conclusions) and writes `TRIAGE.md` with three lines: class, evidence, and what changes before the next run.
3. Route by class:

| class | signatures | action |
|---|---|---|
| INFRA | `Provider capacity is temporarily unavailable`, exit 3/PAUSED, BadRequestError/connection errors, TimeExceeded with < 25% of steps used | the only class that may be resubmitted **unchanged**; wait for capacity (qctl) |
| SPEC | `fixture never reaches`, `never executes line`, `MISSING:` from the model, literal/must_keep miss on text the ref also lacks, the model asks about ambiguity | fix TASK.md or spec, rerun (b) |
| TEST | any G1/G2 reject, `test_unchanged` FAIL / TEST-SUSPECT, a no_hacks hit (the model was forced to cheat), the review finds the test asserts something TASK didn't say | Opus rewrites the test, rerun (b); never just add prose rules to TASK.md (u2e's mistake) |
| SIZE | LimitsExceeded/TimeExceeded after steady progress, peak_prompt > 60% ctx, fn_scope fail across many unrelated lines | split per (d) |
| MODEL | validated test + ref green, gates fail on logic, no hacks, inside the size limits | retry once with the gate message (the existing `rejected` feedback); on a second MODEL failure try `fix_mode: single_shot` if it is one function, otherwise Opus/Sonnet implements directly |

Limits: **at most 2 resubmits per task family**, and the same class twice in a row means a strategy change (the next row down, or escalate). Log every run and triage line to Agent-Dispatch-Log.

## (f) Checklist and templates

### Pre-submit checklist (copy into each job dir as CHECKLIST.md)
```
[ ] bug/feature confirmed at baseline (command + output pasted)
[ ] scope: 1 behavior, <=40 ref lines, <=3 reqs, <=5 tests; else steps[]
[ ] Opus wrote test_<x>.py from the template; committed in baseline_from worktree
[ ] Opus wrote ref/<file> in job dir (NOT in baseline_from)
[ ] spec: target_fn, new_names, red_ok, fn_scope "fail", fix_steps/fix_wall from formula
[ ] mini-precheck ok
[ ] mini-validate ok: V2 static, V3 per-test red reasons, V4 ref GREEN all gates,
    V5 every ref hunk caught, V7 reverse order green
[ ] prior failure in this family? TRIAGE.md present and newer than the failure
[ ] queue-submit; dispatch logged
```

### Python frozen-test template
```python
import os, sys, json, tempfile, importlib
# --- env + fixtures before import ---
TMP = tempfile.mkdtemp(); os.environ["STATE_PATH"] = os.path.join(TMP, "state.json")
mod = importlib.import_module("arr-webhook")      # alias assigned ONLY here and in reload_mod()

def reload_mod():
    global mod
    mod = importlib.reload(mod)                      # rebinds the global; returns nothing on purpose

def setup(): ...                                     # resets every fake + file per test

def test_a():
    setup()
    r = mod.target(...)                              # bind every name you read
    assert r["x"] == 1, f"x: {r!r}"
    assert CALLS == [...], f"calls: {CALLS!r}"

TESTS = [test_a, test_b]                             # explicit order; never globals()

if __name__ == "__main__":
    order = TESTS[::-1] if os.environ.get("TEST_ORDER") == "reverse" else TESTS
    failed = 0
    for t in order:
        try:
            t(); print(f"RESULT {t.__name__} PASS")
        except Exception as e:
            failed += 1
            print(f"RESULT {t.__name__} FAIL {type(e).__name__}: {e}")
    print(f"{len(order)} tests, {failed} failed"); sys.exit(1 if failed else 0)
```

### Spec additions (proposed keys)
```json
{"fn_scope": "fail",
 "new_names": ["_load_rescue_state", "_save_rescue_state", "_rescue_attempts", "_rescue_gave_up"],
 "red_ok": [{"type": "AttributeError", "match": "_rescue_(attempts|gave_up)"},
            {"type": "FileNotFoundError", "match": "rescue.json"}],
 "ref": "~/.ollama-dispatch/<job>/ref",
 "fix_steps": 60, "fix_wall": 1200, "fix_attempts": 2}
```

## Implementation order (each item a separate mini-swe or Opus job, after approval)
1. G1+G2 classifier, G4 no_hacks, G3 fn_scope default fail. Runner self-tests get fixtures from u2c (frame hack) and u2d (UnboundLocalError) as must-reject cases.
2. `mini-validate` (V2 to V7) wrapping `--check-only --try-test/--try-fix`.
3. `mini-triage` plus the `queue-submit` TRIAGE/VALIDATION guards, and G7 class tags.
4. Update the memory notes `feedback_mini_swe_not_auto_dispatch` (Opus writes the test now) and `hub_verify`.

Immediate: hold or cancel u2e (30538c33e9a3). It is the same qwen-authors-the-test shape. Redo webhook-fix-u2 as a 4-step chain under this workflow.
