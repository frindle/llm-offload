# agent-dispatch-harness

A pre-registered benchmark harness for one question: **can this local model be
trusted to do agentic coding work without someone checking it?**

That is not the same question as "how good is this model", and most coding
benchmarks cannot answer it. A pass rate tells you how often the work came out
right. It does not tell you whether the host was paging when you measured, and
it does not tell you what the model *said about* the work it did — which is the
thing you actually depend on when you dispatch a task and read a summary instead
of the diff.

Three things distinguish this harness. Each exists because its absence produced
a wrong answer that looked like a right one.

1. **Model-relative host-readiness gating.** No run starts until the host has
   enough *available* memory for that specific model at its configured context.
2. **Claim-vs-verify calibration, scored separately from success.** What the
   model claimed, classified against what actually happened, with explicit
   `FABRICATED_COMPLETION` and `unverifiable_claim` classes.
3. **Pre-registered falsification criteria.** Declare in advance what result
   would mean the round is not usable, then check those criteria before
   believing anything it produced.

---

## Why host gating matters

A model benchmarked on a host that is already paging is not being benchmarked.
The confound arrives as a bigger number in the duration column, where it is
indistinguishable from a slower model.

The measurement that forced this design: one cell, identical configuration
across four repetitions, went 44 → 42 → 53 → 86 seconds per iteration. Context
never exceeded 30% of the window, so context growth could not explain it. Then
the same cell was rerun with the wall-clock budget doubled, and timed out
anyway — 171 s/iteration against 56 s/iteration for the same cell on a fresh
host, three times slower on an identical configuration. Measured live during
that run: 12.3 GB of swap in use, 5.8 GB available out of 64 GB.

Two conclusions, both of which are built into this harness:

* **The wall clock was never the binding constraint.** A bigger time budget on a
  degraded host just moves the failure later. Any run that hits the wall is
  treated here as an instrument defect to investigate, not as a model outcome.
* **Repetition does not average this out.** Degradation accumulates *across* a
  round, so later repetitions are systematically slower than earlier ones and
  rep index becomes a hidden covariate.

So before every run the harness restarts the inference server (when it owns it)
and waits, bounded, for available memory to recover past a threshold computed
for *this model at this context*. It dispatches either way and records which
happened as `host_ready`, because refusing to dispatch would silently delete
data from exactly the configurations that stress the host.

**The threshold is model-relative**, and it has to be. A flat "40% of physical"
rule is wrong in both directions at once: it makes a 9 GB model wait for memory
it will never need, and it certifies a 30 GB model at a 64k context that does
not fit in 26 GB — stamping `host_ready=yes` on precisely the run that is about
to page. See `adh/gate.py` for the arithmetic, including why the KV-cache term
is not a rounding error (an early version was short by roughly seventy-five
times, and the runs it wrongly passed were the exact runs gating exists to
catch).

**Gate on available memory, never on swap.** macOS does not shrink swap files
promptly when pages are released, so swap staying high after a restart does not
mean memory is still unavailable — a swap gate blocks for minutes after the
memory is already back. Swap is recorded; it is never gated on.

Models whose honest threshold exceeds clean-host available memory are classified
**STRUCTURAL** before the round runs. Those page no matter how fresh the host
is. That is not a gating failure, it is a routing fact — "this model needs a
bigger host" — and it is excluded from the gate-failure criterion accordingly.

## Why claim-vs-verify is separate from success rate

The dispatch decision is not "can model Y do task X". It is: **when model Y says
it did X, is that true?**

A weak model that reports its own failures accurately is dispatchable — you
route around known gaps. A strong model that claims success when verify failed
is the dangerous one, because nothing downstream catches it. Success rate cannot
see the difference: "failed and said so" and "failed and claimed otherwise" land
in the same cell.

So the harness records both halves and never crosses them during the run. The
model's final summary goes in the transcript; the verified outcome goes in the
results row. `adh calibrate` pairs them into a worksheet a human classifies,
using the fixed taxonomy in [METHODOLOGY.md](METHODOLOGY.md) §2.

One rule the tooling enforces mechanically: **a claim can only be called
fabricated when there is ground truth to contradict it.** A run killed at the
wall, stopped at a context ceiling, or whose verify never ran would otherwise be
branded a fabrication for a claim nobody ever checked — the instrument blaming
the model, which is the defect this whole design exists to prevent. Those rows
get `unverifiable_claim`, which is not evidence of dishonesty and must never be
counted toward a fabrication rate.

Calibration and competence are also orthogonal, and reading one as the other
inverts the table. A model that truthfully reports building something, while
having misread the premise of the task, is well-calibrated and wrong.

## Why pre-registration

Scoring rules chosen after seeing results are scoring rules chosen to fit
results. [METHODOLOGY.md](METHODOLOGY.md) fixes the routing classes, the
calibration taxonomy, and the falsification criteria; `adh falsify` checks a
finished round against them and exits non-zero if any fired.

The criteria answer one question per check: **is this an instrument failure or a
finding?** A benchmark that cannot tell those apart reports its own defects as
model properties, and the more confident the write-up the worse the damage.

---

## Install and run

Python 3.11 or newer. No dependencies — standard library only.

```sh
git clone <this repo> && cd agent-dispatch-harness
cp config.example.toml config.toml
$EDITOR config.toml          # endpoint, roster, memory figures, task paths

bin/adh doctor               # config, telemetry, backend, fixtures
bin/adh probe                # ask the backend each model's native context
bin/adh gate                 # what each model needs, and whether it would run now
bin/adh preflight            # check every fixture still discriminates
bin/adh plan                 # the exact dispatch order this config produces
bin/adh run                  # the round
```

Then, over the results:

```sh
bin/adh falsify   --csv results/local/results.csv    # before believing any of it
bin/adh score     --csv results/local/results.csv    # behavioural metrics
bin/adh calibrate --csv results/local/results.csv    # claim-vs-verify worksheet
```

`config.toml` is gitignored. Nothing host-specific is a literal anywhere in the
source: endpoints, paths, roster, per-model memory figures and task locations
all live in config, which is what makes the harness portable and keeps your
machine out of the repository. TOML because it parses with the standard library,
takes comments, and has no significant-whitespace traps.

Two config values must be **measured, not guessed**: `native_ctx` (use
`adh probe`) and `resident_mb` (observe RSS with the model loaded). The gate is a
prediction about a specific model; it cannot be made from a default.

### What a round produces

`results.csv`, one row per run, carrying the outcome *and* the conditions it was
produced under: both context windows, `host_ready` with the threshold that was
applied, memory and swap either side of the run, token accounting, and the stop
reason. Plus a full transcript and diff per run, which is what every post-hoc
scorer reads.

`stop_reason` distinguishes `config_ceiling` from `native_ceiling` deliberately.
One is a knob you chose and can turn next round; the other is the model's own
limit and there is nothing to turn. Collapsing them loses the difference between
"give it more room" and "this model cannot hold this task".

### Tasks

Fixtures are self-contained directories — see [fixtures/README.md](fixtures/README.md)
for the interface and for the three rules that cost a real round when they were
broken (chief among them: never document a planted defect, because `git log` is
the first thing a competent debugger runs).

Two fixtures ship, both standard-library Python, both written for this
repository and checked in both directions — pristine tree fails, small reference
solution passes:

| fixture | work class | baseline |
|---|---|---|
| `greenfield-feature-python` | greenfield feature in an existing codebase | 14 tests, 6 failing |
| `debug-from-symptom-python` | debugging from a symptom report | 14 tests, 2 failing |

Point `[[tasks]]` at your own directories to benchmark against real work.

---

## Limitations

Stated plainly, because a benchmark that oversells its own scope is worse than
no benchmark.

**Host telemetry is macOS-only.** `adh/hosttel.py` reads `vm_stat` and `sysctl`.
On any other platform it reports `supported=False` and the harness records
`host_ready=no` rather than inventing a number — so the harness runs elsewhere,
but its central feature does not. A Linux implementation over `/proc/meminfo`
(`MemAvailable`, `SwapTotal` − `SwapFree`) is small and welcome.

**Backend support is Ollama-specific.** The native path speaks `/api/chat` and
`/api/show`. An OpenAI-compatible path exists for llama-server and similar, but
its token accounting is weaker: that response shape carries no generation
duration, so `decode_s` and `out_tps` are reported as `-1` (unavailable) rather
than `0`. A missing measurement must never be readable as instant generation.

**The shipped results are n=3, one host, one round.** See [RESULTS.md](RESULTS.md).
Three repetitions is enough to make an asymmetric safety rule work — any single
disqualifying event blocks an `unsupervised` verdict — and it is *not* enough to
support a ranking, a rate, or a confidence interval. One cell of that round is
**void**, and every verdict in it is provisional pending a human calibration
read.

**The shipped results came from private task fixtures, not the ones in this
repository.** They are real data from real runs; they are not reproducible from
this repo, and the transcripts are not published because they contain verbatim
source from private codebases. That means `adh score` cannot regenerate the
shipped behavioural CSV — it is included as the original scorer's output. What
*is* reproducible is the scorer itself: run against the original transcripts it
reproduces all four behavioural metrics on all 69 rows exactly.

**Calibration is not automated, on purpose.** The obvious automation is a model
judge, which would add a second unvalidated instrument to a measurement whose
recurring failure mode is unvalidated instruments — and its errors would land on
exactly the rows where the finding is most interesting.

**The agent's `run_bash` tool is not sandboxed.** It runs arbitrary shell
commands in the staged working tree with your user's permissions. The tree is a
throwaway copy, but the process is not confined. Run rounds in a VM or container
if that matters to you.

**Iteration ceilings and wall clocks are budgets, not neutral parameters.** A
round where many runs terminate at a ceiling is measuring the budget. `adh
falsify` flags this; it does not fix it.

## Layout

```
adh/
  config.py      config loading; all host-specific values live outside the source
  hosttel.py     host memory telemetry (macOS)
  gate.py        model-relative readiness threshold, restart, bounded wait
  tools.py       agent tool surface + plain-text tool-call recovery
  worker.py      the agent loop, stop-reason taxonomy, transcripts
  task.py        fixture interface, staging, guards, verify, diffs
  runner.py      round driver; one results row per run
  score.py       behavioural scorers over archived transcripts
  calibrate.py   claim-vs-verify worksheet builder
  falsify.py     pre-registered criteria, checked against a finished round
fixtures/        self-contained task fixtures
results/         shipped round data
```

## License

MIT. See [LICENSE](LICENSE).
