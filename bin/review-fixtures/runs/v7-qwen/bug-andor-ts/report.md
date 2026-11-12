# Review: src/signup/validate.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/signup/validate.ts:8` | Changed logical operator from && to \|\| allows minors with consent to enroll, violating the 'Adults |

## Defects

### [HIGH] Changed logical operator from && to || allows minors with consent to enroll, violating the 'Adults only' requirement.
`src/signup/validate.ts:8`

```
return a.age >= 18 || a.consent;
```
**How it fails:** An applicant with age 16 and consent=true is incorrectly allowed to enroll, whereas the previous logic would have rejected them.
**Verified trigger:** Applicant with age 16 and consent=true. Old code returns false (rejected). New code returns true (accepted).

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 4s of model time
- Wall clock 4s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*