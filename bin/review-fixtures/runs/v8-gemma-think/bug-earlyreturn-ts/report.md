# Review: src/report/collect.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/report/collect.ts:11` | The function returns immediately after finding the first active row, preventing the collection of al |

## Defects

### [HIGH] The function returns immediately after finding the first active row, preventing the collection of all active IDs.
`src/report/collect.ts:11`

```
return out;
```
**How it fails:** rows contains two active items: [{id: 1, active: true}, {id: 2, active: true}]. The function returns [1] instead of [1, 2].
**Verified trigger:** rows = [{id: 1, active: true}, {id: 2, active: true}] results in [1] instead of [1, 2].

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 43s of model time
- Wall clock 43s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*