# Plan: offload more work to local LLMs without losing quality

Date: 2026-10-31. Read-only research. Nothing was enqueued, edited, restarted or committed.
Results so far: section 8 (2026-11-02); details in `offload-rollout.md`.
"EST" marks an estimate. Every other number comes from the files cited.

## 0. Bottom line

- The mini-swe v2 queue looks very good on paper: 109/115 ref-backed jobs passed (95%), with a median of 101s active time.
- But in v2, **Claude writes the frozen test (~64 lines/job) and a validated reference fix (~16 lines/job, median), and qwen then re-derives that same fix.** The share of code that local models actually author is close to zero. The queue's real job today is a *re-verification* service, not offload.
- Claude still spends about 0.5M weighted tokens per v2 job (EST) on spec, test, ref, validation and landing.
- The biggest win is to stop writing the fix twice. Let local models write whichever artifact Claude writes today, and keep Claude as the *checker*, backed by mechanical gates.
- Second: revive the local research/review/diagnose lanes that collapsed after 10-17, behind citation gates.
- Third: cut per-job overhead (batching families, landing, glue scripts).

## 1. Measurements

### 1.1 Queue (`~/bin/ollama-queue-logs/*.done.json`, 805 files, 793 in October)

| Window | Jobs | Result |
|---|---|---|
| Since 10-16 | 236 (203 mini-swe, 33 worker) | mini-swe: 146 done / 53 failed / 4 cancelled |
| mini-swe failure classes since 10-16 | | harness 28, model 23, spec 2 |
| Since 10-17 | 162 jobs, 155 distinct tasks | 137 tasks eventually pass (88%); first try 134/155 (86%); 150/155 needed one attempt |
| v2 (>= 10-24 12:00) | 116 jobs, 115 ref-backed | 109/115 PASS (95%); active median 100.7s, p90 485s, 16.3 GPU-h total |

- Of the 28 harness failures, 27 are "worker produced no iterations", and almost all happened on bring-up day 10-16. Treat them as resolved.
- Since 10-17, PASS active time has a median of ~133s and p90 of ~1072s. FAIL has a median of 799s. Failures used **14.3 GPU-h against 13.2 h for passes**, so failures are long and expensive. The fail-fast budget is a lever.
- Retries are effectively nonexistent: 150/155 tasks ran once. Self-heal or retry has little headroom on the current mix.
- Models: 198/203 jobs ran on `qwen3.6-35b-a3b-vl-mtp-mxfp8` and 4 on `qwen3.8:27b-q4_K_M`. **There is no per-kind routing data.**
- GPU use: about 28 active GPU-h in 15 days, ~8% of wall time (EST from active_s).
- `~/.darkbloom/loaded-models.json` currently lists Qwen3.5-9B and gemma-4-26b-qat-4bit, **not the 35B**, so residency differs from what the docs assume.

### 1.2 Run dirs (`~/.ollama-dispatch/mini-runs`, 164 specs)

Intake results:

| Intake result | Count |
|---|---|
| local-ready | 114 |
| None (pre-intake) | 42 |
| needs-split | 8 |
| needs-stubs / not-local | 0 |

- 7 of the 8 needs-split results were overridden. The override reasons say the guard-count heuristic was wrong.
- Refusals write no dir, so the rejection rate is **unmeasured**.
- 37 local-ready overrides cite "frozen tests committed in baseline", all from the bloom-ledger family (51 tasks).

Run-dir contents and spec keys:

| Item | Count |
|---|---|
| ref/ | 144 |
| SUBMIT-OK | 137 |
| VALIDATION | 143 |
| TRIAGE.md | 2 |
| reach_anchor | 71 |
| stub_error | 50 |
| red_ok | 140 |
| new_names | 28 |
| fn_scope enforce / off / warn | ~135 / 10 / 8 |
| mutation_gate | 0 |
| best_of | 0 |
| update_tests | 0 |
| single_shot | 2 |
| no_think | 2 |

So the triage-class workflow (G7, TRIAGE.md) is barely used.

**Sizing (VALIDATION V6, 161 steps):**
- 2569 ref lines in total: median 16, p90 27.
- 627 tests.
- The ref-backed test files total 7665 lines, about 64 lines per job.
- Claude writes about 80 lines per job and qwen re-types about 16.

### 1.3 Claude effort (session jsonl, Edit/Write since 10-17: 1840 calls)

| Destination | Lines |
|---|---|
| scratchpad (vault_logN.py 1791, patch scripts, gen_tasks/mkrefs) | 22.9k (17.6k .py) |
| `~/.ollama-dispatch` (tests in `worktrees/`: 8109) | 14.8k |
| mini-spec / TASK | 3.8k |
| `~/bin` pipeline | 4.6k |
| frozen tests in dispatch worktrees | 2.15k |
| direct repo src / tests | 441 / 587 |

- The largest test authoring: wt-bloomgauge-cli-access 1321, wt-tax-logging-base 892, aw-library-move-guard 550, plex-season-migration 534.
- Refs were mostly written inside subagents and copied in with cp, so the line counts above undercount them.
- The bloom-ledger orchestrator brief asked for "MINIMAL Claude token use". Its gen_tasks/mkrefs scripts suggest Claude implemented the family first and then split it into refs and tasks for qwen. **Likely, unverified.**
- Tokens since 10-24: 145M weighted in total. 63 subagent sessions touched mini-submit/validate, for 59.9M (~41%): 40 Sonnet, 17 Opus. That is about **0.5M weighted per v2 job (EST, includes landing and review)**.
- Agent dispatches since 10-17: 233 (121 Sonnet, **82 Opus**, against the "agents not Opus by default" rule). Rough classification: research 32, review 32, test/ref/spec 31, diagnose 27, implement 23, land 23, other 52.
- **Local research and review collapsed.** Worker research jobs: 175 before 10-17, 6 after. code-review-agent: 31 before, 0 after. That work moved back to Claude agents.

### 1.4 What the gates catch

- `~/.ollama-dispatch/failures.jsonl`: 569 rows, all in October. Since 10-17 there are 39: model 29, operator 2, spec 2, context 2, harness 1.
- Pre-v2 Phase A (qwen-written tests) failed on mock, vacuous, skeleton, wrong-reason and never-reaches. That is why v2 moved test authoring to Claude.
- PASS-but-review-rejected cases from the vault dispatch log are all pre-v2, with qwen-written tests:
  - bfmr-rest-u3: trim bug
  - u3b
  - clamshell-size-s1-2: no debounce
  - webhook-fix-u1b: dry_run bug
  - u2c: frame hack
  - u2e: reviewer fix
- Diagnose:
  - diag-amazon-miss-2 passed the citation gate with a **wrong root cause**.
  - diag-walmart-photos failed 3/3 at the citation gate (invented quotes).
  - So the citation gate catches fabrication but not wrong reasoning.
- The vault log also records the "sample-file fix" agent spending ~84k tokens on a ~12-line change, and a proposed <20-line direct-edit cutoff that is still awaiting the owner.
- The owner rule (2026-10-31): "Opus reviews only mini-swe queue failures". Passes are not reviewed.

### 1.5 Post-merge quality (weak evidence)

- No reverts were found since 10-17 in these repos:
  - resell-tracker: 97 commits
  - machine-config: 96
  - bloomgauge: 53
  - plex: 87, one "update lane tests for new policy"
  - ev-dashboard: 6
- **Commits cannot be attributed to jobs** (no job-id ledger or trailer), so escaped defects are not measurable today.

### 1.6 Safety net status

- `~/.ollama-dispatch/canary/last.json`: ok=False, 4 failed (seam harnessadm etc.). `pipeline-canary.py` mostly covers the old worker path (15 "mini" mentions), so **it does not guard mini-swe**.
- mini-bench has one real run (pc1). Its "run after any runner change" rule is not followed: there are 19 `.bak` runner files.
- `~/bin` git had no commits from 10-17 to 10-30, then a 10-31 "snapshot uncommitted work". **Gate changes went live unversioned.**
- mutation_gate has never run. Stryker and mutmut are not installed.
- `scope-creep.jsonl` has 4 entries, all from Oct 14-15. Scope creep is either rare under fn_scope or not logged. Unknown which.

## 2. Docs that disagree with reality

| Doc | Says | Reality |
|---|---|---|
| `~/bin/docs/mini-swe-agent.md` status line | "NOT adopted (2026-10-15)" | 203 mini-swe jobs; it is the main path |
| `~/bin/docs/offload-plan.md` §7 | keep the worker loop, mini-swe not adopted | superseded |
| memory `feedback_mini_swe_not_auto_dispatch.md` | "qwen writes the test, then the fix" | v2: Claude writes the test and the ref, qwen writes only the fix |
| mini-swe-agent.md | single_shot / no_think / mutation_gate available | not run live (2/2/0 uses) |
| Dispatch/model notes | 35B + 9B pinned | loaded-models.json now shows 9B + gemma-4-26b |
| canary docs | pipeline E2E guard | does not cover mini-swe; currently red |
| `~/.ollama-dispatch/bakeoff/` | model choice basis | Sep-13 data, old Ollama authoring harness; not valid for mini-swe routing |

## 3. Levers, ranked by (volume unlocked) x (1 / quality risk)

### L1. Stop writing the fix twice: local drafts the test, Claude checks it (inverted v2). Highest value.

- **Change:**
  - Add a `mode: test_draft` to `~/bin/mini-spec` and `~/bin/mini-validate`.
  - Claude writes the TASK and the small ref fix (median 16 lines). The local model (35B) drafts the frozen test(s) against the ref.
  - mini-validate checks the drafted test mechanically: V2 static, V3 red for the right reason on baseline, V4 green with ref, **V5 each ref hunk caught**, V6 sizing, V7 reverse order, plus G5 test static rules (no mocks of the unit under test, no skeletons).
  - Claude *reads* the test (plus the V5 report) instead of writing it, then the ref lands. There is no Phase B.
- **Why this first:** tests are about 4x the lines of the fix (64 vs 16), and V5 is exactly the check that pre-v2 qwen tests lacked. Every pre-v2 PASS-but-wrong case was a test that did not pin the behaviour. V5 against a known-good ref is a strong mechanical oracle for that.
- **Alternative L1b (test-only):** Claude writes TASK + test, no ref, and qwen writes the fix. Guard it by running V5 on the candidate diff (each hunk is needed by the test), diff_reach, red_without_fix and mutation_gate. Saves ~16 lines/job. Lower gain, but closer to today's flow.
- **Gain (EST):** removes ~64 of ~80 Claude-written lines per job and an estimated 30-50% of per-job Claude tokens. Real local authoring moves from about 0% to about 80% of the code per job.
- **Guardrail:**
  - V5 must be 100%. Every ref hunk must be killed by at least one test.
  - The test must fail on baseline for the asserted reason (V3).
  - Claude review of each drafted test stays mandatory during Phase 1.
  - Not allowed for must-not-offload areas (section 4).
- **Measure:** replay the **115 validated v2 specs** as a labeled benchmark. Strip the Claude test, have local draft one, and compare:
  - V5 kill rate
  - Claude review-reject rate
  - mutation score vs the Claude test, once mutmut/Stryker are installed
  - Claude tokens per job
  Use the replay GPU time in the idle ~92%.
- **Effort:** M (mini-validate already has V2-V7; the new parts are the drafting prompt and the mode switch).

### L2. Revive local research, review pre-screen and diagnose behind citation gates.

- **Change:**
  - Route "research" (32) and "review" (32) agent dispatches back to the queue: worker research jobs and code-review-agent, both of which already exist.
  - Require `diag_check.py`-style citation gating (quotes must exist verbatim at path:line).
  - Claude (Sonnet) verifies the conclusions only, not the reading.
  - For diagnose, also require a **reproducing red test** before a root cause is accepted. diag-amazon-miss-2 shows citations alone do not catch a wrong root cause.
- **Gain (EST):** about 60 of 233 agent dispatches in two weeks. These are the token-heavy reading jobs. The 82 Opus dispatches are the most expensive slice.
- **Guardrail:**
  - Findings are only advisory until Claude verifies them.
  - The citation gate must pass.
  - Root causes need a red repro.
  - Track precision (verified findings / claimed findings) per family.
- **Measure:** spot-check 20 reviews against a Sonnet review of the same diff. Target precision >= 80% and missed-critical = 0 on the sample.
- **Effort:** S-M. The lanes exist and only need routing and policy, plus a repro requirement in diagnose.

### L3. Batch families and amortize per-job overhead.

- **Change:**
  - Add a `mini-spec --family` generator that emits N specs from one Claude-written design. The bloom-ledger family is 51 jobs and is the template.
  - Use one landing and review agent per bundle instead of per job.
  - Use stable_prefix for the shared context (already ON; Darkbloom prefix-caches).
- **Gain (EST):** landing is about 23 dispatches and a slice of the 0.5M per job. Maybe 20-30% fewer Claude tokens per landed job in families.
- **Guardrail:**
  - Each job still passes its own gates.
  - Bundle landing must run the full suite after every merge, not once at the end.
  - Stop the bundle on the first red.
- **Measure:** Claude weighted tokens per landed job, family vs single.
- **Effort:** S-M.

### L4. Fail fast and fix intake false positives.

- **Change:**
  - (a) Cap FAIL runs. Failures burn 14.3 GPU-h at a median of 799s. Kill at about 2x the PASS p90 for the spec's size bucket, then route straight to triage. This frees GPU for L1/L5 replays.
  - (b) Replace the intake guard-count heuristic with a probe-reach check, since 7/8 needs-split results were overridden.
  - (c) Log intake refusals, so the rejected shapes become measurable.
- **Gain:** GPU-hours rather than Claude tokens. Also fewer manual overrides, each of which is a Claude turn.
- **Guardrail:** this changes gate logic, so Claude authors it, mini-bench runs before and after, and it is committed in `~/bin` git.
- **Measure:** GPU-h on failures per week; override rate; first-try pass unchanged.
- **Effort:** S.

### L5. Per-kind model routing on real data, using idle GPU.

- **Change:**
  - Run mini-bench and the L1 replay set across the 35B, Qwen3.5-9B, gemma-4-26b and Unraid qwen3.8:27b.
    (2026-11-02: qwen3.8:27b, 17.7 GB, does not fit Unraid's 9.6 GB usable VRAM; on Unraid only 14B-class models fit. See section 8.)
  - Route by kind: fix, test-draft, research, review.
  - The current bake-off (Sep-13, old harness) is not valid for this.
- **Gain:** unknown until measured. The main win is likely a cheaper or faster model for small fixes, and keeping the 35B for test drafting.
- **Guardrail:** a route switches only if pass rate and V5 score are no worse than the 35B on the replay set (within 2 points).
- **Measure:** replay pass rate, V5 rate and median active_s per model per kind.
- **Effort:** M (mostly GPU time and the Darkbloom earning trade-off; see the open questions).

### L6. Cut Claude glue overhead (cheap, no quality risk to product code).

- **Change:**
  - Turn recurring scratch scripts into tools committed in `~/bin`: vault-log rollover (1791-line vault_logN.py), patch scripts, ref/task generators.
  - Adopt the pending **<20-line direct-edit cutoff**: below it, Claude edits directly instead of building a spec (~84k tokens for 12 lines).
  - Enforce Sonnet by default for agents (82 Opus dispatches broke the rule).
- **Gain (EST):** the 22.9k scratch lines are Claude's largest write category.
- **Guardrail:** none needed for product quality. The direct edits still go through repo tests.
- **Effort:** S.

### Lower value / skip for now

- **Self-heal and retry:** 150/155 tasks already ran once, so there is little headroom.
- **Stub seeding:** already widely used (stub_error 50, reach_anchor 71).
- **Docs and refactor kinds:** fine for local, but low volume in the data. Allow them under L1-style gates when they come up.
- **Migrations:** not offloaded (see section 4).

## 4. Must NOT offload (Claude writes and decides)

- Auth, security, secrets and keychain code.
- Money paths: resell P&L, BFMR dedup and salePrice, tax. Local may draft tests here only in shadow mode, never landed without Claude rewrite.
- Production data migrations (plex library moves, DB schema on live data).
- Pipeline gate logic itself: mini-validate checks, runner gates, intake classifier, canary.
- Unknown root cause (diagnosis without a red repro).
- Writing the frozen test for the *same* job where local also writes the fix. Never let one model author both the oracle and the answer.

## 5. Quality scorecard and auto-throttle

Compute weekly per family/repo. A new `~/.ollama-dispatch/landed.jsonl` ledger maps job id to merge commit SHA. It is kept outside the repo, so there is no attribution trailer.

| Metric | Threshold | Source |
|---|---|---|
| Escaped defect rate: fix/revert touching job-changed lines within 14 days | <= 3% | landed.jsonl + git log -L |
| Review-reject rate of gate-PASS jobs (sampled) | <= 10% | 20% random sample of passes reviewed by Sonnet |
| V5 kill rate on landed tests | 100% required; mutation score >= 70% once installed | VALIDATION, mutmut/Stryker |
| Local-drafted test reject rate (L1) | <= 15% in Phase 1, <= 10% after | review notes |
| Research/review finding precision (L2) | >= 80%, 0 missed-critical in sample | verifier notes |
| Wasted delegation (job cost > direct-edit estimate, or abandoned) | <= 10% | tokens + queue logs |
| Claude weighted tokens per landed job | falls week over week; baseline ~0.5M (EST) | session jsonl |
| GPU-h on failures / total | <= 35% (now ~52%) | done.json |
| mini-bench green after any runner/gate change; `~/bin` committed | 100% | mini-bench, git |
| Scope-creep / fn_scope fails on landed jobs | 0 landed | runner verdicts |

**Auto-throttle:**
- 1 breach in a family: that family's pass sampling goes to 100% for 2 weeks.
- 2 breaches, or any escaped defect in a money or security-adjacent area: the family drops back to the previous mode (test_draft to v2 to Claude-direct).
- Re-promote after 10 consecutive clean jobs.
- Because the owner's 10-31 rule reviews only failures, the **20% pass sample** is the only check on false PASSes. It must exist before L1 goes live.

## 6. Phased rollout

**Phase 0: instrument (week 1, S)**
- Add the landed.jsonl ledger and intake-refusal logging.
- Commit `~/bin`, and make mini-bench a hard pre-merge check for runner/gate edits.
- Add mini-swe seams to the canary and fix the 4 red seams.
- Install mutmut and Stryker.
- Adopt L6 (direct cutoff, Sonnet default).
- Capture baselines for the scorecard.

**Phase 1: shadow (week 2)**
- Run L1 test_draft on the 115-spec replay set and on live jobs in shadow: Claude still writes the test, and the local draft is scored against it.
- L5 routing runs in the same replay.
- L4 fail-fast cap.
- Go/no-go for Phase 2: V5 100%, reject rate <= 15%.

**Phase 2: live, low-risk repos (weeks 3-4)**
- Repos: bloom-ledger and bloomgauge (synthetic data, public repo), plex retention/lane logic (not migrations), `~/bin` non-gate tools, ev-dashboard.
- L1 live, with Claude reviewing every drafted test.
- L2 research and review pre-screen live.
- L3 families.

**Phase 3: widen (week 5+)**
- Add machine-config and clamshell.
- Drop the drafted-test review from 100% to a sample once the reject rate stays <= 10% for 3 weeks.
- resell-tracker non-money paths only.
- Money, auth, migrations and gate logic stay Claude-only (section 4).

## 7. Open questions only the owner can answer

1. Passes are currently not reviewed (10-31 rule). Do you accept a 20% sampled Sonnet review of passes as the quality check?
2. Darkbloom earnings vs local GPU use. How many GPU-h/week can go to replay/bench and drafting? Which model should stay resident (the 35B is not loaded now)?
3. Is an out-of-repo job-to-commit ledger acceptable? It avoids commit trailers under the no-attribution rule.
4. OK to install mutmut and Stryker on the M4 Max?
5. Approve the <20-line direct-edit cutoff?
6. For the refs Claude still writes: is Sonnet acceptable, or do you want Opus for test/ref authoring?
7. Should resell-tracker be eligible at all in Phase 3 (non-money paths), or stay fully Claude?

## 8. Results so far (2026-11-02)

Everything below is committed in `~/bin` (branch frindle/local-llm-offload). Numbers and commits: `offload-rollout.md`, section
"Status 2026-11-02".

**L1 (test drafting) is live as an opt-in assist, not as unread landing.**
- The check was calibrated on Claude's own validated tests: 130 of 147 ACCEPT (88%). 10 of the 17 rejects are below the 0.70 mutation
  bar; 7 are tooling limits.
- The mutation gate is now line-scoped (`mini_mutate.py`, contained), replacing mutmut for this check. mutmut's indir mode rejected
  Claude's validated tests for tooling reasons.
- Trial on 11 specs through the real queue (the 35B drafting):
  - 5 ACCEPT: 3 on the first job, 2 after the retry lever. On the same refs the drafts average a mutation score of 0.86, against 0.87
    for Claude's tests.
  - 3 REJECT, each well below Claude's test (0.61-0.67 against 0.77-0.88).
  - 3 produced no usable draft: one hit the step limit, two were caught by the mock gate.
  - Retries for 4 of these are queued.
- Against section 5, the reject rate (3 of 8 drafted) is far above the 15% Phase 1 limit. So Phase 2's "drop Claude's read" is not
  reached. Claude reads every ACCEPTed draft; that is cheaper than writing the test (median draft 1.5x the lines of Claude's test).
- Fixes the trial forced:
  - V6 allows 10 tests in a draft. An 8-test draft at 0.982 had been rejected on size alone.
  - `del sys.modules[...]` is not mocking.
  - A rejected draft is retried with the check's findings.
  - Test-draft rejects are cleared mechanically instead of spawning research follow-ups.

**Unraid** (question from the owner, 11-02): it already runs the work it can do without a quality loss: the gate pre-gate (qwen3:14b, then
the Studio's 27B regate decides), gpu-exclusive jobs and the CPU runner. L1 drafting and mini-swe fixes stay on the Darkbloom 35B:
- Unraid's context clamps (12K / 6K) are below what these jobs use (num_ctx 65536, observed peak about 24.7K);
- the 27B does not fit there;
- the 14B's verdicts are weak (its pre-gate "fail" was confirmed by the 27B in only 2 of 11 cases).

**BloomGauge control** (the queue turns the Manager off while it has Darkbloom work and hands back after 900 s idle):
- Failed writes now back off from the ledger: 120/300/900 s for Manager changes, 300/1800/3600 s for picks, and at least 900 s
  after a 429. BloomGauge's own limit is 2 model switches or restarts and 6 setting changes per rolling hour.
- One alert per streak of 3 failures, closed by the janitor once a write succeeds.
- No model-facing text tells a model to run darkbloom or BloomGauge writes. The retired darkbloom tools refuse to change anything
  without `DARKBLOOM_BY_OWNER=1`.

**Session notifications:** queue events (passed, done, failed, blocked, needs_info, gate non-pass) reach the session that launched the
job, through the hook or a Monitor `--watch`.

**Also on the branch:**
- the old dispatch tools (ollama-dispatch*, qagent, slicer, auto, slice) are removed;
- the retired pipeline canary is removed (section 1.6's canary is gone, not fixed);
- `mini-spec --repo/--base` builds its own worktree;
- git-history based worktree cleanup (plist unloaded);
- an ownerless idle handback for BloomGauge;
- a CLAUDE.md for the folder.

Section 7: questions 4, 5 and 6 are answered in `offload-rollout.md` round 2: the mutation tools are installed, the <20-line
direct-edit cutoff is approved, and agents use Sonnet by default with Opus only to verify. Questions 1, 2, 3 and 7 are not recorded
as answered.

## 9. The 50% target, measured (2026-11-03/05)

**Target** (the owner, 11-02): a fix job through the lane costs at least 50% fewer Claude weighted tokens than Claude fixing the same bug
directly (a blind Sonnet arm), with no quality loss: no escapes, few rejects.

- Weighted tokens: input 1, cache write 1.25, cache read 0.1, output 5, plus 0.1 x the main context for each Claude call.
- A task whose lane job does not land a correct fix is charged the full direct cost (fallback).

**Synthetic bench** (11 bugs from past dispatch jobs in the owner's repos):
- The hidden tests are the ones Claude wrote when those jobs first ran, so the oracle is Claude-written, not independent. These
  figures are provisional.
- Result (11-04, all 11 tasks have a final verdict, C = 111.8k): offload **28% steady-state, -19% as run**. Below the 50% target.
  - Steady counts only each task's last job (the fixed tooling), every Claude call unbatched; as run counts every job, failed
    first tries included. Per-task table: `auto/bench/cost2-2026-11-04.txt` in the session scratchpad.
- What holds it down:
  - Fallbacks: dupes (the costliest direct fix, 759k) and libseed are charged their full direct cost: 29% of the blind total.
  - Claude rework: the repairs after a reject (tomlarr, tomlval, sample) and upstream's finish after a bad frozen test cost
    100-210k each; on ranges Claude did the diagnosis (227k of a 443k direct fix).
  - Clean passes cost 38-95k (relurl, notset, setform, cfgkeys): 56-82% offload on those tasks.
- Quality: 12 lane passes reviewed, 6 rejected (50%), 1 escape (libseed: accepted, then failed the hidden tests).
  - Claude's repairs pass the hidden tests (tomlarr3 4/4, tomlval2 4/4, sample3 5/5), except libseed2's (4/5: a docstring check).
  - The oracle missed 2 of the defects the review caught: the unrepaired tomlarr3 and sample3 lane fixes also pass every hidden
    test.
- The last four retries, each after a runner bug fix (4924292, d51ed93, 729fd73, 60b5df7):
  - upstream2: pass-finished. The frozen lane test contradicted itself (its slot guard asserted the old 502 for an upstream 400
    while its target asserts 400), so no fix could pass. The raw lane fix scores 2/2 hidden; Claude's finish scores 2/2.
  - sample3: rejected (breadth: any path containing "sample"), repaired by Claude, hidden 5/5. Ran with the old guard text.
  - setform2: pass, accepted as is, hidden 2/2. Ran with the new guard hint (8034033).
  - dupes6: fail at the test phase. The diagnosis passed, but the model's guard expected `[8]` where the code records `[[8]]`; the
    first attempt hit the step limit, and the second got the exact gate message but spent its steps re-reading the 3,000-line
    `arr-webhook.py`. The gate worked; this is a lane miss on a large file. Ran with the new guard hint.
- Breadth retry (08e388f): a prefix or substring match where the Problem names exact values is the top reject class (the libseed
  escape, libseed2, sample3). When mini_mutate shows a narrowing `breadth` survivor in a match the fix added, the runner gives the
  green fix one more local attempt to narrow or keep each match from TASK.md, before any Claude review. A bad result puts the green
  fix back byte for byte. It was designed after seeing these failures, so the re-runs queued on 11-04 (libseed3, sample4) show
  whether it works with the real model; they are not independent evidence and stay out of the figure above (`cost2.py --drop`).
- Re-runs (11-04): the retry fired on both and fixed neither. Both were rejected.
  - libseed3: asked about both `label.startswith('library-seed')` matches. The model kept them, reading "goes through exactly
    the same episode / season-pack detection as a 'sonarr' label" as a request for a prefix match. That is the misreading
    Claude's own first review made (the libseed escape). The module docstring was stale as well.
  - sample4: asked about `'sample' in path` and the file-name prefix. The narrowed fix failed the model's own frozen test, which
    asserts the substring behaviour (a `samples/` directory is ignored), so the green fix was put back. The same test expects
    'episode' for two real videos of one episode, which the Problem rules out, and the fix adds an unrequested dedup loop for it.
  - Neither was repaired: a repair would repeat libseed2's or sample3's, whose hidden results are already known. Both count as
    fallback.
  - With both included: 27% steady, -27% as run; review rejects 8 of 14 (57%).
  - Lesson: the retry cannot narrow a match the frozen test asserts, and the local model makes the same prefix-vs-exact call a
    first Claude review got wrong. The fix belongs at the test stage: the test must not lock in matches the Problem does not
    name. Measure that on the real-issue bench, not by more tuning on these 11 tasks, which have all been seen.

**Real-issue bench** (the owner, 11-03: "use real tests from past github issues instead of fabricating tests"):
- Source: 16 SWE-rebench rows (issues fixed January to March 2026) from public Python repos.
- The issue text is the task; the fixing PR's own tests (FAIL_TO_PASS plus PASS_TO_PASS) are the hidden oracle.
- Tool: `real/tools/rb.py` in the session scratchpad.
- Setup never runs repo code:
  - a shallow fetch of the base commit, with history dropped;
  - dependencies read statically: PEP 621 (including setuptools dynamic file lists), Poetry, setup.cfg, and setup.py through `ast`;
  - dependencies installed by uv, wheels only;
  - the project itself gets a metadata-only dist-info.
- All 16 tasks set up. jsonpickle (bson), scrapy (pyftpdlib) and both conan tasks (patch-ng, pluginbase) have sdist-only
  dependencies, which were left out rather than built. twine did not validate (its one FAIL_TO_PASS test fails with the gold
  patch in this environment), so the bench has 15 tasks.
- **Where it runs** (the owner, 11-04, option (b)): claude-sandbox, a disposable Linux container on Unraid.
  - `rb.py` refuses on any other host. Setup, validate, grade and every test run of both arms happen there.
  - Hidden data (the fixing PR's tests, the gold patch) is on the sandbox only during setup, validate and grade, never while a
    model works there.
  - The lane runs remote (c2cf682): the model runs on this Mac through Darkbloom, its commands on a dispatch-cpu-runner
    (unraid-cpu-1, or the runner on claude-sandbox), never on the Mac.
- **Arms:**
  - Blind direct: 3 Sonnet agents with 5 tasks each, working from the issue text alone, running tests only through `sbx-test`.
    Start and end markers split each transcript into a cost per task.
  - Lane: one queue job per task (`real/tools/lane-submit.sh`). Each Claude call for a task carries a marker (submit, watch,
    triage, review, repair, finish), and `real/cost/cost2r.py` charges the task those calls. A failed or rejected task is also
    charged the blind arm's cost (fallback).
  - Retries: a model failure gets one retry. A lane or infra failure is fixed, then resubmitted. Steady counts each task's last
    job; as run counts them all.
- **Round 1** (11-05; jsonpickle refused by mini-submit's C3 rule, because the issue says "secret"):
  - 2 lane passes, both repaired by Claude:
    - mcpsdk: the fix was right. 14 repair calls went on import order, unused locals and formatting (the remote runner had no
      ruff, so the lint gate never ran) and on moving the test into the module's test dir.
    - sphinxth: one of two hunks was wrong; Claude dropped it and rewrote the test.
  - Model failures:
    - sqlmeta and loguru: phase A read code until the step cap and never wrote the test;
    - bibtex: phase B's fix never went green;
    - conan2: diagnose quoted lines that are not in the file and named a function the file does not define;
    - cfnlint: no DIAGNOSIS.md.
  - Lane failures:
    - moto: impact-scan false positive (`models` is the stem of hundreds of modules);
    - optuna: the import check missed a package re-export;
    - scrapy and optuna: the baseline suites used up phase A's budget.
  - Infra failures: isort, param, canvas and loguru r2 (Darkbloom Sandbox holds counted against the model's time); conan1 (the
    runner lost network for a few seconds).
  - Not changed: the diagnose gate's rejections (conan2, param, loguru r2) were real model errors. A fix gate whose baseline
    timed out (rc 124) is WEAK, not failed.
- **Fixes from round 1** (general, none task-specific; each passed its self-tests on claude-sandbox):
  - 8bd4071: phase A's prompt states its deadline and says the test does not need the fix.
  - 0c7d0b7, a5b7f7a: a model call held by the provider is credited, not charged to the model, and a job pauses through a hold
    instead of failing. Verified live: conan1 r2 had a 964 s hold credited, then carried on.
  - a5b7f7a also: the import check counts package re-exports, and phase A's clock starts after the baseline suites.
  - 5c4ff6a: node before 23 reports a test file that failed to load as a crash, not a per-test failure.
  - 886c117: a module stem that other modules share matches only its unique dotted tail.
  - 8977f9b: handoff-autoclear leaves a live session's finished jobs to that session. Its headless Sonnet had triaged 5 bench
    failures on its own (charged as ext-overhead, about 15k each).
  - 5089fe2: draft nudge. While the file a phase must write is still missing, the runner reminds the model from a set step on.
  - 0d3c725: a style pass on what the model wrote (import order, ruff format, unused locals in the test), applied only where
    the project already follows the rule; ruff is installed on the remote runner.
  - deebd81: a new test goes in the test dir that mirrors the source dir, under a name no existing test uses.
  - Open idea: literal noise in the impact scan (a common string like 'key' links unrelated tests).
- **Assisted fallback** (pilot on bibtex): a Sonnet agent fixed the bug starting from the failed lane job's packet (diagnosis,
  test, progress). It cost 1157k against 899k for the blind fix. Starting from a failed job saves no Claude tokens, so a failure
  falls back to a plain direct fix.
- **Interim figure** (11-05 23:10, C = 117k): 3 tasks have a final verdict, giving 33% steady and 32% as run.
  - jsonpickle (refused) is charged its full direct cost, 1056k.
  - mcpsdk: 474k against 492k blind. Most of its 20 review and repair calls were style and test placement, which 0d3c725 and
    deebd81 now handle.
  - sphinxth: 580k against 1621k blind.
- **Cost model:** a Claude call costs about 12-20k weighted, so the number of calls decides the cost.
  - A clean pass takes about 4-6 calls (submit, the finish event, the review packet, the verdict): 50-100k, about 90% offload on
    that task.
  - A failure costs the direct fix plus 50-100k of lane overhead.
  - The blind fixes cost 380k-2000k (about 900k typical), so 50% overall needs about 60-65% of tasks to pass without a repair.
- **Round 2** (queued 11-05, running one at a time): conan1 r2, isort r2, param r2, canvas r2, scrapy r2, optuna r2, moto r2,
  loguru r3, cfnlint r2, sqlmeta r3, bibtex r2, conan2 r2.
  - bibtex, conan2 and cfnlint use their one model retry.
  - sqlmeta r3 tests the draft nudge, which was designed from sqlmeta's own failures. Like every round 1 task, it is tuning
    evidence, not an independent measurement.
- **Round 2 results** (11-05/06; all 12 jobs final, one clean pass). Every fix is general, none task-specific:
  - conan1 r2, lane: the test skeleton put the target's own dir on `sys.path`, so its `profile.py` shadowed the stdlib module for
    every test collected after it, and the suite stopped collecting. cedb88b: the skeleton imports by package path, and phase A
    checks that the suite still collects when its test touches import state.
  - isort r2, infra: a runner crash (`rglob` with an absolute pattern, Python 3.14) after the diagnosis passed. Fixed in 712f266.
  - param r2, model: the diagnosis report never got 5 of its 6 sections; the retry explored again instead of adding them. 712f266
    names the missing headings in the gate message and asks for the whole skeleton in one write.
  - canvas r2, lane: the issue removes a TypeError that an existing test asserts. Both fix attempts were right and edited that
    test, but auto mode never lets a fix update an existing test, so no correct fix could pass.
  - scrapy r2, model, made costly by the lane: the model's test hung in `reactor.run()`, and the 300 s command and gate timeouts
    used up phase A. A diagnosis retry for one ambiguous short path (`crawl.py:33`) had already cost 1208 s.
  - optuna r2, lane: the diagnose gate rejected a report whose only defect was a quote that elided real lines of the cited range.
  - moto r2, model: phase A's test also asserted a value for a case the issue does not mention (the disabled state), which an
    existing test pins, so no in-scope fix could pass both.
  - loguru r3, lane: the reach gate's 30% body-coverage floor rejected a right test. `emit` is 84 lines, and its six
    `format_map` calls sit on alternative branches, so the test of the KeyError path the diagnosis cites ran 26%. The issue
    names no literal, so the spec had no reach anchor, and phase A's last attempt hit the step limit chasing coverage.
  - cfnlint r2, model: the diagnose gate rejected all 3 reports for one mistake. The Minimal fix named a JSON schema as
    `File:` and `CfnInit.validate`, which a .py rule defines, as `Function:`. The gate said only that the file does not
    define the function, so each retry repeated it. This was the task's one model retry, so it is final.
  - sqlmeta r3, model: the draft nudge worked, and D1 wrote its report after about 78 steps and 900 s. The gate rejected it for
    a 511-character Root cause and a Minimal fix that named 2 files. D2 was told to edit the report, not re-explore; it
    re-explored for 59 steps and hit the time cap with the report unchanged. Final (its model retry was r2). The lane gaps:
    the two-file message gave no reason, and nothing nudged a retry that left the rejected report untouched.
  - bibtex r2, model: the diagnose gate rejected all 3 reports for quoted lines that are not in the file. D1 and D3 copied two
    quote-heavy lines of `splitter.py` with one quote character wrong: an extra `'` in a raw regex, and a dropped backtick in
    a string of quotes and backticks. These were D1's only problems. D2 paraphrased 2 lines and wrote a 463-character Root
    cause. The retry notes showed the real lines and the `sed` command to copy them, and D3 still repeated D1's errors. This
    was the task's one model retry, so it is final.
  - conan2 r2, pass with no repair: the diagnose gate rejected D1 for quoted lines that are not in the file and D2 for a
    470-character Root cause, and accepted D3. The test's mutation score was 1.00. The review left four cosmetic nits as they
    were, because the fix is correct and its CI would pass: a blank line added in a parser method about 100 lines from the fix
    (left over from an abandoned edit), an unused `profile_name` parameter, a loop written twice, and the test file at the
    `test/` root. Stage 15 handles the first kind.
- **Stage 10** (cac51ac; round 2's fixes, and the owner's 11-06 "any way to speed up the gates so we're not waiting on them after a gpu
  run?"):
  - Gate speed:
    - The baseline suite and typecheck run in the background on a copy of the work dir while phase A works. Phase B waits for
      them only at its first gate.
    - A whole suite that timed out at baseline is replaced by the existing tests nearest the change (at most 40 files), so a gate
      run checks something in minutes instead of timing out again.
    - A suite that took 120 s or more at baseline is skipped on an attempt whose own test is red; the attempt fails either way.
  - Auto mode lists the existing tests that call a target function as tests the fix may update (at most 3 files, canvas r2).
    Gate `update_flip` checks each edit: only test functions change, at most 3, each keeps its assertions, and each edited test
    fails on the fix without the edit and passes with it.
  - One test run is capped at 150 s, with a message that names a hang (scrapy r2).
  - The diagnose gate resolves a short path through the report's own citations (scrapy r2). It accepts a quoted line with `...`
    inside it when the text around each `...` appears in that order in the real code, within 15 lines (optuna r2).
  - Phase A's prompt: "Test only the cases TASK.md describes" (moto r2).
- **Stage 11** (7869a3c; loguru r3): auto mode takes the lines that the accepted diagnosis's Root cause and Second cause cite
  inside the target function's body (`spec.diag_lines`). A phase A test that runs one of them meets the coverage floor. A test
  that stops at an early return the diagnosis does not cite is still rejected.
- **Stage 12** (34cae7c; cfnlint r2): when the `File:` does not define a `Function:` line's function, the diagnose gate
  names the tracked source files that do (a dotted `Owner.name` needs both in one file). For a data `File:` (JSON, YAML,
  Markdown and the like) it adds that the fix changes only code, so the report names the function that reads the data.
  The diagnose prompt says the same in one sentence. Fixes inside data files stay out of scope; batch 2's filter counts
  only .py files as source.
- **Stage 13** (817e49a; sqlmeta r3): a Minimal fix that names more than one file is told that the fix changes ONE file, and
  which lines to keep and delete. A diagnose retry that starts from a rejected report gets a fix budget: fix the listed problems
  by step 8, and read code only when a listed problem needs it. The draft nudge also fires while the report's bytes still equal
  the rejected ones (`MINI_DRAFT_SHA`).
- **Stage 14** (00a4861; bibtex r2): before the diagnose gate, a quoted line that is nowhere in the cited file takes the text of
  the one source line within 60 lines of the range that matches it once whitespace, quote characters and backslashes are
  ignored (`fix_quotes`). It only touches fences under Code path, Root cause, Second cause and Not verified: a Minimal fix
  or Regression test fence may hold the proposed code, and an escape or quoting fix differs from the original only in those
  characters. A paraphrase, a line two different source lines match, and a line with under 10 characters of text are still
  rejected. bibtex r2's D1 would have passed the diagnose gate.
- **Stage 15** (5ee4d2d; conan2 r2): the style pass first puts back each blank-line change that sits more than 3 unchanged
  lines from every other change in its file, when the file still parses to the same AST (`stray_blanks`). This needs no ruff
  and no project rule, because it restores the project's own text. A blank line inside a string is data and stays. The
  sandbox validation now runs the style and lint self-tests with ruff 0.16.3; before, its venv had no ruff and skipped them.
- Open idea: before phase B, run the existing tests that call the target against the derived test's expectations, and send back
  a test that contradicts one the issue does not change (moto r2).
- **Round 3** (queued 11-06): conan1, isort, param, canvas, scrapy, optuna and moto r3, and loguru r4. A job ships the lane
  tools when it starts, and none had started by stage 14's install, so all of them run with stages 10 to 14. Stage 15 reaches
  every job that starts after its install (11-06 06:53 PST). param, scrapy and moto use their one model retry; the others
  follow lane or infra fixes.
- **Round 3 results so far** (11-06): conan1 r3 is a clean pass. canvas r3 passes with one repair: the fix and its update_flip
  edit to an existing test are right, but the fix left the function's docstring describing the old behaviour. Its review also
  found a lane bug (stage 16), which is not counted against the model.
- **Stage 16** (f49ff86; canvas r3): `mini-auto land`, and so `review ok`, copied only the fixed file and the new test. An
  existing test that the fix updated under gate update_flip stayed behind, and the landed tree failed the project's own suite.
  land now copies every update_tests file the fix changed, with the same guard against a checkout that moved; one the fix
  left alone is skipped. No earlier pass had changed one.
- **Stage 17** (1dbf0b0; canvas r3): the doc pass. After a green fix to a python file, the runner asks the local model one
  question about each docstring the fix left as it was. It asks about at most 2 functions or classes whose code changed,
  the target first: does every sentence and field still hold for the fixed code? The model replies KEEP or with the corrected
  docstring. `doc_apply` lays a correction out like the old docstring and refuses it if it:
  - changes anything but that docstring (the module's AST is compared with both docstrings blanked);
  - adds an example, a quote run, a tab or a backslash;
  - runs past the docstring's width;
  - rewrites more than 4 lines, or more than half the docstring when that is more;
  - adds a compile warning.

  Once a correction is applied, the test re-runs and no_hacks rescans the file. Any result but green puts the file back byte
  for byte. The doc pass is never a gate. REVIEW.md gets an informational `docstring` row. REVIEW-AUTO.md gets a "Docstring
  check" section, so review checks the correction in the fix diff. A file that reads docstrings as data is skipped (`__doc__`,
  `getdoc(`, a click, typer or docopt import, or another tracked file that reads them). It is on by default for a python
  target in mode auto; `doc_pass: false` or `MINI_DOC_PASS=off` turns it off. It needs 6 minutes left before the review
  reserve, and each question is one local-model call with no Claude tokens. A second USAGE SUMMARY line counts the call, and
  the queue API reads the last one. The single-shot model helper it uses has run only under stubs so far, so the first real
  `docstring` row checks the helper too.
  It reaches every round 3 job that starts after its install (11-06 09:00 PST): scrapy, optuna and moto r3, and
  loguru and isort r4. param r3 had already started.
- **Stage 18** (9e7121b; isort r3 and the diagnose tally): two small changes, both general.
  - **Root cause length.** The diagnose gate now allows a Root cause of up to 600 characters (`ROOT_GATE`). The contract
    and the form repair still ask for 400 (`ROOT_MAX`). Across the bench's livelogs, the gate rejected 9 reports at
    401-511 characters, in 8 jobs. Each rejection cost a full re-diagnosis, or the job when it was the last attempt. The
    one-sentence rule is unchanged, and parenthesized line references still don't count toward the length.
  - **Phase A tests the user's view.** When TASK.md shows an example (an input and the output or error it gives, or should
    give), at least one test must feed that input through the public call TASK.md uses and assert the result the user
    sees. A test of an internal helper or data structure may be added, but it never replaces that one. isort r3 shows why:
    both of its tests asserted a key of `parse.file_contents()`'s internal comment dict. The fix turned them green while
    isort still printed the wrong output, and the review rejected it.
  - It reaches every round 3 job that starts after its install (11-06 09:30 PST): scrapy, optuna and moto r3, and loguru
    and isort r4. param r3 had already started.
- **Stage 19** (010fa79; param r3 and scrapy r3 evidence): the reach shim no longer prints a KeyError. To chain the
  interpreter's own sitecustomize, the shim took itself out of `sys.modules` and imported `sitecustomize` again. The remote
  venvs (uv's python 3.12 and 3.13) have none, so the import system's closing `sys.modules.pop` raised
  `KeyError('sitecustomize')` after the shim had run. site then printed "Error in sitecustomize ... KeyError:
  'sitecustomize'" at the top of every traced run, in every python process the run started. The tracer was already in place,
  so reach and coverage were right. But the two lines reached REVIEW.md evidence and the output the model reads, and a test
  that checks a child python's stderr would see them. The shim now puts itself back when nothing chained.
  - Checked on the sandbox with python 3.11 (Debian's own sitecustomize), 3.12, 3.13 and 3.14, with and without a second
    sitecustomize, in a parent and a child process. The new self-test assertion is red on the old shim.
  - A job ships the runner when it starts, so stage 19 reaches jobs that start after its install (11-06 10:41 PST) and a
    parked job when it re-launches. The four round 3 jobs in flight run without it.
- **Stage 20** (ba138f8; moto r3): phase A's red check gives the test count with its first rejection. moto r3's A1 wrote one
  red test and one green one. The rejection told the model to list the green test in GUARDS, but the count of red tests
  outside GUARDS was checked only once GUARDS was non-empty, so A1 never heard that one red test is too few. A2 made the
  green test a guard and was rejected for the count on the last attempt. The count check now runs as soon as a test outside
  GUARDS is green. Acceptance does not change: such a result is already rejected, and the count is only added to its
  problems. The new self-test assertion is red on the old code.
- **Stage 21** (c0ca8fe; loguru r4): phase A checks the user's view that stage 18 asks for. Every loguru r4 test called
  `ErrorInterceptor.print` directly. The Problem's example goes through `logger.add(..., catch=False)` and `logger.info`,
  and with catch=False the handler re-raises before `print` runs, so the fix passed its tests while the example still
  showed the bug. The review rejected it.
  - When the Problem has python code (a fenced block, a backtick span or a doctest) that imports a name the package under
    test defines today, a python test outside GUARDS must refer to that name. An import inside a test, an alias, the
    package attribute or a lazy loader's return value all count.
  - Phase A's early attempts reject a test file that never does, before the test runs and together with the grounding
    check. The last attempt only records it, and REVIEW.md and REVIEW-AUTO.md ask the reviewer to follow the example's
    calls to the fixed code. The skeleton's first placeholder asks for that test. Supplied tests and dry runs skip it, as
    does `user_view: false` or `MINI_USER_VIEW=off`.
  - Offline over the bench's past runs it flags the three loguru runs and nothing else: param r3's test already uses the
    public call, and the other tasks have no such example. Its self-test is red without the wiring.
- Stages 20 and 21 reach jobs that start after their install (11-06 14:09 PST). Every round 3 and round 4 job ran without
  them.
- **Round 3 final** (11-06): every batch 1 task has its last job's verdict.
  - Passes: conan1 r3, optuna r3 and conan2 r2 are clean passes. mcpsdk r1, sphinxth r1 and canvas r3 pass after a repair
    in review.
  - Rejects: isort r4 and loguru r4. isort r4's new branch fires only when the line that reaches `wrap.line` contains
    "import (". output.py builds every alias line without a parenthesis, so the Problem's output does not change, and the
    fix also writes the noqa comment twice.
  - Failures: bibtex r2, cfnlint r2 and sqlmeta r3 fail the diagnosis. scrapy r3 and moto r3 fail the test phase, and
    param r3 fails the fix phase.
  - jsonpickle was refused by C3.
- **Hidden-test grade** (11-06, claude-sandbox). The blind arm passes 12 of 15 tasks and fails cfnlint, conan2 and isort. 4
  of the lane's 6 accepted fixes pass: conan1, mcpsdk, optuna and sphinxth. The bibtex assisted fallback passes.
  - canvas r3 escaped (F2P 1/2). The Problem asks to bring `obj_or_str` "in line with" `obj_or_id`, which checks the
    object's type before it reads the attribute. The fix only returns a str unchanged, and the review and its repair
    missed the type check. The blind arm restructured the function the same way as `obj_or_id`, and it passes. This is a
    quality loss against the blind arm.
  - conan2 r2 escaped too (F2P 0/1). The blind arm fails the same test, so it is no loss against the blind arm.
  - Both escapes are charged the fallback (`escape` and `escape-both` in verdicts.tsv).
  - The grade's first run failed every patch with rc=127 before any test ran. `rb.py grade` used the relative `--keep`
    path both as the working directory and inside the venv python's path. It now resolves the path first.
- **Batch 1 cost, final** (cost2r, C = 100k): steady 6%, as run -23%, as-run-x -16%.
  - Before the escapes were charged, steady was 16% and as run -14%.
  - The 26% reported earlier was wrong. The old script read only the first session's transcript. Its fixed 90 MB tail
    also no longer reached the bench's first marks. It now reads both transcripts from a start found by binary search.
  - isort r4's two submit calls echoed the bare task name. An extra_ops row now names their job, so steady counts them.
  - The lane's correct fixes cover 3363k of the 15993k blind cost, a weighted pass rate of 21%.
- **What 50% takes.** With a lean job (submit, a digest, the verdict: about 40-55k weighted at C = 100k), offload comes to
  about the weighted pass rate minus 4%. On batch 1's graded results the lean protocol alone gives about 16%. 50% needs a
  weighted pass rate near 55%, so the pass rate has to rise about 2.5 times, and the review must still catch
  spec-level misses like canvas.
- **Stage 22** (0acb9c7; scrapy r3, moto r3, param r3): a direct-fix fallback for jobs the lane's own gates or limits stop.
  scrapy r3 and moto r3 ended in phase A with 1700-2000s of the stage left (every test rejected: a step limit with an
  ImportError; one red test outside GUARDS), param r3's fix failed test_green after 1908s. Each then cost a blind Claude fix
  (345k-1919k weighted) where a rejected candidate costs one review (15-40k).
  - After phase A's last rejection, or when phase B's fix fails its gates (not after MISSING:, not an infra stop, not a chain
    step or a check-only run), one plain run gets what is left of the stage plus job room up to 1500s: the code back at
    baseline, the test file as the stopped path left it (the frozen one when phase B edited it), its rejected test or its
    reverted diff quoted. The model writes or repairs the test, sees it fail, fixes the code and runs green. It needs 300s
    plus the gates' share (120s + 2x the suite's baseline seconds), else "direct fallback not run".
  - Evidence: no test or no fix ends the job as today (` fallback=direct FAIL (<why>)`, the class unchanged). Otherwise
    fix_gate runs with fn_scope off and no diff_reach floor (test green, red at baseline, suite, no_hacks, lint, literals,
    must_keep, scope, update_tests, no_missing) plus fb_red_reason (its own baseline run: red on an assertion per test, no
    crash, no wrong-reason failure), fb_tests_ran, fix_locus (information only), mutation only when the fix is inside the
    diagnosed function, the style pass. A pass is `PASS needs-review soft=fallback,... fallback=direct after=test|fix`: the
    queue holds it for review, the runner never accepts it.
  - REVIEW.md opens with `## Direct-fix fallback (read first)` (what stopped, that the test is NOT frozen, what was skipped,
    the evidence); the stopped path's packet stays next to it as REVIEW-main.md; REVIEW-AUTO.md repeats the section, marks
    the test as written in the same run as the fix and asks first whether it fails at baseline for the Problem's reason. Off
    with spec `direct_fallback: false` (auto() sets it on; the spec field reaches a remote runner) or `MINI_DIRECT_FALLBACK=off`.
    The self-test runs the stub through nine shapes (after test, after fix, after an edited frozen test, green at baseline,
    no fix, off by env, off by spec, no time) and is red without the wiring.
- Stage 22 reaches jobs that start after its install (11-06 15:43 PST). No batch 1 round ran with it; its dev runs on batch
  1 tasks are excluded from the scorecard and noted in the vault log.
- **Stage 23** (37e6689; test-only, installed 11-06 16:12 PST): the first stage 22 dev submit (scrapy) stopped at mini-submit's
  C0 preflight (`runner --self-test` on the Mac, cached per runner sha, not overridable): `_self_test_py_reach`'s
  chained-sitecustomize case (stage 19) ended in "No module named pytest". Homebrew's python3.14 reaches its pip packages
  (/opt/homebrew/lib/python3.14/site-packages) only through its own sitecustomize (lib/python3.14/sitecustomize.py rewrites the
  Cellar path), and a sitecustomize on PYTHONPATH shadows it, shim or no shim; the print-only fixture therefore lost pytest on the
  Mac while passing in the sandbox venv. No runner since stage 19 (10:41) had a C0 stamp, so every submit from this Mac was
  blocked. The fixture now chains onward the way the shim does (its five chain lines); the shim and the lane are unchanged.
  Validated on claude-sandbox with a Homebrew-shaped venv (no pytest, its own sitecustomize appends another venv's site-packages):
  the old test red there with the Mac's message, the new one green there and under the plain venv; full --self-test green.
- **Stage 25** (df81703; installed 11-06 17:16 PST; stage 24 is the coordinator's, staged separately): what the first stage 22
  dev run (scrapy, job 05dd5b54ce98) showed. The fallback fired after phase A's two step-limit rejections and produced a one-line
  fix of the diagnosed function with a test red at baseline and green after it, but its gates failed it for three reasons that were
  not about the fix: (1) `fb_red_reason` found a crash because CRASH_RE ran over the WHOLE output and hit the ModuleNotFoundError
  traceback scrapy logs for its optional httpx download handler on every run, which the main test gate does too (scrapy r3's phase A
  "ImportError" rejection has the same shape); (2) the per-test reason check wanted an exception name and the test's FAIL lines were
  plain assertion messages; (3) the suite gate read a pytest captured-log line (`ERROR    scrapy...retry.py:118 Gave up retrying
  <GET https://127.0.0.1:41165/...>`) as a new failure, which with its random port can never match the baseline set. Stage 25:
  `crash_hit` makes the crash check positional once per-test lines parsed (a run-level marker anywhere, or a crash after the last
  per-test line, counts; a logged traceback before the verdicts does not), in the main test gate and the fallback; `fb_red_problem`
  (fallback only) takes a FAIL line that names no exception type as an assertion when it says something; `fail_lines` skips captured-log
  lines; the fallback prompt asks for `RESULT <name> PASS|FAIL <Type>: <msg>` lines when the test runs as a plain script. The scope gate
  (the model also edited a Problem-named test file outside update_tests) is unchanged: the rule was in the prompt and is the main path's.
  Validated on claude-sandbox: the new self-test red on the base runner (fail_lines kept the log line), green with the stage; full
  --self-test green; ruff F unchanged. Expected effect: repos that log tracebacks during tests (optional deps) can pass phase A and the
  fallback's red check at all; the suite gate stops failing on log noise.
- **Batch 2** (set up 11-06): 13 new SWE-rebench tasks (`real/batch2-candidates.txt`), none seen during tuning. It is the
  independent measurement.
  - Filter: one source file changed, up to 40 changed lines, at least one FAIL_TO_PASS test, no ML, GPU, C-library or GUI
    stacks.
  - Setup: 16 candidates. `rb.py` now also reads `uv sync` (extras and groups), `uv pip install`, `export` steps, a `make`
    target whose recipe is pip installs, and setup.py extras assigned by subscript. Dependencies resolve with
    `--exclude-newer` set one day after the issue was filed. Later releases had broken holoviews (its conftest makes a new
    numpy DeprecationWarning an error) and both dmcg tasks (a changed signature).
  - Out: aind (its install needs apt-get), conan3 (the gold patch fails its FAIL_TO_PASS test in this environment) and
    linkml (the repo root is a uv workspace).
  - The hidden data was removed from the sandbox after validation.
  - Blind arm: 3 Sonnet agents with 5, 4 and 4 tasks and batch 1's brief.
  - The lane jobs wait for stage 22, a fallback for jobs that the lane's own gates or step limits stop, so batch 2 measures
    the lane with it.
  - **Lean protocol** for each lane job: one submit call, one read of the finished job's digest, and the verdict. Calls that
    chase a lane bug carry the `dev` op: as run counts them, as-run-x and steady do not. A model failure gets one retry and
    a lane or infra failure is fixed and resubmitted, as in batch 1. Reported: steady, as run and as-run-x, plus the
    hidden-test grade of both arms.
- **Batch 2 results** (the lane on stages 22-25; 13 tasks):
  - The lane landed 2 tasks, both after a Claude repair, and both pass the hidden tests:
    - sphinxth2: the fix wrapped the whole handler in try/except. Claude narrowed it to inspect.unwrap's ValueError.
    - strands: a fallback fix that copied the _PluginRegistry logic inline and left out the docstring the issue asks for.
  - Lost:
    - 4 diagnoses: dmcg1 (twice), dmcg2, marimo.
    - 6 test phases, each followed by a failed fallback: a2a, unyt, kopf, pydap, pyrato, holoviews.
    - 2 reviews: scrapy2 (a wrong peek expectation) and vyper (a wrong root cause). vyper's fix breaks 32 of 53 logging
      tests that the suite gate never ran.
  - Blind arm: 11 of 13 pass the hidden tests. dmcg2 gets F2P 1/2 and kopf 2/4.
  - Cost (cost2r, C = 100k): as run -1%, as-run-x -1%, steady 0%. The lane plus fallback cost 16695k against 16518k for the
    blind arm.
  - Most losses came from the gates, not the model (`real/b2-causes.md`). Stages 26 and 27 address them.
- **Stage 26** (20b97b5; installed 11-07 04:12 PST):
  - Plain-script runner (unyt, holoviews, kopf).
    - The model replaced the skeleton's `__main__` block with one that printed "FAIL: name: err" and exited 0, or wrote
      pytest-style tests with no block at all.
    - Phase A's gate now writes the canonical runner itself, once, when the old block did nothing but run the tests. It
      hoists any TESTS/GUARDS the old block defined, then judges the test with the new runner.
    - When that rewrite is not safe, the problem text says what to change.
  - Protocol reds (a2a). A TypeError or AttributeError for a missing protocol (with, async with, for, len, ...) now counts
    as a right-reason red when two things hold: the object's class is defined in the task's files, and TASK.md names the
    protocol.
  - Duplicate method names (scrapy2).
    - fn_slice shows the definition that TASK.md names by its class.
    - fn_scope sees every definition of a name.
    - A class given as target_fn covers its whole body.
- **Stage 27** (4b76b61; installed 11-07 06:46 PST):
  - Diagnose time: AUTO_DIAG_TIMEOUT goes from 1500 to 2700 s. In 9 of the 12 lost diagnoses, time ran out before a third
    attempt.
  - diag_check:
    - It un-cites proposed code. conan2's Minimal fix fence had been cited as a quote.
    - It resolves placeholder and wrong-directory citation paths (dmcg1).
  - Phase A also rewrites a block that prints "FAIL: name" / "Error: msg" and exits 1 (ultraplot r1).
  - The direct fallback's own test gets the canonical runner when its block prints no RESULT line (holoviews, unyt, pydap).
  - reported_red: a red whose exception type and message appear in TASK.md's Problem section counts as the crash the issue
    reports (pyrato).
  - Correction to the commit message, which says ultraplot's fallback test already prints RESULT lines. It does not.
    - Its block prints a "TEST RESULTS" header. `_fb_runner_fix` looks for the substring "RESULT", finds it in the header,
      and leaves the block alone.
    - Phase A's rewrite has no such check.
    - Fixing this is a stage 28 item.
- **Stage 27b** (9b45773; installed 11-07 07:06 PST): remote jobs take `--remote-python 3.11`. uv finds the runner image's
  Debian python3.11.
- **337776c** (test only; 11-07 07:15 PST):
  - Four test_mini_auto checks were written before stage 22. They now run with the fallback off.
  - New 7g checks cover the fallback itself.
- **Batch 3** (set up 11-07 04:30 PST; `real/batch3-candidates.txt`):
  - 11 new tasks, chosen with batch 2's rule. skypilot is out: its setup.py builds install_requires from a module.
  - Lane first: the blind arm runs only if the lane passes about 8 of 11. Otherwise the estimate uses batch 1-2 blind costs.
  - Jobs that start after 06:46 run stage 27, and those after 07:06 run 27b. ultraplot r2 and pyrato r2 rerun on it as live
    checks, reported separately.
  - So far:
    - nestedpd: pass-repaired, a fallback fix. Claude narrowed it so that tuple keys still go to pandas.
    - iceberg r1: fail-infra. The rustup download failed with errno 101 on the runner. r2 is queued.
    - marimo2 r1: fail-diagnose. Gate 3 rejects a Minimal fix that names the new function the Interface block asks for.
    - ultraplot r1: fail-test, on the stage 26 runner (see stage 27).
    - eodag and openharness: refused by C3 on "auth".
      - openharness is a real auth task.
      - eodag is a keyword false positive: the word comes from an env var in the repro.

## 10. Plan approval sends code steps to the lane (plan-offload, 2026-11-04)

The owner (11-04): when a plan is approved in plan mode, its coding steps should go to the local tools first.

- **Hook** (PostToolUse, matcher ExitPlanMode; `~/bin/plan-offload install-hook` adds it):
  - With ```offload units in the plan, it adds the routed units to Claude's context, plus one step: run
    `plan-offload submit --plan P` before writing any code.
  - With no units but code in the plan, it adds a unit template and the lane rules.
  - With no code in the plan, and for subagent plans, it stays silent.
- **Unit** (one fenced block per code step):

      unit: area-change          # the job name and queue label
      repo: <checkout path>
      base: HEAD                 # optional
      file: <path>               # optional lead
      fn: <function>             # optional lead
      route: claude - <why>      # optional, default lane
      desc: what happens now -> what should happen, with one example

  A lane unit is one behaviour change inside existing functions of one file.
- **Routing** (`plan-offload check`, no side effects): units go to Claude when:
  - the runner's must-not-offload rules hit the repo name, a file lead, the function lead or the description;
  - MINI_AUTO=off;
  - the unit says so.

  Errors:
  - file leads must be tracked at base; an ambiguous suffix is an ERROR;
  - descriptions under 40 chars are refused, because the lane works from the description alone.

  A second lane unit on the same file waits ("later") until the first lands. A landing copies only onto the job's starting copy.
- **Submit:** one `mini-auto new --submit` per lane unit, so the worktree, run dir, mini-submit checks and enqueue are unchanged.
  - Every unit goes in the per-plan ledger `~/.ollama-dispatch/plan-offload/<plan>.jsonl`.
  - A second submit skips queued units; `--only UNIT` resubmits with a -rN name.
  - Any ERROR means nothing is submitted.
- **Quality:** unchanged. Claude reviews every lane pass with `mini-auto review JOB ok|reject` (ok lands it uncommitted), and
  writes failed or rejected units itself. `plan-offload status --plan P` shows each unit's state and next step.
- **Tests:** `test-plan-offload.py` runs 80 checks on claude-sandbox with temp repos and a fake mini-auto. Run it with Python 3.12 or
  later; the runner has a PEP 701 f-string.
- **Kill switch:** PLAN_OFFLOAD=off, or the file `~/.ollama-dispatch/plan-offload/OFF`.
- **Known friction:** dispatch-handauthor-gate.sh still blocks Claude's own Write/Edit of code in app repos, so claude-route units
  there need the owner's bypass, as before.
