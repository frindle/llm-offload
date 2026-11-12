# v8 design — proposal for Fable go/no-go

**Drafted 2026-08-23 after v7 completed 30/30.** Not dispatched. Supersedes the v8 sketch inside
[[Ollama-v7-Test-Design]].

---

## The v7 result that drives everything

**19 of 30 runs usable. 11 lost to the instrument (37%).**

| cell | usable | outcomes |
|---|---|---|
| `qwen3-14b-agentic` / photo | 3/3 | exploration ×3 — the only fully stable cell |
| `qwen3-14b-agentic` / clam | 3/3 | transfer, transfer, exploration |
| `deepseek-r1:32b` / clam | 3/3 | transfer, exploration, transfer |
| `deepseek-r1:32b` / photo | 3/3 | inert, inert, exploration |
| `qwen2.5-coder:14b` / clam | 2/3 | transfer, unfinished, ceiling |
| `qwen3-coder-next` / clam | 2/3 | unfinished, inert, no-transcript |
| `qwen3.8:27b-q8_0` / clam | 2/3 | unfinished, **correct**, timeout |
| `qwen3.8:27b-q8_0` / photo | 1/3 | **correct**, timeout, timeout |
| **`qwen2.5-coder:14b` / photo** | **0/3** | ceiling ×3 |
| **`qwen3-coder-next` / photo** | **0/3** | ceiling ×3 |

Class counts over the 19 usable: exploration 6, transfer 5, inert 3, unfinished 3, **correct 2**.

**Two cells produced zero data from six runs.** Both are photo-upload at 32k. That is not a model
result, it is a configuration that cannot measure half the roster.

## What v8 must fix, in order of damage caused

### 1. Context is sized wrong, and it is the single biggest loss (7 of 11 voids)

`qwen3-coder-next` exceeds 32k by iteration 8; `qwen2.5-coder:14b` by iteration 15. `qwen3.8` peaks
at 72% of **64k** on photo but only 30% on clamshell.

**Proposal**: photo-upload runs at **65536 for every model**. Report the context per row so nobody
later compares models measured at different sizes. If a model cannot hold 64k on this hardware, it
is excluded from that task with the reason recorded — an honest exclusion beats a void row.

**Alternative worth Fable's opinion**: replace resell-tracker with a smaller repo for the
comprehension task. That fixes the cause rather than raising the ceiling, but forfeits comparability
with v1–v7.

### 2. The wall clock destroyed the best model's data (3 of 11 voids)

`qwen3.8` lost 3 of its 6 runs to the 1800s timeout — including both rep-3 cells. It is also the
only model that produced a `correct` outcome, so the timeout is preferentially destroying the most
informative rows.

**Proposal**: raise to 3600s for models above ~25GB resident, or scale the budget to observed
iteration rate. **`num_ctx` and `--timeout` are not independent** — the 64k context that made
qwen3.8's photo cell measurable is what pushed the host into 12GB of swap and made it too slow to
finish.

### 3. Rep number is confounded with host degradation

`qwen3.8`/clamshell, identical config: 44 → 42 → 53 → **86 seconds per iteration** across prelim,
r1, r2, r3. Clamshell peaks at 30% context, so this is the host, not context growth. The Mac Studio
had been cycling 30–52GB models for hours.

**Proposal**: restart the inference server between reps (not between models); record swap, free
memory and load average per run so the confound is visible in the data rather than inferred from
durations; randomise model order per rep so the last slot is not always the most degraded host.

### 4. The task text confounds the comprehension measure

`TASK_RESELL` says *"follow its existing patterns rather than inventing a new style"* but also
**"Implement this as a real, working feature"** and asks which files you *created*. The feature
already exists at baseline. A model that finds `OrderAttachments.tsx` and builds anyway may be
obeying instructions.

**Proposal**: add one line — *"If this feature already exists in some form, say so explicitly and
extend the existing implementation rather than reimplementing it."* Cheapest change available, and
it converts an ambiguous instruction into a clean discriminator.

### 5. n=3 is a floor, not a sufficiency

Cells that looked stable at n=2 were not at n=3: `deepseek-r1:32b`/photo went inert, inert,
**exploration**; `qwen3-14b-agentic`/clam went transfer, transfer, **exploration**;
`deepseek-r1:32b`/clam went transfer, **exploration**, transfer.

**Only one cell in the entire round was unanimous** (`qwen3-14b-agentic`/photo).

**Proposal**: report distributions per cell, never a single label. A guide entry saying
"`qwen3-14b-agentic` fails to transfer" would be wrong half the time. Consider n=5 for cells that
split at n=3, rather than raising n globally.

## Process constraint, from repeated failure

Across this session I generalised early and was corrected by later data **five times**: transfer
failure "concentrated in the most capable model" (falsified by Fable), "the headline pass does not
reproduce" (2/3 at n=3), "the (model,task) cell reproduces" (falsified within the hour),
"`qwen2.5-coder:14b` never verifies its own work" (0 builds vs 31 across two reps), and
"`deepseek-r1:32b`/photo is stably inert at n=4" (rep 3 wrote a file).

Every correction came from data arriving, not from re-reading. **v8 rule: no per-model claim is
written until every rep for that cell has landed, and cells that split get a distribution, not a
label.**

## Deliberately NOT changing

Sampling, worktrees, the two tasks themselves, the outcome taxonomy, and the evidence-column
methodology. v7 established that the taxonomy discriminates — all six classes occurred. The problem
is that 37% of runs never reached the point where the taxonomy could apply.

---

# ⛔ THE 3600s TIMEOUT FIX DOES NOT WORK — measured, 2026-08-23 ~10:50

`qwen3.8:27b-q8_0` / photo-upload, recovery rep 4, **ctx 65536, timeout raised to 3600s**:

```
exit=143  timedout=TRUE  dur=3600s  files=0  iters=21
```

**It timed out again, at double the budget.** 21 iterations in 3600s = **171 s/iteration**, against
**56 s/iteration** for the same cell in v7 rep 1. Three times slower on an identical configuration.

## What this settles

My v8 proposal argued that raising the wall clock to 3600s would stop qwen3.8 losing runs. **That is
now falsified by direct measurement.** The wall clock was never the binding constraint — host
degradation is. A bigger budget just moves the failure later.

**Fable was right and I was wrong on the ordering**: its B3 (control host state per RUN) is the
actual fix, not a larger timeout. Its formulation — *the wall must never be the binding constraint;
`max_iters` is the budget; size the wall from FRESH-host iteration rate* — only holds if the host is
actually kept fresh. Without B3, no wall clock is large enough.

## Consequence for v8

- **Do not ship "raise the timeout" as a fix.** It is at best a safety margin.
- **B3 is now a hard blocker, not a refinement.** Per-run inference-server restart, or a swap-gated
  restart, must land before v8 dispatches.
- The qwen3.8 photo cell remains **unmeasured at n≥2** — destroyed four different ways across five
  attempts now: bug 11, context overflow, the 1800s wall (×2), and now the 3600s wall.
- Recovery rep 5 will likely fail the same way. **Let it run — a second data point on a bigger
  budget is exactly what makes this conclusion safe** rather than a single observation, which is the
  mistake I made five times today.

---

# ✅ v8 HARNESS BUILT — 2026-08-23 ~11:10, ready for Fable re-review

Files, all in `~/Desktop/GitHub Projects/`:

| file | role |
|---|---|
| `bakeoff-v8-lib.sh` | the harness — B3, ceiling split, two-phase verify, arms, telemetry |
| `bakeoff-v8-macstudio.sh` | base round, n=3, five models, randomised order |
| `bakeoff-v8-arms.sh` | the two restored experiment arms |
| `bakeoff-v8-hosttel.py` | host telemetry (swap / available / free / load) |
| `bakeoff-v8-apisurface.py` | brace-matched CryptoKit excerpt for the API-surface arm |

## B3 — host state is controlled per RUN

Unconditional `brew services restart ollama` before **every** run, then a
**readiness gate on available memory** — 40% of physical (26214 MB of 65536), polled
5s, bounded at 240s. If the gate is not met the run still dispatches but is
flagged `host_ready=no`, so a degraded start is *visible in the data* instead of
producing another unexplained 171 s/iteration row.

**The gate is on available memory, never on swap.** macOS does not shrink swap
files promptly when pages are freed, so a swap gate would block for minutes after
the memory was already back. Swap is recorded, not gated on. `available` =
free + inactive + speculative + purgeable; bare `free` reads ~74 MB on a busy
Studio and means nothing.

**Unconditional, not swap-gated.** A conditional restart makes host state a
function of run history again — which is the exact confound B3 exists to remove.

### Live confirmation of the thesis, measured during recovery rep 5

| time | swap used | available | note |
|---|---|---|---|
| 10:53 | 12354 MB | 5857 MB | the state that produced 171 s/iteration |
| 10:58 | 15259 MB | 3294 MB | still climbing mid-run |
| 11:09 | 14454 MB | 5839 MB | 74 MB genuinely free |

Physical RAM is 65536 MB. This is no longer inferred from durations — it is the
host state read directly while the slow run was happening.

## Roster — `num_ctx = min(65536, native)`, derived not typed

Native windows read live from `/api/show`, 2026-08-23:

| model | native | num_ctx | note |
|---|---|---|---|
| `qwen3.8:27b-q8_0` | 262144 | 65536 | |
| `qwen3-14b-agentic` | 40960 | **40960** | native-limited — "64k for all" would have run it 24k out of spec |
| `deepseek-r1:32b` | 131072 | 65536 | **lowered from v7's 131072** so models are comparable where native allows |
| `qwen2.5-coder:14b` | 32768 | **32768** | native-limited; **photo cell retired** |
| `qwen3-coder-next:q4_K_M` | 262144 | 65536 | |

`ctx_for()` derives it; the driver cannot pass a context by hand any more. That
is what allowed v7 to run two models past their own maximum. An unknown model
returns 0 and aborts rather than defaulting.

**`qwen2.5-coder:14b` photo is retired, not skipped.** Ceiling ×3 in v7 at 32768,
and 32768 *is* its native maximum — there is no larger window to retry at. Its
clamshell cell still runs.

## Ceiling split — the change that matters most for reading the CSV

v7 wrote `context_ceiling` for two events with opposite conclusions:

- **`config_ceiling`** — hit the ceiling of a window *we chose* (num_ctx < native).
  A knob to turn next round.
- **`native_ceiling`** — hit the model's own maximum (num_ctx >= native). Nothing
  to raise; the cell is settled.

Both `num_ctx` and `native_ctx` are columns, so no one can ever compare two models
measured at different sizes without seeing it.

## Wall clock — sized from fresh-host rate, and no longer load-bearing

v7 rep 1 was the freshest host of the round, so its rates are the basis:
qwen3.8 56 s/iter, qwen3-14b-agentic 49, deepseek-r1:32b 89, qwen3-coder-next 77,
qwen2.5-coder 7.

```
wall = 900s warmup budget + (fresh_rate × 30 iters × 1.8 safety)
```

The 900s is real, not padding: B3 restarts the server every run, so **every run
now pays a cold model load**, and the worker's `WARMUP_TIMEOUT_S` is 900.

**With B3 holding the host fresh these walls should never be reached. A
`timed_out=true` row in v8 is an instrument defect to investigate, not a model
outcome.** That is the whole lesson of the 3600s recovery run.

## warmup_s / loop_s split, and load_failed

Worker already carries them (exit 3 = `load_failed`, logs `warmup_s=N`); v8
surfaces both as columns. If the `warmup_s=` line is absent the run died *in*
warmup, so all elapsed time was warmup and none was work — v7 row 31 spent
~2710s failing to load and it read as 2710s of running.

## Verify is two-phase — clamshell now runs the self-test

It **cannot be one command**: preflight runs verify on the *pristine* tree, where
`ConfirmationBridge` does not exist by construction (the premise guard asserts
exactly that), so a single self-test verify would `ABORT_PREFLIGHT` every
clamshell run before dispatch.

- preflight: `swift build`
- post-run: `swift build && swift run Clamshell confirmation-bridge-selftest`

The subcommand name is now **given in the task text**. The harness has to know
what to run, and probing usage text would score *naming* rather than correctness —
a model that solved the crypto but named its subcommand differently would be
indistinguishable from one that never wired it up. The project convention is
already `<name>-selftest` (`stream-selftest`, `window-capture-selftest`,
`window-hide-selftest`, `window-at-cursor-selftest`), so naming it costs the model
nothing it was being measured on.

New `selftest` column separates **`missing`** (built, never wired the test) from
**`fail`** (wired it, crypto is wrong) from **`pass`**. v7 collapsed all three
into `verify_passed=no`.

## Randomised order

v7 ran the same five models in the same order every rep, so the last slot was
always the most degraded host — "rep 3" and "ran last" were the same variable.
v8 shuffles per rep, **seeded by rep**, so a reviewer can regenerate the exact
order that produced a CSV. An unseeded shuffle would make position unrecoverable
after the fact — the same class of mistake as not recording context per row.

## Task text — confound #4 fixed

`TASK_RESELL` gains: *"If this feature already exists in some form, say so
explicitly and extend the existing implementation rather than reimplementing it."*
Saying so is now the correct answer, and `files=0` with a correct report is a
**pass**, not a null row.

## The two restored arms (v7 open questions 4 and 5)

Separate driver, deliberately — the base round is what GO/NO-GO rests on and must
not be blocked behind ~6 more hours. Both write to the same `results-v8.csv`,
distinguished by the `arm` column, because the comparison is base-vs-arm on the
same cell at the same context on the same controlled host.

- **`repomap`** — salience. Repo map (`git ls-files` digest, 317 lines, generated
  from the worktree at dispatch time) prepended to the photo task. Subjects
  `qwen3-14b-agentic`, `deepseek-r1:32b`; **`qwen3.8` as control** — if the map
  "improves" it too, the arm is measuring general prompt-strengthening, not
  salience.
- **`apisurface`** — recall vs reasoning. Real P256 surface excerpted verbatim
  from the SDK's own `.swiftinterface`, prepended to the clamshell task.

### ⚠ The API-surface extractor was broken on first build — caught by smoke test

The awk version bled **twenty lines of `P256.KeyAgreement`** (the wrong enum,
truncated mid-declaration) into the excerpt, and stripped the sign/verify
functions out of their owning `extension CryptoKit.P256.Signing.PrivateKey {`
scope — leaving four bare `public func signature(...)` lines with nothing saying
which type they hang off.

**That second defect would have invalidated the arm.** It exists because three
models did not know *where* the call lives (`P256.KeyPair`, `P256.SigningKey`,
`P256.Signing.Signature` — three different wrong answers). An excerpt showing the
call without its owning type reproduces exactly that ambiguity, so the arm would
have measured whether a model can guess from a bad handout.

Rewritten as brace-matched extraction in `bakeoff-v8-apisurface.py`. **Verified by
compiling against it** — a Swift snippet using only the identifiers the excerpt
advertises builds and runs: `verify=true der-roundtrip=true tamper-rejected=true`.

## Smoke tests run (no dispatch — recovery rep 5 was live, so no server restart)

- syntax clean on all three shell files
- roster derivation correct for all five models; unknown model → 0 → abort
- readiness gate 26214 MB of 65536 MB
- shuffle deterministic per seed, different across seeds
- repo map 317 lines, contains `OrderAttachments.tsx`, excludes `app/generated/`,
  lockfiles and `node_modules`
- API excerpt: 0 KeyAgreement bleed, all 4 extension headers intact, braces
  balanced 22/22, compiles and runs
- **CSV alignment: header 24 / abort 24 / success 24 fields**

## Not yet done

- No dispatch. Recovery rep 5 is still running and B3 restarts the server, which
  would kill it.
- **`deepseek-r1:32b` drops from 131072 → 65536.** Deliberate and flagged for
  Fable: it buys cross-model comparability and lowers memory pressure, but it is
  a treatment change, not purely an instrument change, and it is the one place v8
  breaks its own "change the measurement, not the treatment" rule.

---

# ⛔ CORRECTION — "THE 3600s TIMEOUT FIX DOES NOT WORK" IS OVERSTATED. Rep 5 landed 2026-08-23 11:47

Recovery rep 5 completed. It **did not time out**:

```
exit=2  timedout=FALSE  dur=3446s  files=4  iters=31   (ctx 65536, wall 3600s)
```

## The two recovery reps at identical config

| rep | iters | duration | s/iter | outcome |
|---|---|---|---|---|
| 4 | 21 | 3600s | **171** | TIMED OUT |
| 5 | 31 | 3446s | **111** | completed, hit the 30-iteration cap |
| *v7 rep 1 (fresh host)* | *31* | *1737s* | *56* | *reference* |

**At 3600s the score is 1 timeout, 1 completion — not 2 timeouts.**

## What survives and what does not

**Survives — B3's justification is intact.** 111–171 s/iter against 56 s/iter fresh
is a 2–3× spread on an identical configuration. Host degradation is real, large,
and still the thing worth controlling.

**Does not survive** — the section above titled *"THE 3600s TIMEOUT FIX DOES NOT
WORK"*, and specifically its claim that **"no wall clock is large enough without
B3"**. That was written from rep 4 alone. n=2 does not support it. The honest
statement is: *at 3600s this cell is measurable roughly half the time, and B3 is
what would make it reliably measurable.*

**Also does not survive**: "the qwen3.8 photo cell remains unmeasured at n≥2."
Rep 5 is a real row — 4 files, stopped at the iteration cap rather than the wall.

## The part that is quietly good news for v8

Rep 5 hit `exit=2` (the 30-iteration cap) at 111 s/iter — meaning **`max_iters`,
not the wall, was the binding constraint**, on a *degraded* host. That is exactly
the property v8's wall-sizing is designed to produce, demonstrated before v8 has
run. It also suggests the v8 walls (4200/3600/6000/1800/5400s) are, if anything,
generous rather than tight.

## ★ The method failure, again — sixth instance today

My rep-4 writeup said: *"Recovery rep 5 will likely fail the same way. Let it run —
a second data point is exactly what makes this conclusion safe rather than a single
observation, which is the mistake I made five times today."*

**I then wrote the conclusion into the design as settled anyway, in the same
section, before rep 5 landed.** Correctly identifying that I needed n=2 did not
stop me stating the finding at n=1. Naming the trap is not the same as avoiding it.

The v8 rule already covers this and it applies to my own process notes, not only to
per-model claims: **no claim is written until every rep for that cell has landed.**

---

# ✅ FABLE: GO — 2026-08-23 ~12:40. One pre-dispatch step remains.

Second review returned NO-GO with 4 blockers; 1-3 fixed, re-reviewed, **GO**.

**B1/B2 — `selftest` misclassified in both directions.** It greped the whole log
and tested `missing` before `pass`. The worker logs a 300-char preview of every
tool result, and the pristine binary's unknown-command handler prints a Usage
line containing `stream-selftest` inside that window — so a model merely
*exploring* `swift run Clamshell` injected a match that **overrode a genuine
pass**. Separately `fail` absorbed "verify never ran" (wall/ceiling/load_failed),
which is v7's `context_ceiling` disease reproduced inside the column added to
cure it. Fixed: scope to the post-`running verify command:` log, order
`not_run → timed_out → pass → missing → fail`.

**B3 — the 300s verify cap was reachable by a CORRECT solution.** The task
demands waiting out a real expiry window and never bounded it; a model choosing a
plausible 2–5 min window blows 300s honestly, and `subprocess.run`'s
`TimeoutExpired` was **uncaught** — crashing the worker *after* a good run
completed. Fixed both ends: task text dictates 3–5s, and the worker catches the
timeout and records `VERIFY TIMED OUT` as its own non-result. That catch then
created a new hazard (timeout falling through to `fail`, reintroducing B2), so
the classifier greps for it explicitly. Fable confirmed the ordering is sound.

**B4 — the B3 restart→gate path has never run end-to-end.** Still outstanding;
the vision sweep holds the Ollama server. Must run before dispatch.

## ★ The model-aware gate, and the structural problem it exposed

Flat 40%-of-physical (26214MB) was wrong in **both** directions: it over-demanded
for qwen2.5-coder:14b (~9GB resident → spurious 240s waits and meaningless
`host_ready=no`) and under-demanded for qwen3.8 (~30GB + 64k KV). Now
`resident x 1.2 + KV`, capped at 85% of physical:

| model | ctx | resident | gate |
|---|---|---|---|
| `qwen3-coder-next:q4_K_M` | 65536 | 51700 MB | **55705 MB** (at the cap) |
| `qwen3.8:27b-q8_0` | 65536 | 30000 MB | 36128 MB |
| `deepseek-r1:32b` | 65536 | 19900 MB | 24008 MB |
| `qwen3-14b-agentic` | 40960 | 9300 MB | 11240 MB |
| `qwen2.5-coder:14b` | 32768 | 9000 MB | 10864 MB |

**`qwen3-coder-next` needs ~55.7GB on a 65.5GB host.** It is *structurally* going
to page at 65536 ctx regardless of how fresh the host is. Fable's read: if the B4
exercise confirms it, those `host_ready=no` rows are a property of the
host/model pairing, not gate miscalibration — and the honest options are accept
the flagged rows, lower its ctx, or use a smaller quant. **That is a treatment
decision for the owner, not a gate tweak.**

## Corrected time budget

Base round ~16h expected / ~36h worst. Arms ~12h / ~30h. The earlier "~6 more
hours" for the arms was roughly half the real figure.

## Non-blocking, deliberately not done

Native-ctx assertion against live `/api/show` (ship as-is; add before any round
following a model re-pull). Watchdog process-group kill (current containment
adequate — B3 restart clears state, orphans show as rising load; v9 item).

---

# v8 COMPLETE BUILD — 2026-08-23 ~13:45. Awaiting final Fable verdict.

Second Fable pass returned **NO-GO** with five blockers; all five fixed, third
pass sent. What follows is the state as built.

## ★ The most instructive mistake of the round: the debug cell leaked its own answer

The new debug cell was built, and Fable found it handed models the solution
**three separate ways**:

1. `git log --oneline -1` printed *"Bake-off v8 debug baseline: planted
   release-group regex defect"* — and my task text said *"It used to work"*,
   which actively invites git archaeology. The first command an archaeologist
   runs returned the artifact, its location class, and the fact it was planted.
2. The repro's docstring narrated the **complete diagnosis** — named
   `extract_release_group`, the `same_group` gate, the None-return mechanism and
   the downstream consequence. A model that merely opened the failing test read
   the root cause in prose, collapsing the work to a one-character edit.
3. The task claimed *"the existing checks all still pass"* while a failing check
   sat in the tree — contradictory, and it invites treating the failing test as
   the bug.

**Documenting a planted bug as carefully as real code is correct practice for
real code and fatal for a blind test.** I wrote a good commit message and a good
docstring, and that is exactly what ruined the experiment.

**Rebuilt**: defect now ships as `3f5e4ff "Tighten release-group pattern to
alphabetic names"` with a plausible refactor rationale; repro added separately as
`bc08ae2` with a 3-line docstring carrying no diagnosis; old commit purged
(`reflog expire` + `gc --prune=now`, `a5abd52` no longer resolves,
`git log --all | grep -ciE "planted|defect|bake-off"` = **0**); task text
acknowledges the failing repro instead of denying it.

## The debug cell as it now stands

| property | value |
|---|---|
| repo / baseline | `plex-automation` @ **`bc08ae2`** (local branch, **not pushed**) |
| defect | `RELEASE_GROUP_RE` `-([A-Za-z0-9]+)$` → `-([A-Za-z]+)$` |
| effect | digit-bearing groups (`GROUP2`, `3EVILS`, `x0r`, `1080`) return `None` → `same_group` False → PROPER/REPACK soft-supersedes instead of deleting → superseded entries pile up |
| invisible to | the existing suite — it only exercises the all-alpha name `GROUP` (verified: all 6 cases pass under the defect) |
| task shape | symptom report; names no file, function or regex |
| preflight | **INVERTED** — repro must FAIL on the pristine tree, else `ABORT_PREMISE_NOT_BROKEN`. All four polarity combinations unit-tested. |
| verify | restores **both** test files from baseline before running them |
| worktrees | 6, all verified reproducing |

**The verify is ungameable, and this was tested rather than assumed**: replacing
the repro with a trivially-passing stub passes a naive run and **fails** after
the checkout restore.

## Why the debug cell exists at all

Both original cells are single-shot greenfield feature work in **TypeScript and
Swift** — the two languages local models are weakest at. The owner's dispatch fleet is
heavily **Python**, and his commonest dispatch shape is *"investigate this thing
that stopped working"*. v8 as first designed would have **underrated** local
models on the dominant language while saying nothing about the dominant shape.

## Roster and gates (KV constant corrected)

The gate's KV allowance was **wrong by ~75x** — 2MB/1k tokens where the real
figure for this size class (64 layers, 8 GQA KV heads, head_dim 128, q8 cache) is
128KB/token ≈ 128MB/1k, ~8.4GB at 65536. `host_ready=yes` could therefore have
been stamped on a run that was about to page — certifying the exact confound B3
exists to expose. Now 150MB/1k.

| model | ctx | resident | gate |
|---|---|---|---|
| `qwen3.8:27b-q8_0` | 65536 | 30000 | 45600 |
| `qwen3-coder-next:q4_K_M` | 65536 | 51700 | **55705 (at 85% cap — structural)** |
| `deepseek-r1:32b` | 65536 | 19900 | 33480 |
| `qwen3-coder:30b` **(new)** | 65536 | 18600 | 31920 |
| `qwen3-14b-agentic` | 40960 | 9300 | 17160 |
| `qwen2.5-coder:14b` | 32768 | 9000 | 15600 |

## Arms stay at n=3 — Fable proposed n=2, this round overruled it, Fable conceded

Escalation-on-split only fires on *observed* splits, so it cannot catch a clean
unanimous 2/2 that would have been 2/3 — and v7 produced exactly that in **two of
four** fully-usable cells: `qwen3-14b-agentic`/clam (transfer, transfer,
**exploration**) and `deepseek-r1:32b`/photo (inert, inert, **exploration**).
Both are the salience arm's subjects. Funded by dropping `qwen3-coder-next` from
the apisurface arm, which costs nothing — its v7 clamshell rows never survived to
the API-recall failure point.

## ★ ONE PIPELINE — `bakeoff-v8-ALL.sh` does everything

Phase 0 stops the vision queue loop and waits only for the **in-flight** model ·
1 B3 exercise (hard stop) · 2 calibration (hard stop if the measured wall exceeds
the roster's) · 3 base · 4 arms · 5 research eval · **6 remaining vision models**.
Every phase `SKIP_*`-able for resume.

Real budget: base is **17 cells × 3 reps = 51 runs, ~22-26h expected**; arms
~10-12h. The earlier "~16h / ~6h" figures were wrong.

## Early substantive result, before dispatch

`self_verify_count` is **0 on every transcript checked** — and the detector was
validated rather than trusted: `qwen3.8`'s photo run made **16 `run_bash` calls,
all `grep`/`sed`/`ls`, and never ran a build.** The round's strongest model does
not verify its own work. Under the preregistration that is disqualifying for
`unsupervised` dispatch.

## Deferred, recorded so it is not lost

`qwen2.5vl:7b` has **no valid Unraid baseline** — its old sweeps sit in
`results-v1-INVALID-SINGLE-ITEM-SCHEMA` and `results-v2-multifield-superseded`.
The vision round's port-validation premise therefore rests on
`Nanonets-OCR2-3B`, not on the model that just completed. The owner is deferring this.

## Process lesson, from the owner

I sent Fable a *review* brief ("attack these nine things") before ever asking
what the design was **missing** — so two passes returned fixes and none returned
additions, and the third pass then invalidated work. Correct order is: complete
the build including what it structurally lacks, **then** one pass asking both
questions. Now a standing rule.

---

# ★ INFRASTRUCTURE FIX + PIPELINE LAUNCHED — 2026-08-23 14:22

## The failure that exposed it

The research-eval positive control died with `TimeoutError` at **900.3s on its
first task**. Not an eval defect — `qwen3-coder-next` (51.7GB) could not finish
loading inside the warmup budget. Cause: **Ollama was serving every model
straight off the SMB share.**

| source | throughput | 30GB load |
|---|---|---|
| SMB `/Volumes/data` (Unraid) | **299 MB/s** (57 MB/s for a real multi-blob copy) | ~100s+ |
| local SSD `~/.ollama/models` | **2927 MB/s** | ~10s |

Under memory pressure it was far worse: RSS crawled at ~40 MB/s while the host
thrashed at 2GB available, and `ls` on the share timed out entirely. The worker's
own docstring had warned of this since 2026-08-21: *Ollama's model-load path over
SMB hangs indefinitely; a plain file copy from the same share does not.*

## Why the intended design was dead code

`ensure_model_cached()` implements exactly the right architecture — check local,
pull from Unraid, serve local. It **never fired**. Because `OLLAMA_MODELS` pointed
at the share, `_model_visible_to_local_ollama()` returned True for every model and
the function returned on its first line. Unraid was not the store; it was the live
serving directory.

## What changed

**`OLLAMA_MODELS` → `~/.ollama/models`.** Unraid is now the STORE; the Mac serves
from local SSD.

**Two agents were reverting it** and had to be patched — this is the part that
will bite anyone who doesn't know:

- `~/bin/mount-ollama-models.sh`
- `~/bin/unraid-share-agent.sh` (the live one, via LaunchAgent
  `com.example.unraid-share`, ticking every 60s)

Both set `OLLAMA_MODELS` to the share and restart Ollama. Both now point at the
local cache instead. **The mount check is deliberately retained in both** — no
share means nothing to stage FROM. Verified: forcing the agent to run no longer
reverts the value.

**New tool `bakeoff-v8-stage.py`** — `stage` / `evict` / `list` / `check`.
Copies blobs Unraid→local with LRU eviction when space is short, `--keep` to
protect the active roster, temp-name-then-rename so an interrupted copy can never
be mistaken for a complete blob.

**Staging is wired into `restart_inference_server()` in both harnesses** — so it
runs BETWEEN tests, host idle, never during a measured run. A 17-50GB SMB read
concurrent with a run would inject exactly the contention B3 exists to remove.

**Wall-clock semantics** (asked and verified): the staging copy, server restart
and memory gate are all OUTSIDE the wall. `START_TS` is set after them. Model
load into RAM is INSIDE the wall, which is what the 900s warmup budget is for —
and now hugely slack at ~10-18s from local SSD.

## Space

Evicted 4 non-roster models (`deepseek-r1:7b`, `qwen2.5-coder:7b`,
`deepseek-r1:14b`, `devstral:24b`) = 30.5GB. Staged `qwen3-coder:30b` (17.3GB in
313s = 57 MB/s).

**Gotcha**: freed space did not appear in `df`. **21 local Time Machine
snapshots** (hourly, Aug 22→23) were pinning the deleted blobs. It is *purgeable*
— `Container Free Space` reads the true 55.5GB and macOS releases it under
pressure — but `sudo tmutil deletelocalsnapshots /` would make staging
predictable. Real Time Machine backups go to `smb://user@203.0.113.33/
timemachine-owner` and are unaffected.

## ★ B3 EXERCISE PASSED — first end-to-end run, Fable's last blocker closed

```
CLEAN-HOST AVAILABLE: 40402MB of 65536MB
restart -> API answering in 3-13s
gate exercised on qwen2.5-coder:14b -> PASS
```

| model | ctx | gate | reachable? |
|---|---|---|---|
| `qwen3.8:27b-q8_0` | 65536 | 45600 | **NO — STRUCTURAL** |
| `qwen3-coder-next:q4_K_M` | 65536 | 55705 | **NO — STRUCTURAL** |
| `deepseek-r1:32b` | 65536 | 33480 | yes |
| `qwen3-coder:30b` | 65536 | 31920 | yes |
| `qwen3-14b-agentic` | 40960 | 17160 | yes |
| `qwen2.5-coder:14b` | 32768 | 15600 | yes |

**4 reachable, 2 structural.** Per the preregistration the two structural cells
are EXCLUDED from falsifier #1 — their `host_ready=no` is a routing fact
("requires a >64GB host"), not a B3 failure. Note `qwen3.8` joining
`qwen3-coder-next` was NOT predicted; it is the round's strongest model.

## Pipeline running

`bakeoff-v8-ALL.sh` launched 14:22, pid tracked in `bakeoff-v8-ALL.log`.
Phases: 0 stop vision (done, **549/1764 of qwen3-vl:8b preserved** — gc-sweep is
resume-safe) · 1 B3 **PASSED** · 1b positive control · 2 calibration · 3 base ·
4 arms · 5 research eval · 6 remaining vision. Each `SKIP_*`-able for resume.

ETA ~47-55h, i.e. late Tuesday 25th.

## Also settled today

- **The work computer is NOT on WARP and still gets streaming disconnects**, and
  Anthropic reports zero incidents. **WARP is therefore NOT the cause of the
  Claude API errors** — I had wrongly attributed them. The Studio's 2-minute
  outages were a separate, real, now-resolved thing.
- `cloudflared` is **not installed on the Studio**. Remote access is Cloudflare →
  WARP Connector on Unraid → 192.0.2.15. Confirmed by the owner.
- WARP profiles left as: Home Network **WireGuard**, (default) **MASQUE**
  (proven: WireGuard 0/58 on IPv6-only NAT64 cellular), Mesh **MASQUE**.


## v8 post-run review — scorer fix verified, two instrument findings (Fable second pass, 2026-08-24)

**Scorer fix (embedded tool-call parsing in `bakeoff-v8-score.py`) is correct and safe to trust.**
Verified independently, not re-trusted: all 69 transcripts rescanned — every extracted call name
is a legitimate harness tool (zero bogus anchors); files_changed vs time_to_first_mutation
cross-checks 69/69 clean in both directions; executed-vs-extracted call totals (ground truth from
`[tool result for X]` / `role:tool` messages) match exactly on 58/69 rows, the positive diffs
being trailing emitted-but-never-executed calls. The `_NAME_ANCHOR_RE` false-positive concern is
structurally impossible on well-formed calls (escaped inner quotes cannot match the anchor) and
fired zero times in real data.

**Residual scorer gap (contained, 2/69 rows):** deepseek-r1:32b sometimes emits argument values
as Python-style triple-quoted strings (`"content": """...raw newlines..."""`). The worker
normalizes and executed these (`extract_manual_tool_calls`, ollama-worker-v7.py ~line 347); the
scorer's strict `raw_decode` misses them. Affected: clamshell base r2 (1 edit_file), clamshell
apisurface r3 (11 of 13 mutations — churn/tool_calls undercounted, ttfm off by one). Mutation
detection and self-verify columns unaffected. Fix: import the worker's parser into the scorer for
permanent parity. Note the driver runs **ollama-worker-v7.py** (`bakeoff-v8-lib.sh:69`), not the
ollama-worker-v8.py copy.

**Debug cell is instrument-compromised — do NOT fill the debug row from it for the
ceiling-stopped models.** arr-webhook.py is 228KB (~57k tokens) and `tool_read_file` has no size
cap: one read fills the entire 65k window and exceeds qwen2.5-coder's 32k / qwen3-14b-agentic's
41k outright. 10/18 plex rows stopped `config_ceiling` at 5-6 iterations (178-1316 output
tokens); 0/18 verify passes; every nonzero files_changed is untracked junk (app.py, run.sh,
new_script.py) — no model touched the real file. This measured "file doesn't fit in context",
not debugging — the same class as v7's `context_ceiling`. Rerun with a read_file cap/pagination
or grep-guidance before writing verdicts. qwen3-14b-agentic and qwen2.5-coder did genuinely
engage and fail (natural stops; qwen2.5's r2 doom-looped `./run.sh` ports 5001→5020, 21 runs).

**One "inert" run is a harness gap, not a model result:** qwen3-coder:30b clamshell base r3
(7s, 2 iters, 0 executed calls) emitted Qwen-native XML calls (`<function=list_files>`) that
nothing parses. Only transcript in the round with that dialect. Rerun the rep or reclassify
before applying §1's inert disqualifier to that cell.

**VERIFIERS gap:** `npx tsc --noEmit` not counted as self-verify; two qwen3.8 photo rows (base
r2, repomap r2) ran tsc+eslint and score 0 — counting tsc flips qwen3.8 10/15 → 12/15, which
matters because §2 makes self-verify a positive requirement per passing run. `swift test` also
absent (didn't bite this round). One-line fix, then re-emit the behaviour CSV.

**§5 falsifiers: none fire.** host_ready=no among reachable-gate cells is **1/45 = 2.2%**
(qwen3.8 and qwen3-coder-next both pre-classified STRUCTURAL at B3 — qwen3.8 needs ~55.7GB at
65k ctx); zero timed_out; clamshell selftest ran on 21/27 (the 6 "missing" = subcommand never
wired, not not_run); token counts complete 69/69; no decode_s=0 anomalies. For tokens/s claims,
compute qwen3.8 from its host_ready=yes rows only (8/15 flagged, structural paging).

**Corrected behaviour summary is plausible and corroborated:** qwen3.8 12/15 self-verified
(obsessive swift-build cadence, churn 0.0); qwen3-14b-agentic 0/15 — structural, it never issued
a single run_bash across all 15 runs; qwen2.5-coder 0/6 genuine (its 33 bash calls were
app-launching churn, median churn 0.349); deepseek-r1:32b 11/15 mutating, 3/15 self-verifying
(all clamshell, via `swift run Clamshell confirmation-bridge-selftest`).

Minor: prereg table marks deepseek repomap "—" but §4 prose names it a salience-arm subject and
it ran (extra data, favorable — fix the table); stale `qwen3-coder-30b-plex...-tag-base-r1.diff`
sorts first in the scorer's diff glob (both files empty today, fragile); exit codes decode as
0=verify+converged, 2=verify-passed-not-converged, all 9 iter_cap rows are genuine 30-iteration
caps.

## 2026-08-24 — Prior-art check: Claude-Code→Ollama dispatch workflows (agent a9860795ca272fa52)

Dispatched to establish whether the v7/v8 methodology duplicates existing work. Verdict:
**no comparable prior art for the combination.** The basic orchestrator→Ollama delegation
pattern is common and shallow; the three distinctive pieces — host-readiness memory gating,
claim-vs-verify calibration with an `unverifiable_claim` bucket, and pre-registered
falsification criteria — were not found together anywhere.

- Closest peer: neuralnoise.com "harness-bench-wip" — 17 model/quant combos × 5 harnesses
  × 16 SWE tasks, hidden test.sh grading. Self-described WIP; no gating, no claim-vs-verify,
  no pre-registration.
- Delegation-only repos, no verify loop or gating: Jadael/OllamaClaude,
  andrewbrereton/claude-sidekick, PratikHotchandani22/claude-ollama-agents.
- Native per-subagent provider routing in Claude Code is still only a feature request
  (anthropics/claude-code#38698) — the external worker-script approach remains necessary.
- Problem independently validated: bswen.com (42% false-positive rate, GLM-5.2 + Opus),
  gptcode.dev (local Ollama Devstral, capability≠reliability), "Beyond Pass@k"
  (arXiv 2608.14711).
- Worth borrowing from: RouteLLM (threshold calibration), OpenHands SDK (stuck-detection),
  SWE-RM (execution-free calibrated reward modelling).

Note: the agent could not file this itself — no Obsidian MCP tool in a dispatched subagent
context, only the parent session has vault access. Filed here by the parent.

## 2026-08-24 — v8 scorer: VERIFIERS fix + parser unification (both applied)

Acted on items 1-2 of Fable's review. Both are in `bakeoff-v8-score.py`; behaviour CSV re-emitted.

**VERIFIERS gap closed.** Added `tsc --noEmit`, `npx tsc --noEmit`, `swift test`. Result matches
Fable's prediction exactly: qwen3.8:27b-q8_0 goes **9/15 → 12/15 self-verified**. Two routing-table
cells (photo base, photo repomap) corrected from 1/3 to 2/3 self-verified in passing runs.
**No verdict changed** — the numbers were understating the model, not misclassifying it.

**Parser unified.** The scorer now imports `extract_manual_tool_calls` from
`~/bin/ollama-worker-v8.py` by path (soft-fails to the local parser if absent). Rationale: that
function is what actually EXECUTED the calls during the run, so the scorer now agrees with ground
truth by construction instead of by coincidence — maintaining two parsers for one wire format is
precisely how the original bug happened. It also handles triple-quoted values, reversed
`{"arguments",...,"name"}` key order, and string-aware brace matching.

7 rows changed. deepseek-r1:32b apisurface calls **12 → 23** (recovered triple-quoted calls that
were being dropped); deepseek repomap calls **14 → 10**, consistent with the old code
double-counting when it retried raw content through the fenced fallback.

**Verified routing verdicts, recomputed from the corrected CSV** (pass = verify_passed=yes or
selftest=pass; UNSUPERVISED requires self-verify in EVERY passing run AND a clean sweep):
- qwen3.8 clamshell base 3/3, sv 3/3 → UNSUPERVISED
- qwen3.8 clamshell apisurface 3/3, sv 3/3 → UNSUPERVISED
- qwen3.8 photo base 3/3, sv 2/3 → harness-verified
- qwen3.8 photo repomap 3/3, sv 2/3 → harness-verified
- qwen3-coder:30b photo base 3/3, sv 0/3 → harness-verified (perfect record, never self-checked)
- qwen3-coder-next photo base 2/3, sv 1/2 → scaffolded
- deepseek-r1:32b photo base 2/3, sv 0/2 → scaffolded
- qwen3-14b-agentic photo repomap 1/3 → scaffolded; everything else 0/3 → don't dispatch

Still open from the review: qwen3-coder:30b clamshell base r3 (Qwen-XML dialect, harness gap not
inert model), the debug-cell rerun with a capped read_file, and the human claim-vs-verify
calibration read.

**Method note:** Fable's report cited `extract_manual_tool_calls` as existing in "the worker".
A first search of the projects directory found nothing and it looked like a fabricated identifier —
it is real, at `~/bin/ollama-worker-v8.py:315`. The workers live in `~/bin`, outside the project
tree. Search the whole path before calling a reviewer's specific citation invented.

## 2026-08-24 — CORRECTION to the prior-art citations filed earlier today
Verification pass (agent a46b7ca4b2ddd9c8a) checked all 12 citations from the prior-art agent
against their actual sources. **10 of 12 exact; nothing fabricated.** Two defects:

1. **arXiv 2608.14711 "Beyond Pass@k" is REAL but MISCHARACTERISED.** It is about pass@k *metric
   misapplication*, NOT about agents falsely claiming success. It therefore does **not** support
   the "our problem is independently validated academically" claim filed earlier in this note.
   Strike it from that argument. The bswen.com 42% figure and the gptcode.dev post were confirmed
   and do still support it.
2. **RouteLLM is real but stale** — no commit since 2024-08-10. Fine as a source of
   threshold-calibration ideas; not something to depend on.

The neuralnoise harness-bench 17×5×16 numbers, the three delegation repos, and
anthropics/claude-code#38698 all verified as described. The "no comparable prior art" conclusion
stands.

**Process note:** the earlier findings were filed to this vault UNVERIFIED and flagged as such at
the time. Verifying them changed one substantive claim. Citations returned by a research agent
should be verified before being used as evidence, not after.
