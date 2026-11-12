# Bundle triage 2026-10-06

Method: script walk (walk.py) of bundle-history (308 finished, 526 stalled), reaper content-coverage over 10 repos incl. NVMe worktree roots. LANDED-OK rows with "indirect" evidence mean no unlanded worktree exists and the gate passed; not a per-line proof.

## Counts
- finished / LANDED-OK: 126
- finished / NEEDS-REVIEW or confirmed NOT-INTEGRATED: 54 (5 confirmed: fix-order-mismatch-flag, bfmr-order-unlinked-flag, resell-gc-ingest, costco-login-confirm-fix, rt-order-buyerid-patchable)
- finished / REAL-UNFINISHED: 1
- finished / TEST-OR-PROBE: 127
- stalled / REAL-UNFINISHED: 17
- stalled / STALLED-UNVERIFIED: 397
- stalled / TEST-OR-PROBE: 112

| bundle | state | repo | verdict | evidence | action |
|---|---|---|---|---|---|
| rt-giftcard-copy-remaining | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| strata-trial | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-rt-bfmr-push-via-sidecar | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| fcheck-live-smoke | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-chat-frontend-plan | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| chat-frontend | finished | dashboard-chat | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'error': 1, 'live:run-status': 9} | none |
| gemma-reviewer-bakeoff2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-coldload-test | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-replay-endorse | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-replay2-bfmr-replace-tracking | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-idle-slice-text | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-replace-tracking | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'live:run-status+slice': 1} | none |
| rt-941-fixes | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| sidecar-bfmr-login-nudge | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| diag:ladder-ok-le1-v2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| diag:ladder-ok-le1 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| crbench-q36-r2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-authlist:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-refactor:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-refactor:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-relink:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-relink:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-awsflag:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-awscp:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-guard:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-window:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-window:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-colon:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-printf:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-umask:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-scauth:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-min2:qwen3.6-35b-a3b-vl-mtp-mxfp8 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-min2:q36 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-e2e3-nohosts | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-e2e2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-smoke2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bg-eraser-config | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| sidecar-bfmr-login-fetch | finished | resell-tracker | REAL-UNFINISHED | stopped 5/5 last 09-27 | inspect/relaunch (see report) |
| bg-eraser-cmd | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| health | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a | none |
| verify-relevance-js-optout | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bg-automate-optout-form-submission | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bg-retire-searxng-for-playwright | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| verify-relevance | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| web-backfill-date-cutoff | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.68) files=['app/api/bfmr/sync-reservations/route.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| route-fix | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.42) files=['app/api/bfmr/sync-reservations/route.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| route | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.42) files=['app/api/bfmr/sync-reservations/route.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-stale-link-wiring | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| bfmr-stale-link-migration | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| arr-sonarr-new-request-priority | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| aw-scheduled-searches-store | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| arr-deluge-false-supersede-915 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-dashboard-pl-unsubmitted-cc | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-emailsync-imap-configurable | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-bfmr-sync-scope-wiring-v2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-bfmr-sync-scope-wiring | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.15) files=['app/api/bfmr/sync-reservations/route.ts', 'components/BfmrReservationLinker.tsx', 'lib/autoSync.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| rt-walmart-delivered-signal-wiring-v2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-bfmr-pending-sync-scope | finished | resell-tracker | LANDED-OK | reaper: integrated: N changed file(s) ; wt={'live:run-status': 1} | none |
| rt-walmart-store-tracking-gate | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| plan-gen-arr-webhook-yearly-upgrade-batches-r1 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| aw-alert-filters-contract | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-ccwaitlist-cases | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-recalc-xorder | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| webui | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-bfmr-crossorder-guard | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-bfmr-tracking-endorsement | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| bg-dashboard | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.23) files=['broker_guard/webui.py', 'requirements.txt'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| aw-alert-filters | finished | award-search | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'live:slice': 5} | none |
| scheduled_searches | finished | award-search | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.11) files=['src/scheduled_searches.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| aw-scheduled-searches | finished | award-search | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['src/scheduled_searches.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| aw-airport-groups | finished | award-search | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.06) files=['src/airport_groups.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| aw-app-wiring-v2 | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-costco-always-sites | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a,integrated: N changed file(s) ,scaffold-only: only scaffold/a,clean: no changes vs merge-bas,scaffold-only: only scaffold/a,integrated: N changed file(s) ,scaffold-only: only scaffold/a | none |
| aw-fix-united-import | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a,clean: no changes vs merge-bas,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a | none |
| playwright_checks | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.67) files=['broker_guard/playwright_checks.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-serpwatch | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.6) files=['broker_guard/serpwatch.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| scheduler | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.86) files=['broker_guard/scheduler.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| aw-fix-delta-import | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a,clean: no changes vs merge-bas,scaffold-only: only scaffold/a,scaffold-only: only scaffold/a | none |
| serpwatch | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| bfmrWeb | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| bg-orchestrator-run-cycle | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.61) files=['broker_guard/health.py', 'broker_guard/orchestrator.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| rt-bfmr-409-logging-p1 | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| bg-health-update-run-status | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bg-alert | finished | broker-guard | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 3} | none |
| bg-health | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.48) files=['broker_guard/health.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-retry-window-cutover-v2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bg-eraser | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['broker_guard/eraser.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bonsai-ternary-bakeoff-parked | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-retry-window-cutover-bonsai-r2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bonsai-ternary-bakeoff-unraid | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-split-wiring | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| bg-state | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.25) files=['broker_guard/state.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-actions | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.88) files=['broker_guard/actions.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-crypto | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['broker_guard/crypto.py', 'requirements.txt'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-split-reservation-fix | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bonsai-smoke-routing-test-2 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bg-escalation | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.31) files=['broker_guard/escalation.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-brokers | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.26) files=['broker_guard/brokers.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-detection | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.89) files=['broker_guard/detection.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-brokers-dedupe-on-path | finished | broker-guard | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| cc-waitlist | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bfmr-split-reservation-diagnose | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['ANSWER.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-captcha | finished | broker-guard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.87) files=['broker_guard/captcha.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-profile | finished | broker-guard | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 4} | none |
| rt-walmart555-tracking-fix | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.33) files=['sidecar/src/walmart.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bg-selfheal | finished | broker-guard | LANDED-OK | reaper: integrated: N changed file(s) ; wt={'unintegrated/B': 2} | none |
| bg-interpret | finished | broker-guard | LANDED-OK | reaper: clean: no changes vs merge-bas; wt={'unintegrated/B': 2} | none |
| bonsai-ternary-arm | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-sync-reservations-500-guard | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.88) files=['app/api/bfmr/sync-reservations/route.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| sync-status-drag-resize-r3 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bg-formfill | finished | broker-guard | LANDED-OK | reaper: clean: no changes vs merge-bas; wt={'unintegrated/B': 1} | none |
| rt-pl-exclude-unsubmitted-cc | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| arr-new-grab-queue-top | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a,scaffold-only: only scaffold/a | none |
| pricing-discount-penny | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| clamp-relevance-trap-iso | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| clamp-adequate | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| text-truncate-words | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| slugify-util | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| travel-esim-research-arm-command-r | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| travel-esim-research-arm-qwen36-35b-a3b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| travel-esim-research | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-cc-sync-diagnosis | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| rt-churning-research | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-amazon-card-lastfours | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-price-apis-research | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| diag-cloudflare-en0 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| diag-tm-unraid | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| goose-latency-research | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-order919-address-resync | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| rt-cancelled-no-reservation-chip | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| rt-order-buyerid-patchable | finished | resell-tracker | NOT-INTEGRATED (confirmed, deliverable read) | worktree deliverable not on origin/main (cov_min=0.0) files=['app/api/orders/[id]/route.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| vm-status-cells | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| rt-group-address-match | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| rt-reservation-remaining | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| rt-address-normalize | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| rt-autobuy-decision | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| rt-amazon-buybox | finished |  | LANDED-OK | reaper: integrated: N changed file(s)  | none |
| sync-status-overflow | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['components/SyncStatusIndicator.tsx'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| vm-pdf-export | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bfmr-sync-orders-paginate | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.02) files=['.gitignore', 'app/api/bfmr/sync-orders/route.ts', 'verify.sync-orders.test.mts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| rt-saved-address-schema | finished |  | LANDED-OK | reaper: scaffold-only: only scaffold/a | none |
| costco-login-confirm-fix | finished | resell-tracker | NOT-INTEGRATED (confirmed, deliverable read) | worktree deliverable not on origin/main (cov_min=0.09) files=['sidecar/src/costco.js', 'sidecar/src/loginFlow.js', 'sidecar/test.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| vm-phone-and-edit | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| sidecar-pollnow-kick | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.11) files=['app/api/extension/commands/route.ts', 'sidecar/src/poll.js', 'sidecar/src/sidecarUrl.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-sync-perf | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.19) files=['sidecar/src/cashbackmonitor.js', 'sidecar/test.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| amazon-iris-frame-race | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.2) files=['sidecar/src/amazon.js', 'sidecar/test.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| logtable-index | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bo-S-manual-qwen36-27b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-gemma31 | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-gemma26moe | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-A-q4-studio | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-B-q8-studio | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-deepswe | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-gptoss-20b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-gemma4-26b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-gemma4-31b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-D-devstral-manual | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-G-qwen3coder-manual | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-F-laguna-manual | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plex-orphan-seedcheck | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| plex-upgrade-async | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| plex-pathmatch | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| plex-getpost | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| plex-hnr-seedgate | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| worker-capture-final-as-unbound | finished | machine-config | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.86) files=['bin/ollama-worker.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| worker-write-thrash-abort | finished | machine-config | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| resume-grants-budget | finished | machine-config | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| plex-hnr-guard | finished | plex-automation | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.86) files=['arr-webhook.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| nvenergy-influx-import | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| resell-gc-ingest | finished |  | NOT-INTEGRATED (confirmed, deliverable read) | worktree deliverable not on origin/main (cov_min=None) files=[] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| set-caller | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| worker | finished | machine-config | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.86) files=['bin/ollama-worker.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| nvenergy-influx | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-onecontainer | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-influx-dualwrite | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-whisper-compose | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-whisper-dockerfile | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-whisper-worker | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-auth-open | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-compose | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-dockerfile | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-healthz | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-tesla-rest-capture-all | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-tesla-capture-all | finished | ev-dashboard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.03) files=['app/api/metrics/influx/route.ts', 'server/telemetry-influx.js', 'server/telemetry-server.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| ev-metrics-influx-lp | finished | ev-dashboard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.1) files=['sample.json', 'server/telemetry-influx.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| ev-influx-write-request | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-lineprotocol | finished | ev-dashboard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.44) files=['server/telemetry-influx.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-overcount | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-telemetry-influx | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-metrics-raw-endpoint | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-fillclient-v2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| v1dispatch | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| netmon-telegraf-metrics | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-fillclient | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-export | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| vm-extract | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| voicemail-dnc-v2 | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| main | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| servers-config | finished | ollama-queue-dashboard | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| voicemail-inbox | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| ev-circuit-charging-truth | finished | ev-dashboard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.75) files=['lib/circuitStatus.ts', 'tsconfig.tsbuildinfo'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| ev-circuit-status-false-charging | finished | ev-dashboard | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| amazon-iris-payment-fix | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| amazon-iris-frame-diagnosis | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| amazon-card-nomatch-diagnosis | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-cap-link-to-reservation | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| bfmr-split-link-overalloc | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bfmr-phantom-link-diagnosis | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| asian-tv-router | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| diagnose-tracking-flag | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.22) files=['DIAGNOSIS.md', 'app/orders/page.tsx'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| mobile-badge-align | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bfmr-paid-rollup-overcount | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.38) files=['app/api/bfmr/sync-orders/route.ts', 'lib/bfmr.ts', 'lib/bfmrPaidRollup.test.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-shipped-flip-impl | finished | resell-tracker | LANDED-OK | reaper: clean: no changes vs merge-bas; wt={'unintegrated/B': 1} | none |
| tracking-not-uploaded-flag | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.83) files=['app/api/orders/route.ts', 'app/orders/page.tsx'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-sync-webbackfill-perf | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.85) files=['app/api/bfmr/sync-reservations/route.ts', 'prisma/schema.prisma'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| order900-partial-lock | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| sidecar-session-numpad-regression | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| cr:ok-getparse:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-unbounded:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-tzmismatch:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-guardmoved:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-keptfetch:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-deadfetch:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-authlist:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-authlist:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-refactor:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-link-overcount | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| cr:ok-relink:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-relink:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-awsflag:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-awscp:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-guard:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-window:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-window:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-colon:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-chmod:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-printf:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-le1:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-umask:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-scauth:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-min2:qwen3:32b | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-getparse:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-unbounded:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-tzmismatch:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-guardmoved:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-keptfetch:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-deadfetch:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-authlist:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-authlist:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-refactor:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-refactor:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-relink:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-relink:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-awsflag:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-awscp:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-guard:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-window:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-window:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-colon:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-chmod:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-printf:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-le1:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-umask:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-scauth:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-min2:davidau-qwen38-mtp:q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-getparse:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-unbounded:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-tzmismatch:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-guardmoved:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-keptfetch:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-deadfetch:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-authlist:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-authlist:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-refactor:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-refactor:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-relink:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-relink:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-awsflag:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-awscp:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-guard:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-window:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-window:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-colon:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-chmod:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-printf:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-le1:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-umask:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-scauth:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:bug-min2:qwen3.8:27b-q4_K_M | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-link-guard | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.56) files=['lib/bfmrAutoLink.ts', 'lib/bfmrLinkGuard.test.ts', 'lib/bfmrLinkGuard.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-diagnosis-verify-emit | finished | machine-config | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| diag-bfmr-overalloc-guard | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-instant-sync-kick | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| diag-instant-sync | finished | resell-tracker | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| wire-reconcile-landed | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| fix-reconcile-landed-helper | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| fix-bfmr-autosync-scope | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| add-queue-failure-handler | finished | machine-config | NEEDS-REVIEW (worktree coverage <90%, possibly superseded retry; not hand-verified) | worktree deliverable not on origin/main (cov_min=0.17) files=['bin/ollama-queue.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-bfmr-submit-hang | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| fix-bfmr-reservation-overcount | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| bfmr-order-unlinked-flag | finished | resell-tracker | NOT-INTEGRATED (confirmed, deliverable read) | worktree deliverable not on origin/main (cov_min=0.04) files=['app/orders/[id]/page.tsx', 'app/orders/page.tsx', 'lib/trackingUnlinked.test.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-order-mismatch-flag | finished | resell-tracker | NOT-INTEGRATED (confirmed, deliverable read) | worktree deliverable not on origin/main (cov_min=0.0) files=['app/orders/page.tsx', 'lib/payoutMismatch.test.ts', 'lib/payoutMismatch.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-trust-browser-cookie | finished |  | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect) | none |
| fix-sidecar-numpad | finished |  | LANDED-OK | reaper: clean: no changes vs merge-bas | none |
| diag-sidecar-vnc404 | finished | resell-tracker | LANDED-OK | gate-passed, no residual unlanded worktree, no reap record (indirect); wt={'unintegrated/B': 1} | none |
| build-ollama-diagnose | finished |  | LANDED-OK | reaper: missing-dir: worktree dir miss | none |
| merge-branch-merge-src-svc-mjs-07f3985e | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| merge-branch-merge-src-calc-mjs-07f3985e | finished |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| chat | stalled | dashboard-chat | REAL-UNFINISHED | stopped 3/4 last 10-04 | inspect/relaunch (see report) |
| plan-gen-chat-fixes | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| gemma-reviewer-bakeoff | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plan-gen-replay-endorse2 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| idle-pipeline-test | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plex-automation-test-fixes | stalled |  | REAL-UNFINISHED | stopped 2/3 last 10-02 | inspect/relaunch (see report) |
| bfmr-superseded-reservations | stalled | resell-tracker | REAL-UNFINISHED | stopped 2/3 last 10-02 | inspect/relaunch (see report) |
| bfmr-superseded-reservations-v2 | stalled |  | REAL-UNFINISHED | stopped 0/1 last 10-02 | inspect/relaunch (see report) |
| sidecar-bfmr-sink | stalled | resell-tracker | REAL-UNFINISHED | stopped 2/3 last 10-01 | inspect/relaunch (see report) |
| goosechat-probe | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-auth | stalled |  | REAL-UNFINISHED | stopped 2/3 last 10-01 | inspect/relaunch (see report) |
| cr:ok-chmod:qwen3.6-35b-a3b-vl-mtp-mxfp8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cr:ok-le1:qwen3.6-35b-a3b-vl-mtp-mxfp8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-e2e | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-selling-research | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-smoke3 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| darkbloom-smoke | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| mlx-8bit-parsecsv | stalled | award-search | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| mlx-smoke | stalled | award-search,plex-automation | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bg-scan-error-reason | stalled | broker-guard | REAL-UNFINISHED | stopped 0/1 last 09-27 | inspect/relaunch (see report) |
| rt-walmart-delivered-signal-wiring | stalled | resell-tracker | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.3) files=['sidecar/src/walmart.js', 'sidecar/src/walmartDetailSignals.js', 'sidecar/src/walmartTracking.js'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| rt-bfmr-mytrackerid-preserve | stalled |  | REAL-UNFINISHED | stopped 0/1 last 09-23 | inspect/relaunch (see report) |
| rt-cc-waitlist-core | stalled |  | REAL-UNFINISHED | stopped 0/1 last 09-22 | inspect/relaunch (see report) |
| rt-autolink-xorder | stalled |  | REAL-UNFINISHED | stopped 0/1 last 09-22 | inspect/relaunch (see report) |
| bfmrLinkGuard | stalled | resell-tracker | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.56) files=['lib/bfmrAutoLink.ts', 'lib/bfmrLinkGuard.test.ts', 'lib/bfmrLinkGuard.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| scheduled_routes | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-21; no unlanded worktree found | review; clear stale rows if superseded |
| draft-wt-bfmr | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| cc-waitlist-r2 | stalled |  | STALLED-UNVERIFIED | 5/7 last 09-20; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-retry-window-cutover-v3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-20; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-retry-window-cutover-bonsai | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-19; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-retry-window-cutover | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-19; no unlanded worktree found | review; clear stale rows if superseded |
| draft-wt-bg-brokers-dedupe-on-path | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-split-reservation-diagnose-retry1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-19; no unlanded worktree found | review; clear stale rows if superseded |
| bonsai-ternary-bakeoff | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| sync-status-drag-resize-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-18; no unlanded worktree found | review; clear stale rows if superseded |
| sync-status-drag-resize | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-18; no unlanded worktree found | review; clear stale rows if superseded |
| draft-wt-bfmr-relink-v3 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-kotlin-clamp | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-java-clamp | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-rust-clamp | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-go-clamp | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-bash-dedupe | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-swift-clamp | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-relink-v2 | stalled | resell-tracker | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.35) files=['lib/bfmrAutoLink.ts'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| esim-global-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-17; no unlanded worktree found | review; clear stale rows if superseded |
| bfmrAutoLink | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-17; no unlanded worktree found | review; clear stale rows if superseded |
| build | stalled | travel-esim-rates | STALLED-UNVERIFIED | wt={'missing-dir': 3} | review; clear stale rows if superseded |
| draft-wt-checkout-rounding-fix | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-qwen25c-32b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-qwen35-9b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-qwen36-35moe | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-qwen25c-7b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-phi4-14b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-ornith-9b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-N-qwen25coder32b-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-llama31-8b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-qwen3-8b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-mergeB | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-mergeC | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-L-gptoss20-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-qwen35-9b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-seedcoder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-E-dense36-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-J-gemma31-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-qwen25c-14b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-glm47flash | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-H-glm-manual | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-K-gemma26moe-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-M-glm47flash-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-C-moe-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| tcpa-layer | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-13; no unlanded worktree found | review; clear stale rows if superseded |
| bo-U-manual-swe7b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-S-manual-mergeA | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-phi4-14b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-qwen25c-14b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U-manual-gemma4-12b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-H-glm-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-I-deepswe-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-D-devstral-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U1-seedcoder-unraid | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U1-seedcoder-unraid-c24k | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-U2-gemma4-unraid | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-F-laguna-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-G-qwen3coder-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-C-standalone | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-D-q4-unraid | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| voicemail-ui | stalled |  | STALLED-UNVERIFIED | 3/4 last 09-13; no unlanded worktree found | review; clear stale rows if superseded |
| draft-wt-ingest-screenshots | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| server | stalled | ollama-queue-dashboard | STALLED-UNVERIFIED | wt={'unintegrated/B': 1} | review; clear stale rows if superseded |
| draft-nvenergy-influx-import | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-wt-ev-tesla-rest-capture-all | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-wt-ev-influx-write-request | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-wt-ev-lineprotocol | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-wt-ev-metrics-raw-endpoint | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-platesubs-1789171887 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-stacksubs-1789171579 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-maxsubs-1789171385 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-moresubs-1789170664 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-subs-nocop-1789170138 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-img2img-subs-1789169242 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-img2img-1789168965 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-reroll-1789166704 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-v4-1789166018 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-v3-1789165840 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-v2-1789165623 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| dispatcher-1789165422 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bfmr-orphan-link-creator | stalled | resell-tracker | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| bfmr-shipped-flip | stalled | resell-tracker | STALLED-UNVERIFIED | wt={'unintegrated/B': 1} | review; clear stale rows if superseded |
| cr:bug-refactor:qwen3:32b | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-h2h-debug | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-h2h-cellE | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-orders-row-spacing | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-v10-bulk-bakeoff-v2 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-v10-bulk-bakeoff | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| draft-orders-row-spacing-studio | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| v10-bulk-bakeoff | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| orders-row-spacing | stalled | resell-tracker | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.0) files=['DIAGNOSIS.md'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| diag-907-reserved-count | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-08; no unlanded worktree found | review; clear stale rows if superseded |
| fix-bfmr-verify-false-502 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-08; no unlanded worktree found | review; clear stale rows if superseded |
| gate-relabel-concerns | stalled | machine-config | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.07) files=['bin/gate-on-complete.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| gate-front-of-queue | stalled | machine-config | REAL-UNFINISHED | worktree deliverable not on origin/main (cov_min=0.89) files=['bin/gate-on-complete.py', 'bin/ollama-queue.py'] | relaunch via ollama-dispatch-auto / needs the owner confirm |
| fix-reconcile-worktree-gap | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-08; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-shipped-from-record | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-08; no unlanded worktree found | review; clear stale rows if superseded |
| rt-bfmr-linekey-wiring | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-split-dedupe | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| draft-wt-bfmr-split-dedupe | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| plex-unpack-seedtime-gate | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-split-shipment-state | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| plex-hnr-removal-audit | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| bfmr-no-autosubmit | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| plex-hnr-diagnosis | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-07; no unlanded worktree found | review; clear stale rows if superseded |
| rt-bfmr-verify-seam | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-06; no unlanded worktree found | review; clear stale rows if superseded |
| rt-bfmr-verify-filter | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-06; no unlanded worktree found | review; clear stale rows if superseded |
| rt-staleness-gate-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-06; no unlanded worktree found | review; clear stale rows if superseded |
| rt-order-open-perf-check | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-06; no unlanded worktree found | review; clear stale rows if superseded |
| resell-bg-credited-per-shipment | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| tesla-caption-make-aware | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| tesla-footer-diagnosis | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-heartbeat-veto | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| draft-mjs-phase3 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| feed-error-debounce | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| energy-banner-charging-state | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-04; no unlanded worktree found | review; clear stale rows if superseded |
| tesla-charge-backfill | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| tesla-duration-round | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-parallax-datalayer | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| tesla-charge-freshness | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-parallax-first | stalled | ev-dashboard | STALLED-UNVERIFIED | wt={'unintegrated/B': 1} | review; clear stale rows if superseded |
| relevance-low-hardstop | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| queue-dirtycount-scaffold-exclude | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| gate-getdiff-scaffold-exclude | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| arr-refresh-on-grab | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| walmart-costco-since-test | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| resell-walmart-isloggedout | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| login-timeout-capture | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| amazon-login-diagnosis | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| resell-amazon-login-detect | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| resell-numpad-numlock | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| queue-hide-done | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| resell-sidecar-same-origin | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-netmon-r4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-netmon-r5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-netmon-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| vq-json | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-netmon-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-netmon-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-r5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-r4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-r5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-r4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-r5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-r4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-r5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-r4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-NATIVE-TEST | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-03; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-rep5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-rep4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-rep3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-discount-rep1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-rep5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-rep3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-rep2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-rivian-clip-date-rep1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-rep5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-rep4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-rep3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-rep2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-giftcard-pin-join-rep1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-rep5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-rep3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-rep2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-plex-year-dedup-rep1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| tokens-fresh | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diagnosis-live-ftps-leak | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| claude-vs-ollama-tokens | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| context-tracker-window-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| draft-d1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| diagnosis-live-nbm-sqliterow | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diagnosis-calibration-discount | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| claude-context-handoff-trigger | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| radarr-sourcetitle-fallback | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| sonarr-reverse-pack | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| sonarr-blank-did-sourcetitle | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| sonarr-year-token-strip | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| mc-acted-eval-handoff | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| sonarr-perep-keeper | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diplomat-seedtime-gate | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-eval-plex-matcher | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| diag-calib-qwen38 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| amex-effcost | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| scored-arm-smoketest2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| scored-arm-smoketest | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-02; no unlanded worktree found | review; clear stale rows if superseded |
| bo-signoff-stage2-qwen3.8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-automerge-qwen3.8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-panel-qwen3.8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-merged-qwen3.8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-signoff-stage1-qwen3.8 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-signoff-stage2-qwen3.6 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-automerge-qwen3.6 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-panel-qwen3.6 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-merged-qwen3.6 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-signoff-stage1-qwen3.6 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| prep-tokens | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| bo-signoff-stage2-qwen3-coder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-automerge-qwen3-coder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-panel-qwen3-coder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-signoff-stage1-qwen3-coder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-merged-qwen3-coder | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| pa2-devstral | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| pa2-qwen3.6 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| pa2-qwen3.8 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| pa2-laguna-xs-2.1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| pa2-qwen3-coder | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| bo-signoff-stage2-laguna-xs-2.1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo2-pa-supersede-qwen3-coder | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| bo-automerge-laguna-xs-2.1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo2-pa-supersede-laguna-xs-2.1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| bo-handoff-panel-laguna-xs-2.1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-signoff-stage1-laguna-xs-2.1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| bo-handoff-merged-laguna-xs-2.1 | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| rt2-qwen38q4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder-v5b | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder-v5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| handoff-skill | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| pa-supersede-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder-v4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder-v3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| automerge-ladder | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| canary-acceptpath | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| accept-path-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-handoff-left-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| handoff-merged-resolved | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-handoff-left | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-newjobs-v3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-newjobs-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| signoff-stage2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-cc-dueat-fallback-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-cc-dueat-fallback | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| signoff-stage1-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| signoff-stage1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| handoff-panel | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-vnc-public-ip | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-card-last4-ui | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| ws-api-dashboard | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| ws-worker-counters | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-updatesh-scope | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-bfmr-oneclick | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| websearch-usage-tracking | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-card-multi-last4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 09-01; no unlanded worktree found | review; clear stale rows if superseded |
| resell-cc-edit-inline | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| studio-reboot-launchdaemons | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-cardcenter-payout902 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-analytics-spacing | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-giftcard-pin | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-card-enh | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-marriott | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-bfmr-perf | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| evdash-logs-tz | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-newjobs-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-amex-offer | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-sidecar-fixes | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-one-container | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| dedupe-near-duplicates-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-310-hooks-v3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| dedupe-near-duplicates | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-310-hooks-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| evdash-logs-api | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-autoupdate | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-310-hooks-eslint | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| resell-bfmr-link-oneclick | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| parallax-diag-backoff | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-parallax-session-log | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-parallax-phantom-power | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-api-usage-quota | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-31; no unlanded worktree found | review; clear stale rows if superseded |
| test-authplugin-self-signed-probe | stalled |  | TEST-OR-PROBE | noise bundle (probe/test/eval) | none; stale rows may be resolved |
| ev-rivian-persistent-backoff | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| resell-hide-shipsby-when-shipped | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-restore-pollcadence-guard | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-flag-reads-watchdog-verdict | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react-firewalla-msp-hf-co-OpenResearcher-OpenResearcher-30B-A3B-GGUF-Q4_K_M | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-telemetry-watchdog | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-trust-telemetry-no-rest-fallback | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react-rtx3080-12gb-hf-co-OpenResearcher-OpenResearcher-30B-A3B-GGUF-Q4_K_M | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react-dflash-hf-co-OpenResearcher-OpenResearcher-30B-A3B-GGUF-Q4_K_M | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-framelog-and-asleep-flag | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react:firewalla-msp:qwen3.5:9b | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react:dflash:qwen3.5:9b | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| react:rtx3080-12gb:qwen3.5:9b | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rivian-km-to-mi-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-tesla-apifix | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-backfill-node | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| dash-search-api-usage | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ev-charge-backfill | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-relock-research | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-defer-collapse | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-relock-diagnose | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-connection-loop | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| resell-confirm-cleanups | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-lock-debounce | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| resell-syncwindow-test-fix | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| resell-syncwindow-floor | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| llama-swap-setup-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| llama-swap-setup | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_17_d7232a2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_16_b7c0c34 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_15_b73c025 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_14_27be443 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_13_1f49956 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_12_3ea5403 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_11_ca9b623 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_10_88417d8 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_09_272ac0e | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_08_6c16fb6 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_07_314d0d2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_06_e764d13 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_05_60dd9c2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_04_97646a4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_03_f6b09d2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_02_5b2fd7b | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-pass_01_7ca0c61 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_11_0f17536 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_10_c03d386 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_09_3f770da | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_08_892554a | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_07_0c6bd72 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_06_1457304 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_05_04994ea | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_04_7732a2d | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_03_5166d59 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_02_b5ee385 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen314b-flag_01_449b8e5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_17_d7232a2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_16_b7c0c34 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_15_b73c025 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_14_27be443 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_13_1f49956 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_12_3ea5403 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_11_ca9b623 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_10_88417d8 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_09_272ac0e | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_08_6c16fb6 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_07_314d0d2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_06_e764d13 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| ssh-unlock-step | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_05_60dd9c2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_04_97646a4 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_03_f6b09d2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_02_5b2fd7b | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-pass_01_7ca0c61 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_11_0f17536 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_10_c03d386 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_09_3f770da | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_08_892554a | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_07_0c6bd72 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_06_1457304 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_05_04994ea | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_04_7732a2d | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_03_5166d59 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_02_b5ee385 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rb-qwen359b-flag_01_449b8e5 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| prefilter-1788109578 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| prefilter-min2-proof | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rt-syncbox-drag-bfmr-3fixes | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-min2-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-min2-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-control-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-umask-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-30; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-scauth-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-umask-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-umask-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-umask-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-umask-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-scauth-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-scauth-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-scauth-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-scauth-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-min2-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-min2-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-min2-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-control-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-control-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-control-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-3-5-lightning-control-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-umask-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-umask-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-umask-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-umask-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-scauth-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-scauth-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-scauth-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-control-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-control-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-control-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-min2-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-min2-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-min2-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-scauth-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-cascade-coding-cleantest | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-scauth-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-scauth-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-umask-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-vncfallback-cascade-retry | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-scauth-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-umask-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-nemotron-cascade-2-umask-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-vncfallback-cascade | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-vncfallback-qwen | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-vncfallback-lightning | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-min2-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-min2-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-min2-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-min2-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-control-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-control-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-control-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv7-qwen3-coder-30b-ctx64k-control-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| v7-judge-scorer-build | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-status-mem-elapsed-v3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| recovery-key-burn | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-umask-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-umask-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-umask-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-umask-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-scauth-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-scauth-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-scauth-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-scauth-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-min2-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-min2-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-min2-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-min2-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-control-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-control-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-control-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv6-control-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-livelog-ux-v2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| slot-guards-and-pinned-origin | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| otp-enable-gate | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| pam-u2f-root-owned-authfile | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| scauth-pair-and-nfc-case | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-done-unconverged-studio | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-livelog-ux | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| vnc-fallback-on-connect-failure | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| fable-findings-guided-piv | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| capture-target-survives-display-sleep | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-done-unconverged-retry | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-done-unconverged | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| nfc-check-section | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-dnd-and-transitions-studio | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| display-sleep-and-collapse-bootstrap | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| passcli-invocation-fixes | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-dnd-and-transitions-retry2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-dnd-and-transitions-retry | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-dnd-and-transitions | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-running-down-arrow | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-promote-front-button | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-nochecklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-legend-sidebar | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-checklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-umask-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-nochecklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-checklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-scauth-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-nochecklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-checklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-min2-checklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-nochecklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-nochecklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-nochecklist-r1 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-checklist-r3 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| rv5-control-checklist-r2 | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-fix-vacuous-test | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| pam-u2f-hardening | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| clamshell-action-binding | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| touch-sudo-fixes | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-exit-code-legend | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| touch-sudo-section | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-livelog-viewer | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-paused-reorder | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-live-reorder | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-send-to-top | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| dashboard-drag-hint | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| combined-touch-sudo-immurok | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |
| RESTART-DIAG-TEST | stalled |  | STALLED-UNVERIFIED | 0/1 last 08-29; no unlanded worktree found | review; clear stale rows if superseded |

## NVMe worktree root: unlanded deliverables (dispatch-worktree-reap dry-run, 2026-10-06, nvmeroot added)

Reaper now scans /Volumes/NVMe-Models/dispatch-worktrees; dry-run only, nothing reaped. 36 kept-unintegrated (+3 live:run-status kept).

| repo | worktree | branch | files |
|---|---|---|---|
| award-search | home/mlx-8bit-parsecsv-6e7ee61c | dispatch/mlx-8bit-parsecsv-6e7ee61c | src/webui/scheduled_routes.py |
| award-search | home/mlx-smoke-mlx4-parsecsv-9d84d1e0 | dispatch/mlx-smoke-mlx4-parsecsv-9d84d1e0 | src/webui/scheduled_routes.py |
| award-search | ollama/wt-scheduled_searches | dispatch/scheduled_searches | src/scheduled_searches.py |
| broker-guard | ollama/wt-bg-brokers-dedupe-on-path | dispatch/bg-brokers-dedupe-on-path | broker_guard/brokers.py |
| broker-guard | ollama/wt-bg-orchestrator-run-cycle | dispatch/bg-orchestrator-run-cycle | broker_guard/health.py, broker_guard/orchestrator.py |
| machine-config | home/add-queue-failure-handler-e5b03100 | dispatch/add-queue-failure-handler-e5b03100 | bin/ollama-queue.py |
| machine-config | home/gate-front-of-queue-9e284552 | dispatch/gate-front-of-queue-9e284552 | bin/gate-on-complete.py, bin/ollama-queue.py |
| machine-config | home/gate-relabel-concerns-76a2782b | dispatch/gate-relabel-concerns-76a2782b | bin/gate-on-complete.py |
| machine-config | ollama/wt-fix-diagnosis-verify-emit | dispatch/fix-diagnosis-verify-emit | bin/ollama-dispatch-scaffold |
| machine-config | ollama/wt-resume-grants-budget | dispatch/resume-grants-budget | bin/ollama-queue.py |
| machine-config | ollama/wt-worker-capture-final-as-unbound | dispatch/worker-capture-final-as-unbound | bin/ollama-worker.py |
| machine-config | ollama/wt-worker-write-thrash-abort | dispatch/worker-write-thrash-abort | bin/ollama-worker.py |
| plex-automation | ollama/wt-plex-hnr-guard | dispatch/plex-hnr-guard | arr-webhook.py |
| resell-tracker | home/amazon-iris-frame-race-9435d6ec | dispatch/amazon-iris-frame-race-9435d6ec | sidecar/src/amazon.js, sidecar/test.js |
| resell-tracker | home/costco-login-confirm-fix-5b5a5451 | dispatch/costco-login-confirm-fix-5b5a5451 | sidecar/src/costco.js, sidecar/src/loginFlow.js, sidecar/test.js |
| resell-tracker | home/costco-login-confirm-fix-b7615773 | dispatch/costco-login-confirm-fix-b7615773 | sidecar/src/costco.js, sidecar/src/loginFlow.js, sidecar/test.js |
| resell-tracker | home/diag-instant-sync-0e6251bf | dispatch/diag-instant-sync-0e6251bf | DIAGNOSIS.md |
| resell-tracker | home/diag-sidecar-vnc404-b44f1f77 | dispatch/diag-sidecar-vnc404-b44f1f77 | README.md |
| resell-tracker | home/diagnose-tracking-flag-a8451185 | dispatch/diagnose-tracking-flag-a8451185 | DIAGNOSIS.md, app/orders/page.tsx |
| resell-tracker | home/fix-instant-sync-kick-2ebbe68d | dispatch/fix-instant-sync-kick-2ebbe68d | app/api/extension/commands/route.ts, app/orders/page.tsx, sidecar/src/poll.js |
| resell-tracker | home/fix-order-mismatch-flag-da5c5ece | dispatch/fix-order-mismatch-flag-da5c5ece | app/orders/page.tsx, lib/payoutMismatch.test.ts, lib/payoutMismatch.ts, package.json |
| resell-tracker | home/rt-cc-sync-diagnosis-b2aeb13d | dispatch/rt-cc-sync-diagnosis-b2aeb13d | DIAGNOSIS.md |
| resell-tracker | home/sidecar-pollnow-kick-f848fc64 | dispatch/sidecar-pollnow-kick-f848fc64 | app/api/extension/commands/route.ts, sidecar/src/poll.js, sidecar/src/sidecarUrl.js, sidecar/test.js |
| resell-tracker | home/tracking-not-uploaded-flag-d3b087e4 | dispatch/tracking-not-uploaded-flag-d3b087e4 | app/api/orders/route.ts, app/orders/page.tsx |
| resell-tracker | ollama/wt-amazon-card-nomatch-diagnosis | dispatch/amazon-card-nomatch-diagnosis | DIAGNOSIS.md |
| resell-tracker | ollama/wt-amazon-iris-frame-diagnosis | dispatch/amazon-iris-frame-diagnosis | DIAGNOSIS.md |
| resell-tracker | ollama/wt-amazon-iris-payment-fix | dispatch/amazon-iris-payment-fix | sidecar/src/amazon.js |
| resell-tracker | ollama/wt-orders-row-spacing | dispatch/orders-row-spacing | DIAGNOSIS.md |
| resell-tracker | ollama/wt-route-fix | dispatch/route-fix | app/api/bfmr/sync-reservations/route.ts |
| resell-tracker | ollama/wt-rt-bg-cc-exclude | dispatch/rt-bg-cc-exclude | lib/bgReceiptAttribution.ts |
| resell-tracker | ollama/wt-rt-order-buyerid-patchable | dispatch/rt-order-buyerid-patchable | app/api/orders/[id]/route.ts |
| resell-tracker | ollama/wt-rt-order919-address-resync | dispatch/rt-order919-address-resync | app/api/orders/[id]/route.ts, lib/orderFieldSync.ts, prisma/schema.prisma |
| resell-tracker | ollama/wt-rt-walmart-delivered-signal-wiring | dispatch/rt-walmart-delivered-signal-wiring | sidecar/src/walmart.js, sidecar/src/walmartDetailSignals.js, sidecar/src/walmartTracking.js |
| resell-tracker | ollama/wt-rt-walmart555-tracking-fix | dispatch/rt-walmart555-tracking-fix | sidecar/src/walmart.js |
| resell-tracker | ollama/wt-sync-status-overflow | dispatch/sync-status-overflow | components/SyncStatusIndicator.tsx |
| resell-tracker | ollama/wt-web-backfill-date-cutoff | dispatch/web-backfill-date-cutoff | app/api/bfmr/sync-reservations/route.ts |
