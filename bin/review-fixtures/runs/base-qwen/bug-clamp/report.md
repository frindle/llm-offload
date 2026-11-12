# Review: util/numeric.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `util/numeric.py:11` | The clamp logic is inverted, returning the value furthest from the interval rather than constraining |

## Defects

### [HIGH] The clamp logic is inverted, returning the value furthest from the interval rather than constraining it to the interval.
`util/numeric.py:11`

```
return min(lo, max(x, hi))
```
**How it fails:** Input x=10 with lo=0, hi=5. The original code returns 5 (correctly clamped). The new code returns max(10, 5) = 10, then min(0, 10) = 0. Wait, let's trace carefully. min(lo, max(x, hi)). If x=10, lo=0, hi=5. max(10, 5) is 10. min(0, 10) is 0. So it returns 0. If x=-5, lo=0, hi=5. max(-5, 5) is 5. min(0, 5) is 0. So it returns 0. If x=2, lo=0, hi=5. max(2, 5) is 5. min(0, 5) is 0. It always returns lo (the lower bound) unless x is somehow less than lo? No. Let's re-evaluate. Original: max(lo, min(x, hi)). New: min(lo, max(x, hi)). Case 1: x < lo. Original: max(lo, x) = lo. New: min(lo, x) = x. (Correctly returns x? No, clamp should return lo. So it returns x, which is out of bounds). Case 2: lo <= x <= hi. Original: max(lo, x) = x. New: min(lo, x) = lo. (Incorrectly returns lo instead of x). Case 3: x > hi. Original: max(lo, hi) = hi. New: min(lo, x) = lo. (Incorrectly returns lo instead of hi). The function effectively returns `lo` for all inputs `x >= lo`, and returns `x` for inputs `x < lo`. This is completely broken for clamping.
**Verified trigger:** Input x=2, lo=0, hi=5. Old code: max(0, min(2, 5)) = max(0, 2) = 2. New code: min(0, max(2, 5)) = min(0, 5) = 0.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 131,072, think=off — 2 calls, 37s of model time
- Wall clock 37s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*