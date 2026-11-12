# Sampling escalation ladder (A/B arm, default OFF) -- 2026-10-09

A declared, switchable arm in `bin/model_profiles.yaml` (`sampling_escalation:`) implemented by
`bin/model_profile.py` (`build_request_fields(..., sampling_step, seed)`, `sampling_step_overrides`,
`check_sampling`) and `bin/worker_robust.py` (`SamplingLadder`), wired into `ollama-worker.py`.
**Shipped OFF: with the arm off every request body is byte-identical to before** (asserted by the outgoing-
request conformance test, `test-model-profile-conformance.py run_ladder`, and `check_profiles` fails if the
shipped arm is anything but `off`).

## The ladder (author / coding / thinking_coding roles only)

| event | action |
|---|---|
| abort 1 (output-cap cut, prose runaway, reasoning runaway) | NEXT turn only: temperature >= 0.8, repetition_penalty 1.2, fresh seed |
| abort 2 (consecutive) | NEXT turn only: presence_penalty 1.0, fresh seed, thinking OFF **only if the thinking arm is `hybrid`** |
| abort 3 | exit `sampling_ladder_exhausted` (the scheduler re-specs) |
| 2nd abort while the SAME check is the failing check (verify signature, even with a good turn between) | exit `spec_defect_repeat_abort` -- spec-defect path FIRST, no more sampling changes |

Values are only ever raised (`max`) over the card profile / caller values, never lowered. A new seed is
drawn on every take (round-specific base), so a retry or refine round never replays the same sample.
Exit reasons are in `worker_robust.NEW_EXIT_REASONS`, so they print as `TERMINAL REASON:` and become the
job's terminal_reason. **Follow-up (not done here, file owned elsewhere):** `ollama-dispatch-auto` should
route `spec_defect_repeat_abort` to its spec-defect path and `sampling_ladder_exhausted` to re-spec.

## Switch

Per job, no queue change: a `Sampling-arm: ladder` line in the task text (worker process env
`MODEL_SAMPLING_ARM`), or env `MODEL_SAMPLING_ARM=ladder` for the whole process. `Sampling-arm: off` is the
control arm. Thinking-off in step 2 additionally needs `MODEL_THINKING_ARM=hybrid` (`ollama-queue.py
enqueue --thinking-arm hybrid`).

## Evidence quality (read before trusting any of it)

Nothing here is validated on Qwen3.6. Grades:
* Card presets (Qwen3.6 card L663-665: thinking/precise-coding temp 0.6 presence 0.0; non-thinking temp 0.7
  presence 1.5) -- strong, vendor, but they are mode presets, not a recovery ladder.
* Qwen3-14B card L327-329: do not use greedy decoding (endless repetitions); presence_penalty 0-2 "to reduce
  endless repetitions", may mix languages -- medium (older, different model, vendor).
* Darkbloom honours seed, temperature, top_p, top_k, repetition_penalty, presence_penalty, max_tokens and
  ignores min_p (d-inference inference.md L488) -- the knobs are real.
* Step 1 values (0.8 / 1.2) -- weak: our choice, near the 0.4 / 1.2 output-cap recovery turn already in the
  worker (itself unvalidated); repetition_penalty 1.2 is outside the card's 1.0.
* Step 2 (presence 1.0, thinking off) -- weak: a proposal; 1.0 sits inside the 14B card's range.
Sources are mixed quality; the A/B below exists to find out, not to confirm.

## A/B plan (NOT started)

Arms (via bundles, one job per arm per old task, run when the queue is idle):
* A control: no `Sampling-arm` line (arm off).
* B ladder: `Sampling-arm: ladder`.
* C ladder + hybrid thinking: `Sampling-arm: ladder` with `--thinking-arm hybrid` (the only arm where step 2
  turns thinking off).

Tasks: previously failed or capped authoring tasks whose logs show prose/output-cap aborts (the
`output_cap_loop` / `prose_loop` / `reasoning_runaway` family); same task, same base, same model, same
iteration cap per arm.

Measures per arm: prose-abort count (`output_cap_prose_cuts`, `reasoning_runaways`, `prose_runaway_aborts`
in dispatch-metrics), convergence rate, attempts/iterations to convergence, ladder steps used
(`sampling_steps`), terminal_reason mix (`spec_defect_repeat_abort` and `sampling_ladder_exhausted` count as
useful early exits, not failures of the arm), and wall time. Success = fewer prose aborts AND no loss of
convergence versus A; a lift only in C is a thinking-policy result, not a ladder result.

## Tests

`bin/test-sampling-ladder.py` (state machine, builder, yaml, worker wiring); the exact body per step on all
four senders is asserted in `test-model-profile-conformance.py`. Canary seams `sampladder` and `modelprofile`
(red-on-revert: `pipeline-canary.py --prove --only <seam>`).
