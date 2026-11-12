# Worker model-output robustness (Phase 3)

`bin/worker_robust.py` (pure helpers) + seams in `bin/ollama-worker.py`. Tests:
`bin/test-worker-robust.py` (`--revert-check` mutates the worker/module and requires RED),
canary seams `robustrequery`, `robustloop`, `robustrunaway` (proven via `pipeline-canary.py --prove`).
Knobs live in `bin/model_profiles.yaml` under `robust_defaults:` (per-model `robust:` overrides);
`get_profile(...)["robust"]` returns the merged dict.

## 1. Layered tool-call repair
Native `tool_calls` first. Otherwise the content is parsed, in order: the worker's legacy parser
(inline JSON / Mistral `[TOOL_CALLS]` / complete Qwen3-Coder XML), then `repair_tool_calls`:
Qwen3-Coder XML (tolerates missing `</parameter>`, `</function>`, `</tool_call>`), Hermes JSON in
`<tool_call>` (tolerates trailing text, a missing closer, JSON cut mid-string), inline JSON with
string-encoded `arguments` / `function` wrappers, and an OPTIONAL small-model extractor
(`robust.tool_extractor: true`, off by default; only runs when everything else found nothing and the
text looks like a call attempt). Closed `<think>` blocks, an unterminated `<think>`, and a stray
`</think>` prefix are stripped before parsing (a call quoted in reasoning is not a call). A call that
ran to end-of-input with no closer is only accepted for read-only tools; a truncated `write_file`
/ `run_bash` / `edit_file` is REFUSED (never applied) and re-prompted.

## 2. Format-error requery
An unparseable attempt (truncated, unknown tool, bad JSON, dangling `</tool_call>`, ...) gets a
templated error naming the exact problem, plus the correct format. It does NOT consume an iteration
(`i -= 1`) for up to `robust.format_requery_max` (3) consecutive errors; the stored bad call is
replaced by a stub (no lock-in, no context bloat). The 4th ends the run: **`repeated_format_error`**.

## 3. Loop / convergence detector
`LoopDetector` watches repeated identical reads (a mutation resets the count), repeated identical
writes, A->B->A write thrash, and N iterations with nothing new; the job's own verify is exempt.
First detection injects one `[loop detected]` message; the next ends the run: **`loop_detected`**
(an ignored warning that produced genuinely new bytes is treated as progress). Defaults
`repeat_read 5, repeat_write 4, aba_returns 3, no_progress_iters 8, min_iter 4`, `verify_spin 0` (off);
override per profile (`robust.loop_detector`) or env `WORKER_LOOP_<KEY>` (`WORKER_LOOP_ENABLED=0` off).
Calibration (3,242 recorded transcripts): ~0.7% of converged runs would be stopped, ~2-3% of
non-converged ones are caught (~10 iterations saved each) -- loops are a minority of non-convergence;
most of it is wandering, which the stop-gate/spec side addresses.

## 4. Stop-gate
`task_complete` is accepted only if whatever the existing verify gate decided AND the spec's required
files (the `## Entry point` file, `## Required files`) exist and are non-empty AND every `## Must contain`
literal is present (bare bullet: the entry file or any file changed this run; `in <path>:` pins it).
All criteria are re-checked on every claim. Refusals name exactly what failed; `robust.stop_gate_max`
(3) refusals end the run: **`stop_gate_failed`** (and the accept-on-verify-pass rescue does not override
it). `WORKER_STOP_GATE=0` disables. No stated criteria -> the gate stays out of the way.

## 5. Runaway reasoning
The streaming lanes abort a turn that spent `robust.reasoning_budget_chars` (24000) thinking with no
content and no tool call (`ChatAbortedForReasoningRunaway`); the turn is retried ONCE with thinking
disabled (profile non-thinking mode) and a "decide now" message. A second runaway ends the run:
**`reasoning_runaway`**. A think-cap truncation (non-streaming) likewise recovers with thinking off.

## 6. Bash-only mode (`robust.bash_only: true`, off, coding tasks)
One ```bash block per turn becomes a `run_bash` call; `echo TASK_COMPLETE` alone becomes
`task_complete` (all gates apply); zero/multiple blocks are format errors (free requery). Uses the
manual-tools plumbing; no native tools are sent.

## Exit reasons
`repeated_format_error`, `loop_detected`, `stop_gate_failed`, `reasoning_runaway` are printed as
`[worker] TERMINAL REASON: <name>`. `failure_ledger.classify` tags them (`format-error-loop`,
`loop-detected`, `stop-gate-failed`, `reasoning-runaway`) with repeat-failure prompt hints. The queue's
`classify_failure` (`ollama-queue.py` `_FAILURE_CONTEXT_REASONS`) must list them so they classify as
`context` (not `model`).

## Tool-calling preflight timeout (`WORKER_PREFLIGHT_TIMEOUT_S`)
`_tool_calling_preflight` sends one minimal tools-bearing request before the loop. Its timeout is
30s by default; `WORKER_PREFLIGHT_TIMEOUT_S` (positive integer seconds) overrides it for slow-prefill
servers (the Strata expert-offload arm needs minutes; `strata-h2h-arm.sh` exports 900). Invalid,
zero or negative values fall back to 30. The Darkbloom lane keeps `WARMUP_TIMEOUT_S`. Test:
`test-worker-preflight-timeout.py`; canary seam `preflighttimeout` (3 revert proofs). The Strata arm
also warms the server before the driver runs and passes `STRATA_REASONING_BUDGET` (default 12288).

## Darkbloom 422 "Inference generation failed" recovery ladder (2026-10-09)
Darkbloom 0.9.19 (MTP + tool-call generation) deterministically answers HTTP 422
`invalid_request_error` "Inference generation failed" (streaming: `finish_reason:"error"`, "Response
generation failed", which `call_openai_streaming` now raises as the same 422) for some contexts when
tools are present: the model emits `</think>` and dies at the first tool-call token. Identical retries
cannot help, and the old worker paused for review (exit 3), blocking the whole cell. After the normal
retries are exhausted, `run_task` runs `_recover_422` (openai lane, native tools only), one attempt per step,
on a COPY of the messages (the transcript keeps the real tool output):
1. same body with `tool_choice:"none"` + an appended user nudge asking for the text-embedded
   `<tool_call><function=..>` form, which the visible-text fallback parser / `repair_tool_calls` consumes;
2. tools kept, the last tool result cut to its first 40 lines + `[truncated N lines]` (JSON tool results
   are cut per string field; a long few-line blob is cut at 4000 chars).
Each step logs `422 recovery: step N (...) ok|failed`; `_dispatch_metrics["recovery_422"]` records
`{attempts, ok, events[]}`. Cap: `RECOVERY_422_MAX_ATTEMPTS` = 6 per run, so a truly dead server still
ends `PAUSED FOR REVIEW` (`chat_request_failed`). `call_ollama` gained `tool_choice=` and `max_attempts=`.
Test: `test-worker-422-recovery.py` (stub HTTP server; red on the old worker); canary seam `worker422`
(4 revert proofs).

## Bounded, evidence-gated escalation reviews (2026-10-09)

Trigger: esc-review `2ac42be77bcc` (row `444c051f2538`) ran ~7.5 min on a 499-byte stub context. A bare
`needs_opus` job row carried no driver evidence, so the model re-read two tiny files, listed `$HOME` (2149
entries) and ran away. Now:
* `dispatch-escalation-watcher.py` resolves evidence from the label (auto-runs chain record, worktree
  `wt-<label>`, driver log) into a `## driver evidence` block; before spawning, `handle_no_evidence` checks
  the context carries what the review task tells the reviewer to read. If nothing resolves it spawns NO model:
  the escalation gets an explicit `NO EVIDENCE AVAILABLE` line, a deterministic verdict, stays open. A render
  failure still announces (per-entry containment).
* The review job is enqueued `--role review --max-tokens 6144`; for a research task with
  `--capture-final-as` the worker ends a run under a named reason with a `REVIEW CAPPED (<reason>)` marker:
  `repeat_call_loop` (same read-only call twice in a row / third time overall), `output_cap_review` (a turn
  hit the cap), and `list_files` refuses directories over 400 entries.
* `escalation_verdict.is_no_verdict_review` (first line `REVIEW CAPPED` / `NO EVIDENCE AVAILABLE`) makes
  `dispatch-self-heal.py` take no action and the escalation stay open. Test `test-esc-review-bounds.py`,
  canary seam `escbounds`.
