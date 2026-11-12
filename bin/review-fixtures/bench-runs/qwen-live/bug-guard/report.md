# Review: bug-guard.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-guard.py` | Removal of validation for tracking_number allows None or empty values to be written to the order dic |

## Defects

### [HIGH] Removal of validation for tracking_number allows None or empty values to be written to the order dictionary, corrupting the data model.
`bug-guard.py`

```
if not tracking_number:
        raise ValueError("refusing to mark an order synced with no tracking number")
```
**How it fails:** An order is processed with a missing or null tracking number (e.g., `tracking_number=None`). The `mark_synced` function proceeds to set `order['tracking_number'] = None`. Downstream systems expecting a valid string tracking number (e.g., for shipping label generation or database indexing) receive null/empty values, causing data integrity errors, failed API calls, or database constraint violations.
**Verified trigger:** Calling mark_synced(order, None) where order is a dict. The function sets order['tracking_number'] = None and order['synced_at'] = <current_time>. No exception is raised.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 3 calls, 9s of model time
- Wall clock 9s
- Filtered before you saw them: 1 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*