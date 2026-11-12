# Review: bug-min2.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-min2.sh:269` | The threshold for the backup key warning was changed from 2 to 1, allowing users with exactly one pa |
| 2 | HIGH | `bug-min2.sh:266 (removed)` | The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED |

## Defects

### [HIGH] The threshold for the backup key warning was changed from 2 to 1, allowing users with exactly one paired key to proceed without the critical warning.
`bug-min2.sh:269`

```
if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then
```
**How it fails:** A user has exactly 1 paired key (no backup). The script prints the warning message but does not block or strongly discourage proceeding (as it likely did before, or the intent was to flag this as a critical risk). The user loses their single key and is locked out entirely because the script failed to enforce the 'no single key' policy implied by the previous threshold of 2.
**Verified trigger:** PAIRED_COUNT=1. The condition [ 1 -lt 1 ] evaluates to false (exit code 1). The warning block is skipped. Previously, [ 1 -lt 2 ] evaluated to true (exit code 0), so the warning was printed.

### [HIGH] The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then`, and the two are NOT equivalent -- they disagree for at least one value.
`bug-min2.sh:266 (removed)`

```
if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then
```
**How it fails:** Evaluating both forms over an integer range finds a value where the old condition and the new one differ, so every input at that boundary now takes the other branch. Boundary cases are exactly what tests written around the old behaviour tend not to cover.
**Verified trigger:** a value at the boundary between the two forms

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 44s of model time
- Wall clock 46s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*