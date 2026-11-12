# Review: bug-window.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-window.py:18 (removed)` | The comparison operator for 'placed' and 'cutoff' was changed from '>=' to '>', which changes the be |
| 2 | HIGH | `bug-window.py:15 (removed)` | The comparison changed from `if placed >= cutoff:` to `if placed > cutoff:`, and the two are NOT equ |

## Defects

### [HIGH] The comparison operator for 'placed' and 'cutoff' was changed from '>=' to '>', which changes the behavior by excluding items where 'placed' is exactly equal to 'cutoff'.
`bug-window.py:18 (removed)`

```
if placed >= cutoff:
```
**How it fails:** An object 'o' has 'o["placed_at"]' equal to 'cutoff', so it is not added to the 'pending' list.
**Verified trigger:** An object 'o' where 'o["placed_at"]' is exactly equal to 'cutoff'. In the new code, this object is excluded from the 'pending' list, whereas in the old code it was included.

### [HIGH] The comparison changed from `if placed >= cutoff:` to `if placed > cutoff:`, and the two are NOT equivalent -- they disagree for at least one value.
`bug-window.py:15 (removed)`

```
if placed >= cutoff:
```
**How it fails:** Evaluating both forms over an integer range finds a value where the old condition and the new one differ, so every input at that boundary now takes the other branch. Boundary cases are exactly what tests written around the old behaviour tend not to cover.
**Verified trigger:** a value at the boundary between the two forms

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 195s of model time
- Wall clock 195s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*