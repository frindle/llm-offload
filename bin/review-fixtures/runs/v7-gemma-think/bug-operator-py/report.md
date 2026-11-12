# Review: billing/discount.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `billing/discount.py:6` | The `apply_discount` function adds the `discount` value to the subtotal instead of subtracting it. |

## Defects

### [HIGH] The `apply_discount` function adds the `discount` value to the subtotal instead of subtracting it.
`billing/discount.py:6`

```
subtotal = price * qty + discount
```
**How it fails:** price=10, qty=1, discount=2 results in a subtotal of 12 instead of 8.
**Verified trigger:** When price=10, qty=1, and discount=2, the new code returns 12 instead of 8.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 29s of model time
- Wall clock 29s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*