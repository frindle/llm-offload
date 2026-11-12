# Review: src/report/collect.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/report/collect.ts` | The function returns immediately after finding the first active row, preventing the collection of al |

## Defects

### [HIGH] The function returns immediately after finding the first active row, preventing the collection of all active row IDs.
`src/report/collect.ts`

```
+      return out;
```
**How it fails:** If `rows` contains multiple active rows, e.g., `[{id: 1, active: true}, {id: 2, active: true}]`, the function returns `[1]` instead of `[1, 2]`.
**Verified trigger:** rows = [{id: 1, active: true}, {id: 2, active: true}] results in [1] instead of [1, 2].

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 53s of model time
- Wall clock 53s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*