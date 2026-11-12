# v12 Strata head-to-head plan (2026-10-05)

**Question:** Is Strata (Niko1221/Strata @ 6f32ec0, Qwen3.8-Flash-Next 125B IQ2_XS, native CUDA on the claude-sandbox RTX 3080 12 GB) good enough to wire in as a dispatch-lane model, compared with the current dispatch model qwen3.6-35b-a3b-vl-mtp-mxfp8 on local Darkbloom?

**Speed so far** (trial job 36d2d7751cb5): 196 s to load, 1310 tok/s prefill, about 52 tok/s decode, 11,595 of 12,288 MiB peak VRAM. In two trial tests the model spent its whole token budget reasoning, so this round gives it a reasoning budget.

## Cells: existing, calibrated, unchanged
| cell | grader | discriminates? |
|---|---|---|
| plex-release-group-debug (v8 lib `_one_task_v8`) | RUN_VERIFY restores both test files from 33d4735, then runs them | yes (the repro must FAIL on the pristine tree; VACUOUS_PASS probe) |
| bulk-codemod-v10 (cell E) | byte-diff oracle `bakeoff-v10-bulk-score.py` (49 sites / 26 ident / 23 meta) | the oracle carries the score; py_compile is only a gate |

3 reps per cell per arm, 3600 s wall each, `--max-iters 30`.

## Controls
The same for both arms:
- task prompts (the META file records their sha256; checked equal: debug 8463f419…, bulk 9378d6e7…)
- tools, worker (`~/bin/ollama-worker.py --api openai`) and grader
- sampling: T 0.2, top-p 0.95, top-k 20
- `--max-tokens 16384`

Declared differences:
- **num_ctx:** Strata gets 32768, the most the 12 GB card holds. qwen gets 65536 (the v8 rule). Both models have a native context of 262144, so a Strata context ceiling is recorded as `config_ceiling`, a limit of our hardware rather than of the model.
- **Reasoning budget:** Strata gets a server-side `reasoning_budget_tokens` of 4096 (`STRATA_REASONING_BUDGET`). Darkbloom runs as it does in production.
- **Strata run config only:** `fit_max_tokens=true`, so a 16K max_tokens request in a 32K window is clamped instead of returning a 400 that would be scored against the model.
- **Host:** this is the deployment question itself.

## Checks on the grader (suspect the grader before concluding incapacity)
- `bakeoff-v12-calibrate.py` runs before any arm, and nothing runs unless it passes. Each debug-grader case uses a fresh clone at 33d4735:
  - pristine tree: FAIL
  - known-good fix: PASS
  - **alternate correct fix** (different text, same behaviour): PASS
  - test-gaming: FAIL
  - partial fix: FAIL
  - cell E oracle `--calibrate`

  All passed on 2026-10-05.
- `results-v12-strata-h2h-regrade.csv` records, per row:
  - an independent verify re-run
  - **writes to test files** counted from the transcript (the restore in RUN_VERIFY hides them from the diff)
  - chat retries and HTTP errors
  - whether a context ceiling was hit
  - the reasoning budget

  Read this before calling any FAIL a model failure.
- Infrastructure endings (a signal that is not the wall-clock limit, or the endpoint going down) write **no row**. The driver exits 4 and a re-queue resumes from that point.

## How it runs (queue only)
- `strata-h2h/ENQUEUE.sh` (staged) adds four jobs in bundle `strata-h2h`:
  - two `bakeoff-runner.py` jobs on `studio-db` (the qwen arm)
  - two `enqueue-gpu` jobs on `unraid`, each running `~/bin/strata-h2h-arm.sh <cell>`
- Each GPU job runs in this order:
  1. The queue evicts Unraid Ollama.
  2. The VRAM check waits until no more than 1024 MiB is in use.
  3. `strata-serve.sh start` runs `gpu_guard`, which records what Ollama had loaded and the free VRAM in `remote/preflight-guard.txt`. It then writes a budgeted run config (0600) and starts the server with a watchdog.
  4. A tunnel forwards 127.0.0.1:18180 to the sandbox.
  5. The driver runs.
  6. A trap stops everything, deletes the key file and copies back peak VRAM/RAM.
- The Strata key goes to the worker via `--api-key-file` (a 0600 file in the run dir). It never appears in argv, a log or the CSV.

## Outputs (`model-buildoff-2026-08-22/`)
- `results-v12-strata-h2h.csv`: debug cell, 28 columns
- `results-v12-strata-h2h-cellE.csv`: 18 columns
- `results-v12-strata-h2h-regrade.csv`
- `results-v12-strata-h2h.META.md`
- `strata-h2h-runs/<ts>-<cell>/`: arm.txt, plus remote/{preflight-guard.txt, serve.txt, mon.csv, server.log}

## Verdict rule
- Quality: debug-cell verify pass rate (net of tamper flags) and cell-E sites/ident/meta, per arm, over 3 reps.
- Speed: wall time per converged run and out_tps.
- Whether to wire Strata as a lane depends on more than its quality. Every Strata job holds the GPU the Unraid pre-gate uses, about 4 minutes of load plus the run time.


## Update 2026-10-09: Strata context raised to 131072
The owner asked for more context at a similar VRAM. Sandbox config (strata-iq2_xs.json, backup .bak-ctx131k) now has `--max-context 131072 --kv int8 --kv-resident 32768`: KV cache lives in pinned host RAM (251 GB box, ~13.7 KB/token => ~1.8 GB at 128K), only the 32K attended window sits in VRAM. Declared difference vs qwen is now context-equal (qwen auto-ctx 131072). The first debug job (b421bd18a41b) was stopped at load and re-enqueued as bo-v12-strata-debug-r2; compare peak VRAM and tok/s against the 32K trial (52 tok/s, 11.6 GB).
