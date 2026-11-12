# Review: src/orders/summary.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/orders/summary.ts:7 (removed)` | Removing the null check in itemCount causes a runtime error when order is null. |
| 2 | HIGH | `src/orders/summary.ts:4 (removed)` | The null check for the 'order' parameter was removed, which allows a null value to reach the propert |

## Defects

### [HIGH] Removing the null check in itemCount causes a runtime error when order is null.
`src/orders/summary.ts:7 (removed)`

```
if (!order) {
```
**How it fails:** itemCount(null) is called, resulting in a TypeError: Cannot read properties of null (reading 'items').
**Verified trigger:** itemCount(null) results in TypeError: Cannot read properties of null (reading 'items')

### [HIGH] The null check for the 'order' parameter was removed, which allows a null value to reach the property access 'order.items.length'.
`src/orders/summary.ts:4 (removed)`

```
if (!order) {
```
**How it fails:** Calling itemCount(null) will result in a TypeError: Cannot read properties of null (reading 'items').
**Verified trigger:** Calling itemCount(null) results in 'TypeError: Cannot read properties of null (reading 'items')'.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 4 calls, 62s of model time
- Wall clock 62s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*