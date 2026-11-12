# Review: ledger/payments.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `ledger/payments.py:11` | The `src` and `dst` arguments are swapped, reversing the direction of the transfer from merchant-to- |

## Defects

### [HIGH] The `src` and `dst` arguments are swapped, reversing the direction of the transfer from merchant-to-customer to customer-to-merchant.
`ledger/payments.py:11`

```
return transfer(src=customer_acct, dst=merchant_acct, amount=amount)
```
**How it fails:** A refund of $50.00 is requested; instead of the merchant being debited and the customer being credited, the customer is debited and the merchant is credited.
**Verified trigger:** amount=50.00, src=customer_acct, dst=merchant_acct. Result: customer_acct is debited and merchant_acct is credited.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 22s of model time
- Wall clock 22s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*