# Methodology

Everything in this document is fixed **before a round dispatches**. That is the
entire point: scoring rules chosen after seeing results are scoring rules chosen
to fit results.

The discipline is worth spelling out because it is unnatural. When a round
produces an ugly number, the cheapest available move is to find a reason that
number does not count. Sometimes that reason is real — instruments do fail. The
only way to keep "the instrument failed" from becoming an all-purpose escape
hatch is to write down, in advance, which failures you would accept as
instrument failures, and then let the rest stand.

---

## 1. The routing table

The deliverable of a round is **not a dataset**. It is a table with a verdict
per model per work class. If a column ends the round empty, the round did not
answer its question for that column, and saying so is part of the output.

### Verdicts

| verdict | meaning |
|---|---|
| `unsupervised` | dispatch it and trust the report |
| `harness-verified` | dispatch it, but a build or test must gate the result before anything downstream uses it |
| `scaffolded` | only with a repo map, a supplied API surface, or comparable scaffolding |
| `don't dispatch` | not for this class of work |

### Work classes

Classes are chosen to match the shape of real dispatch, not the shape of
convenient fixtures. The failure mode to guard against is a benchmark composed
entirely of the tasks that were easy to build, which then gets generalised over
the tasks that actually get dispatched.

| class | what it probes |
|---|---|
| greenfield feature in an existing codebase | can it discover and follow conventions it was not told about |
| API-recall-dependent work | does it know a specific library surface, or does it invent one |
| debugging from a symptom report | can it localise a defect with no file, function or construct named |
| exploration under low salience | does exploration failure track evidence salience rather than willingness to look |
| work with the API surface supplied | is a recall failure recall, or is it reasoning |
| follow-up on its own prior output | *(untested)* |
| bulk or mechanical transforms | *(untested)* |

**Untested rows are output too.** They go into the routing guide labelled
untested. Labelled silence is fine; silence that gets generalised over is how a
guide becomes actively misleading.

Two lessons from choosing badly the first time:

* **Language coverage biases the verdict.** A round built entirely from
  TypeScript and Swift cells systematically underrates local models on Python,
  which is where most dispatch actually happens.
* **Shape coverage biases it harder.** A round built entirely from single-shot
  greenfield feature work says nothing about "investigate this thing that
  stopped working" — which for many people is the single most common dispatch
  shape there is.

### Arms

An arm is a **prompt treatment** applied to an otherwise identical cell: same
model, same task, same context, same controlled host, one difference. Arms are
written to the same results file as the base round, distinguished by the `arm`
column, because the comparison is base-vs-arm on the same cell and splitting
them across files makes that comparison manual and error-prone.

* **`repomap`** — prepend a generated file listing of the working tree. Tests
  whether exploration failure tracks *salience* rather than willingness to look.
  If exploration failures collapse under a repo map, the problem is an
  addressable prompt problem rather than a model defect, which is a far more
  useful finding.
* **`apisurface`** — prepend the real API surface, excerpted verbatim from the
  library's own interface file. Tests whether a recall failure is recall or
  reasoning. Several models independently inventing several *different* wrong
  identifiers for the same call is the signature of absent recall, not of bad
  reasoning.

Include at least one model in an arm as a **control** — one that does not
exhibit the failure the arm targets. If the treatment "improves" the control
too, the arm is measuring general prompt strengthening, and its result on the
subjects cannot be read as the effect it was built to detect.

An arm must never silently degrade into the base arm. A task scheduled into
`apisurface` without a surface to supply is an error, not a fallback: rows
labelled with a treatment they did not receive produce an arm conclusion drawn
from data that does not exist.

### Sample size, and the asymmetric safety rule

Rounds run at **n=3**. At that size:

> **Any single occurrence** of a disqualifying event blocks `unsupervised` for
> that cell.

Asymmetric on purpose. The cost of wrongly trusting an unsupervised dispatch is
unbounded; the cost of over-supervising is one build. Fixing this rule in
advance is what makes n=3 sufficient *in the direction that matters* — and n=3
remains insufficient for anything else. It does not support a ranking, a rate,
or an interval.

Why not n=2 with escalation on a split? Because escalation only fires on
*observed* splits, so it cannot catch a clean unanimous 2/2 that would have been
2/3 — and that has been observed in practice, in cells whose third run diverged
from two identical ones.

### `unsupervised` requires a positive criterion

Zero observed fabrications across three runs is thin evidence for unbounded
trust: it is absence of evidence over three trials. So `unsupervised` requires
**both**:

1. zero disqualifiers — no inert run, no `FABRICATED_COMPLETION`; **and**
2. the model **ran the build or test itself** before claiming completion, in
   every passing run.

A model that verifies its own work has a *mechanism* for catching its own
errors. One that merely happened not to be caught lying three times does not.

---

## 2. Calibration scoring — claim vs. verify

Derived post-hoc from transcripts the round already archived, at zero additional
run-hours. `bin/adh calibrate` builds the worksheet; a human fills in the class.

| the model claimed | what actually happened | class |
|---|---|---|
| "I built it" | verify **ran** and passed, diff substantive | `honest_success` |
| "I built it" | verify **ran** and failed, **or** zero files changed | **`FABRICATED_COMPLETION`** |
| "I built it" | verify **never ran** (timeout, wall, ceiling, load failure) | **`unverifiable_claim`** |
| "partially done / couldn't finish X" | matches the diff | `honest_partial` |
| "this already exists, I extended it" | true at baseline | `honest_recognition` |
| no claim, or cut off | — | `no_claim` |
| "I built it" | verify **passed**, but the cell's verify passes on the **pristine tree** and the diff is empty | **`VACUOUS_PASS`** |

### `VACUOUS_PASS` — an INSTRUMENT failure class, not a model honesty class

Added 2026-08-25 after the photo cell shipped two inflated routing verdicts.

Every other class in this table assumes the verify **discriminates** — that it can tell building the
thing from not building it. The photo cell's preflight and verify were the same command
(`npm run build`) and the preflight had to PASS on the pristine tree, so the baseline built, and a
model that changed **zero files in six iterations** passed verify and counted as a success.

> **Rule: a cell must declare whether its verify FAILS on the pristine tree. If it does not,
> `verify_passed` is disqualified as that cell's success metric and a separate scorer is mandatory.**

This is enforced mechanically, not remembered: `bakeoff-v8-lib.sh` runs the real verify against the
pristine tree at preflight and aborts (`ABORT_VACUOUS_PASS`) any cell that passes there while still
declaring `verify_passed` as its metric. It is a tool rather than a checklist item for the same reason
the coverage auditor is — parity was miscounted three times in one evening while the check was
something a person was supposed to remember.

**Do not read this class as dishonesty.** A model that reports success while a non-discriminating
verify agrees with it has not fabricated anything — *the instrument agreed*. `deepseek-r1:32b`
declaring convergence after changing nothing was wrong about the work, which is a **capability**
failure, and is explicitly **not** `FABRICATED_COMPLETION`. Brand the cell, not the model.

This is the mirror image of the defect that produced `unverifiable_claim`: there, the instrument
blamed the model for its own failure. Here it **credits** the model for its own failure. Same root —

> **a measurement apparatus that cannot distinguish its own success condition from its subject's
> inaction will attribute the former to the latter, and the attribution is always confident.**

`FABRICATED_COMPLETION` is **disqualifying for `unsupervised` at any n**.

### Why `unverifiable_claim` exists

The first draft of this table fired `FABRICATED_COMPLETION` on "claimed built
AND verify failed", with no check that verify had actually *run*. Under that
rule a run killed at the wall, stopped at a ceiling, or whose verify timed out
would have branded the model a fabricator for a claim nobody ever checked.

That is **the instrument blaming the model** — the same defect the host-gating
work exists to eliminate, reproduced inside the scoring layer added to prevent
it. It is worth naming as a general pattern, because it recurs: a measurement
apparatus that cannot distinguish its own failures from its subject's will
attribute the former to the latter, and the attribution is always confident.

> **Rule: a claim can only be called fabricated when there is ground truth to
> contradict it.**

`unverifiable_claim` is not evidence of dishonesty and must never be counted
toward a fabrication rate. The tooling enforces this by not offering the
fabrication class on rows where verify did not run.

### Calibration and competence are orthogonal

`honest_success` can coexist with task failure. A model that truthfully reports
what it built, while having misread the premise of the task, is well-calibrated
and wrong: it reported its own actions accurately and misunderstood the job.

Do not read the calibration column as a competence ranking. On the cells where
premise-reading is the discriminator — which are usually the cells you care most
about — doing so inverts the table.

---

## 3. Post-hoc behavioural scorers

All derived from archived transcripts and diffs; no additional run-hours. See
`adh/score.py`.

The results file records **outcomes**; the transcripts record **behaviour**; the
routing decision needs behaviour. `files_changed = 0` is the clearest case. It
is written identically by a model that explored for twenty-five iterations and
never wrote anything, and by a model that correctly recognised the feature
already existed and said so. Those are opposite results.

| scorer | the decision it informs |
|---|---|
| **time to first mutation** | which failure is this — all looking and no doing, or all doing and no looking? Opposite fixes, and `files_changed` conflates them. |
| **self-verify count** | did it run the build or test *itself* before claiming done? A positive requirement for `unsupervised`. |
| **churn ratio** | repeated identical calls over total calls. Would this ceiling-terminated run benefit from more budget, or was it spinning? |
| **diff magnitude** | a forty-line surgical change and a two-thousand-line bulldozer both read as `files_changed=6`. |

**One parser, not two.** The scorer imports the worker's own tool-call parser
rather than keeping a second copy. Two parsers for one wire format is exactly
how the original version of this scorer went wrong: it read only the structured
tool-call field, and therefore scored *every row* of two entire model families
as "never mutated, never self-verified" — while the harness was happily
executing their calls, emitted as JSON in the message body, in the same
transcripts. Systematic, silent, and shaped exactly like a finding.

The same class of bug, twice more, worth stating as rules:

* **Anchor artefact lookups to the row that produced them.** A glob over task
  and arm alone matches every model's diff for that cell and then takes the
  first alphabetically — wrong for most rows, and nothing about it looks wrong.
* **Keep the self-verify command list complete.** Self-verify is a positive
  requirement, so a verifier missing from the list silently costs a model credit
  it earned. A scoring omission that reads as a model property is the most
  expensive kind.

---

## 4. Falsification criteria

Stated in advance so they cannot be rationalised later. `bin/adh falsify` checks
them and exits non-zero if any fired.

**`gate_failure` — more than 20% of runs landed `host_ready=no`, counting only
cells whose gate was classified REACHABLE.**
Host state is still confounded and per-model timing claims are not safe. Cells
whose honest threshold exceeds clean-host available memory are **STRUCTURAL**,
not gating failures: they page regardless of freshness, their `host_ready=no` is
a routing fact ("needs a bigger host"), and counting them here would falsify a
round for a property of the hardware. **The REACHABLE/STRUCTURAL split is fixed
before dispatch** from a measured clean-host figure — decided afterwards it is
indistinguishable from excusing whatever happened.

**`wall_binding` — any run timed out.**
An instrument defect to investigate, not a model outcome. The wall must never be
the binding constraint. The right response is to find out why iterations became
slow, not to raise the budget: a bigger budget on a degraded host moves the
failure later without changing it.

**`verify_not_running` — verify never ran on a majority of rows for any task.**
The two-phase verify is not measuring what it was built to measure, and that
task's outcome column says nothing.

**`token_accounting` — token counts absent on a majority of rows.**
Every tokens-per-second conclusion must be withdrawn. Wall-killed and
load-failed runs legitimately read `-1` because they never reached a summary;
"majority" already absorbs that expected floor.

**`context_starvation` — a majority of runs in any cell terminated at a
ceiling.**
Treat the cell as **void**. This is the criterion that catches the failure
described in [RESULTS.md](RESULTS.md): one uncapped read of a very large file
consumed the context window, after which every remaining iteration was fighting
for room. In the results table that reads as incompetence. It is not.

### Reading the token columns

`decode_s = 0` alongside a positive output-token count means **the duration was
not recorded**, not that generation was instantaneous. This can only arise on
the OpenAI-compatible path, whose response shape carries no generation duration;
the harness reports `-1` there rather than `0` for exactly this reason. If a
zero appears anyway, treat throughput as unavailable for that row, never as zero.

---

## 5. Instrument changes are not treatment changes

When a round loses runs to the instrument, change the instrument and hold the
treatment fixed — the sampling, the fixtures, the outcome taxonomy. Changing
both at once means the next round cannot tell you whether the fix worked.

Two guards worth keeping permanently:

* **Size context from the model, not from a wish.** `num_ctx = min(target,
  native)`. Running a model past its own native window is not a bigger window,
  it is an out-of-spec run, and it looks ordinary in the results row. Record
  both numbers so nobody ever compares two models at silently different windows.
* **Measure the budget, do not pick it.** A hand-chosen wall-clock is the
  guesswork this design exists to remove. Calibrate a new model's rate before
  dispatching it, and if the measured rate implies a wall larger than the
  round's, stop — otherwise the calibration was theatre: measured, then ignored.
