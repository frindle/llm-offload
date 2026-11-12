# Review: src/auth/session.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/auth/session.ts:10` | The logic in the session expiration check is inverted, causing valid sessions to throw an error and  |

## Defects

### [HIGH] The logic in the session expiration check is inverted, causing valid sessions to throw an error and expired sessions to be returned.
`src/auth/session.ts:10`

```
if (!isExpired(s)) {
```
**How it fails:** A valid, non-expired session is passed to the function; the function throws ''session expired' instead of returning the session.
**Verified trigger:** A session ''s' where isExpired(s) returns false (a valid session). The function throws 'Error: session expired' instead of returning ''s'.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 4s of model time
- Wall clock 4s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*