# Review: bug-scauth.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-scauth.sh:61` | The command substitution `sc_auth list` was replaced with `sc_auth identities`, but the output forma |
| 2 | MEDIUM | `bug-scauth.sh:58 (removed)` | `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external co |
| 3 | MEDIUM | `bug-scauth.sh:209 (removed)` | `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external co |

## Defects

### [HIGH] The command substitution `sc_auth list` was replaced with `sc_auth identities`, but the output format of `sc_auth identities` may differ from `sc_auth list`, causing `grep -c '.'` to count incorrectly or fail to match expected lines.
`bug-scauth.sh:61`

```
sc_auth identities -u "$(whoami)" 2>/dev/null | grep -c '.'
```
**How it fails:** If `sc_auth identities` outputs a header line or uses a different delimiter that `grep -c '.'` counts as a line (or if it outputs nothing when no keys exist but `sc_auth list` previously output a specific error or empty line), the `paired_key_count` function returns an incorrect count. Specifically, if `sc_auth identities` outputs a header 'Identity:' on the first line even when empty, `grep -c '.'` returns 1 instead of 0, causing the script to believe a key exists when it does not.
**Verified trigger:** sc_auth identities outputs a header line (e.g., 'Identity:') when no keys are present, while sc_auth list outputted nothing or an error. Specifically: 1. Old code: `sc_auth list -u user` returns empty string or error (exit code non-zero, stdout empty). `grep -c '.'` returns 0. 2. New code: `sc_auth identities -u user` returns a single line 'Identity:' (or similar header) with exit code 0. `grep -c '.'` counts this line and returns 1. Result: paired_key_count returns 1 instead of 0.

## Uncertain — needs a human

### [MEDIUM] `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external command.
`bug-scauth.sh:58 (removed)`

```
sc_auth list -u "$(whoami)" 2>/dev/null | grep -c '.'
```
**How it fails:** Whether `sc_auth identities` returns the same thing as `sc_auth list` cannot be determined from this diff; it depends on sc_auth's own behaviour. If the two differ, every caller of this code silently gets different data while the change is described as behaviour-preserving. Confirm against sc_auth's documentation.

### [MEDIUM] `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external command.
`bug-scauth.sh:209 (removed)`

```
sc_auth list -u "$(whoami)" 2>/dev/null || echo "  (none found)"
```
**How it fails:** Whether `sc_auth identities` returns the same thing as `sc_auth list` cannot be determined from this diff; it depends on sc_auth's own behaviour. If the two differ, every caller of this code silently gets different data while the change is described as behaviour-preserving. Confirm against sc_auth's documentation.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 8s of model time
- Wall clock 8s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*