# Model cold-load / warm-up cost on the Mac Studio Ollama benchmark program

Research date: 2026-08-24. Host: Mac Studio, macOS 25.6.0, 64 GiB unified memory, Ollama **v0.32.14** (homebrew, `brew services`), llama.cpp `llama-server` backend.

Read-only investigation. No model was loaded, unloaded, pulled, or inferred against; the live vision sweep (`minicpm-v:8b`, `qwen2.5-coder:14b` resident) was not disturbed.

---

## 0. Headline: the premise is wrong, and the real cost is somewhere else

The task framed this as "cold loads are a dominant share of wall-clock." **They are not.** Three independent measurements from this machine:

| Measurement | Source | Value |
|---|---|---|
| Model load time as a share of the Ollama server's whole 102.4 h log window | `/opt/homebrew/var/log/ollama.log`, 213 load events, 2026-08-20 11:19 → 2026-08-24 17:42 | **1.9 %** (1.94 h) |
| `warmup_s` as a share of `duration_s` across all v8 runs | `ollama-bakeoff/results/model-buildoff-2026-08-22/results-v8.csv`, n=69 | **1.8 %** (0.22 h of 12.07 h) |
| Inter-run driver overhead (stage + restart + readiness wait) as a share of total wall | same round's `driver.log`, 67 gaps | **10.0 %** (1.34 h) |

And the 10 % decomposes almost perfectly into one thing:

| Path after `brew services restart ollama` | n | median gap | total |
|---|---|---|---|
| `host-control: READY` | 49 | **5 s** | 5 min |
| `host-control: DEGRADED` | 18 | **250 s** | **75 min** |

**75 of the 80 minutes of inter-run overhead is the memory-readiness poll loop timing out.** It never succeeds by waiting — every run that did not clear the gate within the first poll burned the full 240 s and then dispatched anyway with `host_ready=no`. Observed yield of waiting: **0 of 18**.

Broken down by threshold (`readiness_threshold_mb` in `bakeoff-v8-lib.sh:150`):

| `need` | model | READY | DEGRADED |
|---|---|---|---|
| 55705 MB (= the 85 %-of-physical cap) | `qwen3-coder-next:q4_K_M` | 0 | **9** |
| 45600 MB | `qwen3.8:27b-q8_0` @ 64k | 7 | 8 |
| ≤ 33480 MB | everything else | 24 | 1 |

The 55705 MB threshold is the `CAP` line itself (`TOTAL * 85 / 100`). It is **unsatisfiable by construction** on a Mac that is also running WindowServer, the harness, and a second Ollama sweep. Nine runs each paid 250 s for a gate that could never open.

Fix that one loop and you recover ~9 % of program wall clock. Everything else in this report is worth 1–2 % combined.

---

## 1. Recommendations, ranked by estimated hours saved

Scaling basis: the v8 round is 69 runs / 13.4 h wall ⇒ ~0.194 h per run. A 60–80 h program ≈ **310–410 runs**. Percentages below are of *total* wall (runs + gaps), measured on v8.

### R1 — Cap the readiness wait at ~15 s, and fix the threshold cap. **Saves 5.5–7.5 h.**

**Change.** In `bakeoff-v8-lib.sh`:
1. Replace the `for _ in $(seq 1 48); do … sleep 5` loop with 3 iterations (15 s), keeping the identical `READY`/`DEGRADED` classification and the `host_ready` CSV column.
2. Separately: `readiness_threshold_mb` clamps `NEED` to 85 % of physical (55705 MB). Lower the cap to something the host demonstrably reaches — the highest `avail` ever observed post-restart in this dataset is 46919 MB. A cap of ~70 % (45875 MB) preserves the flag's discriminating power without a guaranteed-fail bucket. Record the raw `avail` and the raw `need` in the CSV so the old gate is reconstructible after the fact.

**Estimate.** 18/67 gaps × 235 s saved = 63 s/run average. Over 310–410 runs: **5.4–6.7 h**. Add the 5-of-49 READY-path cases that took 11 s instead of 3 s — negligible. Call it **5.5–7.5 h** depending on program length.

**Risk / downside.** The `host_ready` flag flips to `no` slightly *earlier* than before, so its distribution shifts. Mitigate by logging `avail` and elapsed-wait per run; any analyst can then re-derive "would the 240 s gate have passed?" — and this dataset says the answer is always no.

**Comparability.** **None.** This happens entirely between runs, outside `duration_s`, `warmup_s`, and `loop_s`. No timing semantics inside a measured run change. This is the one change that is unambiguously safe to make mid-program.

---

### R2 — Set `OLLAMA_MAX_LOADED_MODELS=1` for benchmark rounds. **Saves ~0 h; removes a real confound.**

**Change.** Add to `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` `EnvironmentVariables` (currently only `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` are in the plist; `OLLAMA_KEEP_ALIVE=24h` and `OLLAMA_MODELS` are in the running process env but *not* in the plist — worth reconciling).

**Why.** `envconfig.MaxRunners` defaults to `0`, which `sched.go:280-290` resolves to `defaultModelsPerGPU * len(gpus)` = **3 models resident at once**. Right now `ollama ps` shows two models from a *different* sweep pinned for 24 h, occupying 17.5 GB of the 51.8 GiB Metal budget. During a bakeoff, a stale resident model from another workload silently shrinks the memory available to the model under test — which is precisely the confound B3 exists to expose, arriving through a door B3 doesn't watch. `sched.go:1664 findRunnerToUnload` evicts by shortest-keep_alive-then-idle, so a 24 h pin is the *last* thing evicted.

**Risk.** If the giftcard vision sweep and the bakeoff overlap, `=1` makes them evict each other on every alternation instead of coexisting. Only set this when the box is dedicated to one program.

**Comparability.** Improves it. But it requires an `ollama serve` restart, so apply at a **round boundary**, never mid-round.

---

### R3 — Prefetch the blob into page cache during the gap, not during the run. **Saves 0.6–0.9 h.**

**Change.** In `restart_inference_server`, after staging, add `dd if=<blob> of=/dev/null bs=1m` (or `cat`) for the model about to run.

**Why it works — measured.** llama.cpp loads with `load_mode = mmap` on this host (confirmed verbatim in the server log: `load_tensors: loading model tensors, this can take a while... (load_mode = mmap)`, with buffers reported as `MTL0_Mapped`). Page-cache hits are visible as a clean bimodality in effective load throughput across 125 loads with known blob size:

| percentile | effective MiB/s |
|---|---|
| p5 | 534 |
| p25 | 1166 |
| **p50** | **1497** |
| p75 | 8759 |
| p90 | 13711 |
| p99 | 15752 |

Rates above ~8 GiB/s cannot come from any SSD; they are page-cache hits. Same blob, same day, back to back: the 27.05 GiB blob loaded in **24.3 s cold** and **2.0 s warm** (13720 MiB/s); the 48.19 GiB blob went **40.0 s → 3.0 s** (16291 MiB/s). Least-squares fits:

```
COLD  load_s = 0.886 × GiB + 1.82      →  1156 MiB/s, 1.8 s fixed overhead   (n=84)
WARM  load_s = 0.0599 × GiB + 0.50     → 17102 MiB/s, 0.5 s fixed overhead   (n=41)
```

The warm path survives `brew services restart ollama` — macOS's unified buffer cache is not tied to process lifetime, and the harness's own restarts are followed by 17 GiB/s loads in the log. **This is a directly confirmed answer to "does the page cache make the second load cheap": yes, by ~14×.**

**Estimate.** Cold *sequential read* peaks at 2916 MiB/s (see R5), while the cold *mmap fault-in* path medians 1231 MiB/s. So a 27 GiB model: prefetch 9.5 s + warm load 2.1 s = 11.6 s, versus 18.8 s of cold mmap load. ~7 s saved per big-model run, and — more valuable — the saving moves *out* of `warmup_s` into the gap. 310–410 runs × ~7 s ≈ **0.6–0.8 h**.

**Risk.** On a 64 GiB box a 48 GiB prefetch will evict most of the rest of the page cache. Harmless between runs; do **not** do it during a measured run.

**Comparability.** Improves it: `warmup_s` becomes near-constant (~2–3 s) across the roster instead of ranging 2–100 s, so `duration_s` stops carrying a model-size-dependent load term. But it *is* a change to what `warmup_s` measures, so apply at a round boundary and note it in the preregistration.

---

### R4 — Replace the per-run `brew services restart ollama` with a targeted unload. **Saves 0.3–0.5 h; do not do it mid-program.**

**Change.** Instead of restarting the server, unload every resident model with the documented 0.32.14 path and let the target load fresh:

```bash
curl -s http://localhost:11434/api/generate -d '{"model":"'"$M"'","keep_alive":0}'
```

Confirmed in source at the exact installed version: `server/routes.go:409` — `if req.Prompt == "" && req.KeepAlive != nil && req.KeepAlive.Duration == 0 { s.sched.expireRunner(m) }`, returning `DoneReason: "unload"`. The harness already has this as `unload_model()` (`bakeoff-v8-lib.sh:696`) but only calls it *after* a model finishes, not instead of the restart.

**What the restart actually buys you** that unload doesn't: a fresh Go heap, fresh scheduler state, and (as of v0.32.15) a cleared model-metadata cache. What it costs: it is what triggers the readiness gate, and it discards nothing that matters — the page cache, which is the thing that actually determines load cost, survives it anyway.

**Estimate.** The READY path already costs only ~5 s, so the direct saving is small (~3 s/run ≈ 0.3 h). The real saving is that removing the restart removes the reason to run the readiness gate at all — but R1 already captures that value, so **do not double-count**. Standalone incremental value: **0.3–0.5 h**.

**Risk.** Real. "Fresh host" is a load-bearing concept in this program's design (v7's 171 s/iteration mystery). Swapping restart→unload changes its definition.

**Comparability.** **Perturbs it.** Round boundary only, with a documented A/B (see E4 below).

---

### R5 — NVMe migration: expect capacity relief, not a load-speed win. **Saves 0.3–0.7 h.**

**Quantification, since the task asked for a number.** The cold-load fit gives 1156 MiB/s median and a 2916 MiB/s maximum over 84 cold loads. That maximum is within 1 % of the separately-measured 2927 MB/s raw local-disk figure — so the current SSD *is* occasionally the ceiling, but the median load runs at only **42 % of it**. The remainder is page-fault and buffer-allocation overhead in the mmap path, which NVMe does not touch.

Modelling a 6 GB/s NVMe as "the disk-bound half gets 2× faster, the rest is unchanged":

| model | current cold load | plausible NVMe cold load |
|---|---|---|
| 48.2 GiB (`qwen3-coder-next`, 51.7 GB) | ~44 s from fit; **21.6 s median observed** (n=26, max 100.4 s) | ~25–30 s from fit; ~14 s from median |
| 27.1 GiB (35B-class) | 18.8 s median (n=26) | ~11–13 s |

Over a 310–410-run program, halving a 1.8 %-of-wall line item is **0.3–0.7 h**. That is a rounding error against R1.

**The NVMe's actual value here is capacity.** `bakeoff-v8-stage.py`'s docstring records the constraint: "47GB free against ~70GB of models still needed", forcing LRU eviction of blobs between runs. Current state: **81 GiB free on a 460 GiB volume**. Moving the store onto NVMe lets the whole roster stay resident on disk, which eliminates staging copies *and* keeps blobs eligible for page-cache hits across runs. That, not raw MB/s, is the reason to do the migration.

**Comparability.** Migrating the store changes the storage substrate for every subsequent run. **This is a hard boundary.** No round may straddle it.

---

### R6 — Things I checked that are **not worth doing**

| Idea | Verdict | Why |
|---|---|---|
| **Batch all reps of a model together** (instead of the per-rep shuffle) | **No** | Would make 2 of every 3 loads warm: saves ~2/3 × 11.5 s × 400 ≈ **0.85 h**. But `bakeoff-v8-macstudio.sh:82-86` documents the shuffle as the fix for the rep-number/host-degradation confound that invalidated v7. Trading a known-good confound control for 0.85 h is a bad trade in a program whose value is comparability. |
| **Overlap next model's load with current model's inference** | **No** | 64 GiB physical, 51.8 GiB Metal budget (`sched.go:620` reports `available="51.3 GiB"` / `free="51.8 GiB"`). The v7 recovery run measured the failure mode directly: 5857 MB available, 12354 MB swap, 171 s/iteration against 56 s/iteration on a fresh host — a 3× degradation. Loading a second 27–48 GiB model concurrently reproduces exactly that. It would also inject disk contention into the run being measured. |
| **`--mlock` / pin weights in RAM** | **Not reachable** | llama.cpp has it (`--load-mode mlock`, `--load-mode mmap+mlock`; `--mlock` is deprecated in favour of it). Ollama 0.32.14's `appendLoadModeArgs` (`llm/llama_server.go:626-638`) emits **only** `--load-mode dio` (Linux integrated CUDA/ROCm) or `--load-mode none` (when `use_mmap:false`). There is no env var and no API option that reaches `mlock`. The full `OLLAMA_*` list at v0.32.14 (`envconfig/config.go:313-360`) contains no arbitrary-llama-server-args escape hatch. |
| **`--no-warmup`** | **Not reachable, and not worth it anyway** | llama.cpp documents `--warmup, --no-warmup` ("whether to perform warmup with an empty run (default: enabled)"). Ollama does not pass it, and the running processes on this box confirm its absence. But the *entire* fixed per-load overhead — process spawn, Metal device init, pipeline compilation, and the warmup empty run — measures **1.8 s** from the cold-load regression intercept, and small models corroborate it (1.44 GiB → 2.0 s total). There is ≤2 s on the table and no supported way to take it. |
| **llama-server slot save/restore, `--slot-save-path`** | **No** | It persists KV *cache* for a prompt, not weights. Every bakeoff run starts from a fresh task prompt, so there is nothing to restore. Ollama does not expose the `/slots` endpoints. |
| **Switch to the MLX engine** (`qwen3.8:27b-mlx` exists as of v0.32.12) | **No, not during this program** | Different runner, different numerics, different load path. It would change what is being measured. Note it as a candidate for a *future* roster, not an optimisation of this one. |
| **`purge` / dropping the page cache to satisfy the readiness gate** | **Actively harmful** | It would destroy the 14× warm-load advantage documented in R3, to satisfy a gate that R1 shows should not be waited on. |
| **Upgrade to 0.32.15** for the "TTFT ~995 ms → ~524 ms" metadata-cache fix | **Not for load cost** | Real, but it's ~470 ms per *request*, not per load, and a version bump mid-program is a comparability change for zero load-time benefit. |

---

## 2. Do it before Friday / at the NVMe boundary / not at all

### Before the NVMe lands (do now, safe mid-program)
- **R1: cap the readiness wait at 15 s and lower the `NEED` cap from 85 % to ~70 % of physical.** Only change here that touches nothing inside a measured run. ~5.5–7.5 h.
- Reconcile the plist: `OLLAMA_KEEP_ALIVE=24h` and `OLLAMA_MODELS` are set on the running `ollama serve` process (pid 48956) but are **not** in `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`. The next reboot or `brew services restart` from a clean env silently reverts `OLLAMA_KEEP_ALIVE` to its 5 m default. That is a latent instrument change waiting to happen mid-round. Zero-cost fix.
- Add `load_s` (parsed from `ollama.log`) and `readiness_wait_s` as CSV columns. Free, and it turns every future version of this question into a query instead of a research task.

### At the NVMe migration boundary (round boundary; requires re-baselining)
- **R2: `OLLAMA_MAX_LOADED_MODELS=1`** for dedicated benchmark rounds.
- **R3: blob prefetch in the gap.** Turns `warmup_s` into a near-constant.
- **R4: unload-instead-of-restart**, only if E4 below shows no s/iteration difference.
- **R5: the migration itself** — for capacity and staging elimination, budgeted as ~0.5 h of load-time saving, not more.

### Not worth it
Rep batching; load overlap; mlock; `--no-warmup`; slot save/restore; MLX swap; `purge`; the 0.32.15 bump. Rationale and numbers in R6.

---

## 3. What is actually unknown, and the cheapest experiment for each

Each experiment is sized in **GPU-occupying minutes** (time the box cannot run a bakeoff cell).

| # | Unknown | Experiment | GPU cost |
|---|---|---|---|
| **E1** | Does an explicit `dd`/`cat` prefetch actually beat the mmap fault-in path, or does mmap already read at sequential speed and the 1231-vs-2916 MiB/s gap is something else (fragmentation, Metal buffer alloc)? | In a gap: pick a blob not loaded in ≥1 h. `time dd if=<blob> of=/dev/null bs=1m`, then load it and read `load_s` from `ollama.log`. Compare against that blob's historical cold median. | **~5 min** (one load) |
| **E2** | What the NVMe actually delivers into the mmap load path. | Repeat E1 on the NVMe after migration, same blob, same procedure. Re-fit the cold regression on the first 20 post-migration loads. | **~10 min**, post-Friday |
| **E3** | Whether the readiness gate ever opens on a *longer* wait than 240 s (i.e. is 240 s just too short?). | **Already answered, 0 min.** 18/18 DEGRADED cases ran the full 240 s and none recovered; 49/49 READY cases cleared in ≤11 s. There is no middle regime. The gate is bimodal and waiting has zero observed yield. |
| **E4** | Does removing the per-run server restart change measured s/iteration? | 6 runs of `qwen2.5-coder:14b` on `clamshell-confirmation-bridge` (mean 203 s, the cheapest cell): 3 with the restart, 3 with unload-only. Compare `loop_s / iterations`. | **~20 min** |
| **E5** | Whether `OLLAMA_MAX_LOADED_MODELS=1` changes anything measurable, or only removes a latent confound. | Free to reason about, but needs an `ollama serve` restart to apply. Fold into the next round boundary; no dedicated experiment warranted. | **0 min** |
| **E6** | Is `OLLAMA_LOAD_TIMEOUT` (default 5 m, currently unset) a stall timer or a total-load timer? If total, it is *below* the harness's 900 s `WARMUP_TIMEOUT_S` and is the likelier real cause of the historical "failed to load inside 900 s" incident. | Read `llm/server.go` / the load path at v0.32.14 rather than testing. The env description says "How long to allow model loads to **stall**", which reads as a no-progress timer, but I did not trace the call site. | **0 min** (source read, ~15 min of my time) |

### Confirmed vs inferred

**Cited and confirmed** (primary source or direct measurement on this host):
- `keep_alive` semantics: negative = indefinite, `0` = unload after response, duration string / seconds accepted; per-request overrides `OLLAMA_KEEP_ALIVE` — [ollama `docs/faq.mdx` at tag **v0.32.14**](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/docs/faq.mdx), lines 297–318.
- Preload via empty request on `/api/generate` or `/api/chat` — same file, lines 267–288.
- `OLLAMA_MAX_LOADED_MODELS` default = 3 × GPU count — same file, line 334; implementation `server/sched.go:280-290` at v0.32.14.
- What evicts a resident model: (a) `needsReload` — **any** difference in *runner* options (`NumCtx`, `NumBatch`, `NumGPU`, `UseMMap`, adapters, projectors, `contextShift`) tears down and reloads even a resident model (`sched.go:1381-1426`); sampling params like temperature do **not**. (b) max-runners pressure → `findRunnerToUnload`, sorted `ByDurationAndName`, idle-first (`sched.go:1664`). (c) memory pressure → `evictAllAndWait` (`sched.go:322`). (d) keep_alive timer expiry.
- Per-request `keep_alive` **persists** on the runner: `useLoadedRunner` only overwrites `sessionDuration` when the incoming request supplies one (`sched.go:483-485`), and the load-time default is `envconfig.KeepAlive()` (`sched.go:512`). So a single `keep_alive:-1` preload **does** pin the model across subsequent calls that omit the field. Confirmed by source read; not tested live.
- Ollama 0.32.14 does not expose `mlock`; `appendLoadModeArgs` at `llm/llama_server.go:626-638`.
- llama.cpp flags `--no-warmup`, `--load-mode {auto,none,mmap,mlock,mmap+mlock,dio}`, `--slot-save-path`, `--cache-reuse` — [llama.cpp `tools/server/README.md`, master](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
- mmap is in use on this host, and Metal buffers are `_Mapped` — verbatim from `/opt/homebrew/var/log/ollama.log`.
- Every load-time, throughput, page-cache, warmup-share, and readiness-gate number in this report — measured from `ollama.log` (213 loads / 102.4 h) and `driver.log` + `results-v8.csv` (69 runs / 13.4 h) on this machine.

**Inferred, not confirmed:**
- The NVMe projections in R5. They assume the disk-bound component halves and the rest is unchanged. E2 resolves it.
- The claim that the 1231-vs-2916 MiB/s gap is mmap page-fault overhead. It is the residual after subtracting a known ceiling, not a profiled cause. E1 resolves it.
- That `dd` prefetch beats mmap fault-in. Plausible from the throughput gap; untested. E1.
- That removing the per-run restart is behaviourally safe. E4.
- The 310–410-run program size, extrapolated from v8's runs-per-hour.

---

## 4. Claims I could not verify — and two in the brief that are wrong

**"`qwen3-coder-next` at 51.7 GB takes ~173 s per load."** **Not reproduced.** That blob is 48.19 GiB on disk and appears 26 times in the 102 h log: **median 21.6 s, min 2.3 s, max 100.4 s.** No load of it took 173 s in the log window. The figure most likely dates from the SMB-mounted era, when `bakeoff-v8-stage.py`'s docstring records it failing to finish inside a 900 s budget. Since the store moved local, it is a ~22 s load. **Any planning that budgets 173 s for this model is overestimating by ~8×.**

**"There is separate evidence that Metal kernel warm-up matters — a comment in the user's sweep script says kernels 'accumulate' across models."** **Misread.** The comment is `giftcard-ocr/scripts/queue-macstudio-vision.sh:4`:

> `# WHY ALL MODELS, not just the missing ones: Metal and CUDA kernels accumulate`
> `# floating point differently, so a cross-host comparison can attribute a GPU`
> `# difference to a model.`

That is about **floating-point accumulation order** differing between Metal and CUDA kernels — a numerics/comparability argument for re-measuring the whole roster on one host. It says nothing about warm-up cost. Independently: the measured fixed per-load overhead, which contains all of process spawn + Metal init + pipeline compilation + the llama.cpp warmup run, is **1.8 s**. Metal kernel warm-up is not a cost centre here.

**Genuinely unverified:**
- Whether `keep_alive:-1` survives across an `ollama serve` restart. It does not (the FAQ says pinning is runtime state), but I did not test — testing requires restarting the server, which is off-limits while the sweep runs.
- Whether `OLLAMA_LOAD_TIMEOUT`'s 5 m default is a stall timer or a total-load timer (E6). If total, it silently caps the harness's 900 s warmup budget at 300 s.
- Whether llama.cpp's Metal pipeline cache (`ggml_metal_pipelines`, an in-process `unordered_map`) has any cross-process component. Search results describe it as per-`ggml_backend_metal_context`, i.e. per-process, which would mean nothing survives a runner restart — but I could not confirm this against the exact ggml revision Ollama 0.32.14 vendors. It does not matter given the 1.8 s ceiling.
- Whether the 88 load events in the log whose blobs no longer exist on disk (median 10.3 s) skew the aggregate. They are excluded from every fit and percentile above; only the 125 known-size loads are used.
- I did not verify how `bakeoff-v8-lib.sh` derives `warmup_s` for the **v8** worker specifically. The v7 worker logs an explicit `warmup_s=` line (`ollama-worker-v7.py:1174`) that the lib greps for; I confirmed the v8 worker times the same warm-up generate call but did not trace its log emission. If v8 emits that line differently, the "1.8 % warmup share" figure could be understated. It would not change the conclusion — the `ollama.log`-derived 1.9 % is an independent measurement of the same quantity and agrees.

---

## Sources

- [ollama/ollama `docs/faq.mdx` @ v0.32.14](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/docs/faq.mdx)
- [ollama/ollama `server/sched.go` @ v0.32.14](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/server/sched.go)
- [ollama/ollama `server/routes.go` @ v0.32.14](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/server/routes.go)
- [ollama/ollama `llm/llama_server.go` @ v0.32.14](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/llm/llama_server.go)
- [ollama/ollama `envconfig/config.go` @ v0.32.14](https://raw.githubusercontent.com/ollama/ollama/v0.32.14/envconfig/config.go)
- [ollama/ollama releases](https://github.com/ollama/ollama/releases) — v0.32.14 (2026-08-15), v0.32.15 (2026-08-19), v0.33.0-rc3 (2026-08-21)
- [ggml-org/llama.cpp `tools/server/README.md`](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Metal Backend (Apple) — DeepWiki, ggml-org/llama.cpp](https://deepwiki.com/ggml-org/llama.cpp/5.2-metal-backend-(apple))
- [ollama/ollama issue #16084 — dedicated CLI preload command](https://github.com/ollama/ollama/issues/16084)
- [ollama/ollama issue #2431 — ability to preload a model](https://github.com/ollama/ollama/issues/2431)
- Local, this host: `/opt/homebrew/var/log/ollama.log`; `ollama-bakeoff/results/model-buildoff-2026-08-22/{driver.log,results-v8.csv}`; `ollama-bakeoff/harness/bakeoff-v8-{lib,macstudio,hosttel,stage}.*`; `giftcard-ocr/scripts/queue-macstudio-vision.sh`; `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`.
