# Review: src/auth/session.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/auth/session.ts:10` | The `isExpired` check is inverted, causing the function to throw an error for valid sessions and ret |

## Defects

### [HIGH] The `isExpired` check is inverted, causing the function to throw an error for valid sessions and return expired sessions.
`src/auth/session.ts:10`

```
if (!isExpired(s)) {
```
**How it fails:** A valid, non-expired session is passed; the function throws "session expired" instead of returning the session.
**Verified trigger:** A non-expired session (isExpired(s) === false) is passed; the function throws ''session expired' instead of returning the session.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 35s of model time
- Wall clock 35s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*