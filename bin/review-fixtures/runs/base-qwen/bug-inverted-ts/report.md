# Review: src/auth/session.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/auth/session.ts:10` | The negation operator was incorrectly added to the isExpired check, inverting the logic so that expi |

## Defects

### [HIGH] The negation operator was incorrectly added to the isExpired check, inverting the logic so that expired sessions are returned instead of rejected.
`src/auth/session.ts:10`

```
if (!isExpired(s)) {
```
**How it fails:** A user presents a session that has passed its expiration time. The function `isExpired(s)` returns true. The condition `!isExpired(s)` evaluates to false, so the `throw` is skipped. The function returns the expired session object `s`, granting access to an invalid session.
**Verified trigger:** Input: s is a session object where isExpired(s) returns true (e.g., s.expiry < current_time). Old Code: if (isExpired(s)) { throw ... } -> throws 'session expired'. New Code: if (!isExpired(s)) { throw ... } -> condition is false, skips throw, returns s.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 131,072, think=off — 2 calls, 17s of model time
- Wall clock 17s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*