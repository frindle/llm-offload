# Review: etl/dedupe.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `etl/dedupe.py:12` | Replacing ''seen.add(key)' with ''seen.add(rec)' causes the ''seen' set to store records instead of  |

## Defects

### [HIGH] Replacing ''seen.add(key)' with ''seen.add(rec)' causes the ''seen' set to store records instead of keys, which breaks the deduplication logic.
`etl/dedupe.py:12`

```
seen.add(rec)
```
**How it fails:** Given rec1 = (1, 'a'), rec2 = (1, 'b'), and key_fn = lambda x: x[0], the function returns both records instead of deduplicating them.
**Verified trigger:** rec1 = (1, 'a'), rec2 = (1, 'b'), key_fn = lambda x: x[0]. The function returns [rec1, rec2] instead of [rec1].

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 63s of model time
- Wall clock 63s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*