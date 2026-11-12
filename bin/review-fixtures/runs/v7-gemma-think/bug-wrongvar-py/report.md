# Review: etl/dedupe.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `etl/dedupe.py:12` | The `seen` set is populated with the record instead of the key, causing the membership check `if key |

## Defects

### [HIGH] The `seen` set is populated with the record instead of the key, causing the membership check `if key in seen` to fail for subsequent records with the same key.
`etl/dedupe.py:12`

```
seen.add(rec)
```
**How it fails:** Two records have the same key but different values (e.g., {'id': 1, 'v': 'a'} and {'id': 1, 'v': 'b'} with key_fn returning the 'id'). Both records are included in the output instead of only the first one.
**Verified trigger:** key_fn returns a value that is not the record itself (e.g., lambda x: x['id']) and there are multiple records with the same key but different values (e.g., {'id': 1, 'v': 'a'} and {'id': 1, 'v': 'b'}). The second record will be incorrectly included in the output.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 56s of model time
- Wall clock 56s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*