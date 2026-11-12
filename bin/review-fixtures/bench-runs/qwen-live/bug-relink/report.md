# Review: bug-relink.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-relink.py:25` | Removing the length check allows substring matches on short strings, causing false positives where u |
| 2 | HIGH | `bug-relink.py` | Removal of length validation allows processing of truncated or malformed names, leading to incorrect |

## Defects

### [HIGH] Removing the length check allows substring matches on short strings, causing false positives where unrelated short names are considered matches.
`bug-relink.py:25`

```
return name == tracked or tracked in name or name in tracked
```
**How it fails:** A torrent named 'a' and a tracked file named 'ab' (both shorter than _MIN_CONTAINMENT_LEN). The original code returned False (no match). The new code returns True ('ab' contains 'a'), incorrectly relinking or matching the torrent to the wrong tracked file.
**Verified trigger:** Input: torrent_name='a', tracked_relative_path='ab'. Precondition: len('a') < _MIN_CONTAINMENT_LEN and len('ab') < _MIN_CONTAINMENT_LEN. Old code: checks len('a') < _MIN_CONTAINMENT_LEN, returns False. New code: evaluates 'ab' in 'a' (False) or 'a' in 'ab' (True), returns True.

### [HIGH] Removal of length validation allows processing of truncated or malformed names, leading to incorrect containment checks.
`bug-relink.py`

```
if len(name) < _MIN_CONTAINMENT_LEN or len(tracked) < _MIN_CONTAINMENT_LEN:
        return False
```
**How it fails:** If `name` is an empty string (length 0) or `tracked` is shorter than `_MIN_CONTAINMENT_LEN` (e.g., a partial filename), the original code returned `False` immediately. The refactored code proceeds to `tracked in name or name in tracked`. If `name` is empty, `tracked in name` is False, but `name in tracked` might be True if `tracked` is non-empty (since empty string is in any string). This causes a false positive match for partial or empty names, potentially linking unrelated or incomplete torrent data to a tracked entry.
**Verified trigger:** name='' (empty string), tracked='some_torrent' (length >= _MIN_CONTAINMENT_LEN)

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 11s of model time
- Wall clock 12s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*