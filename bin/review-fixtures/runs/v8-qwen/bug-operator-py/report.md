# Review: billing/discount.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `billing/discount.py:6` | The arithmetic operator was changed from subtraction to addition, causing the discount to increase t |

## Defects

### [HIGH] The arithmetic operator was changed from subtraction to addition, causing the discount to increase the price instead of reducing it.
`billing/discount.py:6`

```
subtotal = price * qty + discount
```
**How it fails:** A customer purchases 10 items at $10 each with a $20 discount. The original code calculates 100 - 20 = $80. The new code calculates 100 + 20 = $120. The customer is charged $40 more than the correct discounted price.
**Verified trigger:** price=10, qty=10, discount=20. Old result: 80. New result: 120. The customer is overcharged by 40.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 6s of model time
- Wall clock 6s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*