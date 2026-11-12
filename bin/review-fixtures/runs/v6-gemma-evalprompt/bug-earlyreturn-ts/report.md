# Review: src/report/collect.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/report/collect.ts:11` | The `return out;` statement inside the loop causes the function to exit prematurely after finding th |

## Defects

### [HIGH] The `return out;` statement inside the loop causes the function to exit prematurely after finding the first active row, returning only the first active ID instead of all active IDs.
`src/report/collect.ts:11`

```
return out;
```
**How it fails:** If `rows` contains two active rows with IDs 1 and 2, the function returns `[1]` instead of `[1, 2]`.
**Verified trigger:** When `rows` contains multiple active rows (e.g., `[{id: 1, active: true}, {id: 2, active: true}]`), the function returns `[1]` instead of `[1, 2]` because it exits the loop on the first active row.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 4s of model time
- Wall clock 4s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*