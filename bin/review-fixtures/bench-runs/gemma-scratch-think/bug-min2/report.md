# Review: bug-min2.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-min2.sh:266 (removed)` | The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED |
| 2 | MEDIUM | `bug-min2.sh:269` | The comparison threshold was changed from -lt 2 to -lt 1, which prevents the warning from being disp |

## Defects

### [HIGH] The comparison changed from `if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then` to `if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then`, and the two are NOT equivalent -- they disagree for at least one value.
`bug-min2.sh:266 (removed)`

```
if [ "${PAIRED_COUNT:-0}" -lt 2 ] 2>/dev/null; then
```
**How it fails:** Evaluating both forms over an integer range finds a value where the old condition and the new one differ, so every input at that boundary now takes the other branch. Boundary cases are exactly what tests written around the old behaviour tend not to cover.
**Verified trigger:** a value at the boundary between the two forms

### [MEDIUM] The comparison threshold was changed from -lt 2 to -lt 1, which prevents the warning from being displayed when the user has exactly one paired key.
`bug-min2.sh:269`

```
if [ "${PAIRED_COUNT:-0}" -lt 1 ] 2>/dev/null; then
```
**How it fails:** User has exactly 1 paired key; the script does not warn the user that they lack a backup and risk being locked out.
**Verified trigger:** PAIRED_COUNT=1; the warning is not displayed.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 132s of model time, 1 truncated
- Wall clock 132s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*