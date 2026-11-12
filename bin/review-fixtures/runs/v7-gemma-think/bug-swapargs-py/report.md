# Review: ledger/payments.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `ledger/payments.py:11` | The `transfer` function call swaps the `src` and `dst` arguments, reversing the direction of the mon |

## Defects

### [HIGH] The `transfer` function call swaps the `src` and `dst` arguments, reversing the direction of the money transfer.
`ledger/payments.py:11`

```
return transfer(src=customer_acct, dst=merchant_acct, amount=amount)
```
**How it fails:** A refund is processed for an amount of 10.00; instead of the merchant paying the customer, the customer is charged by the merchant.
**Verified trigger:** amount=10.00, merchant_acct='M', customer_acct='C'. Old code: M is debited 10.00, C is credited 10.00. New code: C is debited 10.00, M is credited 10.00.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 36s of model time
- Wall clock 36s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*