# v7 test design — separating "didn't look" from "looked and couldn't apply"

**Drafted 2026-08-23 ~00:10, after v6.1.** For Fable review before dispatch. Nothing here is dispatched yet.

Related: [[Ollama-Dispatch-Log]] · [[Ollama-Fable-Pass-Agenda]] · [[Ollama-Model-Guide]]

---

## The finding this test is built around

The SwiftPM layout error — declaring `.target(name: "X")` then filing sources under `Sources/<OtherTarget>/X/` instead of `Sources/X/` — now has **three occurrences across two model families**. The agenda predicted that a third occurrence would promote it to a documented model property. Reading the logs says the opposite, and the distinction is the whole point of v7:

| model | called `list_files("Sources")`? | read `Package.swift`? | outcome |
|---|---|---|---|
| `qwen3-coder-next:q4_K_M` | **yes** — saw `AXPrivateShim/`, `CGVirtualDisplayShim/`, `Clamshell/` | **yes, twice** | still nested the new target inside `Sources/Clamshell/` |
| `deepseek-r1:32b` | **no** — only `.` and `Sources/Clamshell` | — | never saw the sibling pattern at all |

**Two different defects wearing the same error message:**

- **Exploration failure** (deepseek): did not gather available evidence. Plausibly fixable with prompting, tooling, or a nudge.
- **Transfer failure** (qwen3-coder-next): had the instruction *and* the evidence on screen, and could not apply it. **Prompting cannot fix this.** Only a structural guard can — a verify step that fails fast when a declared target has no matching directory.

This also clears the harness: `SYSTEM_PROMPT` already says to discover layout, never assume it, and specifically *"if you are adding a module or target, read how existing ones are declared."* The repo demonstrates the convention three times over. The information was present and unused. **First finding today that is genuinely about the model rather than the instrument.**

## What v7 must measure

Every previous round could only say *"the model got it wrong."* v7 must say **which of the two ways** it got it wrong, because they have different fixes and one of them is unfixable by prompting.

### The key instrumentation change: an evidence column

Record, per run, whether the model **saw the relevant evidence before acting**:

- `saw_evidence` — did it call `list_files` on the parent directory (or read the manifest) **before** the first `write_file` that creates the new module?
- `acted_at_iteration` / `evidence_at_iteration` — ordering matters; evidence gathered *after* the mistake is not evidence.

This is computed from the worker log, which already records every tool call with arguments, so **no harness change is required** — only a log parser. That matters: every harness change today has introduced or masked a defect, and this round should add none.

With that column, each failure classifies automatically:

| saw_evidence | correct | classification |
|---|---|---|
| no | no | **exploration failure** — did not look |
| yes | no | **transfer failure** — looked, could not apply |
| yes | yes | success |
| no | yes | got lucky / prior knowledge — worth flagging, not crediting |

## Task design

### Task A — sibling target (the transfer probe)

Add a new SwiftPM target to `clamshell`. Unchanged from v6.1 deliberately: it already produced the finding, three times, and changing it would forfeit comparability.

- **Verdict from the build system**: `swift build`. Not a regex, not a column enumerating expected mistakes.
- **Why it is a good probe**: the correct answer is visible in `list_files("Sources")` and in `Package.swift`. There is no ambiguity to hide behind.

### Task B — locate-and-extend (the exploration probe)

Extend an existing feature whose file is **discoverable but not named in the prompt**. The v6.1 photo-upload task accidentally became this and exposed a fourth family building a duplicate.

- **Fix the task defect first**: photo-upload was void as a build test because the feature already existed at baseline. Either pick a feature that genuinely does not exist, or **score it explicitly as comprehension** — did it find `components/OrderAttachments.tsx`, or did it reimplement in `components/OrderForm.tsx`?
- **Grader by presence of the correct thing**, never by enumerating wrong answers. The three graders that failed this session all enumerated expected mistakes; a model that invents a *new* wrong answer defeats them.

### Deliberately NOT in v7

- No new preprocessing of the prompt, no new nudges, no temperature changes. **One variable per round.** v7 changes the measurement, not the harness.
- No new models. The roster stands.

## Open questions for Fable

1. **Is the exploration/transfer split real, or an artifact of two samples?** It rests on one log each. What n makes it safe to state? Is there a cheaper discriminator than a full round?
2. **Does the duplicate-building failure have the same split?** Four families have now built a duplicate on photo-upload. Did any of them `list_files` the components directory first? If the split holds there too, it is a general property of these models rather than a Swift quirk.
3. **Is a structural guard scaffolding or sabotage?** The dispatch log already draws this line ("sabotage vs scaffolding"). A pre-commit check that fails on a declared-target/directory mismatch would fix `qwen3-coder-next`'s output — but it fixes the *symptom* and would mask exactly the defect this round was built to measure. Guard in production, not in the bake-off?
4. **Should the void photo-upload task be replaced or rescored?** Replacing forfeits comparability with five prior rounds; keeping it means every future reader must be told it is a comprehension test, not a build test.
5. **The unreviewed worker.** `ollama-worker-v7.py` has produced every result since bug 11/13 and has never been signed off. It is the instrument for v7 as well.

## Status

**NOT DISPATCHED.** Per the owner's instruction (2026-08-23): consult Fable, dispatch only on Fable's sign-off, do not wait for the owner.

---

## The split, measured across all 22 v6.1 runs (2026-08-23 ~00:15)

Computed from worker logs only — no harness change, no re-run. Third version of the parser; the first two were wrong, in the now-familiar way (see "grader caveat" below).

### photo-upload — did the model ever engage with the file the task was about?

`components/OrderAttachments.tsx` is the real target. "on screen" = the string appeared anywhere in the transcript, in any tool result.

| model | tools | touched target | name ever on screen | writes |
|---|---|---|---|---|
| `qwen3-coder-next:q4_K_M` | 47 | **before writing** | **yes** | 5 |
| `qwen3.8:27b-q8_0` | 25 | **before writing** | **yes** | **0** |
| MFDoom deepseek-r1-tool-calling:14b | 7 | never | no | 4 |
| `deepseek-r1:14b` | 7 | never | no | 5 |
| `deepseek-r1:32b` | 5 | never | no | 0 |
| `deepseek-r1:32b-qwen-distill-q8_0` | 7 | never | no | 0 |
| `deepseek-r1:7b` | 0 | never | no | 0 |
| `devstral:24b` | 8 | never | no | 0 |
| `qwen2.5-coder:14b` | 30 | never | no | 18 |
| `qwen2.5-coder:7b` | 30 | never | no | 0 |
| `qwen3-14b-agentic` | 4 | never | no | 1 |

**9 of 11 models never saw the target file at all.** Four of them wrote files anyway — `qwen2.5-coder:14b` wrote **18 times** without ever laying eyes on the file it was supposed to extend.

### clamshell — did the model list `Sources/` before creating a target there?

Only **3 of 11** ever called `list_files("Sources")`: `devstral:24b`, `qwen2.5-coder:14b`, `qwen3-coder-next`. The other 8 never saw the sibling-target convention that the correct answer depends on.

### Conclusion — and it inverts the working assumption

**Exploration failure is the dominant mode, at roughly 75–80% of runs.** Transfer failure is comparatively rare and **concentrated in the most capable model**: `qwen3-coder-next` is the only model that reliably gathers evidence and still fails to apply it — on both tasks.

Two consequences:

1. **Most of the field is not failing at coding.** It is failing to look. That is plausibly addressable — and it means prior verdicts about *coding* ability are largely unfounded, because those models never reached the coding problem.
2. **`qwen3.8:27b-q8_0` is the standout, again.** It is the only model that located the target and then correctly wrote nothing, on a task where the feature already existed. Its row still reads `exit=1, files=0` because the run was killed by the HTTP 500 at iteration 12 — so **the CSV records the single most correct behaviour in the round as a failure.** A seventh false-reading shape, and the inverse of all the others: a false *negative*.

### Grader caveat, stated because it keeps happening

This table took three attempts. v1 matched `({.*?})`, which terminates inside `write_file`'s content, so every write ordering was wrong. v2 counted `list_files(".")` as evidence — but the system prompt *instructs* every model to do that first, so it discriminated nothing and scored 10 of 11 as "before". Only v3, requiring the components directory or the target filename specifically, is meaningful.

**Fourth purpose-built scoring artifact this project to be wrong on first contact with real data.** The pattern in [[Ollama-Fable-Pass-Agenda]] P1 holds without exception. Any column Fable approves should be assumed wrong until it has been read against a transcript by hand.

---

# FABLE REVIEW — VERDICT: **HOLD**. Blockers cleared 2026-08-23 ~00:25, awaiting re-review.

Fable re-derived the evidence table by hand from raw transcripts, read all 1,397 lines of the worker, and confirmed the HTTP 500 mechanism against Ollama's own server log with matching timestamps and token counts.

## What survived attack

- **"9 of 11 never saw `OrderAttachments`"** — confirmed by hand against all full JSON transcripts (0 hits in nine, 9 in `qwen3-coder-next`).
- **"Only 3 of 11 listed `Sources/`"** — literally true.
- **"String absent from transcript = never saw it"** — sound for the 10 runs that have JSON transcripts; those store complete untruncated results, and every channel (a `run_bash` grep, an import inside a read file) would land in them.
- **The two-defect split is real and worth measuring.** `qwen3-coder-next`'s photo transfer failure survives the strongest attack: it read `OrderAttachments.tsx` at iterations 5 and 10, then began editing `OrderForm.tsx` at iteration 12 — the evidence was in-window at decision time.

## What was FALSIFIED — my claims, corrected

1. **"Transfer failure is concentrated in the most capable model" — WRONG.** `qwen3-14b-agentic` read `Package.swift` in full at iteration 4, wrote the misfiled source at 5, and declared the mismatched target at 6. Evidence on screen, wrong pattern applied, **mid-tier model**. Stronger still: `deepseek-r1:32b` was handed the compiler's verbatim *"should be located under 'Sources/ConfirmationBridge'"* in v5/v6 and did not apply it — transfer failure **with the answer printed at it**. `qwen3-coder-next` is merely the only model that gathered *both kinds* of evidence, not the only one that fails after seeing it.
2. **"~75–80% exploration failure" — OVERSTATED.** Of the 9 photo never-saws, only **4 acted without evidence**; the other **5 wrote nothing at all**. That is a third defect — *inertness / false completion* — with a different fix. The single largest bucket in v6.1 may be "no meaningful action". Defensible phrasing: *most runs never gathered the available evidence*, and *acting-without-looking outnumbers looking-and-misapplying*.
3. **`qwen3.8` photo as "the single most correct behaviour" — WRONG.** It is an interrupted run, not a decision. The same model on the same task in v6 announced *"I'll rewrite the component"* immediately before bug 11 killed it. The row is **no data**, not restraint. My "seventh false-reading shape / false negative" claim is withdrawn.
4. **`deepseek-r1:32b`'s SwiftPM error is NOT a stable trait.** It did not reproduce in v6.1 — the model joined the existing target correctly. 2 of 3, not stable.

## Blockers, and what was done (all verified, not assumed)

| # | blocker | status |
|---|---|---|
| **B1** | No context-overflow guard — the **proven** cause of the qwen3.8 HTTP 500s | ✅ `prompt_eval_count` logged per iteration; run stops cleanly at 90% of `num_ctx` recording `stop_reason: context_ceiling`; `thinking`/`reasoning_content` stripped from re-sent history. **Verified firing**: `context: 1234/1300 (95%) → CONTEXT CEILING`. |
| **B2** | Transcript written only after the loop, so a crash left none | ✅ Written at the top of every iteration; survives SIGKILL. Verified present with `stop_reason` recorded. |
| **B3** | Evidence definition underspecified; missing a fourth outcome class | ✅ Design revised below. |
| **B4** | Bug 11 unfixed on the llama-server path (`reasoning_content`, not `thinking`) | ✅ Both field names read. **Not yet exercised in situ** against a thinking model on llama-server — flagged as untested. |

Also fixed: **`empty_retries` never reset** (a lifetime cap, so four non-consecutive thinking-only turns would silently converge a long run — bug 11 at lower frequency); **`json.loads(raw_args)` unwrapped** (on the OpenAI path every native call is a JSON string, so one malformed call killed the whole run — now returns the error to the model). Deferred per Fable: permanent hard-block, silent drop of unparseable candidates, `run_bash` repeat-success on byte-identical *failing* builds (contained), `is_local_ollama` comment mismatch.

## Revised v7 design

1. **Depth over breadth — n=3 on ~5 models, not 11 × 2 × 1.** `deepseek-r1:32b` spans destructive / inert / inert across three runs of one task; single runs cannot classify a model. The aggregate split can be stated from the 22 runs already collected; per-model verdicts need n≥3. Roster: `qwen3-coder-next` (transfer probe), **`qwen3-14b-agentic`** (second transfer candidate, per the falsification above), `qwen3.8:27b-q8_0` (its photo cell has never been measured at all — now possible with B1), `qwen2.5-coder:14b` and one deepseek as exploration representatives.
2. **Photo-upload: rescore as comprehension, do NOT replace.** Preserving "comparability with five prior rounds" would preserve comparability of an invalid measurement — the feature existed at baseline in every one of them. Score = evidence column + touched `components/OrderAttachments.tsx` (presence of the correct thing). `verify_passed` demoted to secondary.
3. **Structural guard: production yes, bake-off no.** In-loop it is scaffolding that masks the very defect v7 measures; post-run it is redundant, since `swift build` already prints the exact right answer and models fail *with that error in hand*. Put it in the real dispatch path, keep the bake-off raw.
4. **Evidence column from JSON transcripts only, never console logs** (which truncate every tool result to 300 chars). Per-task and per-decision definitions: for clamshell, a manifest read and a `Sources/` listing are *different* evidence (names vs layout) and the compiler error is a third; classify the source-placement decision separately from the target-declaration decision. Four outcome classes: **correct / exploration failure / transfer failure / inert**.
5. **Duplicate-building: question answered, no new task needed.** Every v6.1 duplicate-builder had zero transcript contact with `OrderAttachments` (exploration), while `qwen3-coder-next` read it twice and reimplemented anyway (transfer). The split is a general property, not a Swift quirk.

## Status

**STILL NOT DISPATCHED.** Blockers cleared; returning to Fable for sign-off.

---

# ✅ SIGNED OFF AND DISPATCHED — 2026-08-23 00:53

Fable's verdict: **SIGN OFF**, conditional on `qwen3.8:27b-q8_0` running at `--num-ctx 65536`. Condition met and verified before launch.

## Fable found two real holes in my B1 fix, and fixed them itself

1. **My `usage` fallback could never fire.** `call_ollama`'s OpenAI-path normalisation returned only `{"message": ...}` and discarded `usage`, so the context guard was **silently dead on llama-server** — the exact path qwen3.8 needs. Now passed through.
2. **Reactive-only checking was insufficient.** The crashed run had a single-iteration jump of **+12,205 tokens** (49% → 86%), so a large tool result landing at 89% overflows the *next* request before any reactive check exists. A predictive pre-call estimate (last exact count + appended delta at ~3 chars/token) now stops at ≥0.95 **before sending**.
3. History append moved **before** the ceiling break, so a stopped run never loses its final assistant turn — it may carry the tool call the evidence parser needs.
4. `reasoning` accepted as an alias for `reasoning_content` (llama-server builds vary).

**The verification my own test could not do**: my `--num-ctx 1300` test only exercised iteration 1, where the cache delta equals the total — it could not rule out `prompt_eval_count` reporting only the *uncached* delta on later turns, which would have made the guard silently never fire at exactly the 96% moment it exists for. Fable probed it directly: **3518 → 3537 → 3555** across three calls sharing a prefix. **Cumulative, confirmed.**

## Answers to my three questions

1. **Ceiling**: 0.90 stands, but only paired with the 0.95 predictive pre-call check. Closed by implementation, not advice.
2. **Reasoning strip needs no control.** Re-sending reasoning is not part of either API contract — OpenAI semantics drop it between turns and Ollama's own CLI does not re-send thinking — so *keeping* it was the anomaly. An A/B would spend half the round measuring the instrument. The worker is labelled **instrument v7.1**; never compare context-growth numbers across that boundary. Comparability with v6.1 is already broken by design (new outcome classes, transcripts-only parsing), and archived diffs allow rescoring.
3. **`deepseek-r1:32b`** for the exploration slot — the agenda already mandates n≥3 for it, and its variance is precisely what n=3 answers. Timeouts scored as their own outcome class, not failures.

**Declined by Fable**: testing B4 against devstral on 8091 — devstral is not a thinking model, so it cannot exercise `reasoning_content`; it would produce a green checkmark that means nothing. **B4 stays flagged untested-in-situ** until a real qwen3.8-on-llama-server run exists. Acceptable because the v7 roster runs native.

## Pre-flight checks before launch (all mine, all found something)

- **All 10 worktrees verified present** (5 models × 2 repos) — a slug mismatch silently skipped two models in an earlier round.
- **All 5 models verified on the Studio.**
- **`qwen3.8` at 64k initially failed to load in 10 minutes.** Cause was *my own leftovers*: the devstral `llama-server` from the v6.1 leg still resident at 8.2 GB, plus a background smoke test holding `deepseek-r1:7b`, with swap at 5.9/7 GB. After clearing both: **loads in 19s, 30.1 GB resident.** Eleventh instrument-first instance today, and the fourth that is mine. Always check what is already resident before blaming a model for not fitting.
- **`results-v7.csv` already existed** from the earlier v7 Unraid integration test, with a *different* 13-column schema. The lib only writes a header when the file is absent, so every new row would have appended under mismatched headers — silent corruption of the whole round. Old file preserved as `results-v7-unraid-integration-2026-08-22.csv` (it holds the two validated negative-control rows); fresh header installed.

## Running now

`bakeoff-v7-macstudio.sh` — 3 repeats × 5 models × 2 tasks = **30 runs**. Started 00:53:05 with `qwen3.8:27b-q8_0 / photo-upload @ ctx 65536`, the cell that has never once been measured.

New CSV columns: `rep`, `stop_reason`, and **`transcript`** — the path to the run's JSON transcript, so the evidence parser can find it without guessing. Per-repeat log files (`-r1/-r2/-r3`) so repeats cannot overwrite each other.

---

## First v7 result — `qwen3.8:27b-q8_0` / photo-upload / rep 1 (2026-08-23 01:19)

**The cell that had never been measured is now measured.** Bug 11 killed it in v6; context overflow killed it in v6.1. This run went the distance: `exit=2` (non-convergence at the iteration cap), `verify_passed=yes`, 5 files, 31 iterations, 1572s.

**The 64k condition was the whole difference.** Peak context reached **47,063 / 65,536 (72%)** — the guard never tripped. At 32,768 this run would have overflowed around iteration 11–12, precisely where v6.1 died at 31,623. Fable's sign-off condition converted a crash into data; the guard alone would only have converted it into a labelled `context_ceiling`.

### Classification: TRANSFER FAILURE, and not a marginal one

From the JSON transcript (80 messages), not the console log:

| msg | what happened |
|---|---|
| 26 | tool result contains `import OrderAttachments from '@/components/OrderAttachments'` |
| **27** | **the model deliberately calls `read_file("components/OrderAttachments.tsx")`** |
| 28 | receives the full implementation, including the accepted-MIME array (`image/heic`, …) |
| 30 | sees it again in a directory listing, with size |
| **55** | **first `write_file` — 29 messages later** |

It then wrote `components/PhotoUpload.tsx`, `app/photos/page.tsx`, `components/NavBar.tsx`, `components/OrderDetailShell.tsx`, and `app/api/orders/search/route.ts` — **none of them the existing component it had just read in full.**

This is the strongest single instance of transfer failure yet recorded: the model did not merely have the evidence in view, it *sought the file out by name* and read it end to end, then built a parallel implementation anyway.

**Third model to fail this way**, alongside `qwen3-coder-next` and `qwen3-14b-agentic` — and it is the model previously called the best in the field. Transfer failure is looking less like a quirk of one model and more like a property of the class.

### ⚠️ CONFOUND — the task text pressures toward building

Scoring this as pure comprehension failure is not yet safe. `TASK_RESELL` says *"follow its existing patterns rather than inventing a new style"* — but it also says **"Implement this as a real, working feature"** and *"Build a photo-upload feature"*, and asks for a summary of *"which files you created/changed"*.

A model that reads `OrderAttachments.tsx`, concludes the feature exists, and then builds anyway may be **obeying the instruction it was given**. The duplicate-building failure that four families exhibited may therefore be partly **task-induced**, not purely a model defect.

**This does not invalidate the transfer/exploration split** — that distinction is about whether evidence was gathered, and it holds regardless. But it does mean **"built a duplicate" cannot be scored as comprehension failure while the prompt says "implement this as a real, working feature".**

A clean comprehension probe would instead say: *"…if this already exists, say so and extend it rather than reimplementing."* That is a **one-line task change** and it discriminates exactly the behaviour in question. Deliberately **not** changing it mid-round — v7 is running and a mid-flight prompt change would void comparability across its own repeats. Queued as the top item for v8, and for the next Fable pass.

**Twelfth instrument-first instance today.** The instrument here is the task text, and it went unexamined through six rounds because everyone was busy looking at the models.

---

## Restarted 01:38 — per-repeat diffs (bug caught 2 runs in)

The v7 lib inherited v6.1's diff naming, `$SLUG-$TASK_NAME-${TAG}.diff`, with **no repeat index**. In an n=3 round every model's diff would be overwritten twice, leaving only repeat 3 — silently destroying two-thirds of the round's primary evidence. The lib's own comment says it best: *"The CSV says whether something changed; only the diff says whether it was any good, and every real verdict here came from the diff."*

Same class as the log-naming problem I had already fixed **in the same file** — I threaded `REP` into the log path and not the diff path.

Fixed to `-r${REP}.diff` and the round restarted clean at 01:38:34. Cost: 44 minutes of compute, 2 runs. The two completed runs and their analysis are preserved under `model-buildoff-2026-08-22/v7-prelim-2runs/`; their findings stand (see the `qwen3.8` photo-upload transfer-failure entry above, which was scored from the transcript, not the diff).

### `qwen3.8:27b-q8_0` / clamshell / prelim run — worth keeping

`exit=0` (converged), **`VERIFY PASSED`**, 3 files, 24 iterations, peak context only **19,995 / 65,536 (30%)**. The clamshell task is nowhere near the context ceiling — the overflow problem is specific to photo-upload, which reads far more of a much larger codebase. Useful to know before assuming 64k is needed everywhere.

### Verification note

I twice wrote a check whose pattern could not match (`${REP}` expanded to empty in my own shell), and both times the *check* was wrong rather than the change. Confirmed by reading the file directly. A grep that returns nothing is not evidence of absence until the pattern itself has been verified — the same discipline this project keeps relearning about graders.


# PENDING VAULT UPDATES — Obsidian MCP disconnected 2026-08-23 ~01:45

**Append these to `Claude/Ollama-v7-Test-Design.md` once the Obsidian MCP server reconnects.**
Everything before this point was written to the live vault successfully. The MCP server dropped
mid-session; the Obsidian container was also observed briefly unreachable earlier today after
rapid `obsidian-git` raw commands, so this may be the same instability.

Note: the vault's own git backup (`frindle/obsidian-vault-backup`) commits every 30 minutes from
inside the container, so anything already written is safe. Only these *new* notes are unwritten.

---

## v7 rep 1 — `qwen3.8:27b-q8_0` / photo-upload REPRODUCES

| | prelim run (01:19) | v7 rep 1 (02:08) |
|---|---|---|
| exit | 2 (non-convergence) | 2 |
| verify_passed | yes | yes |
| files | 5 | 5 |
| iterations | 31 | 31 |
| duration | 1572s | 1737s |
| peak context | 47,063/65,536 (72%) | — |

**Two independent runs of a cell that had never been measured at all now agree closely.** This is
the first reproducibility evidence for this model on this task, and it argues the transfer-failure
classification is a stable property rather than a single-run artifact — pending the same check on
reps 2 and 3, and on the transcript rather than the CSV.

Note the CSV `rep` column reads `1` for both because the round was restarted; the prelim pair is
archived separately under `model-buildoff-2026-08-22/v7-prelim-2runs/`.

## Still to do when the vault returns

- Confirm rep 2 / rep 3 of the same cell and state whether the transfer failure is stable at n=3.
- Score the remaining v7 models by the evidence column, computed from JSON transcripts only.
- Score the five remaining vision models with `gc-score-pins.py` once their sweeps finish.
- Carry the task-text confound (`"Implement this as a real, working feature"` pressures toward
  duplicate-building) into the v8 design and the next Fable pass — it is the top queued item.

---

## ★ THE ONLY GENUINE COMPLETE PASS IN THE BAKE-OFF DOES NOT REPRODUCE

`qwen3.8:27b-q8_0` / clamshell, same model, same task, same harness, hours apart:

| | prelim (01:37) | v7 rep 1 (02:30) |
|---|---|---|
| exit | 0 (converged) | 1 |
| verify | **PASSED** | **FAILED** |
| files | 3 | 2 |
| iterations | 24 | 31 (hit the cap) |
| peak context | 19,995/65,536 (30%) | 22,734/65,536 (35%) |

**This is the headline result of the entire project — "the first genuine complete pass of the
whole bake-off", recorded as the dispatch recommendation — and it is 1 of 2 at best.** The vault
entry asserting it should be read as a single observation until reps 2 and 3 land.

### The failure is a DIFFERENT one, which matters

This run **avoided the SwiftPM layout trap**: no new target declared, source filed under
`Sources/Clamshell/Auth/`, joining the existing target. It failed instead on **invented CryptoKit
identifiers**:

```
error: 'SigningKey' is not a member type of enum 'CryptoKit.P256'
error: 'VerifyingKey' is not a member type of enum 'CryptoKit.P256'
error: cannot find 'Data' in scope
```

The real names are `P256.Signing.PrivateKey` / `.PublicKey`. Notably the model wrote the CORRECT
form (`P256.Signing.PrivateKey()`) in a scratch snippet visible in the error context, then used the
invented form in the actual source — it had the right identifier available and did not apply it.
`cannot find 'Data' in scope` is a missing `import Foundation`, the exact defect documented for
`qwen2.5-coder:7b`.

**Consequences:**
1. The wrong-identifier failure mode is now confirmed at 7B (`qwen2.5-coder`), 32B
   (`deepseek-r1:32b` → `P256.KeyPair`) and **27B q8 (`qwen3.8`)**. It is a property of the class,
   not of small models. The guide's framing should change accordingly.
2. Writing the correct identifier in one place and the invented one in another is arguably a
   *transfer* failure at the token level — the same shape as reading `OrderAttachments.tsx` and
   then building a parallel component.
3. **No single-run verdict in this project is safe**, including the ones that look like successes.
   The agenda already said this for `deepseek-r1:32b`; it applies to the best model too.

---

## ⛔ CORRECTION to the section above — I had the sequence BACKWARDS

I wrote that the model "had the right identifier available and did not apply it," calling it a
token-level transfer failure. **That is wrong.** Read from the JSON transcript in order:

| msg | what actually happened |
|---|---|
| 38–49 | uses the INVENTED `P256.SigningKey` |
| 47 | compiler: *'SigningKey' is not a member type of enum 'CryptoKit.P256'* |
| **50 →** | **switches to the CORRECT `P256.Signing.PrivateKey` and keeps it for the rest of the run** |
| 65 | new errors, now on METHOD names: `verifySignature` (real name `isValidSignature(_:for:)`), `.verify` on `ECDSASignature` |
| 71 | **reads CryptoKit's `.swiftinterface` and greps it for `P256`** |
| 72 | iteration cap (31) reached, `converged=False` |

**The model started wrong, read the compiler error, and self-corrected — the edit-verify loop
working exactly as intended.** It then attacked the remaining errors by going to the SDK interface
file itself, which is the single behaviour the guide credits this model with and no other model in
the field exhibits.

### The revised verdict, which is materially different

`VERIFY FAILED` here means **"ran out of iteration budget mid-recovery"**, not "cannot do the task."
The prelim run finished the same task in 24 iterations and passed; this run was still converging at
31. **The difference between the bake-off's only clean pass and a failure may be little more than
how many recovery cycles the run happens to need against a fixed cap.**

That reframes three things:
1. **`max_iters` is a scoring variable, not a neutral bound.** Any model that recovers via
   compile-read-fix cycles is being scored partly on how fast it converges, and `exit=1` conflates
   "wrong" with "not finished". This belongs in the next Fable pass.
2. The wrong-identifier claim for `qwen3.8` is **withdrawn**. It emitted an invented identifier once,
   then fixed it from feedback. That is not the `qwen2.5-coder:7b` pattern, where supplying the
   token did not produce working code.
3. Non-reproduction of the headline pass **stands** — one pass, one non-finish — but the cause is
   budget-and-variance, not a capability ceiling.

**Thirteenth instrument-first instance today, and the fifth that is mine.** I wrote a verdict from
the console log's error text and the order it appeared in, rather than from the transcript. Errors
in a log are printed in the order the compiler emitted them, which is NOT the order the model tried
things. Read the transcript.

---

## ★ THE CSV AND THE DIFF DISAGREE — cleanest demonstration yet

Two `qwen3.8:27b-q8_0` photo-upload runs, same model, same task, same harness:

| | prelim (01:19) | rerun (02:08) |
|---|---|---|
| exit / verify / files / iters | 2 / yes / 5 / 31 | 2 / yes / 5 / 31 |
| modified `components/OrderAttachments.tsx` | **NO** | **YES** |
| other files | `NavBar`, `OrderDetailShell`, `app/api/orders/search`, `app/photos/page`, `PhotoUpload` | `NavBar`, `app/api/attachments`, `app/photos/page`, `PhotoUpload` |

**Identical on every CSV column. Materially different in behaviour.** One run never engaged the
existing component; the other modified it. The classification flips between them — transfer failure
vs. correct.

I had called this pair "reproduces closely" on the strength of the matching CSV numbers. **That
claim was itself CSV-derived and is withdrawn.** The numbers reproduce; the behaviour does not.
This is the project's own rule ("score from the logs and diffs, never the CSV") demonstrated against
me, and it is the strongest single argument for keeping per-repeat diffs — which nearly did not
happen (see the diff-naming bug above).

## Evidence scorer built — `score-v7-evidence.py`

Classifies each run from the JSON transcript into: `correct` / `transfer failure` /
`exploration failure` / `inert` / `unfinished (cap)` / `context_ceiling`.

Current v7 rows:

```
qwen3.8:27b-q8_0     photo  r1  correct              touched target
qwen3.8:27b-q8_0     clam   r1  unfinished (cap)     cycling build/edit at the cap
qwen3-14b-agentic    photo  r1  exploration failure  built elsewhere (never saw the target)
```

**It was wrong on first contact, as every grader on this project has been — the fifth.** Two defects
found and fixed before any verdict rested on it:

1. **Clamshell "misfiled" flagged any `Sources/Clamshell/<dir>/` path.** But filing there with *no
   new target declared* is CORRECT — it joins the existing target, which is exactly what `qwen3.8`
   did. The check now requires that a new target was actually DECLARED and its sources sit outside
   `Sources/<TargetName>/`.
2. **`unfinished (cap)` was a blanket override** for any non-converged run, which would have
   relabelled genuine exploration failures as budget problems. Now requires demonstrated cycling
   (≥2 builds AND ≥2 edits); anything else keeps its failure class and records the counts.

`max_iters` is confirmed as a **scoring variable, not a neutral bound**, and now has its own outcome
class rather than being folded into `exit=1`.

---

# ★ VISION: real accuracy across models — and TWO VOIDED SWEEPS

Truth set has grown to **38 confirmed PINs across 16 images** (it grows automatically as any model
produces a string that matches a stored code, and is model-independent by construction).

## `qwen2.5vl:7b` — 1762 scored runs

| variant | recall |
|---|---|
| **redchan+stretch14** | **59.6%** (68/114) |
| grayscale+stretch14 | 43.0% |
| redchan+shearP8 | 37.7% |
| redchan | 35.1% |
| normal (raw) | 14.9% |
| inverted / greenchan / bluechan | 0.0% |

(Earlier this note reported 68.7% for the top variant; the denominator has since grown from 33 to 38
truths, so the percentage fell while the absolute count did not change. **Recall percentages here
are only comparable within a single scoring run.**)

## `yasserrmd/Nanonets-OCR2-3B` — 1164 scored runs, a real result

| variant | recall |
|---|---|
| redchan+stretch14 | 46.2% |
| redchan | 45.3% |
| redchan+shearP8 | 45.2% |
| normal | 15.1% |
| greenchan / bluechan / inverted | 0.0% |

Genuinely competitive on the red-channel variants and it **reproduces the same physics** — the three
`redchan` variants on top, the three contrast-destroying variants at exactly zero. Below
`qwen2.5vl:7b` overall (25 of 38 truths found vs 33), but this is the first evidence that the
"there is no second competent vision model" conclusion may not survive contact with newer models.

## ⛔ TWO SWEEPS ARE VOID — instrument, not model

- **`qwen3-vl:4b`: 400 of 400 sampled runs returned an EMPTY `response`.** Every token went to the
  `thinking` field. 1,761 runs, zero usable data.
- **`benhaotang/Nanonets-OCR-s`: 391 of 400 empty**, 9 parsed cleanly (and those 9 did contain pins).

**Neither is a model verdict. Do not record either as a score.** This is the SAME defect as bug 11
in `ollama-worker-v7.py` — a turn whose real content sits in `thinking` while `response` is empty —
recurring independently in `gc-sweep.py`, which I wrote without applying the lesson I had just
helped fix. The identical bug class, in two harnesses, on the same night.

**Fixed**: `gc-sweep.py` now falls back to `thinking`/`reasoning_content` when `response` is empty,
stores the reasoning, and records `used_thinking_fallback`. Both models are queued to re-run behind
the current sweep at `--num-predict 3000`, with their void results deleted first (they are `ok:true`
with empty content, so the resume logic would otherwise skip them permanently — the same
sticky-failure trap already fixed once tonight).

**Fifteenth instrument-first instance today.** The lesson is not "check the harness" — it is that a
defect found in one harness must be looked for in every other harness doing the same job.

---

## `deepseek-r1:32b` photo-upload — n=3 reached, and it is STABLE

The agenda demanded n≥3 before any statement about this model was safe. Three rounds now agree:

| round | exit | verify | files | iters | duration |
|---|---|---|---|---|---|
| v6 | 0 | — | 0 | 15 | — |
| v6.1 | 0 | yes | 0 | 7 | 791s |
| **v7 r1** | **0** | **yes** | **0** | **7** | **625s** |

Classified `inert`: **zero writes, and it never saw `OrderAttachments` at all.** `verify_passed=yes`
is false-pass shape #1 — `npm run build` succeeding on a tree nobody touched.

**Statement now safe to make**: on this task `deepseek-r1:32b` reliably does nothing. Not
destructive (that was v5, once), not wrong — *inert*. The earlier characterisation "spans
destructive / inert / inert" resolves at n=4 to one destructive outlier followed by three
consecutive identical inert runs.

This also validates the `inert` outcome class Fable required. Under the old scoring this row reads
`exit=0, verify_passed=yes` and looks like a pass; it is a model that read seven times and wrote
nothing.

---

# ⚠️ TWO INSTRUMENT DEFECTS FOUND WHILE SCORING v7 — both fixed, both mine

## 1. The predictive context guard fired at 32% actual usage

`qwen2.5-coder:14b` / photo / r1 was stopped with `context_ceiling` at **10,346 / 32,768 (32%)**
because the estimator predicted the next prompt at ~31,874 — a +21,500-token jump that did not exist.

Cause: the estimate used `len(json.dumps(delta)) // 3`. `json.dumps` escapes every newline and
quote, inflating a code-heavy delta by 20-40%, and 3 chars/token is aggressive on top of that. Fable
predicted the worst case was "stopping slightly early"; stopping at 32% is not slightly.

**Fixed**: estimate from raw content length at 3.5 chars/token, and require BOTH a high estimate AND
a measurement already past 50% of `num_ctx`. A single huge delta from a low base is far more likely
an estimation artifact than a real overflow, and the reactive check still catches the genuine case.

**Row `qwen2.5-coder:14b` photo r1 is VOID** — an instrument outcome, not a model outcome. Reps 2
and 3 resample the cell on the fixed guard.

A guard built to stop losing measurements destroyed one on its first live firing. Worth remembering
before adding the next safety net.

## 2. `score-v7-evidence.py` was blind to every manual-tools model

Models run with `--manual-tools` (the whole deepseek family, MFDoom) produce transcripts with **no
structured `tool_calls` at all** — the call is JSON text inside `content`, and results come back as
`role: "user"`, not `role: "tool"`. The scorer read only `tool_calls` and `role: "tool"`, so it saw
zero activity and classified those runs **`inert`** while they were writing files the whole time.

`deepseek-r1:32b` / clamshell was scored `inert` when its diff shows `Package.swift` modified,
`main.swift` modified, and `Sources/ConfirmationBridge/ConfirmationBridge.swift` created.

**Note the irony**: Fable's rule "read JSON transcripts only, never console logs" is correct about
truncation, but blind here — the console log *does* record manual-tool calls, because the worker
logs them itself. **Neither source alone is sufficient.** The scorer now parses tool calls from
assistant `content` as well, and accepts `role: "user"` as a result channel.

## Scorer defect count: five, before a single verdict rested on it

1. clamshell "misfiled" flagged correct behaviour (filing under an existing target with no new
   target declared)
2. `unfinished (cap)` was a blanket override that would have relabelled genuine exploration failures
3. blind to manual-tools models (above)
4. evidence/failure-mode conflated, so any failed clamshell build with prior evidence read as
   "transfer failure" — including `deepseek-r1:32b`, which had *recovered* from the layout error and
   died on `no such module`
5. failure-mode returned whichever pattern was checked first rather than what killed the LAST build,
   scoring a run on an error it had already fixed

**Sixth grader on this project to be wrong on first contact with real data. The pattern is now
without exception, across two people and two harnesses.** Treat every scoring column as wrong until
it has been read against a transcript by hand.

## Current v7 classifications (7 of 30 runs)

```
qwen3.8:27b-q8_0     photo  r1  correct              touched target
qwen3.8:27b-q8_0     clam   r1  unfinished (cap)     failed: invented-api; cycling at the cap
qwen3-14b-agentic    photo  r1  exploration failure  built elsewhere
qwen3-14b-agentic    clam   r1  transfer failure
deepseek-r1:32b      photo  r1  inert                never saw target, wrote nothing
deepseek-r1:32b      clam   r1  transfer failure     recovered layout, died on wiring
qwen2.5-coder:14b    photo  r1  VOID                 instrument (guard false positive)
```

---

# ⛔ INCIDENT — Unraid unreachable from ~03:08, 2026-08-23

**Symptoms**: `192.0.2.82:11434` stopped answering mid-sweep; port 11434 closed; **the host does not
respond to ping at all (100% packet loss)**. `resell-tracker` on `192.0.2.201:3000` is also
unreachable. This is the whole box, not one container.

**This also explains the Obsidian MCP disconnect** at ~01:45 — the vault container runs on Unraid.
The two events are ~80 minutes apart, so the container may have failed before the host did, but the
common cause is now the obvious suspect.

**No SSH/Docker access to Unraid (standing constraint), so I cannot diagnose or restart it.**
Needs the owner.

**Action taken:**
- Vision sweeps stopped rather than left burning through the roster recording failures.
- **56 failed results deleted** so they retry on resume (an `ok:false` file would otherwise be
  skipped forever by the resume logic — a trap already fixed once tonight).
- `scripts/RESUME-AFTER-UNRAID.sh` armed: polls `/api/version` every 60s and **auto-resumes the
  sweeps the moment the host returns**. No manual step needed.

**Not affected**: the v7 bake-off runs entirely on the Mac Studio and is still going. That workstream
— the Fable-gated one — is unharmed.

**Vision state at the stop:**

| model | runs | status |
|---|---|---|
| `qwen2.5vl:7b` | 1764 | complete, scored |
| `benhaotang/Nanonets-OCR-s` | 1764 | VOID (empty-response defect), queued to re-run |
| `qwen3-vl:4b` | 1761 | VOID (empty-response defect), queued to re-run |
| `yasserrmd/Nanonets-OCR2-3B` | ~1419 | partial, scored so far: 46.2% top variant |
| `granite3.2-vision:2b` | 0 | queued (re-test of a pre-fix rejection) |
| `minicpm-v:8b` | 0 | queued (re-test of a pre-fix rejection) |

**Deliberately did NOT wake the owner.** Nothing is lost — every sweep is resume-safe, the auto-resume is
armed, and the priority workstream (v7) is unaffected. Waking someone at 03:30 for a resumable
overnight job that will restart itself is the wrong trade.

---

## ★ The guard just cast doubt on the v6.1 rows at 32k

`qwen3-coder-next:q4_K_M` / photo / r1 stopped at `context_ceiling` after **8 iterations**:
measured **29,451 / 32,768 (90%)**, next prompt predicted ~31,321. This one is legitimate — the
reactive check would have fired too, and it is nothing like the 32%-usage false positive fixed
earlier.

**In v6.1 the same model, same task, same 32,768 context ran 31 iterations for 3,494s.** It cannot
have done that without exceeding the context window — so it spent most of that run on
**server-side-truncated context**, silently, with no signal in the CSV. It produced `exit=2,
files=1` and was scored as a model result.

**Implication for the existing dataset**: any v6.1 row where a model ran long at 32,768 on the
photo-upload task may have been measuring a model reading a silently truncated conversation. The
worst case is not a crash — it is a row that looks fine and means nothing. `qwen2.5-coder:14b`
(31 iterations, 32,768, 18 writes) is the other obvious candidate.

**This is what the guard was for**, and it is a stronger justification than the crash it was built
to prevent. The crash was visible; this was not.

### Deliberately NOT changing qwen3-coder-next's context mid-round

The obvious move is to raise it to 65,536 like `qwen3.8`. I am not doing that:

1. Changing a parameter between repeats destroys within-round comparability — the exact property
   n=3 exists to provide.
2. "Cannot complete photo-upload within 32k" **is itself a result**, and a more useful one than a
   half-comparable set of runs.
3. The roster and its parameters were Fable's sign-off, and the owner's instruction was to dispatch on
   that sign-off. Quietly re-specifying it mid-flight is not mine to do.

Queued for the next Fable pass: **per-model context sizing is a measurement input, not a detail.**
`qwen3.8` needed 64k (photo 72%, clamshell only 30%), `qwen3-coder-next` exceeds 32k by iteration 8,
and the deepseeks run at 131k. A round that fixes context per model is comparing models on different
instruments.

---

# v7 REP 1 COMPLETE — all 10 runs (2026-08-23 ~04:05)

```
qwen3.8:27b-q8_0        photo  correct              touched the real target
qwen3.8:27b-q8_0        clam   unfinished (cap)     invented-api, still cycling at 31
qwen3-14b-agentic       photo  exploration failure  never saw the target
qwen3-14b-agentic       clam   transfer failure
deepseek-r1:32b         photo  inert                never looked, wrote nothing
deepseek-r1:32b         clam   transfer failure     recovered layout, died on wiring
qwen2.5-coder:14b       photo  VOID                 guard false positive (fixed)
qwen2.5-coder:14b       clam   transfer failure     misfiled; builds=0 writes=9
qwen3-coder-next        photo  VOID                 context ceiling at 32k, iteration 8
qwen3-coder-next        clam   unfinished (cap)     cycling at 31
```

| class | count |
|---|---|
| transfer failure | 3 |
| unfinished (cap) | 2 |
| **context_ceiling (VOID)** | **2** |
| inert | 1 |
| exploration failure | 1 |
| correct | 1 |

## What this does and does NOT say

**Does not say**: "transfer failure dominates". I told Fable exploration failure was ~75-80%; rep 1
shows transfer outnumbering exploration 3:1. **These are not comparable samples.** The v7 roster was
*deliberately enriched* with transfer candidates — `qwen3-coder-next` and `qwen3-14b-agentic` were
selected precisely because they had failed post-evidence. The v6.1 figure came from the whole field.
Reading rep 1 as an inversion of the v6.1 finding would repeat exactly the overstatement Fable
already corrected once.

**Does say**, and these are safe:
1. **All six outcome classes occur.** The taxonomy discriminates rather than relabelling — the thing
   v7 existed to establish.
2. **Only 1 of 10 runs was correct**, and it is the model that has produced every good result so far.
3. **20% of the round was lost to the instrument** (2 context_ceiling rows). One was a guard false
   positive since fixed; the other is a genuine 32k limit. That loss rate is the strongest argument
   for per-model context sizing in v8.
4. **`builds=0 writes=9`** for `qwen2.5-coder:14b` on clamshell: nine files written, not one build
   run. The "never verifies its own work" trait documented for the 7B is now confirmed at 14B.

## Not scored yet

Reps 2 and 3 (20 runs) are what turn any of these into per-model statements. Nothing here should be
written into the model guide until then — the whole point of n=3 is that `deepseek-r1:32b` spans
destructive/inert/inert and `qwen3.8` clamshell has already gone pass → not-finished across two runs.

---

## Rep 2 opened with a TIMEOUT, and the cause is host memory — not the model

`qwen3.8:27b-q8_0` / photo / r2: `exit=143`, killed at the 1800s wall after 20 iterations.

| run | iters | duration | s/iteration |
|---|---|---|---|
| prelim | 31 | 1572s | 51 |
| r1 | 31 | 1737s | 56 |
| **r2** | **20 (killed)** | **1800s** | **90** |

Same model, same config, same machine — **60% slower per iteration**. The Mac Studio is now paging:
**swap grew from 5.9 GB to 12.0 GB, free memory 19%**. A 30 GB model with a 64k KV cache is enough to
push this box into swap once it has been cycling models for hours.

**So the 64k context Fable required to measure this cell may itself be what prevents measuring it.**
Rep 1 got the measurement; rep 2 was killed by the interaction of context size and wall-clock budget.
Those two parameters are not independent, and nothing in the design treats them as related.

Queued for the next Fable pass, alongside per-model context sizing: **`num_ctx` and `--timeout`
interact through host memory. A context large enough to avoid truncation can be large enough to make
the run too slow to finish.**

### Two instrument fixes from this run

1. **`timeout (wall)` is now its own outcome class** in the scorer (Fable's instruction: score
   timeouts as their own class, not as failures). A wall-clock kill says as much about host load as
   about the model.
2. **Timed-out runs were losing their transcript.** The driver scrapes the transcript path from a log
   line the worker printed only at the END, so any SIGTERM'd run recorded `transcript,none` — even
   though crash-safe writing had the file on disk from iteration 1. The worker now logs the path
   *before* the loop. The affected row was repaired by matching the transcript on disk
   (`20260823T110925Z.json`, 19 iterations, 65 messages) — the crash-safe writing Fable required is
   what made recovery possible at all.

---

## ⛔ CORRECTION — "the only genuine complete pass does not reproduce" was overstated

I gave that section a strong headline at 02:30 on n=2. At n=3 it reads differently:

| run | exit | verify | files | iters | class |
|---|---|---|---|---|---|
| prelim | 0 | **PASSED** | 3 | 24 | correct |
| rep 1 | 1 | failed | 2 | 31 | unfinished (cap) |
| **rep 2** | **0** | **PASSED** | **3** | **24** | **correct** |

**2 of 3 pass**, and rep 2 matches the prelim run exactly on files and iterations. Both passes filed
under `Sources/Clamshell/Auth/` — joining the existing target, which is correct — and produced the
same three-file shape.

**The one failure was `unfinished (cap)`**: still cycling build/edit at iteration 31, having already
self-corrected the invented `P256.SigningKey` from the compiler error. Not a capability ceiling — a
budget ceiling.

**Revised statement**: `qwen3.8:27b-q8_0` completes the clamshell task reliably, roughly 2 runs in 3,
and the third run fails by running out of iterations rather than by getting it wrong. That is a
materially different and much better verdict than "the headline result does not reproduce".

**What I got wrong and why**: I published an alarming headline from a single contradicting run,
having spent the whole night insisting that single runs are not results. The rule applies to
disconfirming evidence exactly as much as to confirming evidence — arguably more, because a
surprising negative feels like a discovery.

---

## ★ Transfer vs exploration is a (MODEL, TASK) property — not a model trait

`qwen3-14b-agentic`, both reps, both tasks:

| task | rep 1 | rep 2 |
|---|---|---|
| clamshell | transfer failure (evid 7, act 12) | transfer failure (evid 9, act 14) |
| photo-upload | exploration failure (never saw target) | exploration failure (never saw target) |

**Perfectly consistent within a task, opposite across tasks.** The same model reliably gathers the
evidence on one task and reliably fails to on the other.

This qualifies how the taxonomy should be used. Fable's framing — and mine — leaned toward
"model X is a transfer-failure model". That is not supported. What reproduces is the *cell*:
(model, task) → class. A model guide entry saying "`qwen3-14b-agentic` fails to transfer" would be
wrong half the time.

Plausible mechanism, untested: clamshell's evidence is small and central (`Package.swift`, one
directory listing), so it gets read; photo-upload's evidence is one file among ~40 components in a
large Next.js app, so it does not. **If that is right, exploration failure is a function of
codebase size and evidence salience, not of the model's willingness to look** — which would make it
far more addressable than a model defect, e.g. by a repo map in the prompt.

## CryptoKit's ECDSA API is a reliable trap — three distinct inventions across three models

| model | invented identifier | real name |
|---|---|---|
| `deepseek-r1:32b` (v5/v6) | `P256.KeyPair` | — |
| `qwen3.8:27b-q8_0` (v7 r1) | `P256.SigningKey`, `P256.VerifyingKey` | `P256.Signing.PrivateKey` / `.PublicKey` |
| `qwen3-14b-agentic` (v7 r2) | `P256.Signing.Signature` | `P256.Signing.ECDSASignature` |

Every one is the right *concept* with a plausible non-existent *token*, and each model invents a
DIFFERENT wrong name — which is exactly why a grader that enumerates expected mistakes cannot catch
this (documented in the agenda's P1 section, and confirmed again here).

**Practical consequence for real dispatch work**: do not send a CryptoKit signing task to a local
model without supplying the exact API surface. `qwen3.8` recovered from its invention via the
compiler error; the others did not. Supplying the `.swiftinterface` excerpt up front would likely
remove this failure class entirely — worth testing as a v8 variant.

---

## ✅ SAFE TO WRITE INTO THE GUIDE — `deepseek-r1:32b` is inert on photo-upload, n=4

| run | exit | files | iterations | duration |
|---|---|---|---|---|
| v6 | 0 | 0 | 15 | — |
| v6.1 | 0 | 0 | 7 | 791s |
| v7 r1 | 0 | 0 | 7 | 625s |
| v7 r2 | 2 | **0** | **31** | 893s |

**Outcome perfectly stable across four runs; effort varies 4×.** Rep 2 consumed the entire iteration
budget with `builds=0 writes=0` — 31 turns of doing nothing at all. It never saw
`components/OrderAttachments.tsx` in any run.

This is the first per-model statement in the project that meets the agenda's own n≥3 bar, and it is
unambiguous: **on this task the model does not act.** Not destructively, not incorrectly — it reads
and stops. The v5 destructive run (deleted `app/layout.tsx`, truncated `app/page.tsx` 266→10 lines)
stands as a single outlier against four inert runs.

**Also note what `exit_code` says about these rows**: three read `exit=0` and one reads `exit=2`, and
under the old scoring the `exit=0` rows with `verify_passed=yes` look like passes. They are a model
that wrote nothing while `npm run build` succeeded on an untouched tree. The `inert` class exists
precisely to stop that from being read as success — and this is the cleanest demonstration of why
the class was needed.

---

## ⛔ CORRECTION — "the (model, task) CELL reproduces" is also too strong

I wrote that an hour ago on the strength of `qwen3-14b-agentic` being perfectly consistent across
both reps on both tasks. `deepseek-r1:32b` on clamshell breaks it immediately:

| rep | class | evidence |
|---|---|---|
| 1 | transfer failure | saw layout at msg 7, wrote at 18 |
| 2 | **exploration failure** | never gathered it |

Same model, same task, same harness — **different outcome class**.

### What is actually supportable

| claim | status |
|---|---|
| `deepseek-r1:32b` writes nothing on photo-upload | **SAFE** — n=4, zero files every time |
| `qwen3-14b-agentic` is exploration-fail on photo, transfer-fail on clamshell | holds at n=2, not yet n=3 |
| `qwen3.8:27b-q8_0` passes clamshell ~2 runs in 3 | holds at n=3 |
| "the (model, task) cell reproduces" as a general rule | **FALSE** |

**Class stability is itself model-dependent.** Some cells are tight (`deepseek` photo: 4/4 identical
outcome), some are not (`deepseek` clamshell: 2 classes in 2 runs). A guide that reports one class
per cell would be fabricating precision for at least some cells; those need a distribution, not a
label.

### The pattern in my own errors, stated plainly

This is the third time tonight I have generalised from the first consistent thing I saw:
"transfer failure is concentrated in the strongest model" (falsified by Fable), "the headline pass
does not reproduce" (falsified at n=3), and now "the cell reproduces" (falsified within the hour).

Each time the correction came from *more data arriving*, not from re-reading what I had. The lesson
is not "be more careful" — it is **do not publish a generalisation while the run that would test it
is still executing.** Nothing here needed to be claimed before v7 finished; the claims cost nothing
to defer and repeatedly cost accuracy to make early.

---

## Guard fix verified on the cell that exposed it — and it confirms the v6.1 doubt

`qwen2.5-coder:14b` / photo, same cell, before and after the estimator fix:

| rep | measured at stop | predicted next | verdict |
|---|---|---|---|
| 1 (pre-fix) | **10,346 / 32,768 (32%)** | 31,874 | **false positive** — run destroyed |
| 2 (post-fix) | **28,499 / 32,768 (87%)** | 46,399 | **legitimate** — genuinely out of room |

The fix stopped it firing when it shouldn't while preserving it firing when it should. That is the
discriminating test, and it is the one my original `--num-ctx 1300` test could not have run.

### The retroactive doubt about v6.1 is no longer a hypothesis

Two of five models in the v7 roster **cannot complete photo-upload within 32,768 tokens**:
`qwen3-coder-next` exceeds it by iteration 8, `qwen2.5-coder:14b` by iteration 15.

**In v6.1, `qwen2.5-coder:14b` ran 31 iterations at 32,768 on this task and wrote 18 files.** It
cannot have done that inside the window. That row was produced by a model reading a silently
truncated conversation for roughly half its run, and it was scored as a model result.

**Confirmed, not suspected**: v6.1 photo-upload rows at 32k from long-running models are unreliable.
The affected rows are `qwen2.5-coder:14b` (31 iters) and `qwen3-coder-next` (31 iters). This is not
recoverable by rescoring — the model was reading different context than the transcript implies.

**For the next Fable pass**: the photo-upload task needs ≥64k for most of this roster, or a smaller
repository. Running it at 32k does not produce a hard failure — it produces plausible rows that mean
nothing, which is the worst possible outcome for a benchmark.

---

## ⛔ CORRECTION — "`qwen2.5-coder:14b` never verifies its own work" is FALSE

I wrote that at ~03:30 from rep 1 (`builds=0 writes=9`), calling it confirmation at 14B of the trait
documented for the 7B. Rep 2, same model, same task:

| rep | `swift build` calls | writes | class |
|---|---|---|---|
| 1 | **0** | 9 | transfer failure |
| 2 | **31** | 3 | unfinished (cap) |

**Zero builds versus thirty-one.** The behaviour is not a trait, it is a coin flip — and the run that
built 31 times is the one that failed by running out of budget while cycling.

This actually **matches** what the agenda already records for the 7B under v7f: given one added
instruction, `b1` made 13 `swift build` calls and `b2` made 12, where earlier runs made zero. The
existing note calls this "a habit, not a capability". Rep 1 vs rep 2 here shows it can flip **with no
prompt change at all** — so even "habit" overstates the stability.

Also note the outcome class differs across these two reps (transfer failure vs unfinished), which is
the second cell after `deepseek-r1:32b` clamshell to be class-unstable at n=2.

### Fourth single-run generalisation of the night, corrected

The others: transfer failure concentrated in the strongest model; the headline pass not reproducing;
the (model, task) cell reproducing. Every one was published while the run that would test it was
still queued, and every one was corrected by data arriving rather than by re-reading.

**Standing rule for the rest of this round, and it is not a resolution to try harder**: no claim
about a model gets written until every rep for that cell has landed. The scorer prints the reps; if
the count is below 3, the only honest output is the table.

---

# v8 DESIGN — drafted 2026-08-23 ~05:20, for the next Fable pass

Written from what v7 established, while v7 is still running. **No model claims here** — this is
instrument and task design only, which is the part that does not need the remaining reps.

## 1. Fix the photo-upload task text (top item)

Current text says *"follow its existing patterns rather than inventing a new style"* AND
*"Implement this as a real, working feature"* AND asks which files you *created*. The feature already
exists at baseline. A model that finds `components/OrderAttachments.tsx` and builds anyway may be
**obeying its instructions**, so "built a duplicate" cannot be scored as comprehension failure.

**Add one line**: *"If this feature already exists in some form, say so explicitly and extend the
existing implementation rather than reimplementing it."*

That converts an ambiguous instruction into a clean discriminator, and it is the single cheapest
change available.

## 2. Size context per model, and treat it as a measurement input

v7 evidence: `qwen3.8` needs 64k for photo but peaks at 30% on clamshell; `qwen3-coder-next` exceeds
32k by iteration 8; `qwen2.5-coder:14b` by iteration 15; the deepseeks run at 131k throughout.

Running photo-upload at 32k does not fail loudly — it produces **plausible rows that mean nothing**,
which is worse. Two confirmed v6.1 rows are unreliable for this reason and cannot be rescored.

**Proposal**: every model runs photo-upload at ≥64k, or the task moves to a smaller repository. State
the context per cell in the results, because comparing models at different context sizes is comparing
them on different instruments.

## 3. `num_ctx` and `--timeout` interact — budget them together

`qwen3.8` at 64k pushed the Studio to 12 GB of swap; iterations slowed 56s → 90s and rep 2 was killed
at the 1800s wall. **The context size required to measure a cell can be what makes the run too slow
to finish.** Neither parameter is safe to choose alone.

## 4. Test the evidence-salience hypothesis

`qwen3-14b-agentic` is exploration-fail on photo (one file among ~40 components in a large Next.js
app) and transfer-fail on clamshell (evidence is one manifest plus one directory listing).

**Hypothesis**: exploration failure tracks codebase size and evidence salience, not the model's
willingness to look. **Cheap test**: same task, same model, with a repo map (a `list_files -R`
digest) prepended to the prompt. If exploration failures collapse, it is an addressable prompt
problem rather than a model defect — a much better outcome than the current framing.

## 5. Supply the API surface for the CryptoKit task

Three models invented three *different* wrong identifiers (`P256.KeyPair`, `P256.SigningKey`,
`P256.Signing.Signature`). Only `qwen3.8` recovered, via the compiler error.

**Variant to run**: clamshell with the relevant `.swiftinterface` excerpt supplied. If it removes the
failure class, the finding for real dispatch work is concrete — supply the API surface, do not expect
recall.

## 6. Instrument work carried forward

- `max_iters` is a **scoring variable**. `exit=1` conflates "wrong" with "not finished"; the
  `unfinished (cap)` class must survive into v8, and the cap itself should be justified rather than
  inherited.
- Report **distributions per cell, not labels.** Two cells were class-unstable at n=2
  (`deepseek-r1:32b` clamshell, `qwen2.5-coder:14b` clamshell). A single label per cell fabricates
  precision.
- Keep per-repeat logs AND diffs. The one finding that most changed the picture — two runs identical
  on every CSV column and materially different in the diff — is invisible without them.

---

## ⚠️ The central cell has now been destroyed THREE different ways

`qwen3.8:27b-q8_0` / photo-upload — the cell Fable's sign-off condition existed to measure:

| round | outcome | destroyed by |
|---|---|---|
| v6 | no data | **bug 11** (thinking-only turn converged the run) |
| v6.1 | no data | **context overflow** at 32k → truncation → template rejection → HTTP 500 |
| v7 prelim | 31 iters, 5 files | measured ✅ |
| v7 rep 1 | 31 iters, 5 files | measured ✅ |
| v7 rep 2 | 20 iters | **1800s wall clock** |
| v7 rep 3 | 23 iters | **1800s wall clock** |

Each fix exposed the next constraint. The 64k context that cured the overflow pushed the Studio to
**12 GB of swap (23% free)**, slowing iterations from ~56s to ~90s until the wall killed the run.
**The parameter required to make the cell measurable is the parameter making it unmeasurable.**

This is the concrete form of the v8 item "`num_ctx` and `--timeout` interact through host memory".
It is not a theoretical concern — it has now cost two of four attempts at the round's most important
measurement.

### Recovery queued, without touching v7

`recover-qwen38-photo.sh` waits for v7 to finish, then re-runs this cell twice with the **same
config and a 3600s wall** instead of 1800s. Results land in `results-v7recovery.csv` as reps 4 and 5.

Deliberately structured so v7 is **not re-specified after the fact**: the v7 rows keep their timeouts
and their `timeout (wall)` classification, and the recovery is a separate, clearly-labelled dataset.
Changing a round's parameters retroactively to get a better answer is how a benchmark stops meaning
anything.

---

## ★ METHODOLOGICAL DEFECT — rep number is confounded with host degradation

`qwen3.8:27b-q8_0` / clamshell, identical config every time:

| run | iters | duration | **seconds per iteration** |
|---|---|---|---|
| prelim | 24 | 1056s | **44** |
| rep 1 | 31 | 1303s | **42** |
| rep 2 | 24 | 1271s | **53** |
| rep 3 | 21 | 1800s (killed) | **86** |

**Monotonic slowdown, roughly 2× from rep 1 to rep 3.** Clamshell peaks at only 30% of its context
window, so this is not context growth — it is the host. The Mac Studio has been cycling 30-52 GB
models for six hours on a five-day uptime; swap has been between 6 and 12 GB all night and macOS has
resized the swap file at least twice.

### Why this matters more than the individual timeouts

n=3 exists to separate run-to-run variance from model properties. **If rep 3 is systematically slower
than rep 1, then rep number is not a repeat — it is a treatment.** Every comparison that touches
iteration count, duration, or "did it finish before the cap" is contaminated across reps, and those
are exactly the quantities the `unfinished (cap)` and `timeout (wall)` classes depend on.

Both `qwen3.8` cells were lost to timeouts in rep 3. That is not a coincidence, and it is not a model
result.

### Not intervening mid-round, deliberately

Restarting Ollama now would give the remaining rep-3 models a fresher host than the two that already
ran in this rep — making rep 3 internally inconsistent on top of being externally incomparable. The
contamination is already in the data; the honest move is to record it, not to half-fix it.

### v8 additions

1. **Record host telemetry per run** — swap used, free memory, load average, at run start. Then this
   confound is visible in the data instead of being discovered by noticing a pattern in durations.
2. **Restart the inference server between reps** (not between models), so every rep begins from
   comparable host state.
3. **Randomise or interleave model order across reps.** Fixed order means the last model in every rep
   always runs on the most degraded host — a systematic bias, not noise.
4. Consider a **wall-clock budget scaled to observed iteration rate**, rather than a fixed 1800s that
   means something different at 42s/iteration than at 86s/iteration.
