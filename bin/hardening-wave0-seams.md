# Wave 0 seams to register in Wave 1 (pipeline-canary.py is held; nothing registered yet)

All six files are new, unwired, import-safe, and have their own tests. Each test file carries a
`MUTANTS` list of `(old, new)` text edits of the module under test; `--mutants` (alias
`--revert-check`) first requires the baseline to pass, then requires every mutant to make the tests
FAIL (a hang counts as killed). They convert 1:1 to the canary dict-proof format
`{"mut": <id>, "old": ..., "new": ..., "why": ...}` against `bin/<module>.py`.

| Seam id | Tool (green run) | Proof target file | Proofs |
|---|---|---|---|
| `laneplan` | `python3 test-lane-plan.py` | `lane_plan.py` | `MUTANTS` in test-lane-plan.py (31): masked-missing, torn->off, pid check, window, served sticky, KeyBox repr/_thaw leak, fp leak, secret scan, casing, expiry (endpoint, model, wall-clock free), refresh (ok/unchanged/expired), kill switches (resolve/pipe/refresh), plan file mode/sha, C10 `model_lane_mismatch`, sha determinism, immutability, `pass_fds` pipe write-end close |
| `vendorcontract` | `python3 test-vendor-contract.py` | `vendor_contract.py` | `MUTANTS` in test-vendor-contract.py (25): every check turned vacuous, version compare, key-drop, exit code |
| `conformancelog` | `python3 test-conformance-log.py` | `conformance_log.py` | `MUTANTS` in test-conformance-log.py (43): redaction, key scrub, full/compact choice, caps, retention, reader filters, each drift class, rotation allowance, kill switches |

Equivalent mutants deliberately NOT listed (documented in the test files): lane_plan
`is_db and state != up`; `KeyBox(key) if key else None`; conformance_log `flock` removal (O_APPEND
single write is already atomic; the lock only guards the day-cap race).

## Wave 1 actions for pipeline-canary.py (cluster B)
1. Register the three seams above (timeouts: laneplan ~10 s, vendorcontract ~5 s, conformancelog ~15 s green;
   proofs run each test once per mutant, so ~5 min / ~2 min / ~2 min).
2. Add to `PIPELINE_FILES`, `_seam_files` and `~/.claude/hooks/pipeline-canary-reminder.sh`:
   `lane_plan.py vendor_contract.py conformance_log.py` and the 11 modules already missing (design F5).
3. Static seam `noreread` (design 1): the vendor-file literals (`local.json`, `provider.toml`,
   `loaded-models.json`, `daemon-state.json`) are allowed in `lane_plan.py` AND `vendor_contract.py`
   (offline shape checks need raw keys; it drops the api_key value on read). `bloom_control.py` is the
   existing mutator allowlist entry. `conformance_log.py` never reads vendor files.
4. Cross-module seams that need wiring first (cannot be registered until the queue/worker land):
   `laneplan` cases (b) worker with `--plan` and local.json deleted for the whole run, (c) 401 -> exactly
   one `refresh_key` and one uncharged retry, (d) changed base -> paused `plan_expired`, (e) key literal absent
   from `ps` argv / child env / job log / plan file / conformance log (the in-module analogue already runs
   in test-lane-plan.py via a real `pass_fds` child), (f) pid mismatch -> INFRA WAIT; and `reviewtoctou`.
5. `--prove` reverts to add once wired (design section 1): worker re-reads local.json in the send path; plan ignored
   in `_build_cmd`; fp check removed; `refresh_key` accepts a different base (mutant `why = None` is the
   module-level analogue); key written into the plan file; `pass_fds` dropped / key into env; code-review-agent
   uses `_dbk.is_darkbloom` on raw disk. Conformance (section 3): record omits `fields` on change; `kfp` raw key;
   comparator ignores `rmodel`; GAP not detected with log disabled.
6. Re-point the existing sticky proofs (`qwconsistency` L2/L2b/L2c/L6, `dbbuildcmd`) at `lane_plan.py` only at
   step 5 of the migration; until then both stay. The `laneplan` fixtures `t_empty_torn_missing`,
   `t_pid_mismatch`, `t_served_sticky_and_casing` mirror those scenarios.
