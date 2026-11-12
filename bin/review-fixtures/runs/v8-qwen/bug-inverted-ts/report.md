# Review: src/auth/session.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/auth/session.ts:10` | The negation operator is incorrectly added to the isExpired check, causing the code to throw an erro |

## Defects

### [HIGH] The negation operator is incorrectly added to the isExpired check, causing the code to throw an error when the session is valid instead of when it is expired.
`src/auth/session.ts:10`

```
if (!isExpired(s)) {
```
**How it fails:** A user with a valid, non-expired session calls the function. The condition `!isExpired(s)` evaluates to true (because `isExpired` returns false), causing the function to throw 'session expired' and locking the user out.
**Verified trigger:** Input: s is a valid session object where isExpired(s) returns false. Old code: if (false) -> does not throw, returns s. New code: if (!false) -> if (true) -> throws Error('session expired').

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 7s of model time
- Wall clock 7s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*