# Review: src/orders/summary.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/orders/summary.ts:7 (removed)` | The `itemCount` function will throw a `TypeError` when `order` is `null` because the null check was  |
| 2 | HIGH | `src/orders/summary.ts:4 (removed)` | The function no longer handles the case where 'order' is null, which will cause a runtime error when |

## Defects

### [HIGH] The `itemCount` function will throw a `TypeError` when `order` is `null` because the null check was removed.
`src/orders/summary.ts:7 (removed)`

```
if (!order) {
```
**How it fails:** order is null, itemCount(order) throws TypeError: Cannot read properties of null (reading 'items')
**Verified trigger:** order = null, itemCount(order) throws TypeError: Cannot read properties of null (reading 'items')

### [HIGH] The function no longer handles the case where 'order' is null, which will cause a runtime error when accessing 'order.items'.
`src/orders/summary.ts:4 (removed)`

```
if (!order) {
```
**How it fails:** Calling itemCount(null) will throw a TypeError: Cannot read properties of null (reading 'items').
**Verified trigger:** itemCount(null) throws 'TypeError: Cannot read properties of null (reading 'items')'

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 4 calls, 110s of model time
- Wall clock 110s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*