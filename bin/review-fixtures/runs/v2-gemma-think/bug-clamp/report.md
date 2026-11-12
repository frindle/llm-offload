# Review: util/numeric.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `util/numeric.py` | The `clamp` function always returns `lo` when `lo <= hi`. |

## Defects

### [HIGH] The `clamp` function always returns `lo` when `lo <= hi`.
`util/numeric.py`

```
+    return min(lo, max(x, hi))
```
**How it fails:** x=15, lo=10, hi=20 returns 10 instead of 15.
**Verified trigger:** x=15, lo=10, hi=20 returns 10 instead of 15

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 719s of model time
- Wall clock 719s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*