# Review: etl/dedupe.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `etl/dedupe.py:12` | The deduplication logic now stores the full record object in the seen set instead of the extracted k |

## Defects

### [HIGH] The deduplication logic now stores the full record object in the seen set instead of the extracted key, causing incorrect deduplication when different records share the same key.
`etl/dedupe.py:12`

```
seen.add(rec)
```
**How it fails:** Two records exist: rec1 = {id: 1, name: 'Alice'} and rec2 = {id: 1, name: 'Bob'}, with key_fn extracting the 'id'. Previously, seen.add(key) would add 1 to seen, and rec2 would be skipped (correct deduplication). Now, seen.add(rec) adds the full rec1 object. When rec2 is processed, key (1) is NOT in seen (because seen contains the object rec1, not the integer 1), so rec2 is appended to out. Result: both records are kept, violating the deduplication intent.
**Verified trigger:** Input: records = [{'id': 1, 'name': 'Alice'}, {'id': 1, 'name': 'Bob'}]. key_fn = lambda r: r['id']. Old behavior: seen={1}, out=[{'id': 1, 'name': 'Alice'}]. New behavior: seen=[{'id': 1, 'name': 'Alice'}], key=1 is not in seen (1 != {'id': 1, 'name': 'Alice'}), so out=[{'id': 1, 'name': 'Alice'}, {'id': 1, 'name': 'Bob'}].

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 7s of model time
- Wall clock 7s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*