# agent-dispatch-mcp

An MCP server that makes dispatching work to local models **safe and measurable**.

Existing Ollama MCP servers expose raw access — `chat`, `generate`, `ps`, `list`. That
is a transport, and a transport is fine right up until you start making decisions
from the results. This server adds the three things that turn dispatch into
something you can actually reason about:

1. **Host-readiness gating.** Refuse or wait when the host cannot fit the model at
   the requested context, instead of producing a run that pages.
2. **Claim-vs-verify enforcement.** Run a verifier *after* the model claims done,
   and report whether the claim held.
3. **Routing rules that ship with their own falsification status.** Every verdict
   carries an explicit confidence field. None of them is confirmed, and the server
   says so in every response.

MIT licensed. stdio transport. macOS only for host telemetry today (see
[Limitations](#limitations)).

---

## 1. Host-readiness gating

This is the most-validated piece here and the reason the project exists.

A model that starts loading onto a host without enough free memory does not fail.
It **pages** — and a paging run still returns output, still records a duration, and
still looks like a data point. The same cell, same config, same model, going 44 →
42 → 53 → 86 seconds per iteration across four repetitions is not a measurement of
anything; it is a measurement of what else was resident at the time. Raising the
timeout does not fix it, it just moves the failure: one run with the wall raised to
3600s timed out anyway at 171 s/iteration, against 56 s/iteration for the identical
cell on a fresh host.

So the gate predicts, before dispatch, whether the next model load fits.

### The rule

```
threshold_mb = min(
    resident_mb * 1.2  +  (num_ctx / 1024) * 150,
    total_mb * 0.85
)
```

compared against **available** memory.

**`resident_mb * 1.2`** — weights plus load-time headroom.

**`(num_ctx / 1024) * 150`** — the KV cache. This is the term that matters and the
term that was wrong.

**`total_mb * 0.85`** — the cap. A threshold the host can never reach even when
completely idle makes every run wait out the full timeout and report "not ready"
universally, which destroys the flag's meaning rather than protecting anything.
The cap only binds at very large context sizes.

### Why it gates on *available*, never on swap

`free` alone is close to meaningless — on a busy machine it reads in the tens of
megabytes, because inactive, speculative and purgeable pages are all reclaimable
under pressure. **Available** = free + inactive + speculative + purgeable is what
predicts a clean load.

Swap is reported but **never gated on**. macOS does not shrink its swap files
promptly when pages are freed, so swap sits high long after the memory is genuinely
back. A swap gate blocks runs that would have been fine — and a gate that blocks
good runs is a gate people turn off.

### The KV constant was wrong by 75x, and that mattered

This is preserved here as rationale, not trivia, because the shape of the error is
the argument for the gate existing at all.

The constant was originally **2 MB per 1k tokens**. That is 2 KB per token. The real
figure for a model in this size class — roughly 64 layers, 8 GQA KV heads, head_dim
128, q8_0 K and V cache — is:

```
2 (K and V) × 64 layers × 8 heads × 128 dims × 1 byte = 128 KB per token
                                                      ≈ 128 MB per 1k tokens
                                                      ≈ 8.4 GB at 65536 context
```

The old constant allotted **128 MB** for that entire **8.4 GB**.

Why this was not a rounding error: the gate exists to predict paging, and the runs
that motivated it were exactly the large models at 64k context — precisely where the
KV term dominates. A 75x-short KV term meant `host_ready=yes` got stamped on runs
that were about to page. The gate was certifying clean the exact confound it had
been built to detect. Corroborated in practice: a ~30GB model at 64k drove ~12 GB
into swap while the gate reported the host ready.

**150 rather than 128** deliberately errs safe. Head counts and cache quantisation
vary across a roster. A too-generous gate costs a wait; a too-tight one costs a
silently invalid measurement. Both constants are configurable.

### Lowering context is a weak lever

Worth knowing before you reach for it. For a 30GB-resident model:

| num_ctx | KV term | threshold |
|---|---|---|
| 65536 | 9.6 GB | **45.6 GB** |
| 32768 | 4.8 GB | 40.8 GB |
| 16384 | 2.4 GB | 38.4 GB |
| 8192 | 1.2 GB | 37.2 GB |

The threshold converges on a 36 GB floor — the weights term — no matter how small
the context gets. Trimming context buys at most 9.6 GB. If you are 20 GB short,
context is not your lever.

### Tools

| tool | what it does |
|---|---|
| `readiness_threshold` | Threshold for a model+context, with the full arithmetic broken out so it is auditable rather than opaque |
| `host_telemetry` | Current available / free / total / swap / load |
| `check_host_ready` | Single-shot gate check, returns the exact shortfall in MB |
| `wait_for_host` | Poll until ready or the (always bounded) timeout expires |
| `gated_dispatch` | Dispatch behind the gate — refuses or waits rather than thrashing |

`gated_dispatch` returns one of three dispositions: `proceeded`, `waited`, or
`refused`. `force=true` overrides a refusal but never silently — the result still
carries `host_ready=false` and the shortfall, so a downstream consumer can exclude
that run from any timing comparison.

---

## 2. Claim-vs-verify enforcement

A model saying "done, the build passes" is not evidence that the build passes. Raw
inference APIs return the claim and stop, so the claim is what enters the record,
and a fabricated completion is indistinguishable from a real one until a human
happens to look.

`gated_dispatch` runs your verifier command after the claim and reports both.

### The taxonomy

| class | meaning |
|---|---|
| `honest_success` | Claimed complete, verifier agrees. The only class supporting unsupervised dispatch. |
| `FABRICATED_COMPLETION` | Claimed complete, verifier says otherwise. A disqualifier, not a data point. |
| `unverifiable_claim` | Claimed complete, no verifier could run. **Not a pass and not a failure.** |
| `honest_partial` | Explicitly reported incomplete work, verifier failed. The claim was accurate. |
| `honest_recognition` | Reported no change needed, verifier passes unmodified. |
| `no_claim` | Iteration cap, error, or stopped mid-work. Exclude from calibration rates. |

`unverifiable_claim` exists specifically so that missing instrumentation cannot be
silently scored as success — the commonest way an instrument like this quietly
stops measuring anything. For the same reason, a verifier that could not run yields
`passed: null`, never `false`: conflating "did not run" with "failed" manufactures
fabrication reports out of misconfiguration.

### ⚠️ This taxonomy is NOT validated

**No human calibration read has been performed.** Nobody has taken a sample of runs,
classified each by hand, and checked that the automated classifier agrees. This is a
**proposed instrument, not a proven one.**

In particular, the keyword-based claim detection cannot tell whether the model's
claim was *about* the same thing the verifier measured — and `honest_success` versus
`FABRICATED_COMPLETION` is exactly the distinction that requires that judgement.

Every classification the server emits carries `taxonomy_validated: false`. **Do not
strip that field.**

It is deliberately **not** automated with an LLM judge. Adding a second unvalidated
instrument to measure the first does not produce validation; it produces two
unvalidated instruments and a false sense of rigour.

### Behavioural metrics

`score_behaviour` computes four metrics from a run transcript, because outcome
fields are ambiguous. `files_changed=0` is written identically by a model that
explored for 25 iterations and never wrote anything, and by a model that correctly
recognised the feature already existed. Those are opposite results.

| metric | the decision it informs |
|---|---|
| `time_to_first_mutation` | All looking and no doing, or all doing and no looking? Opposite fixes. |
| `self_verify_count` | Did the model run the build/test *itself* before claiming done? |
| `churn_ratio` | Repeated identical calls — would more iteration budget help, or was it spinning? |
| `diff_magnitude` | A 40-line surgical change and a 2000-line bulldozer both read as `files_changed=6`. |

`self_verify_count` is a positive requirement, not a tiebreak: "was not caught lying
three times" is thin evidence for trust, whereas "checks its own work" is a
mechanism.

**The verifier list is a whitelist and therefore a systematic under-counter.**
`tsc --noEmit` and `swift test` were both missing from it for most of its life. That
was not cosmetic — runs that genuinely ran `tsc` + `eslint` and checked their own
work scored zero for it, silently costing a model credit it had earned. Both are now
included. Extend the list for your own toolchain via `[verifiers] extra` in config
rather than accepting a quiet undercount.

---

## 3. Routing rules

`routing_index`, `routing_query` and `routing_recommend` serve a table mapping
work class × model → `unsupervised` / `harness-verified` / `scaffolded` /
`don't dispatch`.

**Every entry carries an explicit `status` field, and it is machine-readable, not
prose.** This is a hard design constraint: a consumer must not be able to read a
provisional verdict as settled. `routing_recommend` never returns a bare verdict —
the verdict, its status, and the caveats come back as one object, because separating
them is exactly how a provisional finding turns into folklore.

### The falsification status

| status | meaning |
|---|---|
| `confirmed` | Survived falsification checks **and** a human calibration read. **Nothing currently holds this status.** |
| `provisional` | Survived falsification checks, but rests on an objective proxy alone. |
| `void` | The producing cell was invalid. Carries **no verdict in either direction.** |
| `not_measured` | No data collected. Not a negative verdict. |

Three specific facts that must survive contact with any consumer:

**The debugging row is VOID.** That cell was context-starved — an uncapped read of a
228KB source file, with 10 of 18 runs hitting the iteration ceiling at 5-6
iterations. Its apparent clean 0/18 measures a starved harness, not the models and
not the work class. An earlier draft read it as a genuine finding about the
difficulty of the work class; that reading was wrong and has been withdrawn. The row
carries no verdict for any model, and **absence of a verdict is not a negative
verdict.**

**Every `unsupervised` verdict is PROVISIONAL.** The verdicts use an objective proxy
— *verifier passed* AND *self-verified in every passing run* — not the full intended
rule, which additionally requires a human claim-vs-verify calibration read. That
read has not been done. The proxy has one specific blind spot that matters: it
cannot see a model that claimed success on a run whose verifier failed. That is
`FABRICATED_COMPLETION`, an instant disqualifier, and no `unsupervised` verdict here
has been screened for it. Treat them as strong candidates to try under verification,
never as settled routing policy.

**One zero-tool-call run was a HARNESS failure, not an inert model.** The model
emitted a Qwen-XML tool-call dialect the harness could not parse, so zero calls
executed. Any "the model did nothing" inference from that row is invalid, and the
model in question is judged more harshly in the table than the evidence supports.
Where a harness cannot parse a dialect, the correct record is a harness failure.

`routing_query(min_status="confirmed")` returns **zero rows**. That is the honest
answer, not a bug.

---

## Install

```bash
git clone <this repo> && cd agent-dispatch-mcp
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp config.example.toml config.toml   # config.toml is gitignored
```

Register with an MCP client:

```json
{
  "mcpServers": {
    "agent-dispatch": {
      "command": "/absolute/path/to/agent-dispatch-mcp/.venv/bin/agent-dispatch-mcp",
      "env": { "AGENT_DISPATCH_CONFIG": "/absolute/path/to/config.toml" }
    }
  }
}
```

## Configuration

Nothing host-specific is baked into any module. Endpoints, workspace paths, model
tags and resident sizes all come from `config.toml` (gitignored) or from the
environment — `AGENT_DISPATCH_ENDPOINT`, `AGENT_DISPATCH_WORKSPACE`,
`AGENT_DISPATCH_KV_MB_PER_1K`, and the rest, documented in `config.example.toml`.
Environment variables win over the file, which is the right channel for anything you
do not want written to disk.

### The shipped resident-size table is one machine's measurements

`DEFAULT_RESIDENT_MB` holds resident-set sizes observed for a handful of models
under **one runtime, one quantisation, one host**. They are shipped as a documented
starting table, **not as facts about the models**. Resident size varies with runtime,
quantisation, KV cache type, memory-mapping behaviour and offload settings. If your
numbers differ, yours are right and these are wrong — override them.

To measure your own: load the model, let it settle, read the resident size your
runtime reports (`ollama ps`), round up. Providing a `[resident_mb]` section
*replaces* the built-in table wholesale rather than merging with it, so measurements
from one machine are never silently mixed with numbers from another.

Any model absent from the table falls back to a conservative default, on the
principle that over-demanding costs a wait while under-demanding costs a paging run
whose timings look like data but are not.

## Why Python

Three reasons, in order of weight:

1. **The logic being ported is Python and shell.** The telemetry reader and the
   behavioural scorer already existed in Python. A TypeScript rewrite would have
   meant re-deriving the KV arithmetic and the transcript-parsing edge cases by
   hand, which is precisely how the 75x bug documented above survived as long as it
   did.
2. **The work is subprocess-shaped.** `vm_stat`, `sysctl`, and running an arbitrary
   verifier command in a workspace are all process spawns with output parsing.
   Python's `subprocess` handles this in the standard library.
3. **Runtime dependencies stay at one.** Only the `mcp` SDK. Everything else —
   TOML parsing, HTTP, JSON — is stdlib. For a server whose whole purpose is being
   trustworthy about memory, a large dependency tree would be self-undermining.

## Limitations

- **Host telemetry is macOS-only.** `vm_stat` and `sysctl`. Linux is a clean
  drop-in: implement `available_mb` / `total_mb` / `snapshot` (from
  `/proc/meminfo` `MemAvailable`) and register the backend in
  `telemetry.get_backend`. Nothing else in the package touches the platform — the
  rest of the code sees only the `HostTelemetry` protocol and the `HostState`
  dataclass. The server returns a clear error rather than a wrong number on
  unsupported platforms.
- **The claim taxonomy is unvalidated.** See above. This is the largest caveat in
  the project.
- **No routing row is confirmed.** All are `provisional`, `void`, or
  `not_measured`.
- **The routing data comes from a single round on a single host** with one model
  roster, three repetitions per cell. It is not a benchmark and makes no general
  claim about these models. Re-measure before relying on any row.
- **`gated_dispatch` makes one model call**, not an agentic tool-execution loop.
  That belongs in whatever harness you already run; this server is designed to sit
  in front of such a harness rather than replace it.
- **The gate predicts; it does not reserve.** Another process can take the memory
  between the check and the load. The gate narrows the window, it does not close it.
- **The KV formula assumes a transformer with a conventional GQA KV cache.** Models
  with sliding-window attention, or state-space and hybrid architectures, have
  materially different context memory scaling and are not modelled correctly.
- **Two work classes were never measured at all** — follow-up on a model's own prior
  output, and bulk/mechanical transforms.

## Development

```bash
.venv/bin/python -m pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
