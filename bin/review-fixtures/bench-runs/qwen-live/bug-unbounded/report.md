# Review: lib/logs.ts

## VERDICT: PASS WITH CAVEATS
_2 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `lib/logs.ts:18 (removed)` | Removing the MAX_ROWS slice allows the logs array to grow unbounded, causing uncontrolled memory usa |
| 2 | HIGH | `lib/logs.ts:15 (removed)` | Removal of a bounds check on output array length allows unbounded data to be returned. |

## Uncertain — needs a human

### [HIGH] Removing the MAX_ROWS slice allows the logs array to grow unbounded, causing uncontrolled memory usage.
`lib/logs.ts:18 (removed)`

```
return out.slice(0, MAX_ROWS);
```
**How it fails:** A user generates a large number of log entries (e.g., via a loop or high-frequency events). The `out` array grows without limit, leading to an OutOfMemoryError or significant performance degradation due to memory pressure, whereas previously it was capped at MAX_ROWS.
**Verified trigger:** A user generates more than MAX_ROWS log entries (e.g., 1001 entries if MAX_ROWS is 1000). The `out` array retains all 1001 entries in memory instead of being truncated to the first 1000.

### [HIGH] Removal of a bounds check on output array length allows unbounded data to be returned.
`lib/logs.ts:15 (removed)`

```
return out.slice(0, MAX_ROWS);
```
**How it fails:** If the loop appending to 'out' processes more than MAX_ROWS items (e.g., a large number of log entries or PIV status records), the function returns the entire untruncated array. This can cause excessive memory usage, slow down the script execution, or expose sensitive information (like all account names or security details) in the setup document that was intended to be limited to the most recent MAX_ROWS entries.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 13s of model time
- Wall clock 14s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*