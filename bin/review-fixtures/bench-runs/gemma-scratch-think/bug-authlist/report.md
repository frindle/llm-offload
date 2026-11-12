# Review: bug-authlist.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-authlist.ts:6 (removed)` | Removing '/api/sidecar' from the PUBLIC list changes the access level of the endpoint from public to |
| 2 | HIGH | `bug-authlist.ts:3 (removed)` | '/api/sidecar' was silently dropped from a literal collection that still contains /api/auth, /api/us |
| 3 | MEDIUM | `bug-authlist.ts:11 (removed)` | The deletion removes '/api/settings' from the list of allowed API paths, which will cause the `isAll |

## Defects

### [HIGH] Removing '/api/sidecar' from the PUBLIC list changes the access level of the endpoint from public to private, violating the 'No behaviour change intended' intent.
`bug-authlist.ts:6 (removed)`

```
const PUBLIC = ['/login', '/api/auth', '/api/users', '/api/sidecar'];
```
**How it fails:** A client attempts to access '/api/sidecar' without authentication, but is now denied access.
**Verified trigger:** A request to '/api/sidecar' without authentication.

### [HIGH] '/api/sidecar' was silently dropped from a literal collection that still contains /api/auth, /api/users, /login.
`bug-authlist.ts:3 (removed)`

```
const PUBLIC = ['/login', '/api/auth', '/api/users', '/api/sidecar'];
```
**How it fails:** Every code path that relied on '/api/sidecar' being in that collection now takes the other branch. Because /api/auth, /api/users, /login survived, the collection still looks correct at a glance and any test covering only /api/auth still passes.
**Verified trigger:** any input where the value is '/api/sidecar'

## Uncertain — needs a human

### [MEDIUM] The deletion removes '/api/settings' from the list of allowed API paths, which will cause the `isAllowed` function to return `false` for that path.
`bug-authlist.ts:11 (removed)`

```
'/api/settings',
```
**How it fails:** If the script requires access to '/api/settings' to retrieve configuration for the YubiKey PIV setup, the `isAllowed` check will fail, preventing the script from completing its task.
**Verified trigger:** The script calls `isAllowed('/api/settings')` and expects `true`, but receives `false`.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 4 calls, 337s of model time
- Wall clock 337s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*