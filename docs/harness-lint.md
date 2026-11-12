# harness-lint + operator-edit survival (2026-10-09, rt-walmart-cancel-import)

Incident: a hand-edit to TASK.md / check_literals.py in a live worktree was silently reverted by the stage's
own `auto-harness-check.py` (and the preflight's `revert_refimpl`), because both replay pre-check snapshots
(`pre_snap` / `pre_tracked`, `_pre_refimpl_bytes`) over the tree. The model then chased a literal the operator
had already removed. Fix: protected harness files (TASK.md, verify.sh, refimpl.py, check_literals.py,
auto-harness-check.py, AUTO-TASK.md, .dispatch-harness.json, dispatch-env.*) are fingerprinted after the
baseline/refimpl step (`_note()` / `_note_post_refimpl`); at revert a protected file whose bytes changed since
is KEPT (captured before `git checkout`, rewritten after). Tracked-clean harness files are tracked via `git ls-files`.

`bin/harness_lint.py` (`harness-lint <wt> [--stage S] [--quick] [--full] [--json]`) is the one pre-run validator.
It runs at scaffold `--freeze-literals` (advisory), at authoring and at EVERY stage/round entry in
`ollama-dispatch-auto` (staged and continuation rounds). Every failure is a named
`SPEC_DEFECT: <reason>: <path> -- <msg>` that the existing route (`_failure_route` -> die, `_author_escalate`,
slicer `ERROR: SPEC_DEFECT:`) already understands. `--quick` (stage entry) never runs the refimpl and never
judges a refimpl that is not authored yet.

| Check | Codes |
|---|---|
| must-contain literals at HEAD or in the refimpl-applied tree, difflib "did you mean" (insert/drop only, never a substitution) | LITERAL_NEAR_MISS, REFIMPL_LITERAL_ABSENT, REFIMPL_LITERAL_UNPROVEN (full mode only) |
| scope line / forbidden-edit list / literals / target consistent | SCOPE_FORBIDS_TARGET, TARGET_OUTSIDE_SCOPE, LITERAL_OUTSIDE_SCOPE |
| the stage's own self-check executes and prints parseable output | SELFCHECK_UNPARSEABLE |
| the pending prompt quotes no literal the CURRENT TASK.md dropped (rebuild, never resume a stale transcript) | STALE_SPEC_IN_PROMPT |
| two rounds failing on the same missing-literal set / failure signature park as a spec defect (a newer TASK.md lifts it) | REPEAT_MISSING_LITERALS, REPEAT_FAILURE_SIGNATURE |

Tests: `test-literal-lint.py`, `test-harness-lint.py`, `test-harness-operator-edit-survives.py` (each has `--revert-check`).
Canary seams `literallint harnesslint opedit`, proven with `pipeline-canary.py --only <seam> --prove`.
Canary sandbox pins (scenario drift, not scheduler bugs): `ODS_STAGED_AUTHOR=0` (the scripted rounds assume the
unstaged ladder), `DARKBLOOM_CTX=131072` (the ~45k-token AUTO-TASK estimate was refused by the 65536 ctx gate so the
first author round was never enqueued), and the s3 target is `x >= hi ? hi - 1 : x` (a clamp's `>`/`>=` mutant is
equivalent and unkillable, which now correctly escalates).
