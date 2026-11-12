# Review: ledger/payments.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `ledger/payments.py:11` | The `refund` function swaps the source and destination accounts, causing funds to flow from the cust |

## Defects

### [HIGH] The `refund` function swaps the source and destination accounts, causing funds to flow from the customer to the merchant instead of the merchant to the customer.
`ledger/payments.py:11`

```
return transfer(src=customer_acct, dst=merchant_acct, amount=amount)
```
**How it fails:** A user requests a refund of 10.00. Instead of 10.00 being moved from the merchant_acct to the customer_acct, 10.00 is moved from the customer_acct to the merchant_acct.
**Verified trigger:** Calling refund(amount=10.00) where merchant_acct and customer_acct are defined. The funds will be transferred from customer_acct to merchant_acct instead of merchant_acct to customer_acct.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 13s of model time
- Wall clock 13s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*