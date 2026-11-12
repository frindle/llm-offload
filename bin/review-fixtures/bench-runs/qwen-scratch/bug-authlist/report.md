# Review: bug-authlist.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-authlist.ts:6 (removed)` | Removing '/api/sidecar' from the PUBLIC array breaks authentication bypass for the sidecar API, caus |
| 2 | HIGH | `bug-authlist.ts:11 (removed)` | The deletion removes '/api/settings' from the allowed API path list, causing subsequent requests to  |
| 3 | HIGH | `bug-authlist.ts:3 (removed)` | '/api/sidecar' was silently dropped from a literal collection that still contains /api/auth, /api/us |

## Defects

### [HIGH] Removing '/api/sidecar' from the PUBLIC array breaks authentication bypass for the sidecar API, causing it to require authentication instead of being publicly accessible.
`bug-authlist.ts:6 (removed)`

```
const PUBLIC = ['/login', '/api/auth', '/api/users', '/api/sidecar'];
```
**How it fails:** A request is made to '/api/sidecar'. Previously, this path was in the PUBLIC list and was allowed without authentication. After this change, the path is not in PUBLIC and not in EXTENSION_ALLOWED, so the request is rejected (likely 401/403), breaking any external service or internal process relying on unauthenticated access to the sidecar API.
**Verified trigger:** A GET request to '/api/sidecar' without authentication credentials. Previously, the server would allow this request because '/api/sidecar' was in the PUBLIC list. After the change, the server will reject the request (likely with 401 or 403) because '/api/sidecar' is no longer in PUBLIC and is not in EXTENSION_ALLOWED.

### [HIGH] The deletion removes '/api/settings' from the allowed API path list, causing subsequent requests to this endpoint to be blocked by the access control logic.
`bug-authlist.ts:11 (removed)`

```
'/api/settings',
```
**How it fails:** A client sends a GET or POST request to '/api/settings'. The `isAllowed` function checks the request path against the `list` array. Since '/api/settings' was removed from the array, `isAllowed` returns `false`. The system denies the request (likely returning 403 Forbidden or 401 Unauthorized), preventing the user from retrieving or updating their settings, effectively breaking the settings feature.
**Verified trigger:** A client sends a GET or POST request to the endpoint '/api/settings'. The `isAllowed` function checks this path against the modified list. Since '/api/settings' is no longer in the list, `isAllowed` returns `false`. The system denies the request, returning a 403 Forbidden or 401 Unauthorized error, breaking the settings feature.

### [HIGH] '/api/sidecar' was silently dropped from a literal collection that still contains /api/auth, /api/users, /login.
`bug-authlist.ts:3 (removed)`

```
const PUBLIC = ['/login', '/api/auth', '/api/users', '/api/sidecar'];
```
**How it fails:** Every code path that relied on '/api/sidecar' being in that collection now takes the other branch. Because /api/auth, /api/users, /login survived, the collection still looks correct at a glance and any test covering only /api/auth still passes.
**Verified trigger:** any input where the value is '/api/sidecar'

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 11s of model time
- Wall clock 11s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*