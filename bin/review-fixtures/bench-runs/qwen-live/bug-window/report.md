# Review: bug-window.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-window.py:18` | Off-by-one error in cutoff comparison excludes orders placed exactly at the cutoff time from the pen |
| 2 | HIGH | `bug-window.py:15 (removed)` | The comparison changed from `if placed >= cutoff:` to `if placed > cutoff:`, and the two are NOT equ |

## Defects

### [HIGH] Off-by-one error in cutoff comparison excludes orders placed exactly at the cutoff time from the pending list.
`bug-window.py:18`

```
if placed > cutoff:
```
**How it fails:** An order is placed at exactly the cutoff timestamp (e.g., `placed_at == cutoff`). The original code (`>=`) would include it in `pending`. The new code (`>`) excludes it. This results in an order being silently dropped from the pending queue, potentially causing a missed delivery notification or lost sale.
**Verified trigger:** An order object `o` where `o['placed_at']` is exactly equal to `cutoff` (e.g., `placed_at == cutoff`).

### [HIGH] The comparison changed from `if placed >= cutoff:` to `if placed > cutoff:`, and the two are NOT equivalent -- they disagree for at least one value.
`bug-window.py:15 (removed)`

```
if placed >= cutoff:
```
**How it fails:** Evaluating both forms over an integer range finds a value where the old condition and the new one differ, so every input at that boundary now takes the other branch. Boundary cases are exactly what tests written around the old behaviour tend not to cover.
**Verified trigger:** a value at the boundary between the two forms

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 4s of model time
- Wall clock 5s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*