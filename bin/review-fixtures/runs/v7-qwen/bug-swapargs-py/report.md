# Review: ledger/payments.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `ledger/payments.py:11` | The src and dst arguments to transfer are swapped, reversing the direction of the refund. |

## Defects

### [HIGH] The src and dst arguments to transfer are swapped, reversing the direction of the refund.
`ledger/payments.py:11`

```
return transfer(src=customer_acct, dst=merchant_acct, amount=amount)
```
**How it fails:** When a user requests a refund, the system charges the customer and credits the merchant instead of crediting the customer and charging the merchant, resulting in the customer losing money and the merchant gaining it incorrectly.
**Verified trigger:** Calling refund(merchant_acct='M1', customer_acct='C1', amount=100). The function calls transfer(src='C1', dst='M1', amount=100). The customer's account 'C1' is debited 100, and the merchant's account 'M1' is credited 100.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 6s of model time
- Wall clock 6s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*