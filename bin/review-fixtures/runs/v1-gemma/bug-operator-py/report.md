# Review: billing/discount.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `billing/discount.py:6` | The `apply_discount` function incorrectly adds the discount to the subtotal instead of subtracting i |

## Defects

### [HIGH] The `apply_discount` function incorrectly adds the discount to the subtotal instead of subtracting it.
`billing/discount.py:6`

```
subtotal = price * qty + discount
```
**How it fails:** If price is 10, qty is 1, and discount is 2, the function returns 12 instead of 8.
**Verified trigger:** price=10, qty=1, discount=2 results in subtotal=12 instead of 8.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 10s of model time
- Wall clock 10s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*