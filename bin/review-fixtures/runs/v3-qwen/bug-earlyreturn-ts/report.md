# Review: src/report/collect.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/report/collect.ts:11` | The added `return out;` statement inside the loop causes the function to exit after processing the f |

## Defects

### [HIGH] The added `return out;` statement inside the loop causes the function to exit after processing the first active row, ignoring all subsequent active rows.
`src/report/collect.ts:11`

```
return out;
```
**How it fails:** When `rows` contains multiple objects with `active: true` (e.g., `[{id: 1, active: true}, {id: 2, active: true}]`), the function returns `[1]` instead of the expected `[1, 2]`, silently dropping valid active IDs.
**Verified trigger:** rows = [{id: 1, active: true}, {id: 2, active: true}]. Old output: [1, 2]. New output: [1].

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 6s of model time
- Wall clock 6s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*