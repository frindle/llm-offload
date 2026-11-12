# Review: src/orders/summary.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/orders/summary.ts` | Removing the null check on order causes a TypeError when itemCount is called with null. |
| 2 | HIGH | `src/orders/summary.ts` | Removal of null guard causes TypeError when order is null |

## Defects

### [HIGH] Removing the null check on order causes a TypeError when itemCount is called with null.
`src/orders/summary.ts`

```
return order.items.length;
```
**How it fails:** The function is called with null (e.g., `itemCount(null)`). The removed guard `if (!order)` no longer exists, so the code attempts to access `.items` on null, throwing a TypeError instead of returning 0.
**Verified trigger:** Calling itemCount(null). The function attempts to access order.items.length where order is null, resulting in a TypeError: Cannot read properties of null (reading 'items').

### [HIGH] Removal of null guard causes TypeError when order is null
`src/orders/summary.ts`

```
if (!order) {
    return 0;
  }
```
**How it fails:** When itemCount is called with null (e.g., itemCount(null)), the code attempts to access order.items.length on a null value, throwing a TypeError: Cannot read properties of null (reading 'items'). This crashes the calling function instead of returning 0 as previously intended.
**Verified trigger:** Calling itemCount(null)

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 131,072, think=off — 4 calls, 19s of model time
- Wall clock 19s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*