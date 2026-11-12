# Review: src/fetchState.ts

## VERDICT: FAIL
_a defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | MEDIUM | `src/fetchState.ts:8 (removed)` | The guard `if (ageMs < interval) {` was deleted, making `return { ...cache.state, _degraded: degrade |

## Defects

### [MEDIUM] The guard `if (ageMs < interval) {` was deleted, making `return { ...cache.state, _degraded: degraded };` unconditional. The fall-through path it protected is now unreachable.
`src/fetchState.ts:8 (removed)`

```
if (ageMs < interval) {
```
**How it fails:** Previously, when `if (ageMs < interval) {` was false, control fell through to the code after this block; now it never does. Whatever that fall-through reached no longer runs for any input, and any local used only by the deleted condition is now dead. CHECK THE FALL-THROUGH TARGET to judge severity: if it merely reached another equivalent return this is dead code, but if it reached a refresh, retry, fetch, or a different return shape, that behaviour is lost. The harness can prove the guard is gone; it cannot prove from this hunk alone what the lost path did.
**Verified trigger:** any input for which `if (ageMs < interval) {` was previously false

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 265s of model time, 1 truncated
- Wall clock 265s
- Filtered before you saw them: 2 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*