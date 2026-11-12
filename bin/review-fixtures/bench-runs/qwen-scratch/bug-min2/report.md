# Review: bug-min2.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-min2.sh:269` | The threshold for the backup key warning was changed from 2 to 1, failing to warn users who have exa |
| 2 | HIGH | `bug-min2.sh:266 (removed)` | The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED |

## Defects

### [HIGH] The threshold for the backup key warning was changed from 2 to 1, failing to warn users who have exactly one paired key but no backup.
`bug-min2.sh:269`

```
if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then
```
**How it fails:** A user has exactly 1 paired YubiKey (PAIRED_COUNT=1). The condition `1 -lt 1` evaluates to false, so the warning about losing the key and being locked out is not displayed. The user proceeds with a single key, loses it, and is completely locked out of the system.
**Verified trigger:** PAIRED_COUNT=1. Old code: [ 1 -lt 2 ] is true, warning printed. New code: [ 1 -lt 1 ] is false, warning NOT printed. User has 1 key, no backup, proceeds without warning, loses key, locked out.

### [HIGH] The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then`, and the two are NOT equivalent -- they disagree for at least one value.
`bug-min2.sh:266 (removed)`

```
if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then
```
**How it fails:** Evaluating both forms over an integer range finds a value where the old condition and the new one differ, so every input at that boundary now takes the other branch. Boundary cases are exactly what tests written around the old behaviour tend not to cover.
**Verified trigger:** a value at the boundary between the two forms

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 16s of model time
- Wall clock 16s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*