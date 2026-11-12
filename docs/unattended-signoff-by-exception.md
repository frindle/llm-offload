# Proposal: unattended acceptance ("sign-off by exception") -- DESIGNED, NOT ENABLED

Author: gate-audit pass, 2026-10-06. Status: proposal for the owner. Nothing here is live.
The code that would compute it already exists and is shadow-only:
`gate-on-complete.mechanical_evidence_gaps(payload)` (empty list = rule satisfied)
and `gate_identity.identity_check(payload, cwd)` (verdict still describes the code).

## Why

Measured by `python3 ~/bin/gate-audit.py` over 1,687 gate records:
- 290 code PASS verdicts, only 110 with no mechanical weakness. 216 were marked
  ready-to-apply, 72 of them with relevance NOT proven; 21 PASSes had no both-ways
  proof at all (6 since 09-18).
- 56 of 121 non-pass code verdicts were false BLOCKs (the content later landed
  as-is). The owner's time goes to reading those, not to the risky ones.
- No verdict before 2026-10-06 named the code it judged.

Today, a human (the owner or the coordinator) signs off every landing. The rule below
allows UNATTENDED acceptance only when every claim a reviewer could make is
already machine-checked, and sends everything else to a human.

## The rule: accept unattended iff ALL of these hold

| # | Criterion | Machine check (field on `<id>.gate.json`) |
|---|---|---|
| 1 | Verdict is clean and terminal | `verdict == "pass"`, `gate_authority == "studio-27b-regate"` (authoritative tier, not pregate-terminal-*), no `category=="code"` issue of any severity |
| 2 | Both-ways proof | `verify_failed_at_baseline is True` and `verify_exit_reported == 0`, `job_exit_code == 0` |
| 3 | Relevance proven | `verify_relevance.verdict == "relevant"`, `survived == 0`, `killed >= 3`, `evidence_mutants >= 3`, `truncated` false, `score >= threshold` |
| 4 | Full site coverage | `verify_relevance.unexercised_sites == []` and `untried_sites == []` |
| 5 | No rejected opt-outs | `verify_relevance.optout_rejected` absent/empty (carried since 2026-10-06) |
| 6 | Behavioural fixture | `verify_relevance.source_text_harness.verdict` not in {source-text, mixed} |
| 7 | Repo CI green | the repo's own CI command (per-repo, from `~/.ollama-dispatch/repo-ci.json`, NEW) exits 0 on the worktree; absent config = criterion FAILS |
| 8 | No literal / identifier drift | every TASK.md `Must contain` literal present exactly (`check_literals.py` exit 0); no near-miss identifier of a spec token in added lines; reviewer `stats.identifier_drift_rejected` recorded (not a blocker by itself) |
| 9 | Reviewer agrees | `review_verdict == "PASS"` exactly (not "PASS WITH CAVEATS"); `second_opinion_disagreement` not true; `finding_check` not "confirmed" |
| 10 | Verdict identity current | `judged` stamp present with >= 1 product file and `identity_check(payload, cwd)` ok at landing time |
| 11 | Clean baseline | `untrusted` absent; `launch_baseline.dirty == 0` |
| 12 | Scope clean | no `input` issues; every product file in `judged.files` named by TASK.md scope |
| 13 | Risk tier | `_auto_confirm_risk` == "low" (no money/P&L, DB-write/migration, destructive, auth/secret surface) |

Criteria 1-6 and 9-11 are exactly `mechanical_evidence_gaps() == []`. 7, 8, 12, 13
are additions.

## What happens

- All 13 hold -> land unattended (auto-apply for AUTO_DEPLOY_REPOS; slicer commit
  for chains), record `signoff: "auto-by-exception"` plus the evidence snapshot.
- Any fails -> today's path (human signoff), with the failing criteria named on
  the row, so the reader starts from the gaps instead of re-deriving them.

## Failure modes, and what each one would cost

1. **A relevant verify that pins the wrong property.** Mutation testing proves the
   fixture discriminates the ADDED lines. It does not prove the cases encode the
   intended behaviour (the rt-pl-exclude Prisma-shape case). Mitigation: criterion
   13 keeps money/DB/destructive work human. Residual: a wrong-but-discriminating
   fixture on low-risk code lands. Cost: a visible bug in low-risk code.
2. **Equivalent-mutant blindness / small samples.** `killed >= 3` on a large diff
   is thin. Proposal: require `evidence_mutants >= min(3, sites)` and
   `site_coverage == 1.0`. Changes with fewer than 3 mutable sites fall to a human.
3. **Gate written, then the tree changes.** Covered by criterion 10. It fails
   closed on a missing stamp. Before 2026-10-06 nothing caught this. Residual:
   non-product files (TASK.md, verify.sh) are not hashed. A post-gate verify edit
   is caught by the harness seal, not by this rule.
4. **Reviewer PASS on a cherry-picked review.** A pregate PASS is non-terminal, so
   criterion 1 requires the authoritative tier. The second opinion is additive
   only.
5. **CI absent or flaky.** Criterion 7 fails closed without config. A flaky CI
   makes the rule fail toward human review, never toward acceptance.
6. **Rule drift.** If any criterion's source field is renamed, the check reads it
   as absent and the rule fails closed. `gate-audit.py --self-test` plus a weekly
   `gate-audit.py --json` diff should alarm when the rate of acceptance-eligible
   PASSes moves more than 2x week-over-week.
7. **Dirty-baseline false blocks stay blocks.** 18 of the 56 false BLOCKs were
   untrusted baselines. This rule does not unblock them, because the baseline's
   dirty path names are not recorded today. Prerequisite (needs the owner, needs a
   queue-daemon restart): record the dirty path NAMES in
   `ollama-queue.py` launch_baseline. Then `untrusted` can be narrowed to "dirty
   path overlaps a judged file".

## Rollout (proposed)

1. Shadow: compute `acceptance_by_exception: {eligible, failing:[...]}` on every
   gate record. Takes one call to `mechanical_evidence_gaps` plus 7, 8, 12, 13.
   No behaviour change.
2. Two weeks: compare eligible rows against later outcomes using `gate-audit.py`
   (survival on main, coordinator rejections, reworks). Enable only if eligible
   rows show 0 false-PASS-coordinator-rejected and 0 slice-relanded.
3. Enable per repo (resell-tracker first, as the existing AUTO_DEPLOY_REPOS).
   Keep the kill switch env `GATE_ACCEPT_BY_EXCEPTION=off`.
