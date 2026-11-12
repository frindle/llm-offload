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
**How it fails:** Calling clamp(10, 20, 15) with x=15, lo=10, hi=20. The original code returns 15 (correctly within [10, 20]). The new code returns min(10, max(15, 20)) = min(10, 20) = 10. Calling clamp(10, 20, 5) with x=5. The original code returns 10 (correctly clamped to lo). The new code returns min(10, max(5, 20)) = min(10, 20) = 10. Calling clamp(10, 20, 25) with x=25. The original code returns 20 (correctly clamped to hi). The new code returns min(10, max(25, 20)) = min(10, 25) = 10. In all cases where x is outside the interval, the function incorrectly returns lo, ignoring hi entirely.
**Verified trigger:** Calling clamp(10, 20, 15) returns 10 instead of 15. Calling clamp(10, 20, 25) returns 10 instead of 20.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 45s of model time
- Wall clock 45s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*