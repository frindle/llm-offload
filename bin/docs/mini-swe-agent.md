# mini-swe-agent: how we run it, what to give it, what to gate

Status (updated 2026-10-31): ADOPTED and in production. mini-swe v2 is the main code-edit path (203 mini-swe queue jobs since
10-16; 109/115 ref-backed v2 jobs passed). The original 2026-10-15 text said "NOT adopted"; that was the pilot verdict and is
superseded. Everything goes through the queue via `mini-submit` (the only door); the pilot's direct Darkbloom calls are retired.
Who writes what in v2: CLAUDE writes TASK.md, the frozen test(s) and a small validated reference fix (`ref/`, `mini-validate`
checks them). QWEN writes only the fix, re-deriving what the ref shows. The older "qwen writes the test" text under "Two-phase
mode" below describes the pre-v2 Phase A and is history, not the current flow. Since 2026-11-02 the local model can also draft the
frozen test for a ref Claude wrote (`mini-testdraft`, opt-in): see "Test drafting (L1)" at the end and `offload-rollout.md`.

## Setup
- Installed in the pilot venv: `pip install mini-swe-agent` (v2.4.6). Binary: `venv/bin/mini`.
- Model: `openai/qwen3.6-35b-a3b-vl-mtp-mxfp8` through the OpenAI-compatible local endpoint
  (`OPENAI_API_BASE` / `OPENAI_API_KEY` env, same as the Aider runner; never print the key).
- Env: `MSWEA_COST_TRACKING=ignore_errors MSWEA_CONFIGURED=true`.
- Command shape: `mini -m <model> -y -l 0 --exit-immediately -o <traj.json> -t "<task text>"`.
  `-y` = no confirmation, `-l 0` = no cost limit. The agent is a plain bash loop; it edits
  files with shell commands (sed, python), not a structured edit tool.
- Runner: `scratchpad/pilot/runm.sh <slice>`. Results: `results-mini.txt`
  (first-run copies in `workm-v1/`, `results-mini-v1.txt`).

## What to give it (the task text)
1. `TASK.md` verbatim: confirmed defect, entry point, required change, "must NOT change", and a
   "Must contain" list.
2. `verify.sh` in the working directory, and the instruction: run `bash verify.sh`, finish only
   when it passes.
3. A baseline copy of the worktree, not the live one: `git reset -q`, `git checkout -- .`,
   `git clean` (keep .venv/TASK.md/test_fixture.py/verify.sh), delete any reference impl
   (`refimpl.py`) so the model cannot read the answer.
4. Rules appended to every task (added after the first run, still unproven):
   - (macOS sed note is redundant: upstream's default prompt already injects it when
     `system == Darwin`. The agent still ran GNU `sed -i` once and recovered.)
   - If a file or module the task says already exists is MISSING, stop and say so in the final
     message. Do NOT create it.
   - Do not create files the task does not name.

## Gates (a green verify.sh is necessary, not sufficient)
- Changed-file scope: only the files the task names. Reject any new untracked file the task
  does not name. This is the check that would have caught both bad pilot results.
- Preflight: every module/file the task says exists must exist in the baseline; fail the slice
  before running if not.
- Diff review against TASK.md (an Opus read of the diff, not just the exit code).
- Known weak oracles: wiring slices whose verify only runs `ast.parse` on the file prove little.

## Pilot findings (9 slices, mini vs Aider)
- verify.sh: mini 9/9. Diff review: 7 correct, 1 correct edit plus an invented file
  (aw-app-wiring-s4), 1 over-built plus invented module (aw-sched-routes-s3).
- Aider: first scored 5/9, but the three wiring "failures" were a missing-`uvicorn` env error;
  re-scored (verify2) they pass. Real Aider failure: aw-sched-runner-s17. So roughly tied.
- Failure pattern: on a missing dependency the agent fabricates it and over-builds, then
  passes a verify that never exercises the real module.
- Junk staged files seen in two runs were stale index entries in the source slice worktrees
  (left by earlier queue runs), not agent behavior. Runner now does `git reset -q`.
- Baselines are inconsistent: some slice worktrees lack modules from earlier slices.

## Two-phase mode (2026-10-15; HISTORY: pre-v2 flow where qwen wrote the test, see the status line)
Why: a hand-authored oracle per job (verify.sh + verify.test.ts + refimpl.py) cost more Claude time
than the fix, and the last two were wrong (fixture never reached the code path, vacuous asserts,
refimpl failed its own verify). Now qwen writes the test, the runner gates it mechanically, Opus
reviews test + diff. Per-job authoring = TASK.md + a small spec. Runs through the queue as a
normal `--runner ~/bin/mini-swe-runner.py` job (answers the old "queue or direct" question).

Spec: `"mode": "two-phase"`, `test_file`, `test_cmd` (required); optional `target_fn` (name or
list that must run at baseline), `reach_anchor` (string or list of literals, each unique in the allowed files; see below), `symptom` (regex on the baseline failure), `suite_cmd`,
`typecheck_cmd`, `env`, budgets (`timeout` total, `test_timeout`, `test_steps` 30, `test_attempts`
2, `fix_steps` 45, `fix_attempts` 2, `gate_reserve` 240, `cmd_timeout` 300). `task` may be an
absolute path (copied in as TASK.md), so a job dir can hold its own TASK.md next to spec.json.
verify.sh / verify.test.ts / check_literals.py from the baseline are dropped unless `verify` is set.
With `verify` set, the old oracle (verify_sound + verify.sh) also runs as an extra, stricter gate.

Phase A (fresh mini run, TASK.md + rules): write only `test_file`. Other edits are reverted. Gates:
- static, parsed (Python `ast`; JS/TS via acorn from the repo's node_modules or `MINI_ACORN`, TS
  stripped with node's `stripTypeScriptTypes`; regex fallback when no parser): imports the target;
  at least `min_asserts` (2) real asserts; no vacuous forms through any alias (`a.ok(true)`,
  destructured `ok(true)`, `self.assertTrue(True)`, truthy constants, `x == x`, `>= 0` on a length,
  an upper bound alone like `len(r) <= 3` that allows zero, skipped tests); no mock of the target
  (`mock.module`/`jest.mock`/`vi.mock`/proxyquire/esmock with the target path, `sinon.stub`/`spyOn`
  on a target binding, `monkeypatch.setattr`, `patch`/`patch.object` under any unittest.mock alias,
  `sys.modules` swaps); no assigning or deleting a target export.
- source checks (Python, 2026-11-02): a test whose every assertion is a presence check on the code's own text (reading
  the target with `open`/`read_text`/`inspect.getsource`/`ast.parse` of its `__file__` and asserting a string or node is
  there, directly or through a helper) is rejected, and so is a run where every test red at baseline is one. Loading the
  target by `exec`, copying it into a sandbox, reading fixtures, and absence checks (`not in`, the removal specs) are fine;
  a file that also asserts behaviour passes. Opt out with `"source_checks": "allow"` plus `source_checks_reason`
  (precheck error without the reason); `test_rules: "off"` also skips it. mini-validate V2 honours both keys.
- runs red at baseline, on an assertion (not a crash, import error or timeout), matching `symptom`
  if given, with at least `min_tests` (2) tests when the runner prints a count.
- reach: the test runs under V8 coverage (`NODE_V8_COVERAGE`) or a Python profile hook
  (sitecustomize, which chains to the interpreter's own). Every `target_fn` must have run in the
  allowed files; without `target_fn`, at least one function there must have run. Line coverage
  (V8 block ranges / a settrace hook): each `target_fn` must run at least `min_fn_cov` (0.3) of its
  body's executable lines, so a test that only hits an early return or guard is rejected.
- `reach_anchor` (optional): a literal from the allowed files (e.g. `"waiting for another sync to finish"`) naming the TASK's
  entry-point line. When set and the file is coverage-mapped, the test must execute that line, and the `min_fn_cov` percentage
  check is skipped (unmapped files fall back to it). Use it when the bug is an early exit/guard (the baseline returns early, so
  most of the body cannot run until after the fix and a percentage wrongly rejects correct tests) or the target function is
  large (a weak test can hit 30% without reaching the TASK line). Each literal must appear exactly once across the allowed
  files (precheck error otherwise). Rejection reads "fixture never executes line N `<anchor>`; last line it ran before it: M
  `<src>`" and goes back to the model on retry via the usual rejected-feedback path.
- negative control: a no-op comment appended to the target keeps it red.
Phase A prompt also demands: for an 'at most N' cap feed more than N inputs and assert the exact count (or pair with >= 1), never a bare `<=`, and every assertion must fail on baseline. It also states the deadline (2026-11-05): `Budget: N commands` with N = min(`test_steps`, `FF_TEST_STEP`), a complete first version of the test by command max(8, N // 3), and that the test does not need to know HOW to fix the bug (no fix research in phase A). Added after the real-issue bench's sqlmeta r1 spent both 30-command attempts researching the fix and never wrote the test. When TASK.md shows an example (stage 18, 2026-11-06), at least one test runs that input through the public call TASK.md uses and asserts what the user sees; a test of an internal helper or data structure may come in addition, never instead. Added after isort r3: both tests asserted a key of `parse.file_contents()`'s internal comment dict, the fix turned them green, and isort still printed the wrong output. Compiled bytecode (`*.pyc`, `__pycache__/`) is ignored in every changed-file list, the agent runs with `PYTHONDONTWRITEBYTECODE=1`, and tracked .pyc that changed are `git checkout`ed after each agent run.
Rejected tests go back to qwen (fresh context, reason given) up to `test_attempts`.

Phase B (fresh mini run, prompt = TASK.md + the frozen test only): fix. Gates: test sha unchanged
and green (no fewer tests); reverting the allowed files to baseline turns it red (the green comes
from the diff); suite and typecheck add no failure lines vs their baseline run (both are often
red at baseline); `## Must contain` literals present (literals already at baseline are listed as
no evidence); scope (allowed files only, no stray untracked, baseline untracked files exempt); no
`MISSING:`; `must_keep` (optional: `Must keep: \`lit\`` lines or a `## Must keep` bullet list in TASK.md; each
literal must be in the final allowed file(s) and not be one whose only occurrences the diff removed); `diff_reach`: the frozen test must execute at least `min_diff_cov` (0.5) of the
executable lines the fix changed (git diff -U0 hunks, line coverage of the green run), so dead or
unreached code cannot ride along. Files in `drop` that no longer exist are out of scope (lets a
tracked hand oracle be dropped). A red gate gets one more fresh attempt with the reasons.

**Soft gates (2026-11-03; see offload-rollout.md, "BFMR as the offload test").** These gates catch defects that no test
can, so a miss is sent to review rather than thrown away. When only soft gates fail, the result is `RESULT PASS
needs-review soft=<gates>`, the queue shows `NEEDS-REVIEW (soft: ...)`, and mini-scorecard reviews every such pass, keeping it
out of the sampled-pass rate. A steps chain keeps the strict verdict.

- **Soft gates:** fn_scope, literals, lint, must_not_contain and fix_hunks.
- **ws_only** never fails. ws_cleanup puts back whitespace-only line changes while the frozen test stays green.
  - It is skipped when TASK.md asks for a formatting change, judged by formatting wording only: "whitespace" as data, as in
    "every whitespace character removed", does not count.
  - It is also skipped when the spec sets `"ws_clean": "off"`.
- **must_not_contain:** each literal in TASK.md's `## Must not contain` list must be gone from the final files. The list uses
  the same bullet grammar as `## Must contain`, and literals compare as prose: comment markers and line wraps are ignored.
  - `mini-spec --lint` warns when a TASK asks to change prose and has no such list.
  - mini-validate V4 runs this gate on the reference fix, so a literal that the reference still contains, or that matches
    nothing, fails before submit.
- **fix_hunks** runs after the loop and is never fed back to the model. It is mini-validate's V5 applied to the model's fix:
  - It reverts one code hunk at a time and reruns the test, plus the type check when the spec has one; up to 12 hunks.
  - Its cast_check drops each new TypeScript `x as T` alone and reruns the type check; up to 4 casts.
  - It reports any hunk or cast that is not needed. Turn it off with `MINI_FIX_HUNKS=off` or the spec's `"fix_hunks": "off"`.

Output: `RESULT PASS|FAIL mode=two-phase <gate>=ok|FAIL ...`, per-step tok/s lines (numbering
continues across runs), one `[worker] USAGE SUMMARY` covering all runs, and
`<run dir>/REVIEW.md` (gate table, baseline failure, coverage evidence, test file, diff, the model's
final message, context per run). REVIEW.md opens with TASK.md's 'Behaviour that must NOT change' block and `Must keep:` lines for the reviewer to check the diff against. A phase A failure (rejected or MISSING) also writes REVIEW.md:
the rejected test, the gates it failed, and the model's final message.

Process control: SIGTERM/SIGINT (queue preemption) kills every tracked child tree (mini, test and
suite commands each run in their own session), deletes the key file, restores nothing, and prints
`RESULT FAIL preempted` (rc 1). `timeout` is a hard total deadline: every mini run and gate command
is capped by the time left, and a SIGALRM backstop at `timeout + deadline_grace` (120s) prints
`RESULT FAIL timeout-total`. Legacy mode gets the same handlers (deadline `timeout + 900`).

Key handling: the endpoint key is never in the environment the agent's shell commands inherit.
mini starts through a small launcher (its own interpreter, from the script's shebang) that reads the
key from a 0600 file in a 0700 temp dir, deletes it, sets `litellm.api_key`, and runs `mini`
in-process. Not in `model_kwargs` either (mini serializes those into traj.json). Verified against a
fake OpenAI server: the request carries the Bearer key; `env` run by the agent has no key.

Context: each run logs `[mini-runner] context A1: steps=N peak_prompt=X last_prompt=Y tokens`.
Upstream mini.yaml already truncates long observations (10k chars, head + tail, with a warning);
our READ_HINT asks for line-range reads.

Context: separate mini runs per phase and attempt (qwen loops at 40-60k prompt tokens), per-phase
step limits (`-c agent.step_limit=N`), and a read-by-line-range hint. Each run is killed as a
process group at its budget. The overlay's verify.sh rule is now generic. Legacy mode puts the
verify.sh line in its task prefix.

Also fixed: a copied worktree's `.git` is a `gitdir:` pointer, so `git reset` in the scratch copy
was rewriting the SOURCE worktree's index. prepare() now gives the copy its own repo (same HEAD,
objects via alternates).

Check without a model: `mini-swe-runner.py --check-only --cwd DIR --task-file spec.json` prepares,
records suite/typecheck baselines, gates a supplied `test_file` if one is present, and prints the
phase A prompt. Dry-run the gates on hand candidates: add `--try-test cand.py` (phase A gates) and
`--try-fix src/x.py=fixed.py` (then the phase B gates); exit 0 = accepted/green.

Weak spots: thresholds (`min_fn_cov`, `min_diff_cov`) are heuristics, tunable per spec. JS line
coverage is skipped for a file whose V8 offsets do not map to the source (a transpiled build): the
gate says "unmapped" and falls back to function reach. Repos without acorn (the ev repos) use the
regex fallback for JS/TS static checks. The agent's shell can still read `~/.darkbloom/local.json`
directly (no sandbox; the queue's `--allow-unisolated`). Static checks cannot prove an assert is
meaningful, only that it is not one of the known vacuous shapes; Opus review of REVIEW.md remains
the last gate. TASK.md quality still bounds everything.

## Pre-run check, test skeleton, steps chain (2026-10-16)

Pre-run check: `precheck(spec, cwd)` is mandatory. Two-phase and legacy runs call it after `prepare()` and before any
model call; any error prints `RESULT FAIL bad-spec <reasons>`. It covers: the binaries `test_cmd` needs exist on the
PATH the agent will get (the runner's PATH, i.e. the queue's minimal PATH plus Homebrew, plus `env.PATH`), the first
runtime command does not exit 127, `python -m mod` modules import with that interpreter, every `cd` target exists,
each `target_fn` is defined in the allowed files, the `test_file` directory exists, `baseline_from` is a git worktree
(HEAD behind `origin/main` is a warning only; no fetch), and the existing checks (browser-only `target_fn`, TASK.md
preflight, and `verify_sound` when `verify` is set). `suite_cmd`/`typecheck_cmd` binaries are warnings. Run it before
enqueueing: `mini-precheck SPEC.json [--cwd PREPARED_DIR] [--keep-path]` (exit 1 with the reasons; PATH is cut to the
queue's minimal one unless `--keep-path`). Handles `steps` specs.

Phase A skeleton: when a two-phase `test_file` is absent, the runner writes a skeleton before phase A and the prompt
says to fill it in. node:test (`const { test } = require('node:test'); const assert = require('assert');`, ESM
`import` when the nearest package.json is `"type": "module"` or the file is `.mjs`) with a namespace import of each
allowed node file by its real path relative to the test file, extension kept (`./x.ts` for TS under `node --test`); or
pytest with `import mod` resolved relative to the test dir (a `sys.path` insert for files outside it). Two placeholder
tests (`min_tests` if larger) carry `TODO(mini-skeleton)` comments and no assertions. The gate rejects a test that
still contains the marker, and the static gate would reject it anyway (no real assertions), so an unedited skeleton
never passes.

`steps`: `"steps": [ {task, test_file, test_cmd, allowed, target_fn, symptom, min_tests, ...}, ... ]` on top of shared
keys (`baseline_from`, `suite_cmd`, `typecheck_cmd`, `env`, `timeout`, ...). A step key overrides the shared one; each
step needs `allowed`, `test_file` (distinct per step), `test_cmd`, and (from step 2) its own `task` (absolute path, a
path in the work tree, or inline text; inline = contains a newline or any character not valid in a path). Each step is
a full two-phase run with fresh context, in order, on the tree the previous step left: only step 1 runs `prepare()`,
earlier steps' edits and tests stay in place, and the suite gate also re-runs every earlier step's `test_cmd`
(`prior_tests`). Scope and the diff are judged against where the step started. TASK.md for a step is that step's task
plus a one-paragraph "Already done in earlier steps" summary. `timeout` is per step (the chain deadline is their sum).
The first failing step stops the chain: `RESULT FAIL step-<n> <reason>`; the work dir keeps earlier steps' changes.
All pass: `RESULT PASS mode=two-phase steps=N step-1=ok ...`. `REVIEW.md` has a status table and a section per step
("not run" for steps after a failure). One `[worker] USAGE SUMMARY` covers the chain; context lines are tagged
`s<n>A1`/`s<n>B1`. `--check-only` validates and prepares every step without a model.

## mini-spec: generate a run dir (2026-10-16)

`mini-spec <worktree> <task-paragraph-or-file> [--name N]` writes `~/.ollama-dispatch/mini-runs/<name>/`
(`spec.json`, `TASK.md`, `TASK-sN.md`, `work/`; name defaults to the worktree name without `wt-`) and runs
`mini-precheck` on it (`--check` also runs `--check-only`). No model, no network, nothing enqueued. Exit 2 = refused
(ambiguity, nothing written), 1 = written but precheck/check-only failed.

Preferred form (2026-11-02): `mini-spec --repo <repo> [--base origin/main] --name <area-change> <task>` creates the worktree itself:
it fetches `<remote> <branch>` when `--base` names a configured remote (a failed fetch only warns), then `git worktree add -b
dispatch/<name> ~/dispatch-worktrees/<name> <base>` (`MINI_WT_ROOT` overrides the root; the `dispatch/` prefix is what
`worktree_cleanup` deletes after landing). So a stale local HEAD can no longer become the baseline (precheck only warned about it).
A rerun reuses `<root>/<name>` only when it is a clean worktree of the same repo at the base (or fast-forwards it when it is clean and
behind); a dirty or diverged one, an existing `dispatch/<name>` branch or an existing run dir without `--force` is refused (exit 2).
For a repo whose origin is archived (e.g. `~/bin`), pass a local ref: `--base HEAD` or the branch you land onto.

Inferred, each printed with its evidence: `baseline_from` (the worktree root, path kept as given), `mode`
two-phase, `allowed` (source paths named in the task; else the one file defining each backticked/called/camel/snake
identifier), `target_fn` (functions named in the task and defined in the allowed files; Python via `ast`, JS/TS via
definition regexes confirmed by the runner's own `_defines`, so candidates are exactly what precheck accepts), one
`steps` entry per function (`--single` = one run with a `target_fn` list), runner (node:test or pytest: package.json
deps/scripts, vitest/jest/mocha configs, what existing tests import; pytest must import on the queue PATH; `.venv/bin/python`
when present), `test_file` (nearest convention: sibling `x.test.ts`/`test_x.py`, `test-*.js` in the package dir, or
`test(s)/`; never a name that already exists, since an existing test file counts as supplied), `test_cmd` (`cd <test dir>
&& node --test f` keeping the package script's `VAR=val` prefix and `--experimental-strip-types`; or `python -m pytest -q
-p no:cacheprovider f`), `suite_cmd` (existing tests that import an allowed file, max 12; else the package's `npm test`),
`typecheck_cmd` (TS + tsconfig.json + local tsc), `drop` (default three plus known oracle/harness leftovers present in
the worktree), `timeout` (2400, 3600 with typecheck or `npm test`).

Drafted only: the TASK.md contract. The task paragraph goes under Confirmed defect verbatim; Entry point (file, function,
line span), Scope and Test notes (test file, command, env vars the module reads) are mechanical; must-not-change
candidates (other functions in the file, its public names, callers), Must contain candidates (backticked literals absent
at baseline) and a Must keep candidate (`function fn(` / `def fn(`) are listed with `TODO(mini-spec)`, plus TODO slots
for the defect repro, the exact contract and fixtures. TASK.md goes to the model verbatim, so a human/Opus must
confirm every marker line and remove it: `mini-spec --lint RUN_DIR` fails while any marker remains (and warns on an
empty Must contain), then runs mini-precheck. Lint before enqueueing.

Refuses (exit 2, candidates listed) instead of guessing: several runners (`--runner node-test|pytest`), a non-node:test
framework only, an identifier defined in several files or a basename matching several paths (`--allowed F`), no
function named (`--fn NAME` / `--no-fn`), more than 5 functions or 3 files, mixed languages or packages, tied test
location (`--test-style sibling|prefix|testdir`), an existing run dir (`--force` rewrites spec.json/TASK*.md only).

Limits: `env` is never inferred (Test notes lists the env vars the code reads). JS/TS function spans come from brace
matching (approximate); methods are matched by name only. Suite detection is import-based, so tests that exercise the
file indirectly are missed. Self-test: `mini-spec --self-test` (temp git repos: a node one -> 2-step chain, a python
one -> single spec; both pass mini-precheck and `--check-only`; ambiguity and lint cases fail as expected).

## Intake, stubs, probe, output cap, bench (2026-10-16)

Why: runs kept failing for reasons visible before any GPU time was spent. egift-s1 targeted a placeholder (`export {}`: no function,
so "fixture never runs any function"); egift-s2's CommonJS sidecar used ESM syntax, so `require` threw SyntaxError and the model hid it
in a try/catch; sync-queue and amz-2p reached only 25% and 18% of the target body (tests stopped at early guards), and amz-2p's target
runs only inside `page.evaluate`; mini6-amz-photo (legacy single-phase) timed out after 46 calls at ~39k prompt tokens because full
`node --test` TAP output piled into the context.

**Intake (`mini-spec` fit_check).** Every build classifies the task and writes `INTAKE.md` + `spec["intake"]`. Classes and exit codes:
`local-ready` 0, `needs-stubs` 3 (a file or target function missing at baseline, or a placeholder with no functions/exports),
`needs-split` 4 (several files, more than 2 target functions, a body over 80 lines (over 150 never) with no reach_anchor, or a legacy
single-phase spec), `not-local` 5 (target only runs inside `page.evaluate`, or the anchor is such a call). Early return/throw guards
before the anchor (2 or more) are NOT a split reason since 2026-11-02 (out-of-scope guards): a one-block fix behind guards stays
`local-ready`, INTAKE.md gets a `- note:` listing the guard lines, and `metrics.test_draft_ok` is false. Why: in v2 the frozen test is
Claude's and mini-validate V3 (the runner's reach gate) rejects any test that never executes the anchor line, so getting past the
guards is the test author's setup work. The note matters only for local test drafting (qwen's own tests stopped at guards: sync-queue
25%, amz-2p 18% reach), so don't hand such a test to `mini-testdraft`. `mini-spec --lint` fails on a non-ready class. Flags: `--reach-anchor [FN=]LITERAL`, `--override REASON` (accept and
record), `--tried WHAT` (records an attempted split/seed; only then does INTAKE.md carry the `HANDOFF-CLAUDE (last resort)` line),
`--intake RUN_DIR` (classify an existing run dir; legacy specs come out needs-split). Existing run dirs are not rewritten.

**Seed stubs.** `mini-spec --seed-stubs WORKTREE TASK.md [--file F] [--name N] [--apply]` builds throwing stubs mechanically from the task's
`Signature: name(params)` lines (refuses with exit 2 if there are none). Module system: `.mjs/.mts` or package `"type": "module"` -> ESM
(`export function`), `.cjs` or otherwise -> CommonJS (`function` + `module.exports`), `.py` -> `raise NotImplementedError`. Body is
`throw new Error('not implemented: <fn>')`; TS keeps parameter types, JS drops them. The `export {}` placeholder is removed; functions already
defined are left alone. Default output is a plan + diff; it writes only with `--apply` or when the worktree is under the system temp dir.
Commit the stubs on the worktree branch, then rerun mini-spec: each `'not implemented: <fn>'` literal becomes that step's `reach_anchor`
(precheck needs it exactly once) and `target_fn=<fn>`, and `stub_error` (the message as printed) is set. Runner: spec key `stub_error`
(string or list) makes the stub's own throw an acceptable red in `test_gate` (otherwise "baseline failure is not an assertion failure").

**Probe.** `mini-swe-runner.py --check-only --probe` (per step for chains) writes a throwaway probe test (`probe_test()`, next to
`skeleton()`): it loads every allowed module in try/catch (a load error is printed as `PROBE-LOAD ...`), calls each `target_fn` in
try/catch (Python: `None` for each required parameter), then fails with `assert.fail('probe')`. It runs through the real `test_cmd` with
`full_reach=True` and prints `PROBE ok reach=[...] anchors: ...` or `PROBE FAIL <reason>`: modules load (no CRASH_RE match), the failure is
a plain assertion, every `target_fn` ran (so it is exported and reachable), every `reach_anchor` line exists (the count of early guard
lines before it is reported). `mini-spec --check` runs `--check-only` then the probe and prints those lines. No model.

**Output cap.** `configs/mini-local.yaml` overrides upstream's `model.observation_template` (10000 chars, head 5000 + tail 5000) for every
run, legacy single-phase included. Output under 3000 chars is passed as is, and so is any non-test-runner output under 4500 chars (a 100-line `nl -ba | sed -n` read fits; head and tail can no longer overlap). Larger test-runner output (node TAP or spec reporter,
pytest) is reduced to the failing tests plus their `error:/expected:/actual:/code:` lines, `E` lines, `FAILED` lines and the totals (at most
40 lines of 300 chars). Bigger non-test output is head 1200 + tail 2300 with a hint to use `grep -n` or `tail`. Self-tested in the runner
self-test (renders the template with jinja2).

**mini-bench (regression bench).** `mini-bench [--only a,b] [--runid X] [--dry-run] [--list] [--self-test]` runs the fixtures in
`~/bin/bench/mini/<task>/` (plain directories: `src/`, `TASK.md` [+ `TASK-s2.md`], `spec.tmpl.json` {mini-spec args, expected intake class,
steps}, `expect`; never nested git repos). Ten tasks: text-truncate, checkout-rounding, ev-circuit, pa-supersede, bg-s4 (small versions of
past slices), guard-anchor (a guarded function with a reach_anchor), greenfield-cjs and greenfield-esm (seeded stubs), browser-helper (an
extracted pure helper), chain-2step. Per task: `setup` copies the code to `~/.ollama-dispatch/bench-fixtures/<runid>/<task>` and does git
init + commit; `mini-spec` builds `bench-<runid>-<task>` with the fixture's final TASK*.md; lint; `--check-only`; `--probe`. Only then (never
with `--dry-run`) it enqueues with `--runner mini-swe-runner.py`, label `bench-<runid>-<task>`, bundle `bench-<runid>`, polls
`ollama-queue.py status`, parses the last RESULT line of each job log with `parse_runner_result` from ollama-queue.py, writes
`~/.ollama-dispatch/mini-bench/<runid>.json` and prints a table of RESULT classes (PASS, FAIL-SPEC, FAIL-TIMEOUT, FAIL-MODEL, NO-RESULT),
then runs `qctl retire bench-<runid> --reason bench`. "Accepted first try" = PASS with only A1/B1 trajectory files (`sNA1`/`sNB1` in
chains). `--dry-run` stops after the probe and uses temp dirs: it is the model-free check that the fixtures are sound.

Cost: a full run is ~10-25 min of GPU queue time per task (~3-4 h for all ten; a 3-task `--only` smoke is ~1 h) and it holds the shared
queue, so a non-dry run needs the owner's OK. Only `--dry-run` has been exercised so far; the enqueue/collect path was written against the
`ollama-queue.py enqueue/status` interfaces and is untested live.

**Rule: run `mini-bench` after any change to `mini-swe-runner.py`** (a `--dry-run` at least, a real smoke before relying on the change).
Self-tests prove the gates; only the bench proves a model still gets through them.

## Speed work: context control, wall caps, fail-fast, slices (2026-10-16)

Measured on 264 steps of the local Qwen runs: prefill is ~75% of wall time (1.02 s per 1k prompt tokens, 14.4 s per 1k completion
tokens), `cached_tokens` is 0 (no prefix reuse), thinking arrives inside `content` and is resent every turn (~half the context),
observations are small, 24 of 45 Phase B steps were re-reads like `nl -ba poll.js | sed -n 217,280p`, and attempts mostly ended on
LimitsExceeded. Every change below targets that.

- **Context control** (`mini_ext/mini_ctx.py`, `CtxLitellmModel(LitellmModel)`; overlay `model: model_class: mini_ctx.CtxLitellmModel`;
  mini-swe-agent 2.4.6 `get_model_class` accepts a full import path). It rewrites only the API-bound copy in
  `_prepare_messages_for_api`; the trajectory on disk stays full. The runner's LAUNCH stub does `sys.path.insert(0, bin/mini_ext)`
  (not PYTHONPATH: the agent's shell inherits env).
  - `shrink_obs` (default ON): tool outputs older than the last 6 become their first 300 chars + "rerun the command to see it". The latest
    test-runner output (TAP, pytest, `output_summary`, AssertionError ...) is always kept whole.
  - `ceiling` (default ON, `ceiling_tokens: 32000`, estimate = chars/3.5): over it, only the last 3 tool outputs stay whole (latest test run
    still does); if still over, oldest assistant texts are stubbed, then any tool output over 8000 chars is cut head 4000 + tail 4000.
  - `strip_old_thinking` (default OFF, overlay key): drops the text up to `</think>` in all but the last assistant turn. It deviates from
    the model card (`preserve_thinking: true`), so it needs the owner's approval before it is switched on.
  - Test: `python3 mini-swe-runner.py --self-test` (runs `mini_ctx.self_test()` in the mini venv); offline replay of a saved trajectory:
    `~/.ollama-dispatch/mini-venv/bin/python mini_ext/mini_ctx.py --replay COPY.traj.json` (a copy of resell-sync-queue work7.s1B1: 45 steps,
    raw peak 47k est. tokens -> 32.0k sent, latest test output unchanged).
- **Wall caps**: spec keys `test_wall` (default 600) and `fix_wall` (default 900) become `agent.wall_time_limit_seconds` for each Phase A / B
  attempt. The runner's own kill timer stays as the backstop at cap+60. Step caps are unchanged (`test_steps` 30, `fix_steps` 45).
- **Fail-fast** (watch callback in `run_mini`'s poll thread, every 5 s on the trajectory): Phase A: the test file must exist (and differ from
  its skeleton) by step 30 (`FF_TEST_STEP`, the Phase A late-write check; there is no earlier "test must have run" rule). Phase B: an allowed
  file must differ from its attempt-start snapshot by step N = `ff_fix_step()`: 15 (`FF_FIX_STEP`), 25 if the biggest allowed file is >200
  lines, 35 if >400 (`FF_FIX_SCALE`). A write staged toward an allowed file (`staged_write`: `cp`/`mv`/`tee`/redirect/`sed -i`/python or node
  file write naming the allowed file, or a heredoc of >= `FF_STAGE_LINES` (8) lines redirected into a file, e.g. a rewrite built under /tmp)
  counts as progress, for `FF_STAGE_GRACE` (10) more steps; after that an unchanged file ends the attempt ("a staged rewrite was never copied
  into it"). Either phase: the same command 3 times in a row with identical output. Why: job 72c99d790e4d (s2B1, s3B1, s3B2) was killed at step 15
  while staging its edit under /tmp, one step before copying it in. On trigger the process group is killed and the reason is prepended to
  `rejected` (so the next attempt's prompt says why), only when the gates also reject the attempt's result. The Phase B prompt states N
  ("edit the real file in place, small edits, test after each; staging in /tmp does not count; ended if unchanged at step N"), also on the retry.
- **Stray files between Phase B attempts**: files that are untracked now but were not at the attempt start (e.g. a `.bak`) are deleted before the
  next attempt (`clean_new_untracked`; never pre-existing untracked files, the allowed files, the test file or `IGNORE_UNTRACKED` paths), and the
  retry prompt lists them. Otherwise one leftover `.bak` fails the scope gate of an otherwise green retry.
- **Config text**: `/tmp` is for throwaway scripts only, never for staging edits to allowed files; the BSD-incompatible multi-line `sed -i` is
  replaced by "use a python3 heredoc" (macOS `sed -i '' '54,59c\...'` fails). `READ_HINT` asks for at most 60 lines per read.
- **Function slice** (`fn_slice(cwd, allowed, target_fn, anchors)`; `functions`/`_js_end` moved here from mini-spec, which calls
  `R.functions`): the Phase A / B prompt ends with the file's leading import/require lines, the target function line-numbered (max 150
  lines, or +-60 around `reach_anchor` when longer), and a one-line outline `name:first-last` of the other functions. Recomputed from the
  current file on every attempt. It saves the first read; the model still edits the real file.
- **tok/s labelling**: the `tok/s` in the per-step lines and USAGE SUMMARY is completion tokens / seconds since the previous reply, so it
  includes prefill and tool time. Lines now say `tok/s wall incl. prefill+tools, Ns since prev reply`; the dashboard regexp
  (`([\d.]+) tok/s`) still matches. The decode-only rate is much higher.

### Speed target and bench compare
Target on the small-fix fixtures: median job <= 15 min, p90 <= 25 min, pass rate not lower than baseline minus 1 task. `mini-bench` records,
from trajectory timestamps, per task and per phase/attempt: wall, steps, peak and summed prompt tokens, exit_status (report JSON `tasks.<t>.stats`,
the table, an attempt table, and `speed` = median/p90 job wall vs the target). Compare method: same `--only` set for both arms, n=2 runs per arm
(distinct `--runid`), old overlay (git stash / previous commit of `configs/mini-local.yaml` + runner) vs the new one, then compare pass counts and
the median/p90 from the two reports. Not run yet.

### Awaiting the owner's approval (NOT changed)
- `presence_penalty`: overlay 1.0 vs model card 0.0 (`model_profiles.yaml:192`).
- `max_tokens`: overlay 12288 -> ~6000 (completion tokens cost 14.4 s per 1k).
- Enabling `strip_old_thinking` (deviates from the card's preserve_thinking).
- Lowered step caps (`test_steps`/`fix_steps`).
- Bench GPU time: the compare needs 2 arms x n=2 x the chosen `--only` set of real queue time (~10-25 min per task, see Cost above).

## Offload follow-ups (2026-10-16, research: scratchpad offload-research.md)

### no_think (`enable_thinking:false` per phase)
Probe (2 single requests to the runner's endpoint, `/v1/chat/completions`, user prompt "What is 17+25? Answer with just the number.", max_tokens 300, temp 0.6, top_p 0.95): with top-level
`chat_template_kwargs:{enable_thinking:false}` the reply was `42` in 3 completion tokens, no thinking text; without it, 126 tokens of "Thinking Process: ..." inside `content`.
So Darkbloom/vllm-mlx honours it (litellm's `extra_body` is merged to top level, which is how the overlay already sets `enable_thinking: true`).
Spec key `no_think` (default OFF): `true`, or per phase `{"A": false, "B": true, "single": true}` (A = test writing, B = mini fix loop, single = single-shot repair).
When on, the runner adds `-c model.model_kwargs.extra_body.chat_template_kwargs.enable_thinking=false` to that mini run (mini's recursive merge keeps preserve_thinking, top_k, presence_penalty, max_tokens untouched; self-tested against the real mini venv config merge).
Not changed: sampling stays temp 0.6 / presence 1.0 / max_tokens 12288 (the card's instruct preset 0.7 / 0.8 / presence 1.5 is not applied). The owner enables it per spec; trial = 2 bench fixtures per arm, pass if completion tokens per accepted step drop >=50% and pass count is not lower.

### Single-shot repair (`fix_mode: "single_shot"`, Phase B)

Splice rules (`ss_splice`): the reply is spliced verbatim when its first line already has the original's indentation, else shifted by that one uniform difference, never inside triple-quoted strings / template literals (tokenizer / scanner) and never on blank lines; Python decorators are part of the function span (`functions()` starts at the first decorator) and are re-attached if the reply omits them, so `@property` is never duplicated or lost. A reply with any other top-level statement (assignment, import, expression, `module.exports.x = ...`) or function ends single-shot (needs more). The frozen test file is never swapped or deleted by single-shot (and `test_file` in `allowed` is a precheck error). A syntax-check timeout rejects the candidate. If the mini loop has no time left after a RED single-shot candidate, RESULT reports that candidate's RED gates instead of `no-time-left`.

Why: prefill dominates (no prefix cache) and the spec already localizes (`target_fn`, `fn_slice`), so the Phase B loop mostly re-reads. Default stays the mini loop; opt in per spec
(`mini-spec --fix-mode single_shot`, bench fixture `text-truncate-ss`). Applies to a run/step with exactly ONE `target_fn` defined in exactly one allowed file with a known span of
<= `single_shot_max_lines` (200); otherwise the runner logs why and uses the mini loop.
- One request per sample (fresh context, no tools): TASK.md contract, the frozen test, imports, the target function line-numbered over its whole span, the outline of the other
  functions, the baseline test failure (retries: the previous version and its rejection reason/test output instead). The model answers with the whole new function in ONE fenced block
  (language tag python/js/ts or none); `NEEDS_MORE: ...` means it cannot be done in one function.
- The runner splices it over the span from `functions()` (re-indented to the original first line; the file is restored to baseline before every sample so the span is stable), then
  checks it parses (`compile()`; `node --check`; TS: `stripTypeScriptTypes`), runs the test, and only on a green test the EXISTING `fix_gate` (all Phase B gates, unchanged).
- Rejections that cost an attempt and go back in the next prompt: no fence, wrong fence tag (diff, bash...), diff/SEARCH-REPLACE shape, block does not define the target, reply cut off
  at max_tokens or only reasoning, does not parse after the splice, test still red (with output), any gate red (reasons). Immediate fallback without retrying: `NEEDS_MORE`, or the block
  defines another top-level function/class (the fix needs more than the function).
- Keys: `single_shot_retries` (2, so 3 attempts), `best_of` (1: samples per attempt, sequential, first that passes all gates wins), `single_shot_max_lines` (200), `no_think: {"single": true}`.
  After the attempts fail the file is restored and the normal mini Phase B runs (fresh prior=None). RESULT gets `fix_mode=single_shot` (single-shot passed), `fix_mode=single_shot>mini`
  (fell back; the PASS/FAIL is then the mini loop's), absent when the key is unset. REVIEW.md context lists `B-single-shot: attempts=N green=...`.
- Model path: `mini_ext/single_shot_call.py` run by the mini venv's python: litellm.completion with the overlay's `model_kwargs` (same sampling), `no_think` honoured, key via the
  same 0600 file handoff (never argv/env/stdin). Same endpoint (`~/.darkbloom/local.json`) as mini, run inside the queue job. Tokens are added to the USAGE SUMMARY.
- Verified: offline (stub helper via `MINI_SINGLE_BIN`, stub mini): good splice, unparseable output, wrong fence, multi-function and NEEDS_MORE fallback, exhausted attempts, retries 0, best_of 2,
  model error, no_think passthrough, eligibility, key handling. Live: one tiny request through the real helper (python block `def f(): return 1`, no_think, 12 completion tokens, 0.5 s).
  NOT yet run live end to end (no bench run). Trial (from the research): `mini-bench --only text-truncate-ss,checkout-rounding,guard-anchor` with `fix_mode` set on the latter two; pass >=2/3
  within 3 attempts and median wall <= 3 min per task. Needs the owner's OK for GPU time.

### Mutation-score gate (`mutation_gate`, off by default)

Optional spec key. After Phase B is fully green the runner mutates ONLY the target function's lines in a throwaway clone of the work dir (the real work dir is never written) and scores how many mutants the frozen test kills: `(killed+timeout) / (killed+timeout+survived+nocoverage)`; compile errors and ignored mutants are not counted. The score lands in RESULT (`mutation=ok mutation_score=0.75`), in a `mutation` gate row in REVIEW.md, and in the console log.

```json
"mutation_gate": {"warn_below": 0.6, "fail_below": 0.4, "min_mutants": 3, "timeout": 600, "required": false, "tool": "auto"}
```

- `fail_below` turns a low score into `mutation=FAIL` (the run fails); `warn_below` only prefixes the detail with WARN. `true` means all defaults (warn at 0.6, never fail).
- Fewer than `min_mutants` valid mutants -> WARN only (a one-line function has too little to judge).
- Tool choice: `auto` = mutmut 3 for Python targets, StrykerJS otherwise. JS runs `stryker run <generated conf>` with the `command` test runner (`test_cmd`), `mutate: ["file:L1-L2"]`, JSON reporter; Python runs `mutmut run <selector>` then `mutmut results --all true`, but ONLY when the spec sets `mutation_gate.mutmut_selector` (template with `{mod}` `{fn}`, e.g. `{mod}.x_{fn}__mutmut_*`); without it the gate skips (not fails) with "mutmut selector unverified" and prints the guess (`mut_selector_default`: leading `src.` dropped, methods `xǁClassǁfn`), because mutmut 3's naming was never checked against a real install. Results are filtered to the target functions' mutants and "not checked" mutants are not counted. The gate runs with the spec `env`, `PYTHONDONTWRITEBYTECODE=1` and no `OPENAI_API_KEY`, like every other runner command. Binaries are found via `MINI_STRYKER_BIN` / `MINI_MUTMUT_BIN`, `node_modules/.bin/stryker`, `.venv/bin/mutmut`, then PATH. Never `npx`, never an install.
- **Missing tool or unreadable output = "skipped" (gate stays ok, detail says why)** unless `required: true`. So enabling the key on a box without the tool is harmless.
- `cmd` (+ `format`: `stryker|mutmut|line`) replaces the tool: it runs in the clone with `{file} {first} {last} {fn} {test_cmd} {report}` substituted and `MUT_*` in the env; `line` format reads a `MUTATION killed=K survived=S` line. Used by the self-test (fake tools for stryker, mutmut and a custom command; threshold and precheck cases) and by the e2e cases `mutation warn` / `mutation fail`.
- Multi-target specs mutate each target's span; chained steps run the gate per step.

**Status (2026-10-31): both tools are installed and the gate has run against real tools.** Install (outside any repo, shared): mutmut 3.8.1 in
`~/.mutation-tools/venv`, StrykerJS 10.1.1 in `~/.mutation-tools/node`; symlinks `~/.local/bin/mutmut` and `~/.local/bin/stryker`
(`~/.local/bin` is on the runner PATH, so no env var is needed). Remove with `rm ~/.local/bin/{mutmut,stryker}`.

Trial results (scratch fixtures, mutation_gate never enabled on a live spec):
- Python, real job `bloom-ledger-summary-build-core` (`build`, ref applied): `score=0.93 (75/81 killed, 6 survived)` in a few seconds.
- JS, node:test `add()` fixture: `score=0.91 (10/11 killed)`.

Python recipe (the "selector unverified" skip is replaced by this when `"mutmut_mode": "indir"`): mutmut 3 keys a mutant by the
file path relative to ITS cwd, and the tests import the module by bare name, so a repo-root run records `bloom_x.x_build` but expects
`ingest.bloom_x.x_build` and aborts ("tests record trampoline hits but none match"). `indir` runs mutmut in the target file's own
directory with a generated `setup.cfg` (`source_paths=<file>`, `also_copy=.`, `pytest_add_cli_args_test_selection=<test file>`), keys
`<stem>.x_<fn>__mutmut_N`, and `mutmut_pythonpath` (list of repo-relative dirs, e.g. `["lib"]`) added to PYTHONPATH. Limits: the test file
must live under the target's directory; mutmut runs pytest in its own venv, so the repo's other deps must be importable there.
Without `mutmut_mode` the old behaviour (explicit `mutmut_selector` or skip) is unchanged. `mutmut_selector`, `mutmut_mode` and
`mutmut_pythonpath` are now accepted by precheck (the first was documented but rejected as an unknown key).

Enable per spec (one line, off unless present): `"mutation_gate": {"warn_below": 0.6, "mutmut_mode": "indir", "mutmut_pythonpath": ["lib"]}`.
Keep warn-only until 5 to 10 known-good fixes have been scored; promote to `fail_below` (start 0.4) only if correct fixes score consistently above it.
Kill switch: remove the key from the spec.

### Prefix caching (trial prepared, NOT run; nothing was stopped or started)

Why: the 264-step measurement says prefill is about 75% of wall time and the current server does not reuse the KV cache across turns, so every turn re-prefills the whole history. A server that reuses an identical prompt prefix would only prefill the new suffix. Two pieces are ready, both inert until the owner acts:

1. **`mini_ctx` `stable_prefix: true`** (default off, in the overlay as a commented line). The default `shrink_obs` shrinks "everything older than the last 6" per turn, so one more old message changes every turn and kills the cached prefix at that message. `stable_prefix` shrinks in blocks of `shrink_block` (6): the shrunk set only grows when a whole block has left the keep window, and a message already sent is never rewritten, so between block steps each prompt is the previous prompt plus a suffix, byte for byte. Remaining retroactive changes: a superseded test run (the older one is shrunk once a newer exists) and the 32k ceiling emergency net. Cost: up to one extra block (about 6 outputs x ~2k tokens) stays unshrunk compared with the default. Self-test (`mini_ctx.py --self-test`) replays 40 turns and asserts the prefix breaks at most 6 times versus 25+ times for the default mode. Do not enable it on the current server: it saves nothing without a caching server and costs a few thousand tokens.
2. **`~/bin/prefix-cache-trial.py`**: replays a saved trajectory against a candidate endpoint, one `max_tokens=1` streaming request per assistant turn, and prints TTFT, prompt tokens, server-reported `cached_tokens` and a verdict (TTFT growth divided by prompt growth: under 0.5 effective, over 0.8 not). It only POSTs chat completions, refuses the Darkbloom URL (127.0.0.1:8000 and the `base_url` in `~/.darkbloom/local.json`) unless `--allow-darkbloom` (compared by resolved address and port, so case, `user@`, `[::1]`, `0.0.0.0`, `127.1`, decimal IPs, `localhost.` and mapped IPv6 all match; every loopback/wildcard address counts as the same endpoint), reads the key from `--key-file` only, and has `--dry-run` and an offline `--self-test` (local stub server). `--ctx raw|default|stable` picks how the history is rewritten first, so one trajectory answers both "does the server cache?" and "is stable_prefix worth it?".

**Exact steps for the owner** (all GPU-touching steps are his call; the 35B model and a second server will not both fit in unified memory, so the candidate replaces Darkbloom for the duration, and the queue must be idle first):
1. Pick the candidate and check it has prefix/prompt caching: read its `--help` (vllm-mlx: the prefix-cache option; mlx-lm server: prompt cache; llama.cpp server: `cache_prompt` / `--cache-reuse`). Load the SAME model or the closest quantisation so the numbers transfer.
2. Make sure no queue job is running (`ollama-queue.py status`), then stop Darkbloom the way you normally do and start the candidate on its own port (e.g. 8001). Nothing here is automated.
3. Pick a trajectory of 10+ steps: `ls -S ~/.ollama-dispatch/mini-runs/*/*.traj.json | head`.
4. Dry run, no network: `~/.ollama-dispatch/mini-venv/bin/python ~/bin/prefix-cache-trial.py --traj T --url http://127.0.0.1:8001/v1 --model NAME --ctx raw --steps 12 --dry-run`.
5. Cold vs warm and caching verdict: run once with `--repeat 2 --ctx raw --out /tmp/pc-raw.json` (the second identical send of each step must be fast if anything is cached), then `--ctx default` and `--ctx stable` (one `--out` each). `--key-file` points at a 0600 file if the candidate needs a key.
6. Decide: raw/stable verdict EFFECTIVE and stable noticeably better than default -> turn on `stable_prefix: true` in `configs/mini-local.yaml` and bench with `mini-bench` (needs OK for GPU time). Verdict NOT effective -> restore Darkbloom, nothing else to undo (no config was changed).
7. Restore Darkbloom exactly as before and confirm `curl -s 127.0.0.1:8000/v1/models`.


## Open
- First live two-phase trial: amz (`~/.ollama-dispatch/mini-runs/amz-photo-2p/`).
- Rerun of s4 and routes-s3 with the new rules (does the agent stop on a missing file?).

## Upstream documentation (read 2026-10-15; mini-swe-agent.com + shipped config.mini.yaml)
- Default config (`minisweagent/config/mini.yaml`) already supplies: system prompt, an
  `instance_template` ("Please solve this issue: {{task}}" + workflow: analyze, reproduce script,
  edit, verify, edge cases, submit with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`), the
  Darwin sed note, `step_limit: 0` (unlimited), `cost_limit: 3`, `mode: confirm`.
- Upstream's intended place for extra rules is the config, not the task text: pass
  `-c mini.yaml -c agent.instance_template=...` (giving `-c` replaces the default config, so
  mini.yaml must be named explicitly). We appended rules to the task text instead; it works but
  is not the documented route. A `mini_textbased.yaml` variant exists for models without tool calling.
- Local models: upstream uses litellm; set `model_kwargs.api_base` (we used the OPENAI_API_BASE
  env var, which litellm also reads) and `MSWEA_COST_TRACKING=ignore_errors`. Upstream says
  nothing about missing-dependency or scope rules; those are ours and untested.
- Not set in our pilot: `step_limit` (unlimited; s4's 270s may have been a loop, unchecked),
  temperature / other `model_kwargs`.

## qctl auto-retire (stale failed generations)

`qctl auto-retire [--dry-run]` retires an older bundle whose rows are ALL finished-failed (failed/blocked/needs_opus/done_unconverged, at least one `failed`) when a same-stem bundle supersedes it. Same stem = `label_stem` match (a leading `miniN-`/`miniN_` generation prefix stripped, or an identical stem); unrelated or merely substring-similar bundles are never matched. Superseded by either (a) the newest same-stem PASS finish (older than it), or (b) a different same-stem bundle with a row that is pending/running/queued and newer than every row of the old bundle (row time = done.json `persisted_at`, else `enqueued_at`). The newest bundle is never retired, so its own failure stays on the panel; a bundle with any non-failed row is left alone. Retirement goes through `qctl retire` (supersede marker + plan cancel + `ollama-queue.py resolve`). Tests: `~/bin/test-auto-retire.py`.

## Localization before repair (2026-10-16)
`~/bin/diag-localize` (dynamic-slice + Ochiai `slice`, CoSIL-style two-call `locate`, `diagnose-submit --localize`): see [diag-localize.md](diag-localize.md). Built and self-tested offline; trials awaiting the owner approval (GPU).


### Correction 2026-10-16: Darkbloom DOES prefix-cache
Direct test: identical ~19k-token prompt, 14.3s cold, 1.2s warm, `cached_tokens` 18432. Replay of a real trajectory (prefix-cache-trial.py --allow-darkbloom, 12 steps): raw verdict EFFECTIVE; `--ctx default` lost the cache at step 8 (TTFT ~1s -> ~8s, cached 4096) because per-turn shrinking rewrites the prefix; `--ctx stable` kept 147k of 177k tokens cached. Earlier 'no prefix cache' conclusions were wrong for raw/stable sends. `stable_prefix: true` is now ON in configs/mini-local.yaml. Cache is block-granular (1024-token multiples).

**Result 2026-10-16 (stable_prefix on):** bench pc1 text-truncate ~1 min, checkout-rounding ~3 min, both PASS first try. egift s1 steps 1-2 (job 426f1cdc44bd, mini10-resell-egift-pc2): RESULT PASS, ~15 min per step, ~30 min total, no retries (earlier run: step 1 ~50 min, step 2 failed a fix attempt on the step limit). Confounded: fail-fast/obs-cap/prompt/spec changes landed between the runs, so the cache's share is not isolated.

## Impact scan and `update_tests` (2026-10-18)

Why: a change that replaces behaviour (a kill switch, a removed default) leaves existing tests that assert the old behaviour; the frozen new test
goes green, the suite goes red, and the model may not edit those tests (not `allowed`). `mini-spec` now finds them up front and the runner can let
the model update them under a case-count gate.

- **mini-spec (every build; `--no-impact` skips; `--impact-ignore FILE` repeatable).** `impact_scan` looks at the tracked test files of the same language. Load edge:
  A = the test really requires/imports an allowed file (relative node specs resolved with .js/.ts/.mjs/.cjs//index.js; python `from X import`/`import X` repo-relative or
  next to the test), A' = it loads a file in `callers`, stem-only = only a file-name match. Content: S = a target fn or an export the task names, L = a derived literal
  (task words that appear as a quoted string in the allowed file, `CONST_NAMES`/env vars the task names, backtick literals present at baseline). STRONG = A and (S or L);
  MEDIUM = A' and (S or L), or stem-only and L; WEAK = L with no load edge (IMPACT.md only). Tests that already set the task's env var are noted. More than 6
  STRONG/MEDIUM tests fails the build (split the task or `--impact-ignore`).
- **Outputs.** `IMPACT.md` (literals with reasons, a table per tier, up to 4 hit lines each); `TASK.md` gets `## Tests that assert current behaviour` before `## Scope`, one
  `TODO(mini-spec)` bullet per STRONG/MEDIUM test, so `--lint` blocks until each is decided (delete the bullet after adding the test to the spec's `update_tests`, setting
  env/fixture, or rewriting it as an explanation without the marker); `suite_cmd` is widened with every STRONG/MEDIUM test (MAX_SUITE cap kept; an `npm test` / whole-pytest suite is left
  alone and the evidence line says so). `mini-spec` never fills `update_tests` itself.
- **`mini-spec --impact RUN_DIR`** rescans an existing run dir (spec.json + the "Confirmed defect" paragraph), rewrites IMPACT.md, never edits TASK.md; exit 1 while a STRONG test is
  in neither `update_tests` nor a marker-free TASK.md bullet.
- **Runner spec key `update_tests`** (list, top level or per step): existing tracked test files the model may edit in Phase B. They widen the scope, untracked and chain-guard checks
  (`edit_scope`) but are NOT `allowed` (no coverage, literal or single-shot handling). Precheck errors: missing, untracked, not a test file, the frozen `test_file`, also in `allowed`, or
  its file name absent from `suite_cmd`. Gate `update_tests`: each file keeps at least its baseline number of test cases (`test(`/`it(`/`def test_`; `.skip(` does not count), baseline snapshotted
  before the model runs. The Phase B prompt gains one line naming the files ("Keep every test case; change only expectations the task changes"). `fix_mode: single_shot` cannot edit tests, so
  a spec with `update_tests` runs the mini loop (the runner logs why). The gate does not prove the updated expectations are right (Opus reviews the diff) and cannot see a case whose body was emptied.
- Limits: unquoted lowercase identifiers are covered only through S; dynamic requires land WEAK; the widened suite runs longer; the case-count gate is partial cover.

## Graphify integration: impact, locate, fn_scope (2026-10-19)

`~/bin/mini_graph.py` (read-only, plain Python over `graphify-out/graph.json`, loaded by path from mini-spec and the runner; `python3 ~/bin/mini_graph.py --self-test`). It never runs `graphify update` and writes nothing. Worktrees have no graphify-out, so the graph is the MAIN checkout's (`git rev-parse --path-format=absolute --git-common-dir`, parent dir); spec key `graph` / `mini-spec --graph PATH` overrides. No `--graph-refresh` (not trivial; rebuild the graph yourself).

- **Freshness.** A file is graph-trusted only when it is in `manifest.json`, `built_at_commit` exists in the worktree and `git diff --quiet <built_at_commit> -- file` is clean (never mtimes). The runner gate holds the baseline text instead, so it compares `git show <commit>:file` with it (`fresh_text`). Every output prints `graph: fresh@<sha7>`, `graph: stale [files]` or `graph: absent`; stale and absent fall back to today's text rules.
- **Impact (`mini-spec`, `--impact`).** `callers_of` = stem-match result UNIONED with graph in-edges (calls/indirect_call/imports/imports_from onto the target_fn nodes or the allowed file node). In `impact_scan` a test node importing an allowed file/symbol is edge A (`graph: imports_from <file>`), importing a graph caller is A'; anything else falls through to resolve_loads and the stem rules. S/L literal hits, tiers, IMPACT_MAX and TASK.md bullets are unchanged. IMPACT.md and `--impact` print the graph line.
- **Locate (`mini-spec WT --locate RE`).** Grep pass unchanged; with a graph a `== GRAPH ==` block follows: SOURCE hits mapped to their enclosing graph node, ranked by patterns covered, then hits, then fewest callees, then path (`PROPOSED: --allowed F --fn X`), then callers, callees, test files importing F, community peers ("related, not targets"). `--locate-from ENTRY` prints the shortest calls path ENTRY -> PROPOSED. Without a graph the output is exactly the old one.
- **Scope guard (runner gate `fn_scope`, between diff_reach and the revert-to-baseline run).** `fn_scope_check` classifies every changed line (difflib against the baseline text, plus pure deletions mapped through the baseline spans) as target (inside a target_fn span, nested included), helper (new function the target calls by name, transitively), import (module-level import change that only ADDS names the target/helpers use; dropping/renaming a name or changing a module path is outside), neighbor (1-hop calls neighbour from the graph, else text regex) or outside. It fails on any outside line or on neighbor lines above `scope_slack` (default 4); the detail names the functions/lines to revert, so the Phase-B retry prompt carries it. `fn_scope`: `"warn"` (default: always True, detail starts `WARN`), `"enforce"` (a False gate fails the run), `"off"`. Advisory churn note when over half of the target span changed (a rename like `e` -> `err` inside the target cannot be told from a real edit). `python3 ~/bin/mini-swe-runner.py --self-test-scope` runs only the fixtures and, if still present, replays the gate on the old lock-error-fails-cmd work dir.
- **Known limits.** Same-name functions in one file share one node id; JS spans depend on the runner's `_js_end`; the graph has one edge per caller/callee pair (no call sites) and only start lines; graph.json mtime can differ from the rebuild time (freshness uses the commit, not mtime); multi-line `const {..} = require()` imports are recognised by fn_scope's own regex, other exotic import forms are treated as outside.

## Live graph lookup and pre-submit freshness (2026-10-31)

- **`~/bin/mini-graph`** (wrapper over `mini_graph.cli`; read-only, never runs graphify): `callers FILE FUNC`, `callees FILE FUNC`, `where NAME` (label match, `file:line`), `neighbors FILE` (importers + community peers). Output is capped at 40 lines with a "more lines cut" note. A file that is not `mini_graph.fresh()` prints `STALE: graph not trusted for <file>, use grep` (a `where` hit in a stale file is marked `[STALE: verify with grep]`). No graph: `no graph for this repo: use grep`. Graph location = `locate_graph` from the cwd's git common dir; override with `--graph PATH`, env `MINI_GRAPH_JSON`, or `MINI_GRAPH` when it ends in `.json` (otherwise `MINI_GRAPH` stays the runner's mini_graph.py module override).
- **Runner prompt.** `graph_lookup_note()` adds 3 lines to the fix prompt, the verify-mode prompt and the diagnose prompt telling the model it may run `~/bin/mini-graph --graph <abs graph.json> ...` through the bash tool (absolute paths, so PATH and the clone's missing graphify-out do not matter). Only when a graph exists for the repo (spec `graph` key honoured); `MINI_GRAPH_LOOKUP=off` disables. The runner has no command allowlist, so nothing else was needed.
- **Pre-submit check.** `mini-submit` calls `graph_warning()` after its checks and before enqueue (also on `--dry-run`): one `WARNING: graph stale for N of M target file(s) (...); refresh: cd <repo> && graphify update .` line, or `WARNING: no graphify graph ...`. Warn only: never blocks, never runs graphify. `MINI_GRAPH_WARN=off` silences. The pure logic is `mini_graph.submit_warning(G, wt, files)`. `mini-spec` does not warn (its own output already prints `graph: stale [...]`).
- Tests: `python3 ~/bin/test-mini-graph-lookup.py`, `python3 ~/bin/mini_graph.py --self-test`, runner `--self-test` (asserts the note), `mini-bench --dry-run`.

## Diagnose mode (2026-10-19)

Read-only root-cause reports. Motivation: job e32e17104bc2 wrote a 14KB DIAGNOSIS.md that `test -s` accepted but cited the wrong lines and missed the real mechanism. Diagnose mode replaces `test -s` with `~/bin/diag_check.py`, a deterministic gate that checks every quote against the repo.

- **Spec fields.** `mode: "diagnose"`; `allowed: []` (required, main() reads it before the mode branch); `task` (absolute path copied in as TASK.md, or a TASK.md in the baseline); `baseline_from`; `locate` (list of regexes, run as `mini-spec WT --locate P...` and pasted under `## Starting points`, capped at 60 lines, labelled leads not findings; skipped silently on any failure; graph block included when a graph exists); `must_mention` (literals that must appear in the report); `diag_attempts` (default 3); optional `diag_steps` 80 (was 40 until 2026-10-22), `diag_wall` 900, `timeout` 1800, `env`. `--check-only` prepares the scratch dir and prints the prompt, no model.
- **Flow (`diagnose()` in mini-swe-runner.py).** prepare, prompt = workdir banner + READ_HINT + report contract + Starting points + TASK.md (the model writes with one `cat > DIAGNOSIS.md <<'EOF'`), run_mini, then: tracked file changed -> restored, then `diag_check.check` judges the report against the real code (2026-11-03: an edit is a rejected attempt, not the end of the job; the retry note names the restored file, and a report that quotes the edited code fails its citations; before this, two of four auto bench jobs died on a model that tried its fix while diagnosing). The contract also tells the model to try fix ideas in a copy (`cp -r . /tmp/tryfix`), never here. On rejection a FRESH mini run gets "DIAGNOSIS.md was REJECTED: <fails>. Edit it; do not rewrite from scratch." and the file is kept. Every loop turn is an attempt, including a run that exits without Submitted and wrote nothing (an empty turn leaves a "no usable DIAGNOSIS.md" note). Every prompt carries a STEP BUDGET paragraph (`diag_budget`: write a complete draft by step min(20, steps/2), rewrite it as you learn, submit by steps-5), and the Starting points block is kept on retries. When an attempt wrote no report, `diag_carry` adds the previous trajectory's line ranges read, searches and the model's own notes so the retry does not restart the exploration (added 2026-10-22 after cc6618122c9b: 3 x 40 steps spent reading by line range, no report ever written; ~9 s/step, so 80 steps fit the 900 s wall). The gate's bare-mention file types now include swift, kt, c/h/m/mm, cpp, cs, php, sql, prisma, vue, svelte, scss, lua, zsh, bash. The runner never exits 3 on stream timeout; the retry loop is the recovery. Stray untracked files are deleted between attempts. Writes REVIEW.md (attempts, remaining failures, the report, the final message) and prints `RESULT PASS|FAIL mode=diagnose ...`.
- **diag_check.py gates.** (1) file exists, >= 800 bytes, H2 headings Code path / Root cause / Second cause / Minimal fix / Regression test / Not verified, not starting with "Let me" / "Now I"; (2) Code path has >= 3 fences whose info string is `<lang> <path>:<N>[-<M>]`, every non-blank line of each fence (whitespace-collapsed) appears in `path` within lines N-5..M+5, bare `path:N` prose mentions need an existing file with N <= its line count (a shortened path such as `client.js:179` resolves when exactly one tracked file ends with it; every miss is listed, and a missing path's failure names the tracked files with that file name so the retry can correct it, added 2026-10-22 after diag-clamshell-web 542f8970edcd failed on `client.js:179` once the model dropped a guessed `sideclient/` prefix copied from the contract's old `sidecar/src/lib.js` example; the example is now `<path>` with a `git ls-files` rule); Root cause is one sentence <= 400 chars, not counting parenthesized line references; (3) Minimal fix has exactly one `File:` and one `Function:` line, the file exists and `_defines` (copied from the runner) finds the function; (4) `must_mention`; (5) `git diff --name-only HEAD` empty and untracked files only DIAGNOSIS.md / TASK.md / ANSWER.md (the runner also tolerates files already untracked in the baseline copy via spec `untracked_ok`). CLI: `diag_check.py --repo . DIAGNOSIS.md [--spec JSON|FILE]`; scorer: `diag_check.py --score FILE --expect JSON`. Tests: `python3 ~/bin/test_diag_check.py`, `python3 ~/bin/test_mini_diagnose.py` (stub MINI_BIN, no GPU).
- **What the gate does not prove.** Quotes are real and in the cited place, but not that the explanation is right; Opus still reads REVIEW.md. The acceptance scorer exists for that: it checks that known facts are cited and mentioned.
- **Acceptance procedure (written, NOT run).** Files in `~/.ollama-dispatch/diag-accept/` (`spec.json`, `TASK.md` = symptom section only, `expect.json`, `steps.md`). Clean clone `git clone --no-local <resell-tracker> ~/dispatch-worktrees/diag-accept-825d846`, `git checkout --detach 825d846`, assert HEAD 825d846 and `sidecar/test-acquire-browser-lock.js` absent. Spec: mode diagnose, allowed [], locate `browser\.lock`, `EEXIST`, `BrowserBusyError`, must_mention `acquireBrowserLock`, `writeLock`, `handleCommand`. Run through the queue, then `--score` against Fact A (cites lib.js ~555-568, mentions stale + break/unlink + wx/EEXIST) and Fact B (cites poll.js ~221, mentions the rethrow and `running` left behind). Pass = gate OK and both facts HIT.
- **Worker-path caveat.** When a diagnose job is resumed through the worker path, `_stop_gate_fails` (the stop-gate cap) resets on each resume, so it does not bound total retries; `diag_attempts` and `timeout` are the only caps inside one runner invocation.

## needs_info: ask the owner, park, resume the same job (2026-10-31)

A fix-phase model that is missing a fact it cannot find in the repo may write `NEEDS.md` instead of guessing. The job parks without penalty, the owner answers with one command, and the SAME queue job resumes with the answer in its TASK.md.

- **Kill switch `MINI_NEEDS_INFO=off|shadow|on`, default OFF** (off is byte-for-byte the old behaviour: no prompt rule, NEEDS.md ignored). Env wins when set; otherwise the flag file `~/.config/mini-swe/needs_info` (override path `MINI_NEEDS_INFO_FILE`) is read on every call, so the switch flips without a daemon restart. Any other value means off. `shadow` shows the rule to the model and judges its NEEDS.md but never parks: each verdict is appended to `~/.ollama-queue/logs/needs-info-shadow.jsonl` (override `MINI_NEEDS_INFO_SHADOW_LOG`). Cap on asks per job: `MINI_NEEDS_INFO_MAX` (default 1). Chain steps (`_guard` in the spec) and non-job-local specs never get the rule.
- **NEEDS.md format** (headings required): Question (non-empty, <= 600 chars), Tried (must quote a search/read command that appears in the run log; >= 2 search/read commands before asking), plus the branches section. Rejected asks: a Question naming a repo file or identifier the run never read, a Question about changing the test, or the ask cap. A rejected ask is a normal failed attempt and the reason reaches the next attempt as `needs-info question rejected: why`.
- **Park.** The runner prints `RESULT PAUSED needs-info`, writes `<cwd>.needs-info.json` and exits 3 with a `RESUMABLE TRANSCRIPT` line, so the queue sets `status=paused, pause_reason=needs_info`. No done.json, no streak count, no scorecard row. Two queue edits: the stranded-pause watchdog only SURFACES a needs_info row (with the `mini-answer` hint) and never auto-resumes it, and a paused needs_info row does not hold a bundle lane or count as an incomplete bundle member.
- **Answer.** `mini-answer <job-id-or-prefix> "text"` (`--show` prints question/Tried/branches, `--decline WHY` records "Not available", `--cancel` cancels). It appends `### Q<n>: question` plus the answer and a `<!-- test-sha -->` comment to `<spec task>.answers.md` and resumes the row to pending; if the resume fails it rolls the answer back (exit 2). The rerun starts from scratch like every runner job, prepare() appends the sidecar under `## Answers to your earlier questions`, and the test sha must be unchanged (`FAIL phase=fix test-changed-while-parked` otherwise). The ask count is read from the sidecar because in-process counters reset on relaunch.
- **Surfaces.** handoff-emit shows a `NEEDS ANSWER` line (question + command) on the job page and INDEX. dispatch-escalation-watcher source G raises a no-review notice (INDEX row + desktop ping) once per job and ask number; it is never reviewed, self-healed or answered automatically. `mini-scorecard report` prints ask rate, answered-then-PASS rate and shadow accept/reject counts; done.json carries `needs_info_asks`, and a job cancelled while parked is excluded rather than scored as a failure. Diagnose mode gets the same rule (`DIAG_NEEDS_RULE`) and parks the same way.
- **Rollout.** (1) Land with the switch off. (2) `shadow` for a batch of real jobs; go criteria for `on`: shadow accepts are real questions on inspection and the reject reasons match what a human would reject. (3) Before `on`, run the queue test `test_mini_needsinfo_queue.py` against the DAEMON THAT IS RUNNING: the daemon keeps old queue code until restarted, so the two queue edits (surface-only watchdog, lane release) only take effect after a restart; flipping the switch to `on` before that would let the old watchdog auto-resume a parked row after ~20 min. This ordering (restart first, then `on`) is the main risk. (4) The dashboard answer button would need `ollama-queue-api.py`, which was out of scope; ask that file's owner. Env reaching the runner subprocess was not verified, which is why the flag file exists.
- **Tests (no GPU):** `test_mini_needsinfo_runner.py` (R1-R10, park -> mini-answer -> rerun PASS loop, D1), `test_mini_needsinfo_queue.py`, `test_mini_answer.py`, `test_handoff_needsinfo.py`, `test_watcher_needsinfo.py`, `test_mini_needsinfo_scorecard.py`.

## Test drafting (L1, opt-in, 2026-11-02)

The local model drafts the frozen test for a ref fix Claude already wrote and validated. Claude reads the draft instead of writing it.
Calibration and trial results are in `offload-rollout.md` ("Status 2026-11-02"). The verdict there: use it as an assist, and Claude
still reads every ACCEPTed draft.

- **Make the job.** `mini-testdraft emit SPEC --out DIR` writes:
  - DIR/spec.json (`phase_a_only`, `test_attempts` 3);
  - TASK.md (the brief, with the ref diff);
  - base/ (the baseline without the test).
- **Submit.** `MINI_TESTDRAFT_MODE=on mini-submit DIR/spec.json --label <area>-test-draft --model qwen3.8:27b-q4_K_M --bundle <b>`.
  - Without the env var, mini-submit refuses (C3).
  - Kill switch: `MINI_TESTDRAFT=off`.
  - Money, auth, migration and gate-logic paths are refused.
- **What the runner does.** It runs Phase A only: the model writes the test, then the runner runs `mini-testdraft check` on it.
  - The check is V1-V7 (V6 allows a draft up to 10 tests; fix jobs keep 5) plus a line-scoped mutation score of at least 0.70.
  - `RESULT PASS mode=test-draft` means ACCEPT.
  - A REJECT becomes the next attempt's prompt. It lists the failing checks, the surviving mutants ("assertions too weak", one line per
    survivor) and, when the draft fails against the ref, the draft's own test output.
- **Mutation score (`mini_mutate.py`, Python).**
  - Only the lines the ref changed are mutated: compare flip and boundary, and/or swap, drop a `not`, arithmetic swap, constant change,
    `return X` to `return None`, negate a condition, and a statement to `pass`.
  - Each mutant runs the spec's own test_cmd in a throwaway clone.
  - Before scoring, the unmutated ref must pass and a canary (the file replaced by a raise) must fail. Otherwise the test is
    unscored, never REJECTed for it.
  - Every run is contained: throwaway HOME, DISPATCH_VERIFY_SANDBOX=1 and a sandbox-exec profile.
  - The mutmut indir mode is not used here: it rejected Claude's own validated tests for tooling reasons.
- **Containment of every drafted-test run (2026-11-02, Fable review #5).**
  - The runner's test gate in a `phase_a_only` job and every run in `mini-testdraft check` run contained, the same way as the
    mutants: throwaway HOME, `DISPATCH_VERIFY_SANDBOX=1`, and the sandbox-exec profile. That covers the runner's own gate, and
    mini-validate's V3/V4 (runner `--check-only`), V5 and V7.
  - The profile allows writes only to the temp dirs and the job's work dir. It denies connects to ports that were already
    listening, to the queue, BloomGauge and ollama ports, and to web/ssh ports. It also denies signals to outside processes.
  - `~` and `$HOME` in the test_cmd still mean the real home.
  - Switch: `mini-testdraft check` sets `MINI_CONTAIN=1` for mini-validate, and the runner inherits it; a `phase_a_only`
    spec turns it on by itself. Where sandbox-exec is missing, only the HOME/env part runs (`strict=False`).
  - VALIDATION.md says `- contained:` when it applied. Replaying all 11 L1 trial drafts gave the same verdicts and V lines.
  - Not contained: the agent's own shell while it drafts (mini-swe-agent's LocalEnvironment). A draft the agent runs itself
    still sees the real HOME. Fix-job gates (Claude's frozen tests) also run uncontained, as before.
  - Queue guard on every run (2026-11-02): the agent's shell (`run_mini`), every runner test run (`run_cmd`) and every
    mini-validate run set `DISPATCH_VERIFY_SANDBOX=1`, after the spec env so a spec cannot clear it. ollama-queue.py then
    refuses every mutating subcommand, so code under test can read the queue but never enqueue or change a real job. This
    is the old pipeline's pgrun guard; the mini runners had lost it. Tested in `test-verify-sandbox-guard.py` (section I).
  - The drafting brief tells the model to find files from the test file's directory and to keep state in a temp dir.
- **Test gate rules that matter for drafts.**
  - Mocking the code under test fails the draft: replacing the target in `sys.modules` (`setdefault`, `__setitem__`, `update`,
    `mock.patch.dict(sys.modules, ...)`) or overwriting the target module's attributes.
  - `del sys.modules[target]` (a fresh import of the real module) is allowed, and so is faking the target's own imports and settings.
  - A run where zero tests ran fails.
  - A draft that only reads the code's text and asserts on it (`'retry' in src`, an AST walk for a call) fails: it proves
    the words are there, not that the code works. Absence checks for removals are allowed.
- **After ACCEPT.** Nothing is landed. Read `<run dir>/draft_<test>`, copy it into the baseline and land it with the ref.
- **After REJECT.** handoff-autoclear clears it; no research follow-up is queued.
- **Notice.** The result arrives as a queue event in the session that launched the job (hook, or `queue-events-deliver.py --watch`).

## Finishing a failed job in place: mini-finish (2026-11-02)

When a local job fails on its own work (failure class model, timeout or context), Claude finishes the local model's attempt
in place instead of starting over. The session that launched the job does it, on the subscription: no subagent, no API, no
`claude -p`. A spec, infra, operator or harness failure is not unfinished work and still goes to `~/bin/mini-triage <id>`.

- **The notice.** The failed queue event carries `finish=`~/bin/mini-finish <id>``, and the hook delivers it to the session
  that launched the job. The UNRESOLVED line a later session sees keeps it.
  - It is set only for a mini-swe-runner job of class model, timeout or context whose work dir still exists and whose spec
    is a chain, two-phase or diagnose spec. It is not set for a failed replay, or for a job that is superseded or already
    has a continuation.
- **Claim: `~/bin/mini-finish <id>`.**
  - Writes `<run dir>/finish/FINISH.json` and snapshots the local attempt in `finish/local/`.
  - Puts the frozen test back if phase B edited it, from the baseline or from the REVIEW packet.
  - The frozen test's sha comes from the runner's log. With no log kept, it comes from a REVIEW packet that reached phase B
    (phase A froze that test, which can differ from the baseline's), then from a test the baseline supplies; the brief says
    which. With neither, the brief warns that nothing holds the test fixed. A packet that shows a phase-A rejection means
    nothing was frozen.
  - If the spec has a `mutation_gate`, the brief says the check runs it too.
  - Prints the brief: the work dir, what failed (REVIEW.md gates, DRAFT-CHECK.txt or the RESULT line), each file's change
    against the step's starting point, the frozen test's sha and the must-contain literals.
  - Running it again prints the brief again.
- **Finish.** Edit the listed files in the work dir, in this session.
- **Check: `~/bin/mini-finish <id> --check`.** Run it in the background; it takes minutes. It runs the job's own gates with
  the real runner (`--check-only`) and makes no model call.
  - **single:** `--try-test` and `--try-fix`, then every phase-B gate. If only soft gates fail, the result is
    `PASS needs-review`.
  - **chain, failed at step k:** the saved step-refs replay steps 1..k-1, then step k is taken from the work dir and gated.
    A file in step k's scope that the finish deleted is recorded as deleted, and the replay deletes it too.
  - **single and chain:** the spec's `mutation_gate` runs on the finished code once the other gates are green, as phase B
    runs it on a model's fix (`--mutation`). In a chain it runs on step k only. A score below `fail_below` fails the check.
  - **test draft:** `--try-test --draft-check` runs the job's own `draft_accept_cmd`.
  - **diagnose:** `--try-diagnosis` runs diag_check. Any edit to a tracked source file fails the check.
  - A changed frozen test fails before the runner starts.
  - FAIL lists the red gates and the runner's RESULT line, and keeps the claim. Fix the files and run the check again.
  - `--dry-run` runs the gates only: nothing is queued, resolved or cleared.
- **PASS.**
  - Writes `finish/FINISHED.json` and a line in `~/.ollama-dispatch/mini-finish.jsonl`. Both record the local share: how
    much of the final added code was already in the local attempt.
  - **A chain with steps left:**
    - Writes `<spec dir>-c<j>/spec.json` with a `replay` key, so steps 1..k replay through the gates with no model call.
    - Runs mini-validate, then mini-submit with `--continues <id>`, the same bundle and model, and the label `<label>-c<j>`.
    - The local model then does steps k+1..n.
    - If validate or submit fails, the exit code is 2 and nothing is resolved. The check stays on disk, and running
      `--check` again retries. The same holds when the continuation dir cannot be created.
    - If mini-submit exits 0 but prints no usable `job_id`, the queue row that `--continues` the job gives the id. With no
      such row, the continuation counts as queued with its id unknown: its dir is kept and FINISHED.json says so.
    - The spec needs a validated ref dir, which mini-validate needs. Without one the check still passes, but nothing is
      queued or resolved (exit code 2): write the ref (`ref/s<N>/...`) and run `--check` again.
  - **Resolve and clear:** then resolves the failed job and clears its card (`POST /api/runs/<id>/clear`, never `override`).
    - A 409 from the clear (a sign-off is pending, or a run is live) is reported with exit code 2.
    - Running `--check` again retries only the resolve and the clear. This works even if the spec or baseline is gone by
      then, or FINISHED.json cannot be read.
  - **Landing:** prints where the finished files are (for a test draft, `finish/draft_<test>`; for a diagnosis, nothing to
    land) and the `~/bin/mini-scorecard ledger add` line, with the local share in its note.
- **Refused** (exit code 1):
  - a job that is not a mini-swe-runner job, is not failed, or failed with another class;
  - a failed replay, which is the continuation spec's problem;
  - a job that is superseded or already has a continuation, or whose work dir is gone;
  - a verify-mode spec;
  - a chain that failed at step k>1 in a run that predates per-step refs;
  - a job another live session launched or claimed (`--take` takes it over);
  - `--check` before the claim;
  - a claim, `--take`, check or FINISHED retry while a check of the same job is running. One lock covers all four, so a
    `--take` never rewrites the claim under a running check;
  - a job a check finished between mini-finish's first look and taking the lock (it re-reads `FINISHED.json` under the
    lock).
- **List:** `~/bin/mini-finish --list` shows the failed jobs it can take.
- **Runner support (same date).**
  - A chain saves each green step's files to `<run dir>/step-refs/s<N>`.
  - The spec's `replay` key replays saved steps through the gates. A red replay fails as `bad-spec replay ...`.
  - `--draft-check` and `--try-diagnosis` give the check its single-step gates.
  - `--mutation` (with `--check-only`) runs the spec's `mutation_gate` on a green `--try-fix` and on the last replayed step.
  - `--try-fix` creates the parent directories of a new file.
  - A real (not `--check-only`) run refuses a work dir whose run dir holds `finish/FINISH.json`. It prints
    `RESULT FAIL bad-cwd finish-claimed: ...`, which the queue files as class infra, and it does this before it reads the
    endpoint config or prepares the work dir. A re-run of a claimed job therefore cannot wipe the finish. Move the `finish/`
    dir aside only if that work is no longer wanted.
- **Tests:**
  - `test_mini_finish.py` (F1-F9): refusals, all four modes against the real runner, the continuation, a 409 from the
    clear, `--list` and the event hint. F9 covers the review fixes: the mutation gate in the check, the runner's
    finish-claimed guard, the lock (with the FINISHED retry and the re-read under the lock), the frozen-test inference, an
    unusable job id, a retry with the spec gone, a new file in a new directory, a chain with no ref dir, and a deletion in
    a chain.
  - The runner's `--self-test` (`_self_test_replay`).
  - `test_queue_event_log.py` (`finish=` on failed events).

## Auto mode: a description in, a reviewed test + fix out (mini-auto, 2026-11-03)

Claude writes only the symptom, plus a file or function lead when it has one. The local model diagnoses, writes the failing
test and fixes the code, all in one queue job. Claude reviews every pass. Code: `mode: "auto"` in mini-swe-runner.py
(`auto()`), `~/bin/mini-auto`, `~/bin/diag_check.py`.

- **Stages.** One queue job, one RESULT line (`mode=auto phase=...`), one packet (`<run>/REVIEW-AUTO.md`).
  1. **Diagnose.** The diagnose mode above runs on TASK-D.md: the description plus the leads, labelled "leads, not findings".
     - The report's `## Minimal fix` names one `File:` line and 1-3 `Function:` lines.
     - Several lines are for one fault copied in parallel functions of one file, e.g. a radarr and a sonarr twin
       (`diag_check.MAX_FNS` = 3).
     - `Function: a (and b)` adds b when the file defines it. Any other text after the name is a comment.
     - An accepted diagnosis is kept as `<run>/DIAGNOSIS.md` plus `auto-state.json`. A queue relaunch of the same run, with
       the same baseline commit, description and hints, reuses it instead of spending another ~20 GPU minutes.
  2. **Derive.** mini-spec's build() makes a two-phase spec for that file and those functions (`<run>/spec.auto.json`,
     `<run>/derive/`). It fails when:
     - the file is not tracked;
     - a function is not defined there;
     - there are more than 3 functions;
     - mini-spec refuses (intake).
  3. **Test, then fix (two-phase).**
     - **Phase A:** the model writes the test from the Problem and the diagnosis's `## Regression test`. The test must
       fail on the baseline and reach every target function. Then it is frozen (sha).
     - **Phase B:** a fresh context writes the fix. Gates:
       - the frozen test is green;
       - the suite passes;
       - typecheck;
       - fn_scope over all targets (soft: a change outside them gives `PASS needs-review`);
       - an advisory mutation score.
- **Budgets.**
  - `AUTO_TIMEOUT`: 5400 s for the whole job.
  - `AUTO_DIAG_TIMEOUT`: 1500 s for the diagnosis, 3 attempts.
  - The two-phase stage gets what is left.
  - `AUTO_PASS_KEYS` in the spec are passed through to it.
- **Commands** (`mini-auto --help` has the flags):
  - **`mini-auto new --repo R --name N [--file F]... [--fn X] [--locate RE]... (DESC | --desc-file P) --submit`**
    - Makes a fresh worktree and a run dir with TASK.md and spec.json, then runs mini-submit with all its checks.
    - `--diagnose skip` needs one `--file` and `--fn`, and goes straight to the test and fix.
  - **`mini-auto review JOB ok|reject [--note TXT] [--into DIR]`**
    - The one-step review of a passed job. The queue event prints the packet inline (cut at `## Files`, at most 24k
      chars), so the review costs one call.
    - Read the test against the Problem first, then the diff.
    - ok records the verdict (`mini-scorecard review add --kind auto`) and lands the job.
    - reject needs `--note` and lands nothing.
  - **`mini-auto land JOB [--into DIR]`**
    - Copies the new test and the fixed file into the checkout, or into DIR.
    - It copies each one only while the checkout's copy is still the one the job started from.
    - Nothing is committed.
  - **`mini-auto show JOB`**
- **Who writes the oracle.** Here the model writes both the test and the fix. docs/offload-increase-plan.md section 4 rules
  that out for the other lanes ("Never let one model author both the oracle and the answer"). In its place:
  - Claude reviews every auto pass, the test first and against the Problem;
  - the test is frozen before a fresh context writes the fix;
  - the gates;
  - the must-not-offload refusals;
  - the kill switch.

  Keep auto jobs out of section-4 areas even when the regexes miss one.
- **Must-not-offload refusals.** These run before any model call:
  - `AUTO_FORBID` on repo, path and function names;
  - `AUTO_FORBID_TEXT` on the description (money, auth, secrets, migrations, the offload gates);
  - mini-submit C3 runs the same check;
  - `MINI_AUTO=off` makes mini-submit refuse auto specs.
- **When it fails.**
  - **In the fix stage:** `~/bin/mini-finish JOB` finishes it in place, as `single` against spec.auto.json. Check the
    model's test against the Problem first. Land the result with `mini-auto land JOB`.
  - **Before the fix stage (diagnose or derive):** mini-finish refuses.
    - A diagnose failure: resubmit once with the same hints, or fix it directly.
    - A derive failure after an accepted diagnosis: fix it directly, using the diagnosis. REVIEW-AUTO.md holds the root
      cause, the function and the regression test to write.
    - If you can name the file and function, `mini-auto new ... --file F --fn X --diagnose skip --submit` hands it back.
- **Not supported.**
  - The target must be one Python file or one JS/TS file.
  - A function in an HTML page's inline `<script>` fails at derive (`allowed files [...] must all be python or all JS/TS`).
    Fix those directly with the diagnosis.
  - Supporting them would take two things:
    - Reach. V8 coverage records `vm.runInContext` scripts as `evalmachine.<anonymous>`, with function names, counts and
      offsets, and the top-level range ends at the script's length. So a reach mapping back to the page's lines is
      possible.
    - A test in one language driving code in another. mini-spec's skeleton assumes one language, and the runner's
      probes compare the test's language with the target's.

### Gate hardening from the auto bench (2026-11-03)

Each item came from a bench job that failed, or that passed when it should not have. The bench job is named in brackets.

- **Stage templates.** Before this fix, diagnose and phase A ran under mini-local.yaml's fix workflow, so the model fixed
  code while diagnosing and while writing the test [bench-notset].
  - `configs/mini-diagnose.yaml` and `configs/mini-testwrite.yaml` override only `agent.instance_template`.
  - They are merged after mini-local.yaml.
- **`Constant:` lines.** `## Minimal fix` may name up to 3 module-level values. Derive puts them in the fix spec's
  `new_names`, so fn_scope counts their statements, for JS/TS too. A `Function:` line that names a value fails with a hint to
  use `Constant:` [bench-cfgkeys].
- **Diagnose loop.**
  - A repeat-command watch stops a looping attempt, and the next attempt is told why.
  - The reject note lists non-citation problems first and drops repeated lines.
  - An invented quote is shown next to the real lines of that range.
  - Form-only failures (Root cause too long, more than one sentence, narration) get one short repair attempt [bench-ranges].
  - The repair attempt investigates nothing [bench-dupes3]. Its prompt is the working-directory line, the rejected Root
    cause and a one-command python recipe that replaces that section and drops narration before the first heading. It
    gets no contract, read hint or TASK.md, and runs under `configs/mini-diagnose-polish.yaml` instead of the diagnose
    workflow ("find the relevant files"). A repair that changes any section other than Root cause fails the run.
  - Parenthesized line references in the Root cause (`(line 2586)`, `(lines 10-12 / line 40)`, `(app.py:12)`) do not
    count toward its 400 chars (`diag_check.root_len`). The motivating report was correct at 404 chars, 72 of them
    line references [bench-dupes3].
  - Since stage 18 (2026-11-06) the gate allows 600 chars (`ROOT_GATE`) while the contract and the form repair still ask
    for 400 (`ROOT_MAX`). The real-issue bench rejected 9 reports at 401-511 chars, and each one cost a full re-diagnosis.
- **Unclosed citation fences.**
  - `diag_check.close_fences` runs before `fix_ranges` on every attempt. It closes a citation fence that is still open at the
    next citation, at a known `##` heading, or at end of file. It only does so when the fence's first lines are exactly the
    cited range (after whitespace collapse).
  - The gate names any other fence that swallows `##` headings: "the fence opened at report line N is never closed".
  - The motivating report [bench-dupes2] was correct, but a script wrote its quotes without the closing fence, so it failed
    with 65 citation errors.
- **Repeated `File:` lines.** One `File:` line per `Function:` line, all naming the same file, counts as one file. Before
  this fix it was rejected 3 times as "2 files" [bench-dupes2].
- **Grounding (phase A).** `ungrounded_expectations` reads the test's assertions:
  - Python: the `==`/`in`/`is` operands and assertEqual-style calls.
  - JS: the expected argument of assert.equal/strictEqual/deepStrictEqual and of `expect(...).toBe/...`.

  It flags expected string tokens that appear in none of these: the requirement (TASK.md without its `## Diagnosis`), a
  tracked file, or the test's own inputs. Prose words, hashes, assertion messages, `.not` and `!=` are ignored.
  - **hard** (attempts before the last): the test goes back with "found in neither TASK.md's Problem ...".
  - **soft** (the last attempt): the test is accepted. REVIEW.md and REVIEW-AUTO.md list the values, so the review checks each
    one against the Problem.
  - **Off switches:** spec `ground: "off"` or `MINI_GROUND=off`.
  - **Why** [bench-notset]: the diagnosis's `"metered_days" (or similar)` became an expected value that the Problem never asked
    for. The test and the fix agreed and every gate passed. Only the review caught it.
- **Impact limit (derive).** In the auto lane, up to `AUTO_IMPACT_MAX` = 24 existing tests may load the target. Above that,
  mini-spec still raises Ambiguous.
  - **Above IMPACT_MAX = 6:** the build only succeeds when the suite gate runs every one of them. Either they are listed (at
    most MAX_SUITE = 12 files), or the whole-run suite covers them (pytest, `npm test`).
  - **Otherwise:** the build is Ambiguous.
  - **Why** [bench-cfgkeys2]: 9 config tests stopped derive although the diagnosis was right. With the limit raised, all 9 run
    in the suite and are green at baseline.
  - `mini-spec --impact-max N` sets the same limit by hand.
