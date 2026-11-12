# Harness gates (Phase 2, 2026-10-08)

Goal: the pipeline stops dispatching, or parking on, harness/spec defects. Of 158 diagnosed
escalations 46% were unsatisfiable specs, 37% harness defects, 15% already-satisfied, 3% model.

Shared pure module: `bin/dispatch_harness_gates.py` (also `python3 dispatch_harness_gates.py check <wt>`).

| Gate | Where | Behaviour |
|---|---|---|
| Harness-complete | preflight `check_harness_complete` | NO-GO on TODO / SCAFFOLD_INCOMPLETE placeholders in TASK.md, refimpl.py or the fixture (a *conditional* `if (!CASES_AUTHORED)` guard left in a finished fixture is fine; flag still off is not), no real `## Must contain` literal, or a refimpl anchor absent from the target (skipped for creation tasks). When it fails the dynamic checks are marked UNPROVEN and never executed. |
| Fail-before/pass-after | preflight `check_contract` | its own check; GO requires verify RED at baseline and GREEN under refimpl. A waived/unmeasured half is UNPROVEN, which blocks auto-continue. |
| Stub-aware baseline-clean | preflight `_stub_is_baseline`, scaffold `looks_like_creation_stub` | a declared target that is a stub (`export {};`, empty, comment-only, creation stub) in HEAD *and* the tree is baseline, not dirt. Refimpl-written test files are harness (sealed by `--auto-seal`). |
| State-only blocker repair | auto `repair_state_only_blockers` | if `baseline-clean` is the only blocker, dirty tracked non-harness files are `git checkout`-ed and preflight re-run once. |
| Spec-satisfiability lint | slicer `spec_satisfiability_reason` | a must_contain literal with 0 occurrences in the target and not named in the slice intent/title/verify_shape is refused before any author job (`lint_ack: true` overrides). |
| Invariant already-satisfied | slicer `vacuous_literals` | all literals present -> reason -> slice SKIPPED (no literal re-derivation). |
| Retry-storm -> re-spec | slicer `respec_on_cap` (`RESPEC_CAP=1`) | at the attempt/streak cap (signature-blind) the failure is folded into the slice intent once and authoring restarts from a clean baseline; the second cap parks as before. |
| Relevance survivor -> harness refine | gate `harness_refine_apply` (`HARNESS_REFINE_CAP=2`) | survivors with a green verify write `<home>/harness-refine/<label>.json`, bump the counter, log the decision and (live mode) relaunch the recorded `ollama-dispatch-auto ... --resume-harness` driver; at the cap it escalates worded as a re-spec. |
| Auto-continue at GO | auto `auto_continue_go` | contract PASS + relevance measured (or locked test-file target) -> `draft --confirm`, re-check, `ollama-queue.py enqueue --label L --bundle B`. Pauses (old READY-TO-LAND row) under the slicer, `--drafter-cmd`, `--no-park`, `--no-auto-continue`, `AUTO_CONTINUE_GO=0`, or unproven contract/relevance. A failed re-check returns to the refine loop. |

Every auto-decision is appended to `~/.ollama-dispatch/auto-decisions.jsonl` (`log_auto_decision`).

Tests: `test-harness-gates.py`, `test-preflight-harness-gates.py`, `test-auto-continue-go.py`,
`test-slice-spec-satisfiability.py`, `test-gate-harness-refine.py`, `test-phase2-replay-corpus.py`
(canary seams `harnessgates preflightharness autocontinuego slicesat gaterefine phase2replay`, each proven red-on-revert).
Known limitation: the heal-sweep consumer of the harness-refine request file when no recorded auto argv exists (slicer-driven runs) is Phase 4.

## Test fixtures must be COMPLETE harnesses (2026-10-08)
Phase 2's `harness-complete` gate makes preflight NO-GO before running the refimpl on a toy harness (no
`## Must contain` literal, no refimpl.py/fix.patch, TASK < 200 chars, verify that never imports the target).
`test-preflight-treelock.py` used such a toy fixture and went red (3 checks) after Phase 2; fixture rebuilt as a
complete harness (not a preflight bug; pre-Phase-2 bin 33a3797 passed it only because the gate did not exist).
