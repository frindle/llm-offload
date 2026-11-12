# Review: bug-scauth.sh

## VERDICT: PASS WITH CAVEATS
_3 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | MEDIUM | `bug-scauth.sh:61` | The command substitution sc_auth list was replaced with sc_auth identities, but the grep pattern '.' |
| 2 | MEDIUM | `bug-scauth.sh:58 (removed)` | `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external co |
| 3 | MEDIUM | `bug-scauth.sh:209 (removed)` | `sc_auth list` was changed to `sc_auth identities` -- a different subcommand of the same external co |

## Uncertain — needs a human

### [MEDIUM] The command substitution sc_auth list was replaced with sc_auth identities, but the grep pattern '.' counts non-empty lines rather than filtering for valid identity hashes, causing the count to be inaccurate if the output format changes or includes headers.
`bug-scauth.sh:61`

```
sc_auth identities -u "$(whoami)" 2>/dev/null | grep -c '.'
```
**How it fails:** If sc_auth identities outputs a header line (e.g., 'IDENTITY') or empty lines, grep -c '.' counts them as valid identities, leading to an incorrect count of paired keys. This could cause the script to believe keys exist when they do not (if only headers are present) or miscount the number of keys.
**Verified trigger:** sc_auth identities outputs a header line (e.g., 'IDENTITY') or empty lines. Specifically, if the output is 'IDENTITY\n' (one header line, no identities), grep -c '.' returns 1. If the output is just empty lines or a header, the count is > 0, leading paired_key_count to return a non-zero value even when no valid identities exist.

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

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 22s of model time
- Wall clock 24s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*