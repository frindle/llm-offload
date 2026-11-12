# Offload rollout: what shipped, how to enable, how to kill

Plan: `offload-increase-plan.md`. The 2026-11-03 section comes first, then 2026-11-02. The 2026-10-31 sections after them are
history. Where sections disagree, the newest wins.

## Status 2026-11-03 (committed in ~/bin, branch frindle/local-llm-offload; dashboard repo 9cc9668, 61ae7c2)

**Verdict.** The queue now waits out the three infrastructure stalls seen on 11-02 instead of grading them as model failures:

- Darkbloom sandbox holds;
- locked-screen path freezes;
- git/disk stalls.

The launching session gets each job event exactly once. The dashboard shows a running offload job's step and any wait. Passed fix
jobs are sampled for a Claude review on the subscription: 1 in 5 by default, and every one while more than 10% of recent reviews are
rejects, as now. That review is the only check on a false PASS, because Opus reviews only failures. Every sampled pass has now
been reviewed: the first 53, then the 69-pass backlog. Of those 122, 35 were rejected (28.7%; the target is at most 10%). Every
reject has a fix. Four fixes were on local review branches; all are merged, and resell-tracker's was pushed on 2026-11-03
(see "Review branches"). See "Sampled review results" and "Backlog review" below. Most rejects (27 of 35) had one cause:
a TASK rule that no frozen test checked. Specs now have to name the test for every rule (Rule coverage, 4518c45).

Later on 11-03, the BFMR split bug ran through the queue as a live test. Its review reject led to soft gates for the misses that
no test can catch: stale prose, whitespace-only edits, and unneeded code or casts. When only a soft gate fails, the pass is
kept and gets a 100% Claude review instead of a rerun. See "BFMR as the offload test". The scorecard now counts 131 sampled
passes, 28.2% rejected, plus one needs-review pass, which was rejected.

### What changed since 11-02

| Commit | Change |
|---|---|
| f3144c0 | Workbook: xattrs are read through libc on macOS. `os.listxattr` is Linux-only, so the File Provider root check never ran. |
| cdad242 | Power sampler: per-minute sandbox_jobs and network bytes. It uses `netstat -ibn`, because name lookups took 6.7 s. nettop gets its pty as controlling terminal, so a killed sampler cannot orphan it. (Owned by the bloom session.) |
| 343e4e2 | **Sandbox hold:** see below. **Path-access gate:** macOS freezes a launchd process's first touch of ~/Desktop or a removable volume while the screen is locked (job 50b475235c58 froze at `git rev-parse`). Throwaway probes outside the state lock predict the freeze. Jobs wait (`INFRA WAIT infra-path-access`; kill switch `PATH_ACCESS_GATE=0`) instead of launching into it. Since d447dee at most 8 probes wait for an answer and at most 24 are alive, held ones included; a hung probe is a sleeping process, freed at unlock. **infra_incidents.py:** an append-only incident log written by the queue and the runner. **queue-deadman:** a hold that ends on its own is flagged only past LONG_HOLD_MIN (sandbox 6 h, path 12 h). **BloomGauge control fixes** from the simulated run. |
| 575f414 | Dispatch reconcile and escalation watcher: a git that times out, or never ran, is not a deleted branch. Git calls are bounded and never wait on a child stuck in the kernel's access check (dispatch-ack-reconcile's kill path too, since 788003a). |
| 62fb0c3 | `mini-spec` writes per-step frozen-test skeletons (plain-script RESULT harness) and the C4 fields that need no reference fix. `mini-validate` fills target_fn and fix_steps/fix_wall from the ref, and an unfilled template never passes V3. The runner bounds git (60 s x 3) and has a setup watchdog. A stalled filesystem exits class INFRA (git-stall/disk-stall) with an incident row, instead of being graded as the model's failure. |
| 5a459b9 | fn_scope false positives: a new top-level helper class the target names is in scope, and a qualified call-site update in another allowed file is a 1-hop neighbour. Replays: docsum-cli now passes. The 4 true positives still fail. |
| 706b32e | Queue events, exactly once. A hermetic stress test (`test_queue_events_exactly_once.py`) found 5 bugs, now fixed. The writer repairs a torn last line before appending. The deliverer re-reads when the log is rotated mid-read. It drops in-batch duplicates. It no longer re-delivers an orphan event the launcher's cursor already passed. |
| 0fb41eb | **Sampled pass review:** see below. |
| 8c3c45e, 60b6aa4 | Fixes for three rejected passes that had landed as-is: the docsum reduce loop and heading layout, the bloomgauge control config, and docsum_pdf's unused import. See "Sampled review results". |
| 0cc270a | **Review sampling is enforced**, not report-only. See "Sampled review results". |
| 10694d3 | **Runner lint gate (G4b):** see "Sampled review results". |
| d447dee | Path gate: with the screen locked, one held probe stands for its whole protected root (~/Desktop, ~/Documents, ~/Downloads, `/Volumes/<name>`). Later probes under that root get an inferred blocked verdict at once instead of filling the probe cap and starving other paths. `PATH_PROBE_MAX_INFLIGHT` (8) counts unanswered probes; `PATH_PROBE_MAX_PROCS` (24) caps all live ones. A killed stale probe stays tracked until it exits and never yields a verdict. |
| ed29118 | infra_incidents: an unknown lock state (slow or unreadable ioreg) is cached 10 s, so a stuck ioreg costs one probe timeout per 10 s, not one per record. Open and close rows pair on component, path or model, and target, so two paths blocked at once no longer close each other's incident. |
| 788003a | dispatch-ack-reconcile: after killing a timed-out git it waits at most `kill_wait` for output, so a grandchild holding the pipe cannot hang it. |
| 872b7cf | **Model-not-served bound:** see "Darkbloom sandbox holds". |
| ff09483 | bloomgauge_tick: a bare `run_async(work)` is throttled like the hook's call (backlog reject dcbe12803851, which had landed as-is). |
| 1d67df8 | Scorecard: a job counts as a reject if any review rejected it, so an ok re-review after Claude's fix no longer hides the model's miss. Only fix-kind reviews feed the pass rates. The review-all switch judges a pass by its finish time (`persisted_at`), so it covers passes from when it turned on. |
| d14d473 | ollama-queue: the BloomGauge control thread and the main loop can raise the same alert at once. The open-row check and the append now run under one lock, so the alert writes one row. |
| f436d54 | Scorecard: the review-all switch keeps its closed on-periods, so passes that finished in one stay in `sample list` after a dip. See "What changed in response". |
| 4518c45 | **Rule coverage (V8):** every TASK rule names the frozen test that asserts it, checked by `mini-spec --lint` and `mini-validate`. See "What changed in response" under "Sampled review results". |
| c586d56 | (11-02 evening) test-needs-review's Accept check matches the dashboard, where the route stays and the button was removed. |
| ab51573 | bloom_archive: machine-config's spool_put fix (9eae74f; a lost link race is "same", mkdir and temp-file errors propagate), keeping cdad242's two LOG_GLOBS entries. |
| 1f88995 | **BloomGauge hand-back:** follows its turn-on to the outcome; a blocked turn-on is retried with backoff. See "BloomGauge control: the hand-back follows its turn-on". |
| e769885 | `test-bloom-control.py`: the scenarios that drive the bloom_control queue integration removed in 197b069 report as retired, not failed. |
| 32ed1fc | BloomGauge transport faults: the app's real client against a fake BloomGauge (refused, hung, 429, hand-back). See the same section. |
| 04f8066 | `mini-scorecard exclude add/remove/list`: jobs that do not test the model (synthetic or infra test jobs) are kept out of every count, rate and sample, with a reason. The stress run's 11 toy jobs are excluded. See "Stress run on the live queue". |
| dashboard 9cc9668 | The Live log tile shows a running offload job's runner step (`step 23/78`) and its phase: preparing, model step, or waiting for Darkbloom (sandbox hold or outage). Two stale dashboard tests are fixed. |
| dashboard 61ae7c2 | Run Status: a failed offload job stays in "needs your eyes" at any age. A mini-swe job writes no gate.json, so the day-old fold meant for gate-less research rows had dropped failed offload jobs after 24 h. Passed and cancelled offload rows still fold after a day, with a reason that says what they are. |
| 9578437 | mini-validate (found while writing the BFMR job): V5 skips comment-only hunks. The scratch copy links node_modules and .venv, so the repo's type check and test runner work there. V4 fails when the suite is red at baseline, unless the spec sets `suite_baseline_red`. |
| 32d3838 | Runner preflight resolves `./` import specifiers. mini-spec templates follow the repo's ESM syntax when package.json has no "type". |
| 3e2c8fd | ollama-queue: no 'NO --verify' warning for mini-swe-runner jobs, whose gates are the verification. |
| e2e9f85 | **Soft gates and needs-review passes:** see "BFMR as the offload test". |
| 6fc4a6c | Fixes from replaying the BFMR job: the ws_only wording rule, cast_check, and fn_scope's import check. See the same section. |

Fable reviewed this work twice. Every finding is fixed: in the commits above, in plex-automation 6094208 (on the review branch
below), and in the lint wording under "Why the gates missed them".

### BFMR as the offload test (job 5a220e4fd177; e2e9f85, 6fc4a6c)

The owner asked for the BFMR split bug in resell-tracker to be run through the offload tools as a live test. The bug: three qty-1
BFMR web rows tie on the 1:1 key, so the split halves never reach matchSplitGroups. The task was to break the tie by the row's
own tracking number (resolveTrackerBackfill) and to update the module header's ambiguity paragraph.

**Result.** The model passed every gate. Its logic matched the reference fix, and its 5 tests were real. Claude's review
rejected it for three defects:

1. The module header still said ">1 match means the field stays null", although the TASK said to update that paragraph.
2. It made a whitespace-only edit to an existing doc line (`column.  A bad`).
3. It added a redundant `(l as BackfillLocalRow)` cast. The type already declares `tracking_number?: unknown`.

The fix landed with those three corrected on resell-tracker branch `bfmr-split-tiebreak`, off origin/main 6057e69:

- 894a1fd: prep;
- 9f11c04: tests;
- b90616b: the fix, which is the ledger row;
- 798e95f: npm test runs the BFMR tests.

It is **not pushed**. Its live check after deploy is pending in live-validation-ledger.

**What each defect trips now.**

| Defect | Gate | How |
|---|---|---|
| stale header paragraph | must_not_contain | TASK.md's `## Must not contain` lists the old phrase. `mini-spec --lint` warns when a TASK asks to change prose and has no such list. mini-validate V4 runs the gate on the reference fix, so a literal that the reference still contains, or that matches nothing, fails before submit. |
| whitespace-only edit | ws_only | ws_cleanup puts back whitespace-only line changes while the test stays green. It is skipped only when TASK.md asks for a formatting change, or when the spec sets `"ws_clean": "off"`. |
| redundant cast | fix_hunks (cast_check) | Drops each TypeScript `x as T` the fix added, one at a time, and reruns the type check. A cast the type check passes without is reported. A cast never changes runtime behaviour, because types are erased. |

**Soft gates.** fn_scope, literals, lint, must_not_contain and fix_hunks are soft gates, and ws_only acts as one. When only soft
gates fail, the run ends `PASS needs-review soft=<gates>`:

- the queue records `NEEDS-REVIEW (soft: ...)`;
- the job event's state is `needs-review`;
- the scorecard reviews every such pass, and keeps them out of the sampled-pass rate.

The model's work is kept: Claude fixes the miss at review rather than rerunning the job. To turn fix_hunks off, set
`MINI_FIX_HUNKS=off` or the spec's `"fix_hunks": "off"`. A steps chain keeps the strict verdict.

**The replay found two gaps.** The new gates were replayed on the real model output, on a scratch clone, in the runner's order:

- **ws_only never ran.** The old rule skipped the cleanup whenever TASK.md said "whitespace". The BFMR TASK says it three times,
  about tracking numbers: whitespace as data, not formatting. The rule now matches formatting wording only. Across the 385 TASK
  files in mini-runs, the old rule skipped the cleanup on 45 and the new rule skips it on 2. Of those 2, "delete ... its two
  trailing blank lines" is a real formatting change; "at the same indentation" is a harmless miss.
- **fix_hunks missed the cast.** The cast sits inside a hunk the test needs, so reverting the hunk cannot isolate it. cast_check
  drops each new cast alone, up to 4 per run. It compares the type check's error lines with those of the final files, so a repo
  that already has type errors still works; BFMR has 205.

The replay also taught a lesson about choosing the literal. The first literal chosen, `>1 match means the field stays null`, is
also in the reference fix's header, which reworded around it, so the gate failed the reference too. V4 would have refused that
spec before submit. The right literal is `the field stays null and the sync says so.`

After the fixes, the replay gives these results:

- ws_cleanup puts back lib/bfmrJoin.ts:253, and all 5 tests stay green.
- must_not_contain fails the model output and passes the reference.
- fix_hunks finds all 3 hunks needed and the cast at line 344 redundant.

The verdict is `PASS needs-review soft=fix_hunks,must_not_contain`. The job now gets a 100% review, and its packet names all three
defects.

**fn_scope import check (6fc4a6c).** The check counted names that an import line does not bind:

- the name before `as`;
- the module of a python `from M import n`;
- words in a trailing comment.

Three of the four REVIEW packets that said "added but unused" were this:

- `import bloom_settings as BS  # noqa: E402`;
- `import bloom_accounting_tax as TX`;
- `import bloom_accounting_entries as AE`.

The fourth was `import re` used by a new `PATH_RE` constant that the spec lists in new_names. An import used by such a
constant now counts as used. A renamed alias and a changed module path still fail.

**The one needs-review pass, 4dc87852f0aa, was rejected as low severity.** The TASK said to import `bloom_settings` plainly,
with no alias; the model wrote `as BS`. The pass landed as-is in bloom-ledger da95fe6, and later routes use `BS`, so it stays.
fn_scope flagged it only through the alias false positive above. Only 2 of 303 TASKs use exact-form wording like this, so
there is no lint hint for it. The tool for it is a `## Must contain` literal that only the exact form contains, such as
`bloom_settings.machine_rows(`.

**Decision for the owner: no-ref specs (L1b).** In this mode Claude writes the TASK and the frozen test but no reference fix. That
saves about 16 of the roughly 80 lines Claude writes per job. Without a reference, V4 and V5 cannot check the gates against a
known-good fix: they cannot tell whether a must_not_contain literal is right, or whether every hunk is needed. Recommended
only with fix_hunks on and a review of every no-ref pass.

### Darkbloom sandbox holds (343e4e2)

Darkbloom lends the Mac to sandbox jobs. It unloads the model but keeps listing it on `/v1/models`, so a model check cannot see the
hold. The queue reads the provider and slots log tails outside its state lock. While a hold is on, it holds launches
(`INFRA WAIT darkbloom-sandbox`; kill switch `SANDBOX_HOLD_WAIT=0`). A running job's runner treats model_unavailable/modelNotLoaded
as provider-unavailable, so it waits instead of failing.

Verified live on two holds (UTC, 2026-11-03):

- 07:54 to 08:10:53. The queue held its launches.
- 08:13:07 to 08:27:27. A running step rode it out: the 575 s it spent waiting was excluded from its time budget, and the step then
  PASSED.

A 21:16 to 21:37 PST Darkbloom outage on 11-02 was a provider outage, not a hold. The model reloads after each hold.

**Model not served (872b7cf).** Darkbloom answers a request for a model it does not serve with the same capacity text as an
outage, so a run whose model was swapped out kept waiting, up to 7 days. Each outage wait now also asks the endpoint's `/models`.
Once the lists have lacked the run's model for `MINI_MODEL_ABSENT_MAX_S` (default 1800 s; 0 turns the bound off), the runner
stops with `RESULT FAIL model-not-served`, class INFRA, and records an incident. A sandbox hold keeps the model listed, and an
unreachable endpoint or an empty list resets the count, so neither trips it.

### Sampled pass review (0fb41eb)

A passed mini-swe FIX job is in the sample when `sha256(id) % 100 < MINI_SAMPLE_PCT` (default 20). Test drafts and diagnoses are
excluded, because Claude reads every test draft. The event line of a picked pass ends with
``review=`~/bin/mini-scorecard review brief <id>` ``. The brief names the REVIEW.md packet and gives a four-point checklist and
the two record commands. The review is done in the session or a Sonnet subagent of it; never the API or `claude -p`.

- Record each verdict with `mini-scorecard review add --job <id> --verdict ok|reject --note ...`.
- `mini-scorecard sample list` shows picked passes not yet reviewed.
- `mini-scorecard report` shows the review-reject rate. The plan section 5 target is at most 10%.
- `MINI_SCORECARD=off` turns the sampling off.

Since 0cc270a the sample rate is set by the scorecard state, not only by `MINI_SAMPLE_PCT`. See the next section.

### Sampled review results (2026-11-03)

The first 53 sampled passes were all reviewed: 16 rejects (30.2%). Each was a real defect the gates passed. Fix commits are in
~/bin unless a repo is named.

| Job | Family | Defect | Fix |
|---|---|---|---|
| ffa87c89904d | queue-model-tag-resolver | matched a served model only when exactly one was served; a trailing `/` gave `//v1/models` | before landing, 97e90e8 |
| d7d7ed413f48 | plex-llm-decide | resolve_action raised on an unhashable kind and let a NaN confidence through | plex-automation 0da763d |
| 63e09eb56c6b | plex-llm-decide | parse_decision accepted a NaN confidence | plex-automation 0da763d (math.isfinite) |
| bb5e00ca3286 | clamshell-size | resize handler not debounced; the model's test asserted the immediate send | before landing, clamshell 4cfa9b5 |
| 5a84d209b098 | clamshell-size | model's test omitted TASK cases (4 mutants survive); unrelated formatting edits | clamshell 4cfa9b5 |
| 33eb52c61fc8 | bloom-ledger-settings | dead code: identical if/else arms, unused `import os` | superseded by bloom-ledger 9018014 |
| 7591f7e41a43 | docsum | reduce merged once instead of looping until the input fits; headings on one line | 8c3c45e |
| 10cdfee99ae7 | docsum | PDF xref entries 19 bytes (must be 20); unused `import io` | d570e8c, 60b6aa4 |
| aea08f7dc053 | docsum-ui | refresh() never cleared the table, so rows duplicated every poll | 6391144 |
| e4565ee21009 | queue-bloomgauge-control | an unknown env value fell through to the mode file; `inf` raised OverflowError | 8c3c45e |
| 057e88c13300 | bfmr-rest | required fields hit `.trim()` unguarded (TypeError, not the TASK's error) | resell-tracker c065da5 |
| 9f9b7e42fc98 | bfmr-rest | same `.trim()` gap; an unused `trimStr` | resell-tracker c065da5 |
| 0bbfb8fc66aa | tax-logging | non-str sha raised TypeError (TASK: ValueError); a short `os.write` was ignored | machine-config d59c59a |
| 297eab21621c | ledger-tax | load() added another print handler on every reload | bloom-ledger cc9951f |
| e6c97ab94507 | recheck-incomplete-shipments | fix correct, but the model's guard 6-8 tests pass whatever guards 6-8 do | resell-tracker 6057e69, on origin/main (pushed 2026-11-03) |
| e709c1278476 | plexrepo-seed-floor-universal | unknown seed data: no fetch when seeding_time was absent, a missing upload count read as 0, progress None raised, "seeded Noned" text | plex-automation branch `review/seed-floor-unknown-data` bf9e827, plus 6094208 from the Fable review: an unknown save_path keeps the files, and the purge passes an unknown seeding_time through as unknown (local, not pushed) |

Why the gates missed them:

- 11 of 16: the frozen or model-written test lacked a TASK case, mostly bad or missing input (NaN, non-string, missing field, a
  second pass) or an event the fake never fires.
- 3 of 16: a model-written test was too weak, and nothing mutation-checks model-written tests.
- 4 of 16 carried dead code. Three were unused names that the G4b lint gate now flags: two Python imports (33eb52c61fc8
  `import os`, 10cdfee99ae7 `import io`; ruff F401) and a TypeScript helper (9f9b7e42fc98 `trimStr`; tsc `--noUnusedLocals`).
  G4b has no rule for an unused Python function.

What changed in response:

- **Sampling is enforced (0cc270a).** `review add` refreshes the state, so a reject puts its family at 100% sampling at once.
  - **Global review-all switch:** while more than 10% of the latest 30 reviewed fix jobs (at least 20) are rejects, every pass
    that finishes after the switch turned on is reviewed. A job counts as a reject if any of its reviews rejected it (1d67df8),
    and a pass's finish time (`persisted_at`) decides whether the switch covers it. It turns itself off when the recent reviews
    are clean. A closed on-period is kept (f436d54), so its unreviewed passes stay in `sample list`.
  - On since 2026-11-03T16:28:28Z, with 12 of the latest 30 jobs rejected. It was on from 09:32:21Z. At 16:28:27Z a batch of
    backlog verdicts brought the latest 30 to exactly 3 rejects (10%), which turned it off for one second. No pass was lost: every
    pass from that period has since been reviewed.
  - `MINI_SAMPLE_PCT` is a floor the state cannot lower. `MINI_SCORECARD=off` turns all of it off.
  - Cost: until the reject rate falls, every passed fix job asks its launching session for a review.
- **Lint gate G4b (10694d3).** A soft gate in the runner flags unused imports and locals that the fix added:
  - Python: ruff F401/F811/F841, baseline against fix.
  - TypeScript: when `typecheck_cmd` is a plain tsc call, the same call with `--noUnusedLocals`, on added lines only.
  - The next attempt is told what to remove. If it is still there at the end, the job is `PASS needs-review soft=lint`.
  - `"lint": "off"` in a spec skips it.
- **Rule coverage (4518c45).** This is for the 27 of 35: the 11 of 16 above, plus 16 of the 19 backlog rejects below. Each was a
  TASK rule that no frozen assertion checked. TASK.md now has a `## Rule coverage` section. It lists every rule of Required change,
  bad and missing input included, one bullet each, and each bullet ends with the frozen test that asserts it, in backticks:
  ``- a row without a date is skipped -> `test_skips_undated_rows` ``. One test may pin several rules.
  - `mini-spec` drafts the section with a TODO marker.
  - `mini-spec --lint` FAILs while the marker remains, or when a line names no test that the frozen file defines (frozen/<test_file>,
    else the baseline_from copy). It WARNs when there is no section or no frozen test yet.
  - `mini-validate` runs the same check per step as V8. Because of that, `mini-submit`'s C3 (a fresh VALIDATION.md) holds it too.
  - Python tests are `def test*` names; node and TS tests are test()/it() titles. Every existing frozen test file parses.
  - No section, or no rule about bad or missing input, is a WARN only, so the 384 existing TASK files still validate. Leaving the
    section out is therefore the way to skip the check, and it shows as a WARN in VALIDATION.md.
  - V8 is skipped for a local-model test draft (`mini-testdraft check`, phase_a_only), because the test names there are the model's.
  - Limit: the check proves that each rule names a real test, not that the test's assert matches the rule. The sampled review still
    judges that. The reject rate will show the effect only on specs written from now on.

### Backlog review (2026-11-03, from 16:00Z)

The 69 passes from the last 14 days that were picked but never reviewed were all reviewed, by four Sonnet subagents of this
session: 19 rejects (27.5%; the batches were 1 of 17, 4 of 19, 8 of 16 and 6 of 17). With the first 53 that makes 122 reviewed and
35 rejected (28.7%). `mini-scorecard report` shows 36 of 129 (27.9%) because it also counts the bloom session's 7 reviews of its
own ledger passes, one of them a reject (641fc9f4805d, landed in the reference form). `mini-scorecard sample list` is now empty.

Most of the 19 were already fixed: Claude landed a corrected form, a later commit fixed it, or it never landed. Three needed new
fixes on 11-03: dcbe12803851 (ff09483 in ~/bin), and ceb503eac3bf and 183a2d81da07, whose fixes are on local review branches.
Commits are in ~/bin unless a repo is named.

| Job | Family | Defect | Fix |
|---|---|---|---|
| 3615cebc1e91 | ledger-tax | depreciation and lease totals restart at 0 after a machine with no basis, so the total is a silent wrong figure instead of None unless that machine is last | never landed; bloom-ledger main has the reference form |
| bec02b3da734 | ledger-tax | the settings rows drop the space after `</label>` in all 11 rows; dead `ctl` | replaced by the reference, bloom-ledger 3de79b3 |
| e44e7743e02d | bloom-ledger-settings | load_tokens raised AttributeError on a machines.json that is valid JSON but not an object, a 500 on every ingest | bloom-ledger 4869e09 (a later job) |
| 08b92e8daf58 | bloom-ledger-settings | the add form posted to a route that does not exist; `#onboarding` after the last section, not after the form | removed by the bloom-ledger 2ff3a6e redesign |
| bbb2a0de1ed7 | bloom-ledger-settings | relative-time cut-over 540000 ms, not 5400000; no `resp.ok` check, so an HTTP 500 rendered NaN; dead `formatTime` | bloom-ledger ee9cf12 |
| c75a39df68b0 | queue-bloomgauge-control | float settings read through int(), so "0.5" or "1e3" silently became the default | landed in fixed form (safe_float), f32e164 |
| 8e729e3510f1 | queue-bloomgauge-control | `wanted()` returned False when called bare, as the hook calls it, so control never ran; read_inputs caught only Unavailable | landed in fixed form, 7642cce |
| dcbe12803851 | queue-bloomgauge-control | run_async skipped the tick_s throttle when `now` was not passed | landed as-is; fixed in ff09483 |
| eb1cd958c7bd | queue-bloomgauge-control | `load_client("")` skipped MCP_CANDIDATES, so control always read as unavailable; lease_load raised on an OSError other than ENOENT | f32e164 (landed form), bdbfc0c |
| 5901bc4d6a1e | docsum | pypdf pages not reset before the pdfplumber fallback, so a failure mid-file duplicated pages | fc4b05e |
| ba995d2a569d | docsum | an oversize first page emitted an empty chunk | landed in fixed form, d570e8c |
| 5cf9e590707a | docsum | prepare_job set the root to 0700 only when it created it; the TASK says always | landed in fixed form, d570e8c |
| ba40895bfe6a | docsum-ui | list_jobs raised on a null label or a row without a status (no live impact: the caller filters them) | landed in fixed form, 6391144 |
| 92d4f2728dc2 | recheck-incomplete-shipments | the tracking merge compared carrier and number case-sensitively, so a casing-only difference made a new record | landed in fixed form, resell-tracker 6e61d2d |
| aac019e351a2 | recheck-incomplete-shipments | the model's test covered 2 of the 4 cases the TASK names | landed with all 4, resell-tracker 7aa4034 |
| 01e77dfbe586 | clamshell-size | a Swift doc comment left above the wrong declaration after a move | clamshell 5dd83ef |
| 1dc6703c808a | plex-llm-decide | json.dumps of a field it cannot serialise raised inside the decision path; dead `summary_str` | plex-automation 0da763d |
| ceb503eac3bf | tax-logging | spool_put reported "written" when a concurrent writer had already stored the object, and a kind directory blocked by a file read as success; a short write was ignored | short write: machine-config 2fc9aea (landed form). The rest: machine-config branch `review/bloom-archive-spool-put` 9eae74f (local, not pushed) |
| 183a2d81da07 | tax-logging | a dead `skip` flag, so manifest_run wrote a manifest for a day behind the chain head, which verify_chain then reports as a break | bloom-ledger branch `review/manifest-run-skip-ahead` c9a77c0, and the machine-config copy on a branch of the same name, bc391c5 (local, not pushed) |

Why the gates missed them: 16 of 19 had a frozen test that lacked the case (bad or missing input, an error path, the hook's real
call) or checked too little (element ids only, blind to whitespace). One model-written test covered 2 of 4 TASK cases
(aac019e351a2). One was a dead variable that was itself the bug (183a2d81da07), which G4b's F841 now flags. One, a misplaced Swift
doc comment, is outside every gate.

### BloomGauge control: the hand-back follows its turn-on (1f88995, 2026-11-03)

- **The stall.** BloomGauge answers a Manager-on request at once (`pending`), then checks Darkbloom before it turns the Manager
  on. That check can take up to about 30 minutes, and it can end `blocked`. On 11-02 at 21:30 the hand-back was accepted, then
  blocked: "Could not verify this command. Check Darkbloom on the Mac before retrying." The queue read `blocked` as a change in
  progress and waited on "mid-change" for 13 hours with the Manager left off. It had also dropped its lease as soon as BloomGauge
  answered.
- **Now.**
  - The answer's operation id is kept in the control ledger (the mgr_on result row). Every tick, and the stale-lease guard,
    settles it from BloomGauge's view and writes one `phase: outcome` row.
  - `active`, or replaced by a newer request: done, and the lease is released.
  - The owner chose Manual while it was pending (`overridden`): the queue yields as for any manual change (2 h, then the hand-back).
  - `blocked`, `cancelled`, no outcome after 31 minutes (`timeout`) or the operation gone (`lost`): a failed Manager change,
    retried after 900 s, 1800 s, then 3600 s. The third failure in a row raises one alert (ESCALATIONS.md row and notification);
    the janitor closes it when a turn-on goes active.
  - While a turn-on is pending the queue waits ("confirming") instead of making another change. The lease is kept until the
    outcome, so a daemon restart or the guard picks it up from the ledger.
  - A turn-on that goes active after BloomGauge's readiness wait (more than 180 s after the request) is the queue's own, not a
    manual change. An 'on' click by the owner in between still counts as manual.
- **Live.** Deployed at 10:41 with a daemon restart: the stuck hand-back was retried and went active in 33 s.
- Tests: `test_bgc_turn_on.py` (22; each fix checked by reverting it and watching a test fail). The BloomGauge pytest set: 245 pass.
- `test-bloom-control.py` (e769885) reports the 8 scenarios that drive the bloom_control queue integration removed in 197b069 as
  retired instead of crashing on the missing `_BLOOM_CTL_MOD`; the rest pass.
- **Transport faults (32ed1fc, `test_bgc_transport_faults.py`).** The app's own client (`agent_mcp.py`, loaded as the queue loads
  it) runs against a fake BloomGauge on a spare port; the live app is never touched.
  - Connection refused: every tick waits "app-down: BloomGauge not answering", the lease is kept and nothing is sent. When the
    app answers again, the queue carries on from the same lease.
  - App hung: the client's timeout ends the call; the tick reads it as app-down, keeps the lease and sends nothing more.
  - The app's 429 ("at most 2 times an hour"): the queue backs off at least 900 s with one request sent and no alert, then the
    change goes through.
  - The hand-back follows the operation the real client started: the id BloomGauge answers with is the one the queue waits on,
    through to `active` and release.

### Restarts (done 11-03, the owner's go-ahead)

All three daemons now run the code listed above.

- `com.example.ollama-queue-api` and `com.example.queue-event-log`: restarted 09:54. That brought in the Live log offload progress
  (9cc9668), the Run Status fold fix (61ae7c2) and the event writer's torn-line repair (706b32e).
- `com.example.ollama-queue-daemon`: restarted 10:41 for 1f88995, with no job running, then again at 10:45:40 mid-job as part of the
  stress run (next section). It runs d447dee, ed29118, 8c3c45e, ff09483, d14d473 and 1f88995.

The runner (`mini-swe-runner.py`, 872b7cf and the G4b lint gate), mini-scorecard and the event deliverer start fresh for each job,
command or hook, so they need no restart.

### Stress run on the live queue (2026-11-03, 10:41-11:21)

Eleven toy jobs ran through the real queue, runner and BloomGauge control. Each job edits a small calc repo and has a reference
diff to check against. All passed, and every diff matched its reference.

- **Daemon restart mid-job (10:45:40).** alpha (888ec67a98fa) was killed, requeued and rerun; it passed at 10:46:17. The new
  daemon adopted the BloomGauge lease. Progress inside the killed attempt is lost by design.
- **Burst from one session.** bravo through lima (bundle ringer-a, event seq 44-52) passed between 10:47 and 10:55. The whole
  burst used one Manager change (mgr_off 10:45:29).
- **Unstamped job.** foxtrot (d8721c4c988e, ringer-c, no launcher) was shown once as "no launcher" (global cursor 46).
- **Job from a session that is gone.** delta (0b9cd1f6f311, ringer-b, launched from a dead socket) was delivered once to a live
  session as "#53 passed (launcher gone)" (cursor 46 → 53).
- **Hourly limit.** The hand-back turned the Manager on at 11:10:14 (active in 34 s), and delta arrived during that turn-on. The
  queue had made 3 changes in the hour, so it kept the last one for the next hand-back: "hold: Manager on; Manager budget used
  this hour (one kept for the hand-back)". delta ran with the Manager on rather than spend it. A fresh acquire (no lease) may
  use the last change; that difference is by design.
- **Real Darkbloom sandbox hold.** A hold ran 10:54:40 → 11:19:42 while the Manager was off, so sandbox lending does not depend
  on the Manager. delta waited with "darkbloom sandbox hold since 10:54" and started 1 s after the release.
- **Hand-back after the run.** The queue went idle at 11:20:50. At 11:36:20 it released the lease with "handback: Manager
  already on; released" and made no change.
- No 429s, no alerts, no failed jobs.

Not run live:
- BloomGauge down or hung: stopping the app needs the owner. Covered by the fake-app tests in 32ed1fc.
- The owner flipping the Manager by hand: covered by the manual/yield unit tests.
- A refused hand-back: it cannot be forced live. Covered by the policy tests.
- A fresh acquire spending the hour's last change: setting it up again would take over an hour of Manager changes and
  Manager-off time, which costs Darkbloom earnings. 04f8066 pins it in `test_bgc_policy_busy_step.py`.

The toy jobs are out of the scorecard (04f8066). No review verdicts were recorded for them, because an ok verdict would dilute
the reject rate. They are excluded with the reason "synthetic stress-run job"; `mini-scorecard exclude list` shows them. After
the exclusion, the 14-day report counts 252 jobs and 248 passes (it counted 263 and 259 before). The reviews stay at 130 with 36
rejects, `sample list` is empty, and the review-all switch is unchanged.

### Review branches (merged 11-03, except one push for the owner)

Nothing was pushed.

- plex-automation main: bf9e827 and 6094208.
- machine-config master: 9eae74f, and merge 2eef0a9 (bc391c5). ~/bin took the spool_put fix as ab51573 and kept its two extra
  LOG_GLOBS entries (`power/sandbox-jobs-*.csv`, `power/net-bytes-*.csv`, from cdad242). machine-config a3766f6 adds the same
  two entries, so its bloom_archive.py is now identical to ~/bin's. machine-config is 4 commits ahead of origin and **not
  pushed**; the push is the owner's. No LaunchAgent runs machine-config's copy; only ~/bin's is live (bloom session, 2026-11-03).
- bloom-ledger main: merge 3cb3b5f (c9a77c0).
- resell-tracker: pushed on 2026-11-03 with the owner's go-ahead. `review/shipment-guard-tests:main` fast-forwarded origin/main
  97c4b88..6057e69. Local main was not touched: it has uncommitted work that is not from this project.
- Also in resell-tracker: the browser-lock fixes from the 10-18 diagnosis (job e32e17104bc2), c98efa6 and a7b1d7a, are only on
  branch `worktree-costco-off`, 7 commits ahead of main.

### Known limits

Queue events are not fully exactly once. These are pinned as strict xfail in `test_queue_events_exactly_once.py`:

- a SIGKILL between the cursor write and the print loses an event (at most once);
- a stale socket after a SIGKILL hides events until the next hook;
- there is one global cursor per session and a single writer.

queue-deadman is not scheduled by any LaunchAgent, so a hold is flagged only when something runs it.

machine-config's `bin/` mirror has drifted from ~/bin: of its 1,715 tracked files, 66 differ and 172 no longer exist in ~/bin
(2026-11-03). Some of the missing ones are retired dispatch tools that must not come back, so a refresh has to be reviewed
file by file; a plain copy in either direction is wrong.

## Status 2026-11-02 (committed in ~/bin, branch frindle/local-llm-offload)

**Verdict.** L1 (the local 35B drafts the frozen test, Claude checks it) works through the real queue. Use it as an assist: Claude still
reads every ACCEPTed draft before it lands. It is NOT a go for landing drafts unread: 3 of the 8 specs that produced a draft were
rejected, and 3 more produced none, far above the plan's 15% Phase 1 reject limit. A second run (r2) of the 4 failed specs changed
none of those outcomes. The 10-31 "BLOCKER" is gone: the branch is in
~/bin, so `mini-submit` accepts test-draft jobs. Everything else in the 10-31 tables is still opt-in and off by default.

Launch (unchanged, one line): `mini-testdraft emit SPEC --out DIR`, then
`MINI_TESTDRAFT_MODE=on mini-submit DIR/spec.json --label <area>-test-draft --model qwen3.8:27b-q4_K_M --bundle <b>`
(mini-submit maps that model name to the Darkbloom 35B). RESULT PASS = ACCEPT; the draft is at `<run dir>/draft_<test>`.

### What changed since 10-31

| Commit | Change |
|---|---|
| 008f3ba | `mini-testdraft check` scores Python refs with `mini_mutate.py`: only the lines the ref changed are mutated, and each mutant is killed with the spec's own test_cmd. mutmut's indir mode rejected Claude's own validated tests for tooling reasons (path keys, its own venv), so the check no longer uses it. |
| f768ecb | Every scored run (ref, canary, mutants) is contained: throwaway HOME, DISPATCH_VERIFY_SANDBOX=1 and a sandbox-exec profile (no writes outside temp dirs, no connections to live services, no signals to outside processes). A test that only passes with the real HOME or a live service is left unscored, never run uncontained. |
| 57d8c6d | Runner test gate: `del sys.modules[target]` (a fresh import of the real module) no longer counts as mocking. Fakes installed with `sys.modules.setdefault/__setitem__/update` or `mock.patch.dict(sys.modules, ...)` are caught. |
| 05b5d74 | A rejected draft is retried with the check's findings, up to `test_attempts` (`emit` sets 3). When the draft fails against the ref, its own test output is included. The mock gate names every offender and allows faking the target's imports and settings. A run where zero tests ran fails. |
| bf67a7e | V6 allows a draft up to 10 test functions (fix jobs keep 5). An 8-test draft at mutation 0.982 had been rejected on size alone; its retry, cut to 4 tests, scored 0.702. The V6 retry text now says to merge tests and keep every assertion. |
| 1c9cb6a | handoff-autoclear clears test-draft rejects mechanically instead of queueing a research follow-up for each one. |
| 74eaadb | Queue events reach the session that launched the job (see below). |
| 1346467, 45b598c | BloomGauge control hardening; the retired darkbloom tools refuse to run (see below). |
| b6f537d | BloomGauge control: when BloomGauge turns the Manager back on during queue work, the queue turns it off again (see below). |
| 9e89c7d | BloomGauge control: no Manager change, pick or lease while BloomGauge's activity log is unreadable (see below). |
| 2caf419 | Queue events: a requeued, resumed or answered job reports its next outcome again; no double delivery after /clear; subagents never consume events (see below). |
| 8bda400 | `mini-answer <id> "..."` answers a needs_info job. The queue, the escalation watcher and the handoff panel already told the operator to run it, but the CLI had stayed in a worktree. |
| 19e99d1 | Frozen tests: a Python test is rejected when every assertion only checks the code's source text (open/read_text, inspect.getsource, ast.parse of `__file__`). Such a test turns green once the fix contains that text, so it proves nothing about behaviour. Checked statically and at runtime. On the existing corpus it flagged 0 of 211 frozen tests and 0 of 1477 test files. |
| aa12799 | A drafted test runs contained everywhere: in the runner's test gate and in every mini-testdraft check (V3/V4/V5/V7), not just for the mutants (see Containment). |
| b740999 | BloomGauge control: owner first (see below). Live since the daemon restart at 16:40. |
| e797ba9 | Every mini run sets DISPATCH_VERIFY_SANDBOX=1 (see Containment). Also fixes three stale tests. |
| 7cd3be2 | `mini-finish`: when a local job fails on its own work (class model, timeout or context), the session that launched it finishes the local model's attempt in place, on the subscription, and the job's own gates decide (runner `--check-only`, no model). PASS resolves and clears the job; a chain's remaining steps go back to the local model as a continuation that replays the finished steps. The failed event carries `finish=`. See mini-swe-agent.md, "mini-finish". |
| 49cc78f | `mini-finish` review fixes: the check runs the spec's mutation_gate; a real runner run refuses a run dir mini-finish holds (`bad-cwd finish-claimed`, class infra); one lock covers claim, `--take`, check and the FINISHED retry; the frozen test is inferred when no runner log was kept; an unusable job_id or uncreatable continuation dir is handled; `--try-fix` creates new directories; a FINISHED retry works with the spec gone; `finish=` only for chain, two-phase or diagnose specs. test_mini_finish 103/103, full suite green. |

### Calibration: Claude's own tests through the same check

Claude's frozen tests from validated v2 specs, run contained through `mini-testdraft check` (V1-V7 plus line-scoped mutation >= 0.70):
147 specs, 130 ACCEPT (88%), 17 REJECT. Of the 17 rejects:
- 10 scored below 0.70, a real weakness by this measure;
- 2 were unscored because the test fails on the unmutated ref copy outside the real HOME;
- 2 had "no changed function found";
- 3 are JS with "no target_fn span".

The last 7 are tooling limits, not test quality. An earlier, uncontained run gave 66 of 70. So Claude's own tests clear this bar about 9
times in 10.

### L1 trial: the 35B drafts the test, through the real queue (bundle l1-shadow-replay)

"Claude" is the mutation score of Claude's own test for the same ref. r2 = a second job after the retry lever (05b5d74).

| Spec | Result | Draft (killed) | Claude | Attempts | Notes |
|---|---|---|---|---|---|
| docsum-chunk | ACCEPT | 0.958 (23/24) | 1.0 | 2 | |
| ledger-profile-acquisition-store | ACCEPT | 0.839 (26/31) | 0.839 | 1 | |
| settings-profile-post | ACCEPT | 0.702 (40/57) | 1.0 | 2 | attempt 1 (8 tests, 0.982) failed V6 only; it would pass under bf67a7e |
| docsum-proxy-forward | ACCEPT (r2) | 0.898 (53/59) | 0.763 | 3 | r1 REJECT: the draft failed against the ref (V4/V7) |
| docsum-render | ACCEPT (r2) | 0.90 (54/60) | 0.767 | 3 | r1 REJECT: V4/V7 |
| ledger-settings-tunnel-guard | REJECT | 0.613 (19/31) | 0.806 | 1 | V5 missed ref hunk 3. r2: REJECT, 0.548 (17/31), the same hunk missed |
| settings-machine-rows-profile | REJECT | 0.606 (20/33) | 0.879 | 2 | below 0.70 |
| bloomgauge-ownerless-tick | REJECT | 0.667 (40/60) | 0.767 | 2 | below 0.70 |
| plexbypass-served-model-upstream | no draft | - | 0.947 | 2 | step limit; the draft passed at baseline. r2: no draft, wall-time limit |
| plexrepo monthly-upgrade-never-seeded | no draft | - | 0.81 | 2 | test gate: replaced `sys.modules['monthly_upgrade']`. r2: no draft; it faked the code again (`m.s`), then hit the step limit |
| plexrepo queue-dupes-deluge-unknown | no draft | - | 0.909 | 2 | test gate: overwrote `aw.requests`. r2: no draft; it faked the code again (`aw.get_all_torrents`), then hit the wall-time limit |

- Across the 5 ACCEPTs, drafts average 0.86 and Claude's tests 0.87 on the same refs: 2 drafts are better, 1 is equal and 2 are lower.
  settings-profile-post (0.70 against 1.0) is the case that shows why Claude still reads every draft.
- Every REJECT scored well below Claude's test, so the check is separating weak drafts from good ones. It never accepted a draft below 0.70.
- Drafts are longer than Claude's tests (median about 1.5x the lines) and do not copy them (text similarity 0.03-0.17).
- The r2 runs used 3 attempts each. Both plexrepo specs need a test that lets the real code run against a fake client. The 35B kept
  faking the code under test instead, so for this kind of spec Claude should write the test.

### Unraid (RTX 3080, 12 GB card, 9.6 GB usable)

Already used where quality holds:
- the gate pre-gate (qwen3:14b, 251 runs; the 27B regate on the Studio is authoritative);
- gpu-exclusive jobs;
- the CPU runner.

Not used for L1 tests or mini-swe fixes, on purpose. The context clamps (12K / 6K) are far below what these jobs use (runner num_ctx
65536, observed prompt peak about 24.7K). qwen3.8:27b (17.7 GB) does not fit in 9.6 GB, so only 14B-class models fit, a quality drop
against the 35B. The 14B is weak even as a pre-gate: its "pass" was confirmed by the 27B regate in about 20 of 29 cases, its "fail" in only
2 of 11. mini-triage is deterministic, so there is nothing to offload there. A docsum summary on a 9B would be worse than on the 35B.

One change: `status` no longer flags "unraid IDLE ... POSSIBLE STUCK SEAM" for jobs that can only run on Darkbloom
(`wait_job_lanes(db_only)`). When a Darkbloom endpoint is configured, auto/studio jobs never overflow to Unraid, so an idle Unraid is
expected, not stuck.

### Queue events: the launching session hears about its jobs (74eaadb)

- `queue-event-log.py` (launchd com.example.queue-event-log) logs passed, done, failed, blocked, needs_info and gate_nonpass events to
  `~/.ollama-dispatch/queue-events.jsonl`. It reads done.json sidecars, because done rows are pruned on the next tick.
- Each event is stamped with the launching session. `queue-events-deliver.py` delivers it to that session: through a hook while the
  session is active, or through `--watch` (Monitor) to wake an idle one. `--watch` exits about 3 minutes after the session has nothing in
  flight.
- Events with no owner, or whose owner is gone, are shown once. Cursors survive /clear.
- 2caf419:
  - A job seen running again after it ended (requeued, resumed, or a needs_info answered) reports its next outcome as a new event.
    Before, it was dropped as a duplicate.
  - The delivery cursor is the higher of the session's and the process's, so after /clear the old watcher and the new session's
    hook do not deliver each other's lines again.
  - A hook call from inside a subagent delivers nothing and advances nothing, so events stay for the main session.
- Tests: `test-queue-events.py` (18), `test_queue_event_log.py` (4).

### BloomGauge control: the queue turns the Manager off while it has work (1346467, 45b598c)

BloomGauge lets an agent make 2 model switches or restarts and 6 setting changes (Manager on/off) per rolling hour; past that
it refuses with 429. The waits stay as they were:
- the Manager is handed back after 900 s of queue idle;
- the budget waits;
- a 2 h backoff after a manual Manager change.

What changed:
- **Failure backoff.** After a failed write, the queue waits before trying again: Manager changes 120 s, 300 s, then 900 s; picks
  300 s, 1800 s, then 3600 s; at least 900 s after a 429. The backoff is read from the control ledger, so it survives a daemon restart.
  A failed pick keeps the lease without the pick; any other failed write keeps the old lease.
- **One alert per streak.** 3 failed writes of one kind in a row raise one ESCALATIONS.md row and one desktop notification. The janitor
  closes the row when a write of that kind succeeds or control is switched off. A Darkbloom drift row closes when the queue sees the
  drift end.
- **Manager back on during queue work (b6f537d).** BloomGauge can turn the Manager back on by itself while the queue holds the lease,
  for example 'recovery' when a timed Manual run ends. The queue used to leave it on until the hand-back; now it turns it off again
  with the same waits. It acts only after the Manager has read on for 60 s (2 or more ticks). A manual change can show in BloomGauge's
  view before its activity-log row lands; within those 60 s the row lands and the queue yields instead. It never acts while the log is
  unreadable, and always leaves one Manager change in the hour for the hand-back.
- **Unreadable activity log (9e89c7d).** When BloomGauge answers but its Manager activity log cannot be read, the queue cannot see a
  manual change. So it makes no Manager change, no pick and no lease until the log reads again; that covers the acquire, the
  hand-back and the ownerless repair. It reads 500 log rows (about a day; 100 rows spanned about 25 h), so a manual change stays in
  view well past the 2 h backoff. An unreadable log for 30+ minutes raises one alert.
- **Owner first (Fable review #2, #8; b740999, live since the 16:40 daemon restart).**
  - With no lease, the queue turns an off Manager back on only when it left it off itself. That means its newest successful
    Manager change was a Manager-off and no manual change came after it, for example when the lease file was lost after an
    acquire. It never undoes an owner's off outside a work cycle, and a repair never repeats.
  - Every manual change the tick sees is also kept in the control ledger (a `phase: manual` row), so it still counts after it
    scrolls out of the activity log.
  - A manual change while the queue holds the Manager off no longer drops the lease. The lease is kept (`yielded_at`): no change
    for the 2 h backoff, then the usual hand-back. Before, a late activity-log row for the queue's own Manager-off read as manual,
    and the Manager was left off with nobody to turn it back on. The stale-lease guard waits out the same 2 h.
- **Hands off for models.**
  - Queue, worker and alert texts no longer tell a model to run `darkbloom start`, `sudo purge` or BloomGauge writes. They say what
    the queue is waiting for and "needs the owner" when it persists.
  - The retired `bloom_control.py`, `darkbloom-widen-now.sh`, `darkbloom-apply-wide-models.sh` and `bloom-integration-livetest.sh`
    refuse to change anything (rc 2) unless `DARKBLOOM_BY_OWNER=1` is set.
  - CLAUDE.md has a "Darkbloom / BloomGauge: hands off" section.

Any Claude model launches work the same way: `mini-submit` puts the job in the queue, and the queue's control path takes the lease,
turns the Manager off, waits on the budget and hands back. Nothing in that path needs a model to touch Darkbloom or BloomGauge.
Tests (pytest): `test_bgc_hardening.py` (18), `test_bgc_logok.py` (25), `test_bgc_owner_first.py` (13), `test_bgc_queue_alerts.py` (12),
`test_bgc_reassert.py` (11), `test_bgc_tick_ownerless.py` (6), `test_bloomgauge_hook.py` (6), the five `test_bgc_policy_*.py` (21),
`test_load_client.py` (3), plus `ollama-queue.py --self-test`. All pass (115).

### Containment of local-model code (f768ecb, aa12799, e797ba9)

- **Drafted tests (L1) run fully contained.** That covers the mutants, the ref replay, the runner's test gate and every
  mini-testdraft check (V3/V4/V5/V7). Each runs with a throwaway HOME, DISPATCH_VERIFY_SANDBOX=1 and the sandbox-exec profile;
  only the job's work dir is writable. Replaying the 11 L1 drafts this way gave the same verdicts.
- **Every mini run carries the queue guard.** That covers the agent's shell, every test command and mini-validate. With
  DISPATCH_VERIFY_SANDBOX=1, code under test can read the queue (`status`, `results`, `needs-opus`), but `ollama-queue.py`
  refuses every subcommand that would change it (exit 3). A spec's env cannot clear the marker. The old pipeline's pgrun did
  this; the mini runners had lost it. `test-verify-sandbox-guard.py` checks it on the live runners, and it fails if the change is
  reverted.
- **Caveat.** A fix job's agent shell and its gates still run with the real HOME and without sandbox-exec. They cannot touch the
  queue, but they are not fully contained. Some projects' tests pass only with the real HOME: 2 of the 147 calibration specs were
  unscored for that reason.

### Queue cleanup and daemon restart (2026-11-02 16:40)

- 11 stalled failed bundles retired with `qctl retire` (undo: `qctl supersede BUNDLE --undo`). bundle-history now shows 0 stalled.
- 219 failed or cancelled run cards cleared: each was triaged, resolved, then cleared through the dashboard endpoint, never with
  `override`. Their sidecars went to `ollama-queue-logs/archive/` and are listed in `clear-manifest.json`, so a clear can be
  undone. The livelogs are kept.
- **Left for the owner:**
  - 24 failed rows from Oct 5-14 that need a required sign-off (psab/psab2, arr-webhook, aw-codec-floor-s3, the smoke-egift and
    rt auto-authors, rt-egift-link-s1-s0-db-schema). Clearing them needs the owner's sign-off or an override.
  - 1 needs-eyes row: a8efd61a7e1e, handoff-followup-4e6d7891ecaa, cancelled.
- The queue daemon was restarted in a lull (`launchctl kickstart -k`, pid 25225) so b740999 is live: `bloomgauge_tick` is
  imported once per process. It was restarted with an empty queue, nothing in flight, no lease, and the last control action (an
  ok hand-back) finished. Mini-runner, mini-validate and mini-testdraft changes need no restart, because each job runs them as a
  new process.

## 2026-10-31 (history)

Every piece below was a new tool, an opt-in env var or an opt-in spec key.

| Piece | Enable (one line) | Kill switch | Tests | Result |
|---|---|---|---|---|
| Mutation gate, real tools (mutmut 3.8.1, StrykerJS 10.1.1 in `~/.mutation-tools`, symlinks in `~/.local/bin`) | add `"mutation_gate": {"warn_below": 0.6, "mutmut_mode": "indir", "mutmut_pythonpath": ["lib"]}` to a spec | delete the key; `rm ~/.local/bin/{mutmut,stryker}` | `mini-swe-runner.py --self-test` (new indir case) | py build(): 75/81 killed = 0.93; js add(): 10/11 = 0.91 |
| Scorecard / ledger / sampled-review log / throttle (report-only) | `mini-scorecard report`; log landings with `mini-scorecard ledger add --job ID --commit SHA --repo PATH --family F`; log reviews with `mini-scorecard review add --job ID --verdict ok\|reject` | `MINI_SCORECARD=off` (throttle and sample); nothing reads its state (until 11-03: sampling now reads it, see Status 2026-11-03) | `python3 test-mini-scorecard.py` | `scorecard_dryrun.txt` below |
| 20% pass sampling | `mini-scorecard sample list` (rate `MINI_SAMPLE_PCT`, default 20, 100 for throttled families; since 11-03 also 100 while the global review-all switch is on) | same | same | 37 of 143 passes in 14d picked |
| Fail-fast cap dry run (lever 4a) | `mini-scorecard caps` | n/a | same | cap 2x PASS p90 would have saved 9.0h of 23.8h fail GPU, 3 of ~147 passes over cap |
| Intake refusal log (lever 4c) | `MINI_LOG_REFUSALS=1` in the shell running mini-spec | unset it (default OFF) | `python3 test-mini-refusal-log.py`, `mini-spec --self-test` | rows go to `~/.ollama-dispatch/intake-refusals.jsonl` |
| Test drafting check (lever 1) | `mini-testdraft prompt SPEC` then `mini-testdraft check SPEC --draft FILE [--mutmut-pythonpath lib]` | `MINI_TESTDRAFT=off`; refuses money/auth/migration/gate paths without `--shadow` | `python3 test-mini-testdraft.py` | Claude's test: ACCEPT (mutation 0.93). Weak "returns a dict" draft: V1..V7 all PASS but mutation 0.42, REJECT |
| vault-log-append (lever 6) | `vault-log-append "text"` (rolls to -part2/-part3 on 413) | `VAULT_LOG_APPEND=off` | `python3 test-vault-log-append.py` | fake-shim test |

Also: `.gitignore` was dropping the extensionless `mini-validate`, `mini-submit` and `mini-triage` (and now mini-scorecard etc.), so those gate
files were never versioned. They are now whitelisted and committed on this branch.

### Finding that changes lever 1
V5 (every ref hunk caught) is blind when the reference replaces a stub with a whole function: that is ONE hunk, so any test that fails on the
stub passes V5. A weak draft passed V1..V7. The mutation score on the changed functions is the check that separates good from weak, so
`mini-testdraft check` requires it (default >= 0.70, strict if the tool cannot score). Use the same score as a Claude-test quality metric.

### Round 2 (the owner's answers, 2026-10-31)
Added rows (all default OFF, kill switch each):

| Piece | Enable (one line) | Kill switch | Tests | Result |
|---|---|---|---|---|
| Lever 1 live path: `test_draft` job (runner `phase_a_only`, `mini-testdraft emit`, mini-submit C1/C3/C4 draft branch) | `mini-testdraft emit SPEC --out DIR [--mutmut-pythonpath lib]` then `MINI_TESTDRAFT_MODE=on mini-submit DIR/spec.json --label <area>-test-draft --model M --bundle B` | unset `MINI_TESTDRAFT_MODE` (default off) or `MINI_TESTDRAFT=off` | `python3 test-mini-testdraft-mode.py`, `test-mini-testdraft.py`, runner `--self-test` | dry-run submit C0-C8 ok on 4 bloom-ledger specs. REAL-QUEUE TRIAL BLOCKED: see below |
| Levers 2+5 policy: `offload_routing.py` (route, quote_gate, diagnosis_gate, GPU estimate) | `OFFLOAD_LOCAL_RESEARCH=1` / `_REVIEW=1` / `_DIAGNOSE=1`, plus `OFFLOAD_ROUTING_DRYRUN=0` to apply (default 1 = log only to `$OFFLOAD_HOME/routing-dryrun.jsonl`) | `OFFLOAD_ROUTING=off` | `python3 test-offload-routing.py` | decisions are logged only; nothing consumes them yet |
| Fail-fast cap enforcement (4a) | `MINI_FAILFAST_CAP=1` (+ `mini-scorecard caps --write`) | unset (default off) | `python3 test-mini-failfast-cap.py` (no-op when off; only cuts past 2x pass p90 when on) | caps table dry run above |

How test_draft works: the draft job is a normal two-phase spec whose baseline has NO test file. The runner runs Phase A only (qwen writes the test from a brief
that contains the ref diff), then runs `mini-testdraft check` on it (V1-V7 + mutation >= 0.70 + the must-not-offload refusal). `RESULT PASS mode=test-draft` = ACCEPT.
The runner never lands anything: the draft is kept at `<run dir>/draft_<test>`; Claude reads it and copies it into the baseline. mini-submit refuses a draft job
when `draft_accept_cmd` is not a `mini-testdraft check` command, when it carries `--allow-unscored`, or when paths match money/auth/migration/gate.

BLOCKER (RESOLVED by 11-02: the branch is in ~/bin and the trial ran through the real queue; kept as history): the queue only launches `/Users/user/bin/mini-swe-runner.py` (ALLOWED_RUNNERS). The `phase_a_only` runner change lives
on the branch, so `mini-submit` is refused at enqueue ("runner ... is not allowlisted"). The real-queue trial of 3-5 specs needs the branch merged to ~/bin (your call;
I was told not to merge). Staged for it: 4 emitted jobs in the scratchpad `td/` dir (power-rate-lookup, report-day-power, power-interval-cost, report-day-rows;
bundle `bloom-ledger-test-draft-trial`, labels `bloom-ledger-<area>-test-draft`). After merge: re-run the mini-submit line per job (C0-C8 already pass in dry-run).
So no real accept/reject numbers exist yet; the only measured pair is the checker proof (good ACCEPT 0.93 mutation, weak REJECT 0.42).

Lever 2 as built (policy only): `quote_gate` rejects any research/review answer whose backticked or double-quoted span (>=12 chars) is not verbatim in a source, or that has no
quote; `diagnosis_gate` accepts a local diagnosis only if its repro command FAILS now and shows the claimed symptom. Not yet wired into diagnose-submit/review dispatch:
that wiring touches the queue API / dispatch scripts another session owns, so it is the next step after you pick it up. Lane = darkbloom.
Lever 5 as built: `route()` returns the model by job need while the owner is working, and `model=None, decided_by=bloomgauge-manager` when idle (never queries or sets the Manager).
Full replay GPU estimate (NOT run): `python3 offload_routing.py replay --estimate --models M1,M2` -> 115 specs at the queue's historic median PASS time and failure rate is about
4.8 GPU-h for qwen3.6-35b-a3b and about 7.3 GPU-h for qwen3.8-27b (only 4 history samples), ~12 GPU-h for both. Lever 3 belongs to another agent.

### Open questions left
1. DONE (11-02): the branch is in ~/bin; the test_draft trial ran through the real queue (results in the 11-02 section).
2. Wiring of the lever-2 gates into diagnose/review dispatch (files owned by another session).

### Rules to adopt (docs only; memory is not edited by this work)
- Direct-edit cutoff: Claude edits directly below 20 changed lines; dispatch above (approved).
- Sonnet by default for agents; Opus only to verify.
- FIX MEMORY: `feedback_mini_swe_not_auto_dispatch.md` says "qwen writes the test, then the fix". v2 reality: Claude writes TASK + frozen test + a validated
  ref fix; qwen writes only the fix. Update that note (and hub_dispatch.md if it repeats the claim).

### Pre-existing, not touched
- `test_mini_submit.py` ends with FileNotFoundError on SUBMIT-OK.json also against live ~/bin (not caused by this branch).
- Canary is red (4 seams) and does not cover mini-swe (plan 1.6).
