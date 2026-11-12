# Review: src/fetchState.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/fetchState.ts` | The stale cache guard is removed, causing the system to report degraded status indefinitely when the |
| 2 | HIGH | `src/fetchState.ts` | The deletion removes a cache staleness guard that prevented serving stale data when the system was i |
| 3 | MEDIUM | `src/fetchState.ts:8 (removed)` | The guard `if (ageMs < interval) {` was deleted, making `return { ...cache.state, _degraded: degrade |

## Defects

### [HIGH] The stale cache guard is removed, causing the system to report degraded status indefinitely when the upstream is unreachable, instead of reverting to a known-good state after the timeout.
`src/fetchState.ts`

```
if (ageMs < interval) {
      return { ...cache.state, _degraded: true };
    }
```
**How it fails:** The upstream service is unreachable. The cache is 10ms old (within the 500ms interval). The old code would return `_degraded: true` to warn the user. The new code calls `readHealth()`, which also fails/returns false, so it returns `_degraded: false`. The user is not warned that the system is degraded, and the application proceeds as if healthy, potentially causing data loss or incorrect behavior.
**Verified trigger:** cache.source is 'stream', cache is not busy, cache.mode is not 'Active', ageMs is less than the interval (ACTIVE_MS or IDLE_MS), and the upstream is unreachable causing readHealth() to return false.

### [MEDIUM] The guard `if (ageMs < interval) {` was deleted, making `return { ...cache.state, _degraded: degraded };` unconditional. The fall-through path it protected is now unreachable.
`src/fetchState.ts:8 (removed)`

```
if (ageMs < interval) {
```
**How it fails:** Previously, when `if (ageMs < interval) {` was false, control fell through to the code after this block; now it never does. Whatever that fall-through reached no longer runs for any input, and any local used only by the deleted condition is now dead. CHECK THE FALL-THROUGH TARGET to judge severity: if it merely reached another equivalent return this is dead code, but if it reached a refresh, retry, fetch, or a different return shape, that behaviour is lost. The harness can prove the guard is gone; it cannot prove from this hunk alone what the lost path did.
**Verified trigger:** any input for which `if (ageMs < interval) {` was previously false

## Uncertain — needs a human

### [HIGH] The deletion removes a cache staleness guard that prevented serving stale data when the system was in a specific state (Active/Idle). The replacement logic calls `readHealth()` unconditionally, which may not respect the same staleness threshold or may return a different definition of 'degraded'.
`src/fetchState.ts`

```
if (ageMs < interval) {
      return { ...cache.state, _degraded: true };
    }
```
**How it fails:** When `cache.source !== 'stream'` (e.g., a cached state from a previous run), the code previously checked if the cache was too old (`ageMs < interval`). If the cache was fresh, it returned the cached state with `_degraded: true`. The new code calls `readHealth()` regardless of cache age. If `readHealth()` is expensive, blocks, or returns a different value than the cached `_degraded` flag, the system may behave incorrectly. Specifically, if `readHealth()` returns `false` (healthy) but the cache was actually stale (older than `interval`), the system will report a healthy state when it should report degraded/stale, potentially leading to incorrect status reporting or missed warnings about system health.
**Verified trigger:** cache.source = 'file', cache.busy = false, cache.mode = 'Idle', cache.state = { status: 'ok' }, ageMs = 500, IDLE_MS = 1000, readHealth() returns false. Old: { ...cache.state, _degraded: true }. New: { ...cache.state, _degraded: false }.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 14s of model time
- Wall clock 14s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*