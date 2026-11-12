# Offloading Claude usage to Darkbloom models: clean-slate design vs. what we run now

## Context
The owner asked for a system designed from scratch that moves as much Claude usage as possible onto models Darkbloom can host, so session limits last longer. It should then be compared with the current dispatch pipeline and queue.

- **Clean-slate design:** a Plan agent wrote it without reading any of our pipeline code. It worked only from the model cards and the Darkbloom docs.
- **Existing pipeline:** the mapping agent is paused at the owner's request. This side of the comparison therefore comes from what this session already knows about the pipeline: the memory index, the failure ledger numbers, today's agent reports, and the hooks that fire in this session. The numbers marked *(measure)* have not been verified.

Hardware: Mac Studio M4 Max with 64 GB. Darkbloom can serve:
- Qwen3.6 35B-A3B (~21 GB, the strongest coder here)
- Qwen3.5 35B-A3B
- Qwen3.5 9B
- Gemma 4 26B
- Nemotron 3.5 Lightning
- Bonsai 27B ternary
- GPT-OSS 20B

Qwen3.8 is not eligible on this machine.

## 1. Clean-slate design (summary)
**Main idea:** keep raw bytes out of Claude's context. Use deterministic tools first, local models second and Claude last. Every result that comes back gets checked mechanically before Claude acts on it.

Prices and shares below are estimates, weighted by token price.
- Reading and exploring is about 40% of Claude usage. Command output (tests, builds, logs) is about 20%. Writing code is about 15%. Planning is about 12% and cannot be offloaded.
- Realistic target: 35–50% fewer Claude tokens per landed change. Anything claimed above 60% usually means quality is being traded away without anyone noticing.

Four small parts:
1. **`lx` CLI**, called from Bash and described in about 400 tokens of CLAUDE.md:
   - `lx run -- <cmd>`: full log to disk; returns ≤1.5k tokens (exit code, failing tests, first error with file:line). Uses plain parsers, and the LLM only for messy logs.
   - `lx ask "<q>" paths`: narrows with rg and a repo map, then answers. Every claim must cite `path:line` with a quoted snippet that is checked mechanically; it may ABSTAIN. Answers are ≤800 tokens.
   - `lx task --spec --verify --scope`: a mini-swe-agent loop in a throwaway worktree, with a fail-before/pass-after requirement. Returns PASS or FAIL with the diff, or a 5-line failure reason (never the transcript).
   - `lx review`: plain checks plus at most 5 high-severity findings, each with a citation.
2. **Darkbloom direct mode, one resident model:** Qwen3.6 35B-A3B.
   - With only 3B active parameters it decodes as fast as the 9B, so the 9B saves memory but not time.
   - No model switching. If the model is unavailable, return `LOCAL_UNAVAILABLE` and Claude does the work itself, with no silent fallback to a weaker model.
   - Qwen's prefix cache is not reused (`supportsPrefixReuse=false`), so local prompts must stay small.
3. **Worktree runner** with diff checks:
   - only files inside the scope may change
   - no deleted asserts, `skip`, `@ts-ignore` or `eslint-disable`
   - no lockfile changes
   - Claude writes the acceptance test, and Claude always reads the final diff
4. **Ledger plus transcript parser:** headline metric is Claude tokens per landed change. Also tracks the wasted-delegation rate (a local FAIL that Claude then redoes) and rework rate by origin, and has a weekly `LX_OFF=1` control day.

Routing:
- Claude decides from a short policy. There is no router model.
- Edits under about 30 lines in files Claude already has loaded stay with Claude.
- Spec interpretation, debugging an unknown root cause, security and architecture stay with Claude.
- Escalate to Claude after 2 failed attempts, a scope violation, an abstain, or failed citations.
- Hooks *suggest* and never block, because a blocking gate makes Claude spend tokens arguing or working around it.

Rollout order: measure → `lx run` → `lx ask` → `lx task` → `lx review` and overnight batches. Each stage has a stop rule.

## 2. Comparison with the existing pipeline

| Dimension | Clean slate | Existing | Verdict |
|---|---|---|---|
| **What it optimizes** | Claude tokens per landed change | Getting code *authored* by qwen (rules: "qwen always", "max offload", dispatch gate) | **Biggest gap.** We measure pipeline pass rate, not Claude savings. Nothing shows whether the pipeline saves Claude usage overall. |
| **Measurement** | Ledger, transcript parser, control day | dispatch-metrics.jsonl (3,091 runs, 517 failed), canary coverage | We have none of the Claude-side numbers. |
| **Biggest cheap wins** (output compression, cited reading) | Stages 1–2, with almost no machinery | Not built. Claude still reads logs, test output and files in full. Research and diagnosis exist as dispatch kinds, but go through the heavy queue. | Missing. Highest return for the least work. |
| **Code-edit offload** | `lx task`: one loop, a fail-before test, a diff guard, 2 tries | scaffold → preflight → slice → auto-author harness → queue/lanes/bundles → worker → relevance mutation gate → reviewer models → finding check → escalation watcher → self-heal → integrate → handoff | Ours is far more thorough. The mutation relevance gate and the finding check are ahead of the design. Its weakness is coordination cost. |
| **Where failures come from** | Assumes model failure, bounded by 2 tries | Past verdicts: 46% unsatisfiable spec, 37% harness defect, 3% model. Today's card-bonus park was a false positive from our gate (missing `mutant_sweep.py` on the runner, plus a finding check that only handled Python). | Most failures are our own tooling. Every one of them costs Claude tokens to diagnose. |
| **Claude overhead of the machinery** | Kept under the savings; wasted-delegation rate tracked | Large and not measured. Examples: SessionStart injects escalations, the handoff panel, about 100 canary seam names and an Aug-28 session brief; Stop hooks block on vault writes; whole sessions (like this one) go on pipeline repair; 79 handoff items wait for Claude to triage. | Likely the largest Claude cost in the system *(measure)*. |
| **Gates and hooks** | Suggest only | They block. The dispatch gate blocked the design agent from reading *reference docs*. The vault gate blocks turn end. | This is exactly the design's failure mode 6. |
| **Models and residency** | One 35B resident, fail fast | 35B+9B pinned on Darkbloom, qwen3.8 27B on Unraid, Gemma. A queue owns swaps. Reload and drain failures happen. | Roughly aligned; ours is more complex. Whether the 9B earns its slot is an open question. |
| **Routing threshold** | Small in-context edits stay with Claude | "Qwen always" | Ours sends small edits through a pipeline whose spec and review cost more than the edit. |
| **Quality controls** | Fail-before/pass-after, diff guard, citation check, Claude reads the diff | Fail-before/pass-after, mutation relevance, grounded reviewer findings, finding check, canary with about 255 proven seams | Ours is stronger. Keep all of it. |

**Bottom line:**
- The existing system is a strong *quality* machine for offloaded code edits. It points at the wrong target, though: share of code authored by qwen instead of Claude tokens saved.
- It skips the two cheapest, largest savings: compressing command output, and reading with citations.
- Its own upkeep (hooks, triage, debugging pipeline failures) probably eats much of what it saves. That has not been measured.

## 2b. Measured baseline: past Claude session logs, last 30 days
Source: `~/.claude/projects/**/*.jsonl`, including subagents. Messages were deduped by id, and tokens were weighted by price (input 1, cache write 1.25, cache read 0.1, output 5).
- Total: 82k assistant messages, about 1.76B price-weighted tokens.
- **Cache reads are about 65% of the weighted cost**, cache writes about 28% and output about 7%. Most of the cost is context being re-sent every turn, so trimming hook-injected context and keeping bytes out of the context pays off on every turn.
- **About 65% of Claude usage went to pipeline work.** Each message was tagged by the paths its tool calls touched (strict pattern: ollama-*, .ollama-dispatch, canary, gate, darkbloom, dispatch-*), with the tag carried forward through the session. Pipeline work means building, debugging, driving, triaging and A/B testing the pipeline. The other 35% was product work.
- The share is **recurring, not one-time**: every day of the last 10 was between 53% and 80%.
- Local side over the same 30 days: 2,310 runs, 1,217 with verify passed; 35M prompt and 24.5M completion tokens processed locally.
- Caveat: the tags are approximate, and they can't separate building the pipeline from upkeep. Phase 1 adds ccusage plus tagging to track this over time. No 1–2 week wait is needed, because the logs go back.

## 2c. Projected Claude savings (estimates; verify with the same measurement after the pilot)
- **Current pipeline:** most likely net negative right now. To break even, the work it delivered would have to have cost Claude more than its ~1.1B weighted tokens of pipeline usage if done directly. That is roughly 0.9M weighted tokens per verify-passed run, far above what a typical small direct edit costs.
- **Pilot, by lever:**
  - Hook and context trim: 5–10% of the total, on every session.
  - Output compression (RTK) plus symbol reads (Serena): 30–50% of product-work tokens, which is about 10–18% of the total at today's mix.
  - Aider delegation replacing the harness, escalation and handoff chain: the large lever. If pipeline upkeep falls from ~65% toward 15–25% of usage, total Claude usage drops about 40–50%.
- **Net projection:** about 45–60% less Claude usage for the same landed work. This rests mostly on cutting upkeep, not on qwen writing more code. Upkeep is the one number we can measure today.

## 2d. Darkbloom model management (the queue stays the single owner of loads)
- **Interactive tools never trigger a model load or swap.** That covers Aider, PAL and RTK's LLM fallback. They use only what is currently served.
- **One model covers every interactive role:** Qwen3.6 35B, which is already pinned with the 9B under the owner's standing 35B+9B setup and also handles vision.
- **Single endpoint:** tools call a LiteLLM proxy (tested; OpenAI-compatible) with one alias, `local-coder`, pointing at the Darkbloom 35B. Short timeouts, fallbacks off.
- **If the 35B isn't served** (missing from `/v1/models`, draining, a 429, or memory pressure), the call fails fast with `LOCAL_UNAVAILABLE` and Claude does the step itself. There is no waiting and no loading a substitute.
- **Interactive lease in the queue:** a wrapper takes a lease while an interactive call is in flight. While a lease is held, the queue won't start a swap or evict the 35B, and it may delay new job starts. Leases expire after a few minutes, so a crash can't wedge the queue.
- **Pipeline jobs that need other models** (Gemma, Unraid qwen3.8) keep the queue's fit-routing. A swap that would evict the 35B waits until no lease is held and the queue is in a quiet window.
- **Measure in the pilot:** how often `LOCAL_UNAVAILABLE` fires. More than 10% means the residency policy needs changing, not more machinery.

## 3. Chosen direction (the owner, 2026-10-15): pilot tested off-the-shelf tools next to the current pipeline
The owner prefers tested tools to building our own, so the custom `lx` CLI is out. The current pipeline keeps running during the pilot, and the Opus failure-review fixes (F1–F7), arm C and card bonus continue there as planned. All tool names below come from my own knowledge and have not been verified. Step 0 confirms them.

0. **Verify the tools.** A research agent checks each candidate for:
   - maintenance and release activity
   - license
   - install method
   - whether it works with Darkbloom's OpenAI-compatible endpoint

   Candidates: ccusage, RTK, Serena MCP, Aider, mini-swe-agent, PAL MCP. Search goes through SearXNG / `studio-research`, because this session's WebSearch budget is used up. Any tool that fails the check drops out, and only its fallback replaces it.
1. **Baseline from the existing logs (section 2b; no wait needed).** Install ccusage for ongoing daily reports. The only custom code is the small per-message tagging step that splits pipeline work from product work. Re-run it weekly.
2. **Trim our own hooks.** This is config, not a new tool:
   - cut the SessionStart payload: RED canary lines only, one-line counts, and the stale Aug-28 brief refreshed or dropped
   - vault gate: block → remind once
   - dispatch gate: allow read-only access to reference docs and paths outside the app
3. **RTK pilot**, for command-output compression: enable its hook for test, build and git commands in resell-tracker and `~/bin`. Pass: at least 5× compression with no lost failure lines. Rollback: remove the hook.
4. **Serena pilot**, for symbol-level reads and edits: register it as an MCP server for resell-tracker (TypeScript) and `~/bin` (Python). Pass: read tokens per session go down, and Claude doesn't fall back to whole-file reads.
5. **Aider on Darkbloom pilot**, for delegated mechanical edits:
   - Claude writes the failing test.
   - `aider --message-file spec.md --test-cmd "<verify>" --auto-test --yes` runs in a worktree against the resident Qwen3.6 35B.
   - Our existing gate checks run afterwards: fail-before/pass-after, mutation relevance, diff scope.
   - After 2 tries, Claude does the task itself.
   - Run 10 real tasks side by side against the current pipeline. Compare Claude tokens, success rate and the owner touches.
6. **PAL MCP (optional):** local second-opinion reviews. It stays only if its findings are right more than 50% of the time.
7. **Decision after 2 weeks**, using the ccusage numbers: Claude tokens per landed change and rework rate. Keep each tool that measurably helps. If Aider wins, retire the harness-authoring, escalation and handoff chain and keep the gate's checks.

**The owner's call: GPU contention.** Aider and PAL call Darkbloom directly, outside the queue, which conflicts with "all GPU work through the queue". Recommendation: during the pilot the queue gets a short "interactive" hold, so pilot calls pause new queue job starts, and that rule is amended for interactive calls.

## 3b. Original custom-build recommendations (superseded by section 3; kept for reference)

1. **Measure first (about a day).** Build a transcript parser over `~/.claude/projects/*/*.jsonl` that splits Claude tokens per session into four buckets:
   - product work
   - pipeline upkeep (turns touching `~/bin` pipeline files, handoff and escalation triage)
   - hook-injected context
   - delegation specs and reviews

   Report weekly Claude tokens per landed commit. This decides everything below with data.
2. **Cut injected context (quick win).** Measure the SessionStart and Stop hook payloads, then:
   - trim the canary list to just the RED/ANCHOR-LOST lines
   - collapse the handoff, escalation and ledger notices to one line each with counts
   - refresh or remove the stale Aug-28 session brief
   - turn the vault Stop gate from *block* into *remind once*
3. **Build `lx run`** as a new small tool in `~/bin`, with plain parsers for vitest/jest, tsc, eslint, next build, pytest and the canary. Add a CLAUDE.md rule: run tests and builds through `lx run`. No queue involvement; call Darkbloom directly for messy logs.
4. **Build `lx ask`** with a citation checker and a repo map. It talks to the resident 35B directly and does not go through the queue.
   - The queue still owns GPU scheduling. `lx` jobs are short and interactive, so they need a priority lane or a "short interactive" class in `ollama-queue.py`.
   - The "all GPU work through the queue" rule needs the owner's call here.
5. **Routing thresholds.** Edits under about 30 lines already in context stay with Claude. Debugging an unknown root cause stays with Claude, with local models only gathering evidence. The heavy pipeline stays the path for multi-file mechanical changes.
   - This means amending the "qwen always" and "max offload" memories.
6. **Soften the dispatch gate.** Let read-only access to reference docs and non-app paths through. Allow suggest-mode for reads, and keep blocking only hand-edits of app source.
7. **Residency test.** A/B the 35B alone against 35B+9B on `lx` latency and queue throughput, using the existing bake-off tooling.
8. **Keep** the heavy pipeline's quality gates as they are. Re-evaluate in two weeks using the metrics from step 1.

Critical files:
- new: `~/bin/lx`, `~/bin/lx_parsers.py`, `~/bin/lx_cite.py`, `~/bin/claude-usage-report.py`
- edit: `~/.claude/hooks/*` (SessionStart payload, vault-staleness gate, dispatch gate), `~/bin/ollama-queue.py` (interactive lane), `~/.claude/CLAUDE.md` (routing section), memory feedback files on dispatch posture

Everything gets mirrored to machine-config.

## Verification
- **Usage report:** shows Claude tokens per landed commit and the upkeep share for the last 2–4 weeks. That is the baseline.
- **Hook trim:** SessionStart payload size before and after, measured in characters and tokens. The canary still flags RED seams.
- **`lx run`:** fixture logs per parser. At least 5× compression, with failing test names and the first error kept. Canary seam plus a revert proof.
- **`lx ask`:** citation checker tests. A fabricated snippet is dropped, and more than 30% bad citations turns the answer into ABSTAIN. Measure the pass rate on 20 real questions.
- **Effect:** after 2 weeks, compare Claude tokens per landed change with the step 1 baseline, plus one `LX_OFF=1` control day. Rework rate must not rise.

## 4. Raising savings while protecting quality

### Measured driver
Context size per turn drives the cost. Last 30 days:

| | Median context per turn | p90 | Turns over 100k |
|---|---|---|---|
| Main session | 163k | 245k | 80% |
| Subagents | 121k | — | 63% |

- At 163k tokens of context, re-reading it costs about 16k weighted tokens per turn. The average output costs about 1.6k. So **context is about 10× the cost of the actual answer.**
- Always-loaded context includes MEMORY.md (21 KB, about 5–6k tokens) plus SessionStart and Stop hook injections. Those are paid again on every turn.
- Subagents account for about 55% of all usage, and they also run large contexts.

### Savings levers, biggest first (all reversible)

| # | Lever | Est. saving of total | Quality risk | Guard |
|---|---|---|---|---|
| 1 | **Context discipline.** One task per session, `/clear` between tasks, handoff through the plan file plus vault, compact earlier (verify which setting controls auto-compact). | 25–35% | Loses in-session memory | Handoff notes written before clearing; memory index kept current |
| 2 | **Shrink always-loaded text.** MEMORY.md index from 21 KB to about 5 KB (one line per topic, details in files); SessionStart hooks to one-line counts; Stop gates to remind-once. | 5–10% | Missed reminders | Counts still shown; red items stay verbatim |
| 3 | **Subagents run lean.** Explore and search subagents use `model: haiku` or local (Serena plus a local model) and return conclusions of 1k tokens or less. Tight briefs, no "read everything" prompts, a cap on concurrent agents. | 10–20% | Shallower search | The Opus/Sonnet coordinator verifies any conclusion it acts on (existing rule) |
| 4 | **Keep bytes out.** RTK for test, build and git output; Serena for symbol reads instead of whole files. | 10–18% | Lost failure detail | Full log path always returned; RTK keeps failing test names and the first error |
| 5 | **Delegate mechanical edits.** Aider on Darkbloom 35B, behind the gate, instead of the harness, escalation and handoff chain. | Removes most of the ~65% upkeep over time | Wrong code lands | Risk tiers below |
| 6 | **Model mix.** Sonnet builds, Opus only verifies conclusions and handles proven hard limits (existing rule, enforced in agent briefs). | 5–10% | Weaker builds | Opus verifies |

**Projection:** these levers overlap, so they don't simply add. Combined, about 60–70% less Claude usage for the same landed work. Lever 1 needs no new tools and is the biggest single win.

### Quality: route by risk and require evidence before anything lands
- **Tier A, Claude only, plus Opus verification:**
  - auth/security and secrets
  - money: P&L, payouts, BFMR submissions
  - prod data migrations
  - pipeline gate logic
  - anything with an unknown root cause
- **Tier B, local implements and Claude writes the test:**
  - multi-file mechanical changes, codemods, typed field plumbing
  - lint and type fixes
  - test writing
  - docs sync

  Before anything lands: fail-before/pass-after, our mutation relevance gate, a diff-scope guard, repo CI, and Claude reads the final diff.
- **Tier C, local only, deterministic checks:** formatting, version bumps, dependency bumps that pass CI, log and command-output triage.
- **Spot audit:** Opus reviews a random 10% of Tier B/C changes that landed with local help. Each finding feeds back into routing.
- **Tighten automatically:** if a category's rework (a revert or a fix to the same lines within 7 days) or post-merge CI failure rate rises above the Claude-only baseline, that category moves up one tier until the cause is fixed.
- **Control day:** once a week with offload off, to confirm the savings and quality numbers against the same kind of work.

### Verification for section 4
- Re-run the log measurement weekly. Targets: median main-session context under 80k, subagent median under 40k, pipeline-upkeep share falling, and price-weighted tokens per landed commit falling.
- Quality: rework rate and post-merge CI failures by origin (local-assisted vs Claude-only), plus the spot-audit pass rate.

## 5. Automating levers 1–5 (no manual /clear, no manual memory upkeep)
Settings below were confirmed to exist in the installed Claude Code 2.1.295 binary. Their exact behaviour gets tested in a throwaway session before rollout. The claude-code-guide agent said several of these don't exist; the binary shows they do.

- **L1, context size, automatic:**
  - Set `autoCompactWindow` (or the `CLAUDE_CODE_AUTO_COMPACT_WINDOW` env var) to about 80k. Conversation compacts automatically when it crosses that point, instead of near the model's limit.
  - Tune with `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` if needed.
  - `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` compacts idle sessions above a minimum size.
  - A **PreCompact hook** writes a task-state handoff (open tasks, IDs, decisions) to a file. A **SessionStart(compact) hook** re-injects only that file. Nothing is lost and nothing needs a manual `/clear`.
- **L2, always-loaded text:**
  - A one-time restructure of MEMORY.md into short topic hubs, about 5 KB. Detail lives in the linked files, which are recalled on demand.
  - A **PostToolUse hook on memory-dir writes** enforces a byte budget. If the index exceeds it, the hook tells Claude to fold the entry into a hub file. Maintenance happens automatically as I write.
  - Hook scripts are rewritten to emit one-line counts, plus verbatim lines only for RED or urgent items.
  - The vault Stop gate becomes remind-once.
- **L3, subagents:**
  - `CLAUDE_CODE_SUBAGENT_MODEL` sets the default subagent model to Sonnet.
  - Custom `.claude/agents/*.md` definitions: `scout` (Haiku, read-only, returns at most 1k tokens) and `local-scout` (calls the local model through MCP).
  - The brief template is enforced by a PreToolUse hook on Agent that rejects a brief without a return-size cap.
- **L4, bytes out:** RTK hook and Serena MCP. Both are installed and registered once and need no upkeep; status depends on the verification agent.
- **L5, delegation:** mini-swe-agent through LiteLLM (it replaced Aider; see section 6). Two backends:
  - **Darkbloom 35B** for code.
  - **Unraid Ollama** (RTX 3080, qwen3:14b or 9b) for small low-risk work: log triage, classification, RTK fallback summaries. Unraid also keeps the CPU runner for tests and builds.

  LiteLLM routes by alias (`local-coder`, `local-small`). An unavailable backend fails fast, and Claude does the step itself.
- **Monitoring is automatic:** a weekly usage report from the log measurement (median context, upkeep share, tokens per landed commit) is written to the vault by the queue-owned scheduler. No manual checks.

## 6. Tool verification results (2026-10-15): final tool picks
| Tool | Decision | Why / constraints |
|---|---|---|
| ccusage (19k★, active) | **Use** | Read-only usage reports |
| RTK (83k★, active) | **Use only for git, ls, grep, find output** | Open bugs drop or green-wash failures in vitest (#3935, #4224), pytest (#4426), tsc (#3750), next build (#2013) and eslint (#4172). It must never wrap tests, typecheck, builds or anything a gate reads. |
| Test and build output | **The runners' own quiet modes** | `vitest --reporter=dot --silent`, `pytest -q --tb=short`, `tsc --pretty false`, `eslint -f unix`. These are long-tested. Full logs go to a file through a `tee` wrapper so nothing is lost. |
| Serena (30k★, active) | **Pilot, read-only symbol tools only** | Editing tools off: `replace_symbol_body` corrupts TS exports (#1956). Install via `uv tool install serena-agent`, not a marketplace. |
| Aider (no release in 14 months; Qwen issues #5486, #5118, #5396) | **Dropped** | Maintenance mode, and local Qwen edits break or get silently discarded |
| mini-swe-agent (8.5k★, active, MIT) | **Delegation runner in place of Aider** | `mini -t … -y`. The text-based backend sidesteps Qwen tool-call format problems. Set `MSWEA_COST_TRACKING=ignore_errors`. Our gate checks wrap it. |
| PAL MCP (no merges in ~10 months) | **Dropped** | Unmaintained; local second-opinion review is off the pilot |
| LiteLLM proxy (61k★, active) | **Use** as the single local endpoint (`local-coder` = Darkbloom 35B, `local-small` = Unraid) | Retries 0, no fallbacks, cooldowns off, background health checks off. Its `/health` would force an MLX load, so availability comes from Darkbloom `/v1/models` and `ollama ps`. Watch #44979. |
| claude-mem (101k★) | **Skip** | Default is a hosted observer, and self-hosted queue bugs drop memory (#4737). Basic Memory already covers this. |
| cc-sessions | **Skip** | Stale |
| Auto session rotation | **Built-in only** | No adopted tool exists. Use Claude Code's `autoCompactWindow` and PreCompact/SessionStart hooks (section 5). |

Effect on the projection: the L4 lever is now about 8–15% of the total (quiet test modes plus RTK on git, plus Serena reads). The overall 60–70% estimate holds, because L1 and L5 carry most of it.

## 7. Revisions (the owner, 2026-10-15). These override the earlier sections.
- **No fallback to Claude when the local model is unavailable.** Every delegated job goes through the existing queue. The queue already checks Darkbloom state: `/v1/models` plus `loaded-models.json` for which models are served, `/health`, `bloom_idle_hold`, and the memory fit check. It holds the job until the right model is served and fits, then sends it.
  - Claude never waits on a job. It enqueues the job, carries on, and the queue watcher notifies it when the job finishes.
  - LiteLLM and the interactive lease are removed from the plan.
  - The queue keeps routing for **both** Darkbloom (Studio) and Unraid: fit-routing, residency and swaps.
- **(Superseded 2026-10-31: mini-swe v2 is now the main code-edit path, see `mini-swe-agent.md`.) Keep our worker loop. Aider and mini-swe-agent are not adopted.** The failures we measured sit upstream of the edit loop:
  - qwen harness authoring converges only 27% of the time
  - the gate mislabels and misroutes
  - 83 false UNTRUSTED verdicts
  - infra outages
  - iteration-cap near-misses

  A different edit loop fixes none of these. Our worker also carries the arm C prose-runaway fix and its local guards.

  mini-swe-agent stays only as an optional later A/B, run through the queue against `ollama-worker` on the same tasks.
- **The fixes that address our failures, in place of a tool swap:**
  1. **Claude writes the acceptance test and fixture** for each delegated task. qwen implements only. This removes the 27% harness-authoring step.
  2. **Opus failure-review fixes:**
     - F1/F2: one failure taxonomy that drives gate routing
     - F3: the agent runner records an "authored" manifest
     - F4: fit-check every lane for `auto` jobs
     - F5: per-case satisfiability proven before stage 3
     - F6: ledger link fixes
     - F7: the gpu-exclusive test
  3. **Split targets larger than about 1.5k lines before slicing.**
  4. **Near-miss continuation:** a measured extra-iterations class for runs 1–3 cases from green.
  5. **Arm C** for prose loops.
- **Confidence:** levers L1–L4 never touch the pipeline, so they can't cause pipeline failures. L5's failure rate is judged by measurement before anything expands: the failure-rate share by class in `failures.jsonl`, before and after F1–F7 plus Claude-written tests.

## 8. Local escalation ladder and guard against fix-churn
- **Local escalation ladder before Claude.** The queue runs each rung. Claude spends nothing while it climbs.
  1. Retry with fresh context and a 5-line failure summary.
  2. Extra iterations if the run was 1–3 cases from green.
  3. Best-of-k sampling, with tests picking the winner.
  4. A second local model.
  5. Re-slice into smaller parts.
  6. Claude only after every rung fails, or after the per-task GPU budget (about 2 h) runs out.

  This reuses the existing sample-ladder, extra-iterations and requery machinery. The new pieces are the order, the budget, and a harness check before rung 1, so we don't climb the ladder on a bad test.
- **Guard against a repeat of the 2-month churn:**
  - **Scope freeze:** during the pilot, only F1–F7 and bug fixes go into the pipeline. No new features.
  - **Upkeep budget:** pipeline upkeep must stay at or below 20% of weekly Claude usage, measured by the weekly report. If it goes over, pipeline work stops and delegation shrinks to Tier C.
  - **Two-week stop rule:** if the failure rate by class doesn't drop after F1–F7 plus Claude-written tests, delegation narrows to the categories that already succeed. We do not build more machinery.
- **Tool confidence:**
  - **Legitimate and current: high.** `gh api` on 2026-10-15 showed ccusage 19k★ (release that day), RTK 83k★ (10-14), Serena 30k★ (10-13).
  - **Works on our setup: not yet known.** Each tool gets one throwaway-session trial and a revert path before rollout. The Claude Code compaction settings exist in the 2.1.295 binary, but their behaviour is untested.

## 9. The owner's constraints and go-ahead (2026-10-15 10:14)
- **No new services on the Mac.** Any new service runs on Unraid.
- **Start time:** all work begins at **11:01 AM today**, through a one-shot scheduled check-in. Nothing starts before then.

What the constraint means for each item:

| Item | Where it runs |
|---|---|
| Claude Code settings and hooks (`autoCompactWindow`, PreCompact/SessionStart handoff, memory budget hook, subagent defaults) | Mac. These are config read by Claude Code itself, not services. Allowed. |
| Quiet test-runner flags and the RTK hook for git, ls, grep | Mac. RTK is a CLI the hook invokes per command, with no daemon. Allowed. |
| Serena | **Unraid only.** It runs over its HTTP transport with a clone of each repo. If Serena can't serve the Mac's working copy without going stale, it is dropped. Its saving is small, so dropping it costs little. |
| ccusage and the weekly usage report | **Unraid.** The existing queue scheduler, already running on the Mac, ships the usage lines to the Unraid CPU runner. The report is computed there and written to the vault. No new Mac process. |
| Delegation, the local ladder, F1–F7 | Existing queue and pipeline, with Darkbloom on the Studio GPU (existing) and Unraid. No new Mac services. |
| LiteLLM | Removed in section 7, so nothing to place. |

Order from 11:01:
1. Arm C safety fix: revert, then redo under the full test list.
2. Card-bonus landing through the pipeline.
3. Opus review F7, F6 and F3 land; F1, F2, F4 and F5 are staged.
4. Claude Code settings and hooks, each trialled in a throwaway session first.
5. Memory restructure.
6. Weekly report on Unraid.
7. RTK and quiet test flags.
8. Serena feasibility on Unraid.
9. Local escalation ladder ordering and budget.

## 10. Move the queue into the `dispatch-cpu-runner` container on Unraid (the owner, 2026-10-15)
**Target.** One container on Unraid, with every process run by supervisord (a long-used process supervisor):
- the queue daemon (`ollama-queue.py run`)
- the queue API and dashboard
- the CPU runner agent
- the workers and their worktrees, under `/mnt/user/data/dispatch-cpu-runner/`
- the canary
- the Serena HTTP server
- ccusage and the weekly report

The compose file and appdata stay where they are now. The owner applies updates from the Unraid shell, as he does today.

**Mac side after the move: no services.** Darkbloom keeps serving the Studio GPU. Claude Code hooks and dispatch commands become thin HTTP clients of the container's API. The Mac queue stays installed but stopped, for rollback.

**Problems to solve, in order:**
1. **Darkbloom inference from Unraid:** run `darkbloom start --local-endpoint --bind 0.0.0.0` with the bearer token from `~/.darkbloom/local_token`, and allow only the Unraid IP (pf rule or a TLS reverse proxy, per the docs).
2. **Darkbloom control from Unraid** (switch, pin, status, BloomGauge on `127.0.0.1:8765`). These are local to the Mac. Plan: macOS Remote Login (SSH) with a forced-command key that only allows the darkbloom and BloomGauge calls. That is an OS setting, not a new service, so **the owner has to approve it**.
3. **Repos.** The container clones from GitHub and pushes the `dispatch/*` and `integrate/*` branches. Landing becomes a push to main. Tasks must be based on pushed commits, because local uncommitted Mac work is no longer visible to the queue.
4. **Toolchain parity:**
   - The image pins the Node and Python versions each repo needs. This also fixes the node 22 vs 26 mismatch around `mock.module`.
   - Jobs that need macOS stay on Claude.
5. **Secrets:** today they live in the Mac Keychain. In the container they come from an Unraid secrets file (mode 0600 under appdata), never baked into the image.
6. **Hooks and state:**
   - SessionStart, Stop and the queue watcher read through the API instead of local files.
   - `ESCALATIONS.md`, the handoff panel and the ledgers move into the container volume and are read via the API.
7. **Notifications:** `notify-owner.py` runs from the container.

**Rollout, to avoid the churn cycle:**
1. Build the image.
2. Run the container queue in **shadow mode** (dry-run scheduling) next to the live Mac queue for 2–3 days, comparing decisions.
3. Cut over one lane at a time: CPU stages first, then Unraid GPU jobs, then Darkbloom jobs.
4. Rollback: stop the container queue and start the Mac queue again.

The canary has to pass in the container before each cutover step.

**Risk:** this is the largest change in the plan and conflicts with the scope freeze. It runs after the low-risk levers (steps 1–8 of section 9) and is capped by the upkeep budget. If it goes over budget, it pauses.

## 11. Remote Darkbloom management from the Unraid container (35B, 9B and Gemma)
**Checked on the Studio today:**
- The provider runs with `--local-endpoint --port 8000 --bind 127.0.0.1`, so inference is loopback-only. The CLI takes `--bind 0.0.0.0`, and the docs say the endpoint then needs the port opened and a reverse proxy with TLS.
- `darkbloom` has no remote-control API in the docs I read. Model switching, pinning and status are local CLI commands.
- Port 8765 (BloomGauge) did not answer a request just now. Whether BloomGauge is running is unverified, so any step that assumes it waits for a check.

**Three channels for the container:**
1. **Inference (send work):** switch to `--bind 0.0.0.0`, with the bearer token and a firewall rule allowing only Unraid's IP. A restart of the provider is needed, which happens in a window with no running jobs.
2. **Status (monitor):**
   - `GET /v1/models` and `/health` on that endpoint show what's loaded.
   - The coordinator route is an alternative: `GET /v1/models` with `X-Darkbloom-Route: self` lists what your own machine serves, through `api.darkbloom.dev`.
3. **Control (switch, pin, preload):**
   - **Option A, SSH forced-command:** a key that can only run the allowlisted `darkbloom` and BloomGauge commands, through macOS Remote Login.
   - **Option B, nothing remote:** keep the 35B+9B pinned as now and put Gemma in the preload list, if 64 GB allows 35B (21 GB) + 9B (6 GB) + Gemma 4-bit (16 GB). That is about 43 GB plus reserve and KV, which is tight with desktop apps running. Verify with `darkbloom status` memory numbers before committing. If it doesn't fit, Gemma jobs run on Unraid or wait for a swap window.
- The coordinator may be able to load models on a provider by itself (its warm-pool controller sends `load_model`), but I can't confirm it applies to a self-route to your own machine. UNCONFIRMED; test it before relying on it.

**Recommendation:** inference and status over the LAN endpoint (items 1 and 2), and control via Option A. Option B is the fallback if the owner would rather not turn on SSH.

## 12. Fallback if remote Darkbloom management isn't possible (the owner, 2026-10-15)
Keep only the bare minimum on the Studio, and move everything else to the Unraid container:
- **Studio keeps:** Darkbloom itself plus whatever small piece the research shows must be local to switch or pin models (for example a thin controller that applies a model-set request).
- **Moves to the container regardless:** the queue daemon, API and dashboard, workers, canary, Serena, ccusage and the weekly report.
- **Model control without a remote API:** the container writes the wanted model set as a request file or API call. The Studio-side piece applies it. If even that isn't acceptable, the 35B+9B stay pinned and Gemma jobs run on Unraid.
- The agent's findings decide which of these applies.

## 13. Remote management research result (corrects sections 10–12)
- **BloomGauge is gone** (removed 2026-10-13; the `127.0.0.1:8765` API no longer exists). Nothing in the plan should call it. `bloom_control.py` and the retired idle-switch shim are the old path.
- **No HTTP route can load, unload, switch or pin models**, on the local endpoint or on the coordinator. Those are local `darkbloom` CLI commands.
- **Inference and light status are remote-capable.** Either restart once with `--bind 0.0.0.0` (firewalled to Unraid, bearer token) or use coordinator self-route with an `sk-db-` key. The `/health` and `/v1/models` routes work, but `/v1/models` lists advertised models, not loaded ones.
- **Remote management needs one of:**
  1. **Forced-command SSH** (recommended): macOS Remote Login, which already shows as enabled. One `authorized_keys` line restricts the key to `from=<unraid-ip>` and a wrapper script `~/bin/bloom-remote` with an allowlist of `status`, `switch`, `idle`, `autopilot` and `restart`. No new service.
  2. A file-watcher service on the Mac. It is a new service, so it's ruled out by your Mac constraint.
- **Section 12 fallback applies in one form:** the only Mac-side piece is that wrapper script. It's a script, not a daemon.
- **Three models don't fit live.** The 35B and 9B leave about 12.6 GB free, and Gemma 4-bit probably needs more. Plan:
  - Advertise all three, set `max_model_slots` to 3 and `idle keep-loaded`.
  - The memory gate keeps two resident. The third loads on demand in about 10–30 s by evicting an idle one.
  - The owner decides whether to disable Autopilot (enabled but only observing). Disabling it makes `darkbloom switch --model A --model B --model C` change models live without a restart.
- **Queue changes in the container:**
  - Replace BloomGauge calls with `ssh darkbloom-mac bloom-remote <verb>`.
  - Health checks use `/health` and `/v1/models` over the LAN endpoint.
  - Memory-gate numbers come from `status` over SSH.
  - Jobs that need Gemma wait for a swap window or run on Unraid.
- **Unverified:** whether sshd is actually listening (test with `nc -z <mac> 22` from Unraid), `/metrics` contents, and the undocumented `port = 8100` setting.

## 13b. Autopilot decision (the owner, 2026-10-15): keep Autopilot enrolled
- Model changes use `darkbloom start --model … --force` restarts through `bloom-remote`, as `bloom_control.py` does today. No live `darkbloom switch`.
- Each model-set change therefore costs a restart (drain, reload). The queue batches jobs by model set to keep swaps to a minimum, per the existing "order to minimize swaps" rule.
- `bloom-remote` verbs: `status`, `start-set <models…>`, `idle`, `restart`, and the Autopilot pin/unpin/pause/resume verbs, which are allowed while enrolled.

## 14. Container migration: measure first, then decide (the owner, 2026-10-15)
Expected slowdowns (estimates; to be replaced by measurements):
- **Steady state:** LAN calls add milliseconds and an SSH control call about 0.25 s. Token speed is unchanged.
- **Model swaps:** with Autopilot enrolled, every model-set change is a restart. Estimated 1–3 min; Gemma is the worst case. The queue keeps batching jobs by model set to limit swaps.
- **CPU stages on Unraid:** probably slower than the M4 Max, but they already run there today, so I expect a modest effect.
- **Migration itself:** 2–3 days of shadow mode, then a lane-by-lane cutover.

**Measurements, run alongside the low-risk levers from 11:01 (read-only against the live system, no restarts while jobs run):**
1. Provider restart time (drain → reload → healthy) at the next idle window.
2. Gemma 4-bit cold load time and whether it fits next to the 35B+9B (`darkbloom status` memory numbers).
3. Unraid CPU runner vs Mac timings for 3 real stages: a vitest run, `tsc --noEmit`, and the canary.
4. `nc -z <mac> 22` from Unraid to confirm sshd is listening.
5. Round-trip latency from Unraid to `:8000` once the bind change is tested.

**Gate:** the numbers come back to the owner before any cutover. Migration proceeds only if the measured swap and CPU costs are acceptable to him. Until then the queue stays on the Mac. The Unraid pieces that don't touch it (Serena, the weekly report, the CPU runner) can start.

## Arm C: canary regression found
- The card-bonus agent's latest full canary was 253/255. `modelprofile` and `planwire` failed. Both check the worker's outgoing request, and they started failing after the half-finished live `worker_levers.py` flip.
- **First action on exit:**
  1. Restore `worker_levers.py.bak-20261015-armc-default`.
  2. Rerun those two seams to confirm the flip caused the failure.
  3. Redo the flip with the seam and profile expectations updated, under the full test list below.

## Arm C call (the owner: "good on your call if 100% solid")
- **Solid:** the runaway reduction. Prose-runaway runs were 0/14 against 11/14.
- **Not proven:** the pass-rate gain. It was 14/14 against 11/14, and the intervals overlap.
- **Decision:** keep the flip, but only after the remaining tests are green:
  - LAST_ENV fix
  - revert-check with the NOTHINK_RUN mutants visible
  - workerlevers seam update
  - full canary
  - worker, conformance and prose-sticky tests
  - wire capture showing `enable_thinking=false` on the author turn only
- **Auto-revert guard:** if the first 20 live author runs pass below 75% (arm A's baseline was about 79%), or a new failure class appears, set `NOTHINK_RUN=0` and report.
- **If any test fails before then:** restore `.bak-20261015-armc-default` immediately.

## Open items unrelated to this plan (blocked by plan mode)
- The Opus failure review is finished, and its fix plan is in the agent's plan file:
  - land: F7 (gpu-exclusive test), F6 (ledger links), F3 (agent-runner authored manifest)
  - stage: F1/F2 (one failure taxonomy that drives gate routing), F4 (fit-check lanes), F5 (per-case satisfiability)
  - It resumes when plan mode ends.
- The arm C flip is live but untested in `~/bin/worker_levers.py`. To roll back, set `NOTHINK_RUN=0` or restore `.bak-20261015-armc-default`.
- The card-bonus gate fixes are live, the canary is 255/255, and nothing is committed. Both agents resume when plan mode ends.
- The mapping agent stays paused.

## 15. Session-aware Darkbloom model set (the owner, 2026-10-15; overrides §13b "pair pinned, Gemma off")
- Active (Claude prompt/tool call or queue work within 30 min): serve the pair only. Idle 30 min: advertise pair + Gemma. Each switch is a restart.
- Full design, guards and tests: `~/.claude/plans/can-we-make-one-sleepy-dijkstra.md`. Dispatch through the pipeline after the arm C revert/re-flip, as part of the 11:01 run. Daemon restart only at idle.
