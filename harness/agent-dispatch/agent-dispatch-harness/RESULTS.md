# Results — round 2026-08

69 runs. Six models, three tasks, three arms, n=3, one host, one Ollama
instance.

**Read the two warnings first.**

> **One cell of three is void.** The debugging cell produced 18 rows and zero
> usable results. The cause is identified below and it is an instrument defect,
> not a model property. Nothing in that cell may be read as a finding about any
> model.
>
> **No verdicts are final.** The claim-vs-verify calibration pass has not been
> done. It requires a human reading each run's final summary against ground
> truth ([METHODOLOGY.md](METHODOLOGY.md) §2), and until it is done no cell can
> be assigned `unsupervised` — the taxonomy, not the pass rate, is what governs
> that verdict. Everything below is provisional.

The data is in [`results/round-2026-08/`](results/round-2026-08/). Its
provenance, and the limits on reusing it, are in
[that directory's README](results/round-2026-08/README.md).

---

## The round survives four of its five criteria

Actual output of `bin/adh falsify --csv results/round-2026-08/results.csv`:

```
pre-registered falsification check — 69 rows

  [ok    ] gate_failure         9/60 REACHABLE-gate runs were host_ready=no
                                (15.0%, threshold 20.0%); 9 rows excluded as STRUCTURAL
  [ok    ] wall_binding         0/69 runs hit the wall clock
  [ok    ] verify_not_running   verify reached a verdict on a majority of rows for every task
  [ok    ] token_accounting     prompt_tokens missing on 0/69, output_tokens on 0/69
  [FIRED ] context_starvation   cells where a majority of runs never converged:
                                debug-symptom-python/base (11/18) — treat these cells as VOID

stop_reason: none=37, config_ceiling=12, converged=10, iter_cap=9, native_ceiling=1
verify:      no=49, yes=20
```

Taking those in turn.

**Host gating worked.** 18 of 69 runs were dispatched onto a host that had not
recovered — but 9 of those are a single model whose memory requirement exceeded
clean-host available memory before the round began. That model was classified
STRUCTURAL in advance; it pages regardless of freshness, and its `host_ready=no`
is a routing fact ("needs a bigger host"), not a gating failure. Counting only
cells whose gate was reachable: 9 of 60, 15%, under the 20% threshold fixed
beforehand.

This distinction is the whole reason the classification is made before dispatch.
Made afterwards, "those don't count" is unfalsifiable.

**The wall was never binding.** Zero timeouts across 69 runs. In the round that
motivated this harness, timeouts were the dominant loss.

**Token accounting is complete.** All 69 rows carry prompt and output token
counts, so throughput figures are usable.

---

## The debug cell is void

18 rows. Zero verify passes. 11 of 18 terminated at `config_ceiling` — the
model filled its configured context window before finishing.

**Cause, confirmed from the transcripts:** the fixture's main module is a single
228,649-byte file. The agent's `read_file` tool had no size cap, so reading it
returned roughly **226,700 characters in one tool result** — against a 65,536-
token window, which that single result exceeds several times over. Every model
in the cell did this, and one did it twice in the same run.

After that, the run is over in every sense that matters. The oldest turns —
including the task itself — get evicted, and the model spends its remaining
iterations reasoning about a fragment it does not know is a fragment.

Two things follow.

**It reads as incompetence and is not.** Six models, five of them from
independent families, all failing a debugging task 18/18 is the shape of a
striking finding. It is entirely explained by one missing constant in the
harness. Any write-up that reported "no local model can debug from a symptom
report" from this cell would have been confidently, systematically wrong, and
the confidence would have come from the sample size.

**The fixture design was sound; the tool was not.** The reproduction failed on
the pristine tree as intended, the ground-truth tests were restored before
verify, and the defect was invisible to the pre-existing suite. None of that
mattered, because the cell never reached the point where any of it applied.

Fixed in this harness: `read_file` takes a configurable character cap
(`[worker] read_file_max_chars`), announces truncation to the model, and offers
`start_line`/`max_lines` so a large file can be read in slices. The
`context_starvation` criterion in `adh falsify` exists to catch the next version
of this before anyone writes it up.

Cost of the omission: 18 runs, and the entire work class the round was extended
to cover.

---

## What the two usable cells show

Provisional. Pass counts are base arm, n=3.

### Greenfield feature in an existing codebase (TypeScript, large repo)

| model | verify passed | files changed |
|---|---|---|
| `qwen3-coder:30b` | 3/3 | 2, 4, 5 |
| `qwen3.8:27b-q8_0` | 3/3 | 3, 1, 3 |
| `deepseek-r1:32b` | 2/3 | 1, 1, 0 |
| `qwen3-coder-next:q4_K_M` | 2/3 | 5, 3, 1 |
| `qwen3-14b-agentic` | 0/3 | 1, 2, 2 |

### API-recall-dependent work (Swift)

| model | base | with API surface supplied |
|---|---|---|
| `qwen3.8:27b-q8_0` | 3/3 | 3/3 |
| `deepseek-r1:32b` | 0/3 | 0/3 |
| `qwen3-14b-agentic` | 0/3 | 0/3 |
| `qwen2.5-coder:14b` | 0/3 | — |
| `qwen3-coder:30b` | 0/3 | — |
| `qwen3-coder-next:q4_K_M` | 0/3 | — |

One model passed; five did not, and supplying the real API surface verbatim did
not rescue either of the two models it was tried on. That is a **negative
result for the arm's hypothesis** on those two models: their failure is not
explained by missing recall alone. It is reported because a negative arm result
is exactly as informative as a positive one and considerably easier to leave out.

### Arms

| | base | treated |
|---|---|---|
| `repomap` on `qwen3-14b-agentic` (the subject) | 0/3 | 1/3 |
| `repomap` on `deepseek-r1:32b` | 2/3 | 0/3 |
| `repomap` on `qwen3.8:27b-q8_0` (the control) | 3/3 | 3/3 |

The salience hypothesis is **not supported** here. The subject moved by one run,
the control did not move, and the third model moved two runs in the *wrong*
direction. At n=3 all three of those are consistent with noise. The honest
reading is that this arm needs more repetitions, not that a repo map hurts.

### Self-verification

Base arm, count of runs where the model ran a build or test itself before
claiming done:

| model | greenfield | API-recall | debug (void) |
|---|---|---|---|
| `qwen3.8:27b-q8_0` | 2/3 | 3/3 | 2/3 |
| `qwen3-coder-next:q4_K_M` | 1/3 | 3/3 | 2/3 |
| `qwen3-coder:30b` | 0/3 | 2/3 | 1/3 |
| `deepseek-r1:32b` | 0/3 | 1/3 | 0/3 |
| `qwen3-14b-agentic` | 0/3 | 0/3 | 0/3 |
| `qwen2.5-coder:14b` | — | 0/3 | 0/3 |

This is the column that matters most for `unsupervised`, and it does not track
pass rate. `qwen3-coder:30b` passed the greenfield cell 3/3 while never once
checking its own work there; `qwen3-coder-next` self-verified consistently on a
cell it failed. A model in the first category is dispatchable only behind a
harness that runs the build for it — which is precisely the distinction between
the `unsupervised` and `harness-verified` verdicts.

---

## The routing table, as far as this round fills it

| work class | verdict |
|---|---|
| greenfield feature, large repo (TS) | **provisional**, pending calibration — two models at 3/3, but self-verify is uneven |
| API-recall-dependent work (Swift) | **provisional** — one model at 3/3; `don't dispatch` indicated for the rest, and the API-surface treatment did not change that |
| debugging from a symptom report (Python) | **VOID** — instrument defect, no data |
| exploration under low salience | **inconclusive** — arm underpowered at n=3 |
| work with API surface supplied | **negative result** on the two models tested |
| follow-up on own prior output | **untested** |
| bulk or mechanical transforms | **untested** |

Four of seven rows are void, inconclusive, or untested. That is the honest
output of one round, and it is stated as such rather than filled in by inference
from the rows that did work.

---

## What this round is not

* **Not a ranking.** n=3 supports an asymmetric safety rule — a single
  disqualifying event blocks `unsupervised` — and nothing else. It does not
  support an ordering, a rate, or an interval.
* **Not reproducible from this repository.** These runs used private task
  fixtures. The results are real; they are not re-runnable here, and the
  transcripts are not published because they contain verbatim source from
  private codebases. The two fixtures that ship are synthetic replacements in
  the same work classes, not the originals.
* **Not a claim about these models in general.** One host, one quantisation set,
  one prompt, one harness, one day.

## Reproducing the scoring

The behavioural CSV shipped here is the original scorer's output. The scorer in
this repository was checked against it: run over the original transcripts, it
reproduces `time_to_first_mutation`, `self_verify_count`, `churn_ratio` and
`tool_calls` **identically on all 69 rows**. `diff_lines` is not comparable,
because this version resolves diffs from an explicit column rather than a glob —
that change was made because the glob matched every model's diff for a cell and
took the first alphabetically, which was wrong for most rows.

The falsification checker runs against the shipped CSV directly; the output at
the top of this document is its real output, not a transcription.
