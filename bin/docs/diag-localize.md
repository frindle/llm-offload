# diag-localize: CPU-first localization for offloaded diagnosis

`~/bin/diag-localize` (python3, stdlib only). Implements items #1 and #2 of `diagnosis-research.md`. Linked from
`docs/mini-swe-agent.md`. Status: built and self-tested offline; **trials awaiting the owner approval (GPU). No model call has been made.**

## `slice` (item #1, CPU only, no model)
`diag-localize slice --repo DIR --replay-cmd CMD [--passing-cmd CMD] [--file F ...] [--timeout 300] [--top 30] [--json OUT] [--md OUT]`
- Runs CMD (shell, cwd = repo; CMD is executed as a shell command, so never pass untrusted text) with a `sys.settrace` call recorder
  (global trace function that records on `call` and returns None, so there is no per-line cost) injected through a temp `sitecustomize`
  (it chains the interpreter's own sitecustomize, which Homebrew needs for its site-packages) and, for pytest, a plugin loaded via
  `PYTEST_ADDOPTS=-p diag_plugin` (per-test function sets, outcomes, traceback frames from `excinfo`). The plugin is a separate module
  from the tracer on purpose: the tracer is imported before pytest starts, and `-p` on an already-imported module raises
  `PytestAssertRewriteWarning`, fatal under `filterwarnings = error`. If the replay output still mentions `diag_trace`/`diag_plugin`
  the result carries a warning.
- Every Python process writes its own `OUT.<pid>.json` (tmp file + rename); `run_traced` merges all of them (wrapper scripts that spawn
  pytest, xdist workers) after deleting stale trace files. Outcome precedence on merge: failed > passed > skipped. Skipped, xfail and
  xpass tests are NOT counted as passing evidence. A timeout kills the whole process group (own session + `killpg`). Non-pytest commands: one pseudo-test, frames parsed from
  `File "x", line N, in f` in the output.
- coverage.py is not importable here and is not used; function-level data needs only call events. Python only. **JS mode is not
  implemented** (V8 coverage gives byte ranges that need the runner's mapping code; for JS reach use the runner's `NODE_V8_COVERAGE`
  gate). `locate` itself works on JS files (runner `functions()` + regex call index) with no slice.
- Function boundaries: the runner's `functions()` (imported), plus an `ast` pass for class-qualified names and duplicate names.
  Files under site-packages/.venv/node_modules and test files (`test_*`, `conftest.py`, `tests/`) are not ranked (`--include-tests`).
- Ranking: with `--passing-cmd` and >=1 failing test, score = 0.6*Ochiai + 0.4*traceback proximity; without, traceback proximity,
  executed-in-failure, later first call. Reasons: `in traceback (failure frame|depth n)`, `executed in n failing test(s)`, `Ochiai x (ef= ep=)`.
  Output JSON: `ranked[] {file,name,first,last,score,ochiai,reason}`, `traceback`, `tests {failed,passed}`, `warnings`
  (e.g. "the replay did not fail"). SBFL is weak with one failing test (research caveat): it is a filter for `locate`, not an answer.
- Assertion failures end in the test, so the callee that returned the wrong value has no traceback frame; it is ranked by Ochiai/order.

## `locate` (item #2, two calls + repair)
`diag-localize locate --repo DIR --file F ... --symptoms FILE [--slice slice.json] [--no-think auto|all|none] [--budget-tokens 60000] [--max-completion-tokens 16000] [--body-cap 80] [--live | --dry-run | --stub-model [JSON]] [--out OUT]`
- L1: symptoms + slice summary + ALL function signatures with line ranges (names only if over `--max-sig-chars`) -> JSON `suspects[5]`.
  Names not in the index are dropped; if none valid, the slice order is used.
- L2: suspect bodies (line-numbered, `--body-cap` lines each, shrunk to fit the token budget) + callers/callees signatures
  (ast for Python, regex for JS) -> JSON `top[2]`, `diagnosis` with `at <fn>:<line> expect <expr>`, `condition {function,line,expr}`.
- Parsing is strict (one JSON object, optional single fence, `<think>` stripped). A malformed reply gets ONE short repair call
  (no_think) per stage; still malformed -> error result (exit 1). `--budget-tokens` caps prompt+completion: the prompt is an estimate
  (len/3.2), and every completion is HARD-capped with `max_tokens` = min(`--max-completion-tokens`, budget left after the estimated prompt),
  never below the 1500-token reserve a call needs to start (repair calls <= 2000). `ss_call`/`single_shot_call.py` take an optional
  `max_tokens` (default unchanged for the runner). A thinking reply cut by the cap yields a parse error, not an overrun.
- The `condition` line must lie inside the named function's line range, else the condition is dropped (`condition_rejected` + a note).
  `usage` is recorded on every path that made calls, including an L1 failure.
- Model access: reuses the runner's `ss_call` (mini venv + litellm, overlay sampling, endpoint/key from `~/.darkbloom/local.json`,
  key via 0600 temp file). `--no-think auto` = L1 and repair no_think, L2 thinking (research); `all`/`none` override.
  Real calls are explicit opt-in: without `--live` (and without `--dry-run`/`--stub-model`) `locate` refuses (exit 2). NOTE: like the 2026-10-15 pilot, this talks to the endpoint directly, not through
  the queue (plan section 7); run it only with approval.
- `--dry-run` prints both prompts (L2 from slice order), no model call, local.json not read. `--stub-model` = deterministic offline
  model; `--stub-model replies.json` = canned reply list (for malformed/repair cases).

## Wiring
`diagnose-submit --localize --replay-cmd CMD [--passing-cmd CMD] [--replay-cwd DIR]` runs `slice` on the `--code` files first and
writes the ranked suspects plus bodies of the top 3 into `EXCERPTS.md` (also `slice.md` and a redacted `slice.json` in the workdir).
`--replay-cmd`/`--passing-cmd` run as shell commands. The replay repo is `--replay-cwd`, else the git root above the first `--code`
file; with neither it warns and skips (it no longer uses the code file's directory). The workdir is what the model sees, so
`slice.json` there omits `replay_cmd`, `passing_cmd`, `replay_tail` and `repo`; the full result is kept local-only in
`<workdir>.local/slice.full.json`. Without `--localize` behaviour is unchanged; without `--replay-cmd` it warns and skips. It never calls the model.

## Tests
`diag-localize --self-test` (offline): toy repo whose `apply_discount` is buggy; slice ranks it top-3 with pytest (with/without passing
tests) and with a plain script; passing replay -> warning; locate dry-run (all signatures present, a trap `MINI_SINGLE_BIN` proves no
model call); deterministic stub; canned malformed -> exactly one repair; hallucinated name dropped; unrepairable -> error; budget
-> error; real-call plumbing against a FAKE helper (key never in the result, `max_tokens` reaches the helper); JS index; `diagnose-submit --localize --dry-run`.
Review-fix cases: `filterwarnings=error` repo, tail-mentions-tracer warning, plain replay + plain passing cmd, wrapper-spawns-pytest merge, skip/xfail not passing, timeout killpg + decoding, `--live` refusal, max_tokens vs budget, out-of-range condition line, usage on L1 failure, slice.json redaction, git-root default for `--replay-cwd`.

## Trial plan (from the report) -- awaiting the owner approval (GPU)
Same 3 past bugs in the 7000-line service with known fix commits (checkout the parent of each fix, build the fixture from the real
payload/log; add 2 more bugs if fast). Not run.
- **#1 trace-localize (CPU only, could run without GPU but still not run):** `slice` per bug. PASS: fixed function in the top 5 for
  >=2/3, runtime <60 s each. FAIL: gold function outside top 10 in 2/3.
- **#2 two-call localize (GPU):** arms "#2 alone" vs "#1 + #2". PASS: gold function in L2's top 2 for >=3/5 with #1+#2 and total wall
  <=3 min. FAIL: <=1/5. Record tokens/calls/repairs per run (`usage` in the result JSON).
