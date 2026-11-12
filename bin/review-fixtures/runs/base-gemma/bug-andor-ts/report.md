# Review: src/signup/validate.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/signup/validate.ts:8` | The `canEnroll` function uses a logical OR (`\|\|`) instead of a logical AND (`&&`), allowing minors |

## Defects

### [HIGH] The `canEnroll` function uses a logical OR (`||`) instead of a logical AND (`&&`), allowing minors to enroll if they have consent.
`src/signup/validate.ts:8`

```
return a.age >= 18 || a.consent;
```
**How it fails:** An applicant with `age: 10` and `consent: true` returns `true` for `canEnroll`, violating the requirement that only adults can enroll.
**Verified trigger:** An applicant with age 10 and consent true returns true, whereas the requirement (and previous code) required both age >= 18 AND consent to be true.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 12s of model time
- Wall clock 12s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*