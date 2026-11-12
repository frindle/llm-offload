# Review: ledger/payments.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `ledger/payments.py:11` | The `src` and `dst` arguments are swapped, reversing the direction of the fund transfer. |

## Defects

### [HIGH] The `src` and `dst` arguments are swapped, reversing the direction of the fund transfer.
`ledger/payments.py:11`

```
return transfer(src=customer_acct, dst=merchant_acct, amount=amount)
```
**How it fails:** A refund is processed, and money is transferred from the customer's account to the merchant's account instead of from the merchant's account to the customer's account.
**Verified trigger:** Calling the function with any positive amount results in funds being transferred from 'customer_acct' to ''merchant_acct' instead of from ''merchant_acct' to 'customer_acct'.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 19s of model time
- Wall clock 19s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*