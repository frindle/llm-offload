# Review: etl/dedupe.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `etl/dedupe.py:12` | The `seen` set now stores the entire record `rec` instead of the unique identifier `key`, which will |

## Defects

### [HIGH] The `seen` set now stores the entire record `rec` instead of the unique identifier `key`, which will cause deduplication to fail if records have identical keys but different non-key fields.
`etl/dedupe.py:12`

```
seen.add(rec)
```
**How it fails:** A list of records contains two records with the same `id` but different `timestamp` values. The function returns both records instead of deduplicating them because `rec1!= rec2`.
**Verified trigger:** A list of records where two records have the same `key` (returned by `key_fn`) but different values for other fields (making `rec1!= rec2`). The function will return both records instead of deduplicating them.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=off — 2 calls, 5s of model time
- Wall clock 5s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*