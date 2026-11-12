# Review: etl/dedupe.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `etl/dedupe.py:12` | The code adds the record to the seen set instead of the key, causing the deduplication check 'if key |

## Defects

### [HIGH] The code adds the record to the seen set instead of the key, causing the deduplication check 'if key in seen' to fail for subsequent records with the same key.
`etl/dedupe.py:12`

```
seen.add(rec)
```
**How it fails:** Input records [{'id': 1}, {'id': 1}] with key_fn = lambda x: x['id'] results in both records being returned instead of just one.
**Verified trigger:** Input records: [(1,), (1,)], key_fn: lambda x: x[0]. The new code returns [(1,), (1,)] instead of [(1,)] because 1 is not in {(1,)}.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 57s of model time
- Wall clock 57s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*